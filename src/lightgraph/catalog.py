"""Versioned metadata outputs. Only declarations and typed interpretations persist.

Neither this module nor its writers accept query batches, model transcripts or
free-form explanations of sampled values. Interpretation prose is generated from
registered roles; the original source declaration remains separately available.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field

from .contracts import StrictModel, TableSpec, SourceRef, ExtractionGoal, digest
from .discovery.models import DataUnderstanding, Role
from .errors import LightGraphError
from .runtime.retention import atomic_text

CATALOG_VERSION = "metadata_catalog/1"
SAFE_CONFLICTS = frozenset({
    "schema_value_conflict", "comment_echo", "comment_empty", "fd_violation",
    "same_code_diff_label", "label_conflict", "null_key", "mixed_value_shapes",
    "unknown_role", "no_confirmable_entity_group",
})


class SourceDescription(StrictModel):
    source_id: str
    product: str
    version: str = "unknown"
    driver: str = "unknown"
    capabilities: dict[str, Literal["verified", "unsupported", "unknown"]] = Field(default_factory=dict)
    # Unknown means not read by this connector, not that the constraint is absent.
    constraint_coverage: dict[str, Literal["read", "not_declared", "unknown"]]
    format: dict[str, str | bool | int | list[str]] = Field(default_factory=dict)
    type_origin: Literal["declared", "parser_inferred", "parser_assigned"] = "declared"


class DeclaredCatalog(StrictModel):
    artifact: Literal["declared_metadata"] = "declared_metadata"
    format_version: Literal["metadata_catalog/1"] = CATALOG_VERSION
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    sources: list[SourceDescription]
    tables: list[TableSpec]


class ColumnInterpretation(StrictModel):
    column: str
    role: Role = "unknown"
    comment_quality: Literal["empty", "echo", "generic", "informative"] = "empty"
    status: Literal["candidate", "rule_validated"] = "candidate"
    conflict_codes: list[str] = Field(default_factory=list)
    rule_refs: list[str] = Field(default_factory=list)


class TableInterpretation(StrictModel):
    source: SourceRef
    declaration_revision: str
    context_revision: str = ""
    coverage: Literal["sampled", "full", "metadata_only"] = "metadata_only"
    derived: Literal[True] = True
    columns: list[ColumnInterpretation]
    document_refs: list[str] = Field(default_factory=list)


class SemanticCatalog(StrictModel):
    artifact: Literal["metadata_interpretation"] = "metadata_interpretation"
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    declaration_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    goal_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    tables: list[TableInterpretation]


def declared_catalog(adapters) -> DeclaredCatalog:
    tables = []
    sources = []
    for adapter in adapters:
        local = sorted(adapter.describe(), key=lambda t: (t.source_id, t.qualified_name))
        tables.extend(local)
        manifest = adapter.capabilities()
        source_ids = sorted({t.source_id for t in local})
        source_format = getattr(adapter, "format_metadata", {})
        allowed_format = {"encoding", "delimiter", "header", "header_row", "null_values", "type_inference",
                          "table_name", "schema_name", "input_revision"}
        source_format = {k: v for k, v in source_format.items() if k in allowed_format}
        file_source = manifest.product in ("excel_memory", "csv_memory")
        for sid in source_ids:
            sources.append(SourceDescription(
                source_id=sid, product=manifest.product, version=manifest.version,
                driver=manifest.driver, capabilities=dict(manifest.capabilities),
                type_origin=("parser_assigned" if manifest.product=="csv_memory" else
                             "parser_inferred" if file_source else "declared"),
                constraint_coverage={
                    "primary_key": "not_declared" if file_source else "read",
                    "unique_keys": "not_declared" if file_source else "read",
                    "foreign_keys": "not_declared" if file_source else "read",
                    "check": "unknown", "default": "unknown", "constraint_state": "unknown",
                }, format=source_format,
            ))
    tables.sort(key=lambda t: (t.source_id, t.schema_name, t.name))
    if len({(t.source_id, t.schema_name, t.name) for t in tables}) != len(tables):
        raise LightGraphError("duplicate_catalog_table")
    sources.sort(key=lambda s: s.source_id)
    # Live snapshot state is intentionally absent; this is a physical declaration.
    revision = digest(CATALOG_VERSION, [t.model_dump(by_alias=True) for t in tables],
                      [s.model_dump() for s in sources])
    return DeclaredCatalog(revision=revision, tables=tables, sources=sources)


def semantic_catalog(declared, goal, understandings=(), rules=(), documents=(), previous=None):
    if not isinstance(declared, DeclaredCatalog) or not isinstance(goal, ExtractionGoal):
        raise LightGraphError("typed_catalog_required")
    from .discovery.engine import _comment_quality
    from .documents import DocumentCorpus
    from .context import context_dependency_fingerprint
    corpus=DocumentCorpus(documents)
    corpus.bind_catalog(declared.tables)
    prior={(t.source.source_id,t.source.schema_name,t.source.table):t
           for t in previous.tables} if isinstance(previous,SemanticCatalog) else {}
    accepted = [r for r in rules if r.status in ("validated", "canary_checked", "published")]
    doc_refs = {d.document_id + "@" + d.revision for d in documents}
    by_source = {}
    for u in understandings:
        if not isinstance(u, DataUnderstanding):
            raise LightGraphError("typed_understanding_required")
        by_source[(u.source.source_id, u.source.schema_name, u.source.table)] = u
    interpretations = []
    for table in declared.tables:
        key = (table.source_id, table.schema_name, table.name)
        source = SourceRef(source_id=key[0], schema=key[1], table=key[2])
        understanding = by_source.get(key)
        proposed = {c.name: c for c in understanding.columns} if understanding else {}
        context_revision=context_dependency_fingerprint(corpus,[table],goal)
        declaration_revision=digest(table.model_dump(by_alias=True))
        old=prior.get(key)
        if old and (old.declaration_revision!=declaration_revision or old.context_revision!=context_revision):
            old=None
        retained={c.column:c for c in old.columns} if old and not understanding else {}
        matching = [r for r in accepted if r.source == source]
        columns = []
        for column in table.columns:
            proposal = proposed.get(column.name)
            retained_column=retained.get(column.name)
            role = (proposal.inferred_role if proposal else
                    retained_column.role if retained_column else "unknown")
            refs = []
            inferred = set()
            for rule in matching:
                if column.name in rule.identity.key_columns:
                    inferred.add("identity_code"); refs.append(rule.rule_id)
                for name, col in {**rule.attributes.single_valued, **rule.attributes.multi_valued}.items():
                    if col == column.name:
                        mapped = {"label": "label", "alias": "alias", "english_name": "alias",
                                  "definition": "semantic_attribute", "formula": "semantic_attribute",
                                  "unit": "semantic_attribute"}.get(name)
                        if mapped:
                            inferred.add(mapped); refs.append(rule.rule_id)
                if rule.hierarchy:
                    for level in rule.hierarchy.levels:
                        if level.code == column.name: inferred.add("identity_code"); refs.append(rule.rule_id)
                        if level.label == column.name: inferred.add("label"); refs.append(rule.rule_id)
            # Conflicting interpretations remain candidate instead of silently choosing one.
            if len(inferred) == 1:
                candidate = next(iter(inferred))
                try:
                    role = ColumnInterpretation(column=column.name, role=candidate).role
                except ValueError:
                    pass
            columns.append(ColumnInterpretation(
                column=column.name, role=role,
                comment_quality=proposal.comment_quality if proposal else _comment_quality(column.name, column.comment),
                status="rule_validated" if refs and len(inferred) == 1 else "candidate",
                conflict_codes=(sorted(set(proposal.conflicts) & SAFE_CONFLICTS) if proposal else
                                retained_column.conflict_codes if retained_column else []),
                rule_refs=sorted(set(refs)),
            ))
        interpretations.append(TableInterpretation(
            source=source, declaration_revision=declaration_revision,context_revision=context_revision,
            coverage=understanding.coverage if understanding else old.coverage if old else "metadata_only",
            columns=columns,
            document_refs=sorted(({ref for r in matching for ref in r.dependencies.documents} |
                                  set(understanding.document_refs if understanding else old.document_refs if old else [])) & doc_refs),
        ))
    goal_revision = digest(goal.model_dump())
    revision = digest(declared.revision, goal_revision, [t.model_dump(by_alias=True) for t in interpretations])
    return SemanticCatalog(revision=revision, declaration_revision=declared.revision,
                           goal_revision=goal_revision, tables=interpretations)


def read_semantic_catalog(output):
    """Load only the typed interpretation; legacy/malformed files are not reused."""
    path=Path(output)/"metadata.yaml"
    if not path.exists():return None
    try:
        value=yaml.safe_load(path.read_text())
        return SemanticCatalog.model_validate(value["semantic_annotations"])
    except (ValueError,TypeError,KeyError,yaml.YAMLError):
        return None


def write_catalog(output, declared, interpreted):
    """Write immutable revisions and a current pointer-like composed YAML."""
    if not isinstance(declared, DeclaredCatalog) or not isinstance(interpreted, SemanticCatalog):
        raise LightGraphError("typed_catalog_required")
    declared = DeclaredCatalog.model_validate(declared.model_dump(mode="json",by_alias=True))
    interpreted = SemanticCatalog.model_validate(interpreted.model_dump(mode="json",by_alias=True))
    expected_declared = digest(CATALOG_VERSION, [t.model_dump(by_alias=True) for t in declared.tables],
                               [s.model_dump() for s in declared.sources])
    expected_semantic = digest(declared.revision, interpreted.goal_revision,
                              [t.model_dump(by_alias=True) for t in interpreted.tables])
    if declared.revision != expected_declared or interpreted.revision != expected_semantic:
        raise LightGraphError("catalog_revision_mismatch")
    if interpreted.declaration_revision != declared.revision:
        raise LightGraphError("catalog_dependency_mismatch")
    output = Path(output)
    def dump(value):
        return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    physical = declared.model_dump(mode="json", by_alias=True)
    semantic = interpreted.model_dump(mode="json", by_alias=True)
    for path, value in (
        (output / "metadata" / "declarations" / (declared.revision + ".yaml"), physical),
        (output / "metadata" / "interpretations" / (interpreted.revision + ".yaml"), semantic),
    ):
        content = dump(value)
        if path.exists():
            if path.read_text() != content:
                raise LightGraphError("catalog_revision_conflict")
        else:
            atomic_text(path, content)
    atomic_text(output / "metadata.yaml", dump({"declared": physical, "semantic_annotations": semantic}))
