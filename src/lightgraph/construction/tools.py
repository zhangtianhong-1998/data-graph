"""Small domain tool surface for an autonomous graph-building Agent.

No naming heuristics, table ordering, submission windows or model calls live
here. The caller chooses what to inspect and when to save or revise a mapping.
"""
from __future__ import annotations

import hashlib
import uuid
from copy import deepcopy
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from lightgraph.contracts import digest
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.tools import ToolExecutor
from lightgraph.documents import DocumentCorpus, load_documents
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import open_sources

from .models import GraphDraft, draft_evidence_refs
from .access import access_fingerprint, ensure_supported_access, filter_graph_scopes
from .executor import execute_draft
from .intent import effective_goal_markdown, goal_descriptor, read_goal_material
from .workspace import GraphWorkspace, dump_yaml, write_yaml


SKILL_ROOT = Path(__file__).parent / "skills" / "build-graph"


def _file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class ConstructionTools:
    """Own one run's sources, issued evidence, and durable candidate workspace."""

    def __init__(self, config, workspace, *, adapters=None, allow_publication=True):
        self.config = config = config.model_copy(deep=True)
        ensure_supported_access(config)
        # This is a host capability, never an Agent-supplied tool argument.
        self.allow_publication = bool(allow_publication)
        self._lock = RLock()
        self._own_adapters = adapters is None
        self.adapters = list(adapters if adapters is not None else open_sources(config,
            metadata_only=config.resolved_model.mode in {"disabled", "metadata_only"}))
        self.executors = {}
        self._read_chunks = {}
        try:
            self.documents = DocumentCorpus([document for document in load_documents(config.documents)
                if document.document_id not in config.removed_document_ids])
            mapping = {t.source_id: adapter for adapter in self.adapters for t in adapter.describe()}
            budget = BudgetAllocator(Cost(db_cu=config.max_db_queries, llm_calls=None,
                llm_tokens=None, memory_bytes=config.max_working_bytes, seconds=config.max_run_seconds))
            self.budget = budget
            for adapter in self.adapters:
                executor = ToolExecutor(adapter, config.goal, budget, documents=self.documents,
                    allow_source_samples=config.resolved_model.mode in {"in_environment", "approved_external"},
                    source_adapters=mapping)
                for source_id in executor._local_source_ids:
                    self.executors[source_id] = executor
            self.tables = {key: table for executor in self.executors.values()
                           for key, table in executor.tables.items()}
            self._sections = self.documents.contexts_for(list(self.tables.values()))
            self._source_hashes = [(s.id, _file_hash(s.path)) for s in config.sources if s.path]
            self._material_hashes = [(path, _file_hash(path)) for path in config.documents]
            fingerprint = digest(config.goal.model_dump(mode="json", by_alias=True), effective_goal_markdown(config), config.isolation_domain, access_fingerprint(config),
                [t.model_dump(mode="json", by_alias=True)
                for _, t in sorted(self.tables.items())],
                self._source_hashes,
                [s.model_dump(mode='json') for s in config.sources],
                # A schema fingerprint cannot certify unchanged database rows.
                uuid.uuid4().hex if any(not s.path for s in config.sources) else None,
                [(d.document_id, d.revision, d.content_hash) for d in self.documents.documents])
            self.workspace = GraphWorkspace(workspace, fingerprint)
            # Changes in source content or goals invalidate old evidence, even
            # when the source path and the visible schema remain unchanged.
            self._document_refs = {}
            for section in self._sections:
                ref = f"{section['doc_id']}@{section['revision']}:{section['section_id']}"
                self._document_refs[ref] = section
            self._issue_document_versions()
        except Exception:
            self.close()
            raise

    def close(self):
        for executor in set(self.executors.values()):
            executor.close()
        if self._own_adapters:
            for adapter in self.adapters:
                adapter.close()

    def _issue_document_versions(self):
        # References are issued only when material is actually returned below.
        self._versions = {f"{s['doc_id']}@{s['revision']}" for s in self._sections}

    def _executor(self, table):
        matches = [(key, value) for key, value in self.tables.items()
                   if table in {key, value.name, value.qualified_name,
                                f"{value.source_id}.{value.qualified_name}"}]
        if len(matches) != 1:
            raise LightGraphError("ambiguous_table" if matches else "unauthorized_object")
        key, value = matches[0]
        return self.executors[value.source_id], key, value

    def call(self, tool_name, **arguments):
        """Serialize mutations and return actionable errors without raw values."""
        methods = {key: getattr(self, key) for key in ("describe_sources", "inspect_source",
                   "read_material", "save_graph_draft", "validate_graph_draft", "read_workspace",
                   "publish_graph_draft")}
        with self._lock:
            try:
                if tool_name not in methods:
                    raise LightGraphError("unknown_construction_tool")
                return methods[tool_name](**arguments)
            except ValidationError as exc:
                return {"status": "error", "code": "invalid_graph_draft", "diagnostics": [
                    {"path": list(e["loc"]), "constraint": e["type"]}
                    for e in exc.errors(include_input=False, include_url=False)],
                    "next_action": "Read draft_schema and revise this draft. Other drafts are retained."}
            except LightGraphError as exc:
                return {"status": "error", "code": exc.code,
                        "next_action": "Correct the affected request or retain it as unresolved; other drafts remain available."}
            except (TypeError, ValueError, KeyError):
                return {"status": "error", "code": "invalid_construction_arguments"}
            except Exception:
                # MCP otherwise reflects exception text, which can contain a
                # backend's SQL or source values. Durable drafts stay available.
                return {"status": "error", "code": "construction_tool_failed",
                        "next_action": "The operation failed. Retain the draft and retry after checking the source or workspace."}

    def describe_sources(self):
        """Return the authorized physical catalog without inferred business roles."""
        titles = {d.document_id: d.title for d in self.documents.documents}
        return {"status": "ok", "goal": goal_descriptor(self.config),
                "output_constraints": {"entity_kinds": self.config.goal.entity_kinds,
                                       "relation_kinds": self.config.goal.relation_kinds},
                "result_visibility": {"scopes": self.config.scopes,
                    "scope_bindings": [binding.model_dump(mode='json', by_alias=True) for binding in self.config.scope_bindings],
                    "unbound_scope": "public", "managed_by": "operator"},
                "tables": [{"table": key, "source": t.source_id, "schema": t.schema_name,
                            "name": t.name, "columns": [c.model_dump(mode="json") for c in t.columns],
                            "declared_unique_keys": t.unique_keys,
                            "declared_foreign_keys": [fk.model_dump(mode="json") for fk in t.foreign_keys]}
                           for key, t in self.tables.items()],
                "documents": list({s["doc_id"]: {"id": s["doc_id"], "title": titles.get(s["doc_id"], s["doc_id"]), "revision": s["revision"]}
                                   for s in self._sections}.values()),
                "workspace": self.workspace.read_summary()}

    def inspect_source(self, table: str, operation: str, arguments: dict | None = None):
        """Run a named read-only probe using physical names, not guessed IDs."""
        self._check_current_inputs()
        executor, key, physical = self._executor(table)
        arguments = dict(arguments or {})
        if operation in {"get_metadata", "search_knowledge", "profile_columns", "check_fd"}:
            if "table" in arguments and arguments["table"] not in {key, table}:
                raise LightGraphError("probe_table_mismatch")
            arguments["table"] = key
        result = executor.execute(operation, arguments)
        # The generic draft protocol uses physical column names. Legacy probe
        # aliases are implementation details, not references the Agent must copy.
        result.pop('probe_id', None)
        if operation == 'get_metadata' and result.get('status') == 'ok':
            data = result.get('data', {})
            for entry in data.get('tables', []):
                for column in entry.get('columns', []):
                    column.pop('id', None)
            data['identifier_usage'] = {
                'table_arguments': 'Use tool_table_id in source probe table arguments.',
                'column_arguments': 'Use each column.name in source probes and YAML mapping columns.',
                'sql': 'Use sql_table and column.sql_identifier in SQL on the current source.',
                'graph_mappings': 'Use source_id, schema, physical table name and physical column names.',
            }
        if result.get('code') in {'unauthorized_object', 'unauthorized_column', 'ambiguous_column'}:
            result['guidance'] = (
                'Read describe_sources or get_metadata for authorized tables and columns. '
                'Use physical column names in probe arguments and YAML mappings; copy '
                'sql_table and sql_identifier only when writing SQL. Unknown objects and '
                'excluded columns remain forbidden.')
        context = result.pop("context", None)
        refs = []
        if context:
            visible_sections = []
            for section in context.get("documents", []):
                visible_sections.append(section)
                # A partial document window cannot stand in for its full section.
                if section.get("read_state") != "complete":
                    continue
                ref = f"{section['doc_id']}@{section['revision']}:{section['section_id']}"
                if ref in self._document_refs:
                    refs.extend(self._issue_section(self._document_refs[ref]))
            result["context"] = {"documents": visible_sections, "document_refs": refs,
                                 "read_more": "read_material(document:<id>)"}
        if operation == 'search_knowledge' and result.get('status') == 'ok':
            for section in result.get('data', {}).get('sections', []):
                ref = f"{section['doc_id']}@{section['revision']}:{section['section_id']}"
                if section.get('read_state') == 'complete' and ref in self._document_refs:
                    refs.extend(self._issue_section(self._document_refs[ref]))
            result['document_refs'] = list(dict.fromkeys(refs))
        if result.get("status") == "ok":
            # Only counts and column references enter evidence. Rows remain in
            # the current tool result and the separately configured run trace.
            details = {"operation": operation, "table": key,
                       "request": arguments if operation != "execute_readonly_sql" else
                           {"query_fingerprint": digest(arguments)},
                       "result_digest": digest(result),
                       "complete": bool(result.get("coverage", {}).get("result_complete", False))
                                   and not result.get("coverage", {}).get("truncated", False)}
            if isinstance(result.get("aggregates"), dict):
                details["aggregates"] = result["aggregates"]
            evidence = self.workspace.issue_evidence("source_probe",
                source_ref={"source_id": physical.source_id, "schema": physical.schema_name,
                            "table": physical.name}, details=details)
            result["evidence_ref"] = evidence.ref
        return result

    def _issue_section(self, section):
        version = f"{section['doc_id']}@{section['revision']}"
        ref = f"{version}:{section['section_id']}"
        common = {"document_id": section["doc_id"], "revision": section["revision"]}
        self.workspace.issue_evidence("document_version", details=common, ref=version)
        self.workspace.issue_evidence("document_section", details={**common,
            "section_id": section["section_id"], "sources": section["sources"],
            "content_digest": digest(section["text"]), "complete": True}, ref=ref)
        return [ref]

    def read_material(self, name: str, page: int = 0):
        """Read Markdown goal, skill, reference, schema or document on demand."""
        if type(page) is not int or page < 0:
            raise LightGraphError("invalid_material_page")
        if name == "goal":
            return read_goal_material(self.config, page)
        if name == "skill":
            return {"status": "ok", "text": (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")}
        if name == "draft_schema":
            return {"status": "ok", "format": "yaml", "content": dump_yaml(GraphDraft.model_json_schema())}
        if name == "source_operations":
            executor = next(iter(self.executors.values()), None)
            operations = deepcopy(executor.tool_specs()) if executor else []
            for tool in operations:
                function = tool['function']
                if function['name'] == 'get_metadata':
                    function['description'] = (
                        'Read authorized physical tables, column names, comments and declared keys. '
                        'Use tool_table_id in probe table arguments and physical column names in '
                        'probe column arguments and graph YAML mappings. Use quoted sql_table and '
                        'sql_identifier only in SQL. Declarations are evidence, not confirmed semantics.')
                elif function['name'] == 'execute_readonly_sql':
                    function['description'] = function['description'].replace(
                        'tool_table_id and opaque column.id are not SQL identifiers.',
                        'Use physical table and column names, with the supplied SQL quoting.')
            return {"status": "ok", "operations": operations}
        if name.startswith("reference:"):
            filename = name.partition(":")[2]
            permitted = {p.name: p for p in (SKILL_ROOT / "references").glob("*.md")}
            if filename not in permitted:
                raise LightGraphError("unknown_skill_reference")
            return {"status": "ok", "text": permitted[filename].read_text(encoding="utf-8")}
        if name.startswith("evidence:"):
            return {"status": "ok", "evidence": self.workspace.read_evidence(name.partition(":")[2]).model_dump(mode="json")}
        if name.startswith("document:"):
            self._check_current_inputs()
            doc_id = name.partition(":")[2]
            sections = [s for s in self._sections if s["doc_id"] == doc_id]
            windows = [(s, offset, max(1, (len(s["text"]) + 15999) // 16000))
                       for s in sections for offset in range(0, max(1, len(s["text"])), 16000)]
            if page >= len(windows):
                raise LightGraphError("unknown_document_page")
            section, offset, count = windows[page]
            ref = f"{section['doc_id']}@{section['revision']}:{section['section_id']}"
            seen = self._read_chunks.setdefault(ref, set())
            seen.add(offset // 16000)
            complete = len(seen) == count
            return {"status": "ok", "section": {**section,
                    "text": section["text"][offset:offset + 16000],
                    "read_state": "complete" if count == 1 else "partial"},
                    "section_fully_read": complete,
                    "evidence_refs": self._issue_section(section) if complete else [],
                    "next_page": page + 1 if page + 1 < len(windows) else None}
        raise LightGraphError("unknown_construction_material")

    def save_graph_draft(self, yaml_text: str):
        record = self.workspace.save_draft(yaml_text)
        return {"status": "ok", "draft_id": record.draft.draft_id,
                "revision": record.revision, "state": record.status,
                "next_action": "Validate this draft, or revise it as new evidence arrives."}

    def validate_graph_draft(self, draft_id: str):
        if self.config.resolved_model.mode not in {"in_environment", "approved_external"}:
            raise LightGraphError("metadata_only_query_forbidden")
        self._check_current_inputs()
        record = self.workspace.get_draft(draft_id)
        if record.status == "stale":
            raise LightGraphError("stale_graph_draft")
        for ref in draft_evidence_refs(record.draft):
            self.workspace.read_evidence(ref)
        result = execute_draft(record.draft, self.adapters, list(self.tables.values()),
            goal=self.config.goal, max_working_bytes=self.config.max_working_bytes,
            max_rows=self.config.limits.max_result_rows, max_seconds=self.budget.remaining_seconds(),
            scope_bindings=self.config.scope_bindings)
        self._check_current_inputs()
        evidence = self.workspace.issue_evidence("data_validation", details={
            "draft_id": draft_id, "draft_revision": record.revision,
            "passed": result["passed"], "metrics": result["metrics"],
            "diagnostics": result["diagnostics"]})
        graph = {**result.pop('graph'), 'draft_id': draft_id, 'draft_revision': record.revision,
                 'access_fingerprint': access_fingerprint(self.config),
                 'evidence_refs': list(dict.fromkeys(record.draft.evidence_refs + [evidence.ref]))}
        relative = f"previews/{digest(draft_id)[:16]}-{record.revision}.yaml"
        path = self.workspace.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        write_yaml(path, graph)
        current = self.workspace.record_validation(draft_id, revision=record.revision,
            passed=result["passed"], diagnostics=result["diagnostics"], metrics=result["metrics"],
            evidence_refs=[evidence.ref], graph=graph)
        return {"status": "ok", "state": current.status, **result,
                "evidence_ref": evidence.ref, "candidate_graph": relative,
                "semantic_assessment": "agent_authored", "published": False}

    def publish_graph_draft(self, draft_id: str):
        """Publish an Agent-selected revision only while its checks are current."""
        if not self.allow_publication:
            raise LightGraphError("construction_publication_disabled")
        if self.config.resolved_model.mode not in {"in_environment", "approved_external"}:
            raise LightGraphError("metadata_only_query_forbidden")
        self._check_current_inputs()
        record = self.workspace.get_draft(draft_id)
        preview = self.workspace.root / f"previews/{digest(draft_id)[:16]}-{record.revision}.yaml"
        if not preview.exists():
            raise LightGraphError("graph_validation_required")
        from .workspace import safe_load_yaml
        graph = safe_load_yaml(preview.read_text(encoding="utf-8"), max_bytes=128 * 1024 * 1024)
        if any(not s.path for s in self.config.sources):
            # A per-run schema token cannot prove unchanged database rows.
            # Recheck the materialized facts immediately before publication.
            fresh = execute_draft(record.draft, self.adapters, list(self.tables.values()),
                goal=self.config.goal, max_working_bytes=self.config.max_working_bytes,
                max_rows=self.config.limits.max_result_rows, max_seconds=self.budget.remaining_seconds(),
                scope_bindings=self.config.scope_bindings)
            compared = {key: value for key, value in graph.items()
                        if key not in {'draft_id', 'draft_revision', 'evidence_refs', 'access_fingerprint'}}
            new_graph = {key: value for key, value in fresh['graph'].items() if key != 'evidence_refs'}
            if not fresh['passed'] or digest(compared) != digest(new_graph):
                raise LightGraphError('construction_source_changed')
        publication = self.workspace.publish(draft_id, revision=record.revision, graph=graph)
        manifest = self.workspace.read_summary()
        merged = filter_graph_scopes(self.workspace.read_published_graph(), self.config.scopes)
        return {"status": "ok", "draft_id": draft_id, "publication": publication,
                "graph": "manifest.yaml:current_graph", "publications": len(manifest['publications']),
                "nodes": len(merged['nodes']), "edges": len(merged['edges']),
                "semantic_assessment": "agent_authored"}

    def _check_current_inputs(self):
        try:
            changed = (self._source_hashes != [(s.id, _file_hash(s.path)) for s in self.config.sources if s.path]
                       or self._material_hashes != [(path, _file_hash(path)) for path in self.config.documents])
        except OSError:
            raise LightGraphError('construction_source_unavailable') from None
        if changed:
            raise LightGraphError('construction_source_changed')

    def read_workspace(self, draft_id: str | None = None):
        value = self.workspace.get_draft(draft_id).model_dump(mode="json", by_alias=True) if draft_id else self.workspace.read_summary()
        return {"status": "ok", "format": "yaml", "content": dump_yaml(value)}
