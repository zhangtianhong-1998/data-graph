"""Versioned graph workspace; one construction worker owns each writer.

Immutable records are written before atomically advancing the manifest. An
interrupted write may leave an unreferenced record, but never a manifest pointing
at a half-written record. No operation deletes drafts, evidence or validations.
"""
from __future__ import annotations

import math
import os
import re
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from yaml.nodes import MappingNode

from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.retention import atomic_text

from .models import DataValidation, DraftRecord, EvidenceRecord, GraphDraft, draft_evidence_refs

# Only legacy/internal records use this compatibility limit. Agent-authored YAML
# remains bounded at 8 MiB; instance graphs are stored in Parquet, not YAML.
INTERNAL_YAML_MAX_BYTES = 512 * 1024 * 1024

# These failures describe an unsuccessful attempt, not a counterexample to the
# facts in an earlier completed snapshot. Semantic conflicts are excluded.
SNAPSHOT_RETRY_FAILURES = frozenset({
    'query_budget_exhausted', 'db_cu_budget_exhausted', 'run_time_budget_exhausted',
    'wall_time_budget_exhausted', 'construction_time_exhausted', 'query_timeout',
    'query_cancelled', 'query_execution_failed', 'construction_source_execution_failed',
    'returned_rows_budget_exhausted', 'returned_bytes_budget_exhausted',
    'memory_budget_exhausted', 'source_closed',
})


class _GraphYamlLoader(yaml.SafeLoader):
    """JSON-like scalar semantics with duplicate keys and aliases forbidden."""

    yaml_implicit_resolvers = {}

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise LightGraphError("yaml_alias_forbidden")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, MappingNode):
            raise LightGraphError("invalid_yaml_mapping")
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, (str, int)) or isinstance(key, bool):
                raise LightGraphError("invalid_yaml_key")
            if key in result:
                raise LightGraphError("duplicate_yaml_key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


class _GraphYamlDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


_GraphYamlLoader.add_implicit_resolver("tag:yaml.org,2002:null", re.compile(r"^(?:null|Null|NULL|~|)$"), ["n", "N", "~", ""])
_GraphYamlLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false|True|False|TRUE|FALSE)$"), list("tTfF"))
# Leading-zero values such as business code 0012 remain strings, even unquoted.
_GraphYamlLoader.add_implicit_resolver("tag:yaml.org,2002:int", re.compile(r"^[-+]?(?:0|[1-9][0-9]*)$"), list("-+0123456789"))
_GraphYamlLoader.add_implicit_resolver("tag:yaml.org,2002:float", re.compile(
    r"^[-+]?(?:(?:0|[1-9][0-9]*)\.[0-9]*(?:[eE][-+]?[0-9]+)?|(?:0|[1-9][0-9]*)[eE][-+]?[0-9]+|\.[0-9]+|\.(?:inf|Inf|INF|nan|NaN|NAN))$"
), list("-+0123456789."))


def _check_yaml_tree(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise LightGraphError("yaml_depth_exceeded")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LightGraphError("non_finite_yaml_value")
        return
    if isinstance(value, list):
        for item in value:
            _check_yaml_tree(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, (str, int)) or isinstance(key, bool):
                raise LightGraphError("invalid_yaml_key")
            _check_yaml_tree(item, depth + 1)
        return
    raise LightGraphError("unsupported_yaml_value")


def safe_load_yaml(text: str, *, max_bytes: int = 8 * 1024 * 1024) -> Any:
    """Parse one bounded YAML document without arbitrary tags or implicit dates."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > max_bytes:
        raise LightGraphError("yaml_document_too_large")
    try:
        value = yaml.load(text, Loader=_GraphYamlLoader)
        _check_yaml_tree(value)
        return value
    except (yaml.YAMLError, RecursionError, ValueError, TypeError):
        raise LightGraphError("invalid_graph_yaml") from None


def dump_yaml(value: Any) -> str:
    """Serialize typed knowledge while retaining scalar types and Unicode labels."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", by_alias=True)
    _check_yaml_tree(value)
    return yaml.dump(value, Dumper=_GraphYamlDumper, allow_unicode=True, sort_keys=False)


def read_internal_yaml(path: str | Path) -> Any:
    """Read existing internal YAML with a bound checked before allocating text."""
    path = Path(path)
    if path.stat().st_size > INTERNAL_YAML_MAX_BYTES:
        raise LightGraphError('yaml_document_too_large')
    return safe_load_yaml(path.read_text(encoding='utf-8'), max_bytes=INTERNAL_YAML_MAX_BYTES)


def write_yaml(path: str | Path, value: Any) -> None:
    atomic_text(path, dump_yaml(value))


def parse_graph_draft(text: str) -> GraphDraft:
    return GraphDraft.model_validate(safe_load_yaml(text))


class GraphWorkspace:
    """Retain Agent-authored models, exact data checks and immutable graph versions."""

    def __init__(self, root: str | Path, context_fingerprint: str):
        self.result_policy_id = None
        if not context_fingerprint:
            raise LightGraphError("construction_context_required")
        self.root = Path(root)
        self.context_fingerprint = context_fingerprint
        self.write_seconds = 0.0
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.yaml"
        if not self.manifest_path.exists():
            self._save_manifest({"format_version": 1, "target": "graph", "drafts": {},
                                 "evidence": {}, "evidence_history": {}, "publications": {}})
        self._manifest()

    def _manifest(self) -> dict:
        manifest = read_internal_yaml(self.manifest_path)
        if (not isinstance(manifest, dict) or manifest.get("format_version") != 1
                or manifest.get("target") != "graph"
                or not isinstance(manifest.get("drafts"), dict)
                or not isinstance(manifest.get("evidence"), dict)):
            raise LightGraphError("invalid_graph_workspace_manifest")
        return manifest

    def _save_manifest(self, manifest: dict) -> None:
        started = time.monotonic()
        try:
            write_yaml(self.manifest_path, manifest)
        finally:
            self.write_seconds += max(0.0, time.monotonic() - started)

    def _read_record(self, relative: str) -> dict:
        if not isinstance(relative, str):
            raise LightGraphError("invalid_graph_workspace_record")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root.resolve()) or path == self.root.resolve():
            raise LightGraphError("invalid_graph_workspace_record")
        try:
            if path.is_dir():
                from .graph_storage import read_graph_record
                return read_graph_record(path)
            return read_internal_yaml(path)
        except OSError:
            raise LightGraphError("graph_workspace_record_unavailable") from None

    def _append_record(self, folder: str, value: Any) -> str:
        started = time.monotonic()
        try:
            if folder in {'previews', 'published'} and isinstance(value, dict):
                from .graph_storage import write_graph_record
                relative = f'{folder}/{uuid.uuid4().hex}.graph'
                (self.root / folder).mkdir(parents=True, exist_ok=True)
                write_graph_record(self.root / relative, value)
                return relative
            serialized = dump_yaml(value)
            if len(serialized.encode('utf-8')) > INTERNAL_YAML_MAX_BYTES:
                raise LightGraphError('yaml_document_too_large')
            relative = f"{folder}/{uuid.uuid4().hex}.yaml"
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation protects snapshots even if a caller ignores the
            # single-writer contract. The manifest moves only after fsync succeeds.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(serialized)
                output.flush()
                os.fsync(output.fileno())
            return relative
        finally:
            self.write_seconds += max(0.0, time.monotonic() - started)

    def issue_evidence(self, kind: str, source_ref=None, details: dict | None = None,
                       *, ref: str | None = None) -> EvidenceRecord:
        """Called by trusted tools, never exposed as an Agent evidence writer."""
        details = details or {}
        if {"status", "validated", "semantic_status", "semantic_decision", "conditions_satisfied"} & set(details):
            raise LightGraphError("evidence_authority_field_forbidden")
        canonical(details)
        manifest = self._manifest()
        if ref is None:
            sequence = max((int(key[1:]) for key in manifest["evidence"]
                            if isinstance(key, str) and re.fullmatch(r"e[1-9][0-9]*", key)), default=0) + 1
            ref = f"e{sequence}"
        record = EvidenceRecord(ref=ref, kind=kind,
                                context_fingerprint=self.context_fingerprint,
                                source_ref=source_ref, details=details)
        if record.ref in manifest["evidence"]:
            previous = EvidenceRecord.model_validate(self._read_record(manifest["evidence"][record.ref]))
            if previous == record:
                return previous
            if previous.context_fingerprint == self.context_fingerprint:
                raise LightGraphError("evidence_reference_conflict")
        relative = self._append_record("evidence", record)
        manifest.setdefault("evidence_history", {}).setdefault(record.ref, []).append(relative)
        manifest["evidence"][record.ref] = relative
        self._save_manifest(manifest)
        return record

    def read_evidence(self, ref: str) -> EvidenceRecord:
        relative = self._manifest()["evidence"].get(ref)
        if relative is None:
            raise LightGraphError("unknown_construction_evidence")
        record = EvidenceRecord.model_validate(self._read_record(relative))
        if record.ref != ref:
            raise LightGraphError("invalid_graph_workspace_record")
        if record.context_fingerprint != self.context_fingerprint:
            raise LightGraphError("stale_construction_evidence")
        return record

    def _check_refs(self, refs) -> None:
        for ref in refs:
            self.read_evidence(ref)

    def save_draft(self, draft: GraphDraft | dict | str) -> DraftRecord:
        # Revalidate a model instance too: nested mutable mappings may have
        # changed since GraphDraft construction.
        if isinstance(draft, str):
            draft = parse_graph_draft(draft)
        else:
            draft = GraphDraft.model_validate(draft.model_dump(mode="python", by_alias=True)
                                             if isinstance(draft, GraphDraft) else draft)
        self._check_refs(draft_evidence_refs(draft))
        fingerprint = digest(draft.model_dump(mode="json", by_alias=True))
        manifest = self._manifest()
        entry = manifest["drafts"].get(draft.draft_id)
        previous = self.get_draft(draft.draft_id) if entry else None
        if previous and previous.fingerprint == fingerprint and previous.context_fingerprint == self.context_fingerprint:
            return previous
        record = DraftRecord(draft=draft, revision=(previous.revision + 1 if previous else 1),
                             fingerprint=fingerprint, context_fingerprint=self.context_fingerprint)
        history = list(entry["history"]) if entry else []
        relative = self._append_record("drafts", record)
        history.append(relative)
        manifest["drafts"][draft.draft_id] = {"current": relative, "history": history}
        self._save_manifest(manifest)
        return record

    def get_draft(self, draft_id: str) -> DraftRecord:
        return self._read_draft_entry(draft_id, self._manifest()["drafts"].get(draft_id))

    def _read_draft_entry(self, draft_id, entry):
        if entry is None:
            raise LightGraphError("unknown_graph_draft")
        payload = self._read_record(entry['current'])
        record = DraftRecord.model_validate(payload)
        if (record.draft.draft_id != draft_id
                or digest(payload['draft']) != record.fingerprint):
            raise LightGraphError("invalid_graph_workspace_record")
        if record.context_fingerprint != self.context_fingerprint:
            return record.model_copy(update={"status": "stale", "validation": None,
                                              "last_successful_validation": None})
        return record

    def _draft_record_payload(self, record: DraftRecord) -> dict:
        """Keep the signed historical draft shape when only state changes.

        Adding optional schema defaults must not rewrite an old draft while
        retaining its old fingerprint. Explicit save_draft creates a revision
        with the current schema and a new fingerprint instead.
        """
        payload = record.model_dump(mode='json', by_alias=True)
        if digest(payload['draft']) != record.fingerprint:
            entry = self._manifest()['drafts'][record.draft.draft_id]
            original = self._read_record(entry['current'])['draft']
            if digest(original) != record.fingerprint:
                raise LightGraphError('invalid_graph_workspace_record')
            payload['draft'] = original
        return payload

    def record_validation(self, draft_id: str, *, revision: int, passed: bool,
                          diagnostics: list[dict] | None = None, metrics: dict | None = None,
                          evidence_refs: list[str] | None = None, graph: dict | None = None,
                          read_window: dict | None = None) -> DraftRecord:
        record = self.get_draft(draft_id)
        if record.context_fingerprint != self.context_fingerprint:
            raise LightGraphError("stale_graph_draft")
        if revision != record.revision:
            raise LightGraphError("graph_draft_revision_conflict")
        inherited_relations = (record.draft.extends_base_graph and record.draft.relations
                               and record.draft.inherited_entity_types)
        if passed and (diagnostics or not (record.draft.entities or inherited_relations)):
            raise LightGraphError("invalid_graph_validation_success")
        self._check_refs(evidence_refs or [])
        validation = DataValidation(validation_id='v_' + uuid.uuid4().hex,
                                    graph_path=self._append_record('previews', graph) if graph is not None else None,
                                    read_window=read_window or {},
                                    revision=revision, draft_fingerprint=record.fingerprint,
                                    context_fingerprint=self.context_fingerprint, passed=passed,
                                    diagnostics=diagnostics or [], metrics=metrics or {},
                                    evidence_refs=evidence_refs or [], graph_digest=digest(graph) if graph is not None else None)
        canonical(validation.model_dump(mode="python"))
        previous_success = record.last_successful_validation or (
            record.validation if record.validation and record.validation.passed else None)
        record = record.model_copy(update={"status": "validated" if passed else "needs_revision",
                                           "validation": validation,
                                           "last_successful_validation": validation if passed else previous_success})
        manifest = self._manifest()
        # Do not overwrite the candidate or the previous validation attempt.
        relative = self._append_record("drafts", self._draft_record_payload(record))
        entry = manifest["drafts"][draft_id]
        entry["history"].append(relative)
        entry["current"] = relative
        self._save_manifest(manifest)
        return record

    def select_validation(self, draft_id: str, validation_id: str | None = None, *, isolate_conflicts=False) -> DataValidation:
        """Resolve a successful snapshot without erasing the latest attempt.

        Explicit historical selection is required after a data counterexample.
        Every selection remains bound to the current draft and input context.
        """
        record = self.get_draft(draft_id)
        if record.status == 'stale':
            raise LightGraphError('stale_graph_draft')
        selected = record.validation
        if validation_id is not None:
            selected = None
            for path in reversed(self._manifest()['drafts'][draft_id]['history']):
                candidate = DraftRecord.model_validate(self._read_record(path)).validation
                if candidate and candidate.validation_id == validation_id:
                    selected = candidate
                    break
        elif selected and not selected.passed:
            codes = {d.get('code') for d in selected.diagnostics}
            # Skipped dependent mappings are consequences of the failed read,
            # not additional semantic counterexamples to the old snapshot.
            causes = codes - {'mapping_source_unavailable', 'relation_dependency_unavailable'}
            if causes and causes <= SNAPSHOT_RETRY_FAILURES:
                selected = record.last_successful_validation
        from .conflicts import subset_available
        if (selected is None or not (selected.passed or isolate_conflicts and subset_available(selected))
                or selected.revision != record.revision
                or selected.draft_fingerprint != record.fingerprint
                or selected.context_fingerprint != self.context_fingerprint):
            raise LightGraphError('graph_validation_required')
        return selected

    def read_validation_graph(self, draft_id: str, validation: DataValidation) -> dict:
        relative = validation.graph_path or f'previews/{digest(draft_id)[:16]}-{validation.revision}.yaml'
        graph = self._read_record(relative)
        if validation.graph_digest is None or digest(graph) != validation.graph_digest:
            raise LightGraphError('validated_graph_mismatch')
        return graph

    def latest_conflict_validation(self, draft_id: str) -> DataValidation:
        """Retain counterexamples across revisions, without rereading a failed source.

        Historical observations may explain a repair, but cannot authorize
        publication of a newer draft. Access/context changes invalidate them.
        """
        self.get_draft(draft_id)
        for path in reversed(self._manifest()['drafts'][draft_id]['history']):
            validation = DraftRecord.model_validate(self._read_record(path)).validation
            if (validation and validation.context_fingerprint == self.context_fingerprint
                    and any(d.get('code') == 'same_identity_conflicting_label'
                            for d in validation.diagnostics)):
                return validation
        raise LightGraphError('graph_conflict_evidence_missing')

    def publish(self, draft_id: str, *, revision: int, graph: dict,
                validation_id: str | None = None, isolate_conflicts=False, fusion_policy=None) -> dict:
        """Publish the exact validated graph, retaining independent draft outputs.

        Semantic definitions remain Agent-authored. Publication attests only to
        the recorded deterministic checks and does not claim independent review.
        """
        record = self.get_draft(draft_id)
        validation = self.select_validation(draft_id, validation_id, isolate_conflicts=isolate_conflicts)
        if record.status == 'stale':
            raise LightGraphError('stale_graph_draft')
        if revision != record.revision:
            raise LightGraphError('graph_draft_revision_conflict')
        if validation.graph_digest is None or digest(graph) != validation.graph_digest:
            raise LightGraphError('validated_graph_mismatch')
        from .conflicts import isolate_validation, isolate_publications
        isolated = not validation.passed
        if isolated:
            graph = isolate_validation(graph, validation.diagnostics)
        publication_status = 'needs_revision' if isolated else 'published'
        manifest = self._manifest()
        publications = manifest.setdefault('publications', {})
        existing = publications.get(draft_id)
        # Once quarantine is in use, a strict low-level caller must not silently
        # reintroduce an identity blocked by another active publication.
        if fusion_policy is None:
            isolate_conflicts = isolate_conflicts or any(value.get('conflict_policy') in {'isolate', 'quarantine'}
                and value.get('context_fingerprint') == self.context_fingerprint for value in publications.values())
        policy_name = fusion_policy.label_conflict_policy if fusion_policy else ('isolate' if isolate_conflicts else 'reject')
        if (existing and existing['revision'] == revision
                and existing['context_fingerprint'] == self.context_fingerprint
                and existing.get('validation_id') == validation.validation_id
                and existing.get('graph_digest') == validation.graph_digest
                and existing.get('conflict_policy', 'reject') == policy_name):
            if record.status != publication_status:
                restored = record.model_copy(update={'status': publication_status})
                relative = self._append_record('drafts', self._draft_record_payload(restored))
                manifest['drafts'][draft_id]['history'].append(relative)
                manifest['drafts'][draft_id]['current'] = relative
                self._save_manifest(manifest)
            return existing
        entry = {'draft_id': draft_id, 'revision': revision, 'context_fingerprint': self.context_fingerprint,
                 'draft_fingerprint': record.fingerprint, 'graph_digest': validation.graph_digest,
                 'validation_id': validation.validation_id, 'read_window': validation.read_window,
                 'semantic_origin': 'agent_authored', 'conflict_policy': policy_name,
                 'coverage': 'conflict_isolated' if isolated else 'complete',
                 'published_nodes': len(graph.get('nodes', [])), 'published_edges': len(graph.get('edges', [])),
                 'isolated_nodes': graph.get('conflict_isolation', {}).get('nodes', 0),
                 'isolated_edges': graph.get('conflict_isolation', {}).get('edges', 0)}
        published = {**graph, 'status': 'published', 'semantic_origin': 'agent_authored',
                     **({'validation_id': validation.validation_id} if validation.validation_id else {})}
        entry['published_graph_digest'] = digest(published)
        # Check conflicts against every still-current independent publication
        # before advancing either the publication set or the merged graph head.
        graphs = [self._read_record(value['graph']) for key, value in publications.items()
                  if key != draft_id and value['context_fingerprint'] == self.context_fingerprint]
        contributors = graphs + [published]
        if fusion_policy and fusion_policy.entity_rules:
            from .fusion import prepare_graph
            contributors = [prepare_graph(g, fusion_policy) for g in contributors]
        if isolate_conflicts:
            contributors, isolation = isolate_publications(contributors)
            merged = _merge_graphs(contributors, fusion_policy=fusion_policy)
            merged['conflict_isolation'] = isolation
        else:
            merged = _merge_graphs(contributors, fusion_policy=fusion_policy)
        entry['graph'] = self._append_record('published', published)
        manifest.setdefault('publication_history', []).append(entry)
        publications[draft_id] = entry
        old_head = manifest.get('current_graph')
        if old_head and 'version_id' not in old_head:
            manifest.setdefault('graph_history', []).append(deepcopy(old_head))
        manifest['current_graph'] = {'context_fingerprint': self.context_fingerprint,
                                     **({'result_policy_id': self.result_policy_id} if self.result_policy_id else {}),
                                     **({'run_id': self.publication_run_id} if getattr(self, 'publication_run_id', None) else {}),
                                     'access_fingerprint': merged.get('access_fingerprint'),
                                     'path': self._append_record('published', merged)}
        from .snapshot_registry import record_graph_head
        record_graph_head(manifest, manifest['current_graph'], root=self.root,
                          coverage=merged.get('conflict_isolation', {}).get('coverage', 'complete'))
        published_record = record.model_copy(update={'status': publication_status})
        relative = self._append_record('drafts', self._draft_record_payload(published_record))
        manifest['drafts'][draft_id]['history'].append(relative)
        manifest['drafts'][draft_id]['current'] = relative
        self._save_manifest(manifest)
        return entry

    def read_published_graph(self) -> dict:
        head = self._manifest().get('current_graph')
        if not head:
            return _merge_graphs([])
        if head['context_fingerprint'] != self.context_fingerprint:
            raise LightGraphError('stale_published_graph')
        data = self._read_record(head['path'])
        if data.get('storage_layout') == 'sharded_v1':
            from .merger import load_sharded_graph
            return load_sharded_graph(self.root, data)
        return data

    def read_publication_contributors(self) -> list[dict]:
        """Unmerged snapshots retain cross-draft counterexamples for diagnosis."""
        return [self._read_record(entry['graph']) for entry in self._manifest().get('publications', {}).values()
                if entry['context_fingerprint'] == self.context_fingerprint]

    def list_drafts(self) -> list[DraftRecord]:
        return [self._read_draft_entry(key, entry) for key, entry in self._manifest()["drafts"].items()]

    def draft_index(self, draft_id=None, *, offset=0):
        """A bounded progress page with no graph materialization or evidence scan."""
        manifest = self._manifest()
        ids = [draft_id] if draft_id is not None else sorted(manifest['drafts'])
        if not isinstance(offset, int) or offset < 0:
            raise LightGraphError('invalid_page')
        page = []
        for key in ids[offset:offset + 50]:
            record = self._read_draft_entry(key, manifest['drafts'].get(key))
            check = record.validation
            page.append({'draft_id': key, 'revision': record.revision, 'status': record.status,
                         'validation_passed': check.passed if check else None,
                         'unresolved_count': len(record.draft.unresolved) if record.status != 'stale' else None,
                         'draft_material': 'draft:' + key, 'validation_material': 'validation:' + key if check else None})
        return {'drafts': page, 'total_drafts': len(ids),
                'next_offset': offset + len(page) if offset + len(page) < len(ids) else None}

    def read_summary(self) -> dict:
        manifest = self._manifest()
        current_refs = []
        stale_count = 0
        for ref in manifest["evidence"]:
            try:
                self.read_evidence(ref)
                current_refs.append(ref)
            except LightGraphError as error:
                if error.code != "stale_construction_evidence":
                    raise
                stale_count += 1
        return {"format_version": 1, "target": "graph",
                "context_fingerprint": self.context_fingerprint,
                "drafts": [{"draft_id": item.draft.draft_id, "revision": item.revision,
                            "status": item.status, "entity_types": len(item.draft.entities),
                            "relation_types": len(item.draft.relations),
                            "mappings": sum(len(d.mappings) for d in item.draft.entities + item.draft.relations),
                            "decisions": item.draft.decisions,
                            "suggestions": item.draft.suggestions,
                            "unresolved": item.draft.unresolved,
                            "validation": item.validation.model_dump(mode="json") if item.validation else None,
                            "last_successful_validation": item.last_successful_validation.model_dump(mode='json')
                                if item.last_successful_validation else None}
                           for item in self.list_drafts()],
                "evidence_refs": current_refs, "stale_evidence_count": stale_count,
                "structure_checks": manifest.get('structure_checks', {}),
                "structure_publications": manifest.get('structure_publications', {}),
                "publications": [p for p in manifest.get('publications', {}).values()
                                 if p['context_fingerprint'] == self.context_fingerprint]}


def _merge_graphs(graphs, *, fusion_policy=None):
    """Combine independently checked subgraphs without silently replacing facts."""
    from lightgraph.source_values import persisted_identity_key
    from .fusion import (merge_definition, deduplicate_supports, stamp_execution,
                         retain_definition_variants, definition_conflict_diagnostics)
    # A noisy independent draft keeps its own definition and facts. Never mutate
    # the validated publication, whose digest remains part of the audit trail.
    graphs = [deepcopy(graph) for graph in graphs]
    retain_definition_variants(graphs, self_contained=[False] * len(graphs))
    result = {'format_version': 1, 'target': 'graph', 'status': 'published',
              **({'extends_base_graph': True} if any(g.get('extends_base_graph') for g in graphs) else {}),
              'semantic_origin': 'agent_authored', 'definitions': {'entities': [], 'relations': []},
              'nodes': [], 'edges': [], 'evidence_refs': []}
    definitions = {category: {} for category in ('entities', 'relations')}
    items = {category: {} for category in ('nodes', 'edges')}
    retained_global_labels = not fusion_policy and any(
        definition.get('identity_scope') == 'global' and definition.get('identity_domain')
        and definition.get('identity_evidence_refs')
        for graph in graphs for definition in graph.get('definitions', {}).get('entities', []))
    key_versions = {graph.get('source_key_semantics', 'legacy_scalar_v1') for graph in graphs}
    if len(key_versions) > 1:
        raise LightGraphError('published_graph_key_semantics_conflict')
    if key_versions:
        result['source_key_semantics'] = next(iter(key_versions))
    access = {graph.get('access_fingerprint') for graph in graphs}
    if len(access) > 1:
        raise LightGraphError('published_graph_access_conflict')
    if access and next(iter(access)) is not None:
        result['access_fingerprint'] = next(iter(access))
    visibility_versions = {graph.get('support_visibility_version') for graph in graphs}
    if len(visibility_versions) > 1:
        raise LightGraphError('published_graph_visibility_conflict')
    if visibility_versions and next(iter(visibility_versions)) is not None:
        result['support_visibility_version'] = next(iter(visibility_versions))
    for graph in graphs:
        if graph.get('read_window'):
            result.setdefault('validation_snapshots', []).append({
                'draft_id': graph.get('draft_id'), 'draft_revision': graph.get('draft_revision'),
                'read_window': deepcopy(graph['read_window'])})
        for ref in graph.get('evidence_refs', []):
            if ref not in result['evidence_refs']:
                result['evidence_refs'].append(ref)
        for category in definitions:
            for definition in graph.get('definitions', {}).get(category, []):
                definition = deepcopy(definition)
                if category == 'entities':
                    definition.setdefault('display_mode', 'auto')
                    definition.setdefault('identity_origin', 'row')
                previous = definitions[category].get(definition['type'])
                definitions[category][definition['type']] = merge_definition(previous, definition)
        for category in items:
            for item in graph.get(category, []):
                if fusion_policy:
                    stamp_execution(item, graph)
                previous = items[category].get(item['id'])
                if previous is None:
                    items[category][item['id']] = item
                    continue
                fixed = ('type',) if category == 'nodes' else ('type', 'source', 'target')
                if (any(previous.get(key) != item.get(key) for key in fixed)
                        or category == 'nodes' and persisted_identity_key(previous) != persisted_identity_key(item)):
                    raise LightGraphError('published_graph_identity_conflict')
                if category == 'nodes':
                    if (previous.get('identity_origin', 'row') != item.get('identity_origin', 'row')
                            or previous.get('identity_domain') != item.get('identity_domain')):
                        raise LightGraphError('published_graph_identity_conflict')
                    if previous.get('identity_origin') == 'physical_column':
                        from .metadata_columns import merge_column_supports
                        merge_column_supports(previous, deepcopy(item))
                        continue
                    if 'supports' not in previous or 'supports' not in item:
                        if previous.get('label') != item.get('label'):
                            raise LightGraphError('published_graph_identity_conflict')
                    else:
                        contract = definitions['entities'].get(item['type'], {})
                        default_global = (not fusion_policy and contract.get('identity_scope') == 'global'
                            and item.get('identity_domain')
                            and contract.get('identity_domain') == item['identity_domain']
                            and contract.get('identity_evidence_refs'))
                        retained_global_labels = retained_global_labels or bool(default_global)
                        reject_labels = not default_global and (not fusion_policy
                            or fusion_policy.label_conflict_policy != 'retain_variants')
                    if 'supports' in previous and 'supports' in item and reject_labels:
                        labels = {}
                        for support in previous['supports'] + item['supports']:
                            label = support.get('label')
                            if label is not None:
                                values = labels.setdefault(support['scope_ref'], set())
                                values.add(label)
                                if len(values) > 1:
                                    raise LightGraphError('published_graph_identity_conflict')
                for key in ('aliases', 'provenance', 'supports', 'english_names', 'descriptions'):
                    if key in item:
                        values = previous.setdefault(key, [])
                        for value in item[key]:
                            if value not in values:
                                values.append(deepcopy(value))
                for key, values in item.get('properties', {}).items():
                    merged = previous.setdefault('properties', {}).setdefault(key, [])
                    for value in values:
                        if canonical(value) not in {canonical(v) for v in merged}:
                            merged.append(deepcopy(value))
    result['definitions'] = {key: [value[name] for name in sorted(value)] for key, value in definitions.items()}
    result.update({key: sorted(value.values(), key=lambda item: item['id']) for key, value in items.items()})
    result['definition_conflicts'] = definition_conflict_diagnostics(result)
    pending = {canonical(item): item for graph in graphs for item in graph.get('unresolved_relations', [])}
    result['unresolved_relations'] = [pending[key] for key in sorted(pending)]
    if fusion_policy:
        result['label_conflict_policy'] = fusion_policy.label_conflict_policy
        result['fusion_policy'] = fusion_policy.model_dump(mode='json')
        result['fusion_policy_digest'] = digest(result['fusion_policy'])
    elif retained_global_labels:
        result['label_conflict_policy'] = 'retain_variants'
    if fusion_policy or retained_global_labels:
        for item in result['nodes'] + result['edges']:
            if 'supports' in item:
                deduplicate_supports(item)
    for node in result['nodes']:
        labels = sorted({support['label'] for support in node.get('supports', []) if support.get('label') is not None})
        if labels:
            node['label'] = labels[0]
    # Separate valid drafts can form a cycle only after their edges are joined.
    # Check declared graph constraints before atomically advancing any head.
    from .context_topology import detect_context_cycles
    result['context_cycle_diagnostics'] = []
    for relation in result['definitions']['relations']:
        if not relation.get('acyclic'):
            continue
        edges = [edge for edge in result['edges'] if edge['type'] == relation['type']]
        topology = detect_context_cycles(edges)
        if not topology['complete']:
            result['context_cycle_diagnostics'].append({'status': 'unresolved',
                'reason': 'context_cycle_analysis_incomplete', 'relation': relation['type'],
                'node_ids': sorted({edge[end] for edge in edges for end in ('source', 'target')}),
                'edge_ids': sorted(edge['id'] for edge in edges)})
        if topology['cycle_edge_ids']:
            raise LightGraphError('published_relation_cycle')
    from .display import derive_display_graph
    return derive_display_graph(result)
