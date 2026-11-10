"""Small domain tool surface for an autonomous graph-building Agent.

No naming heuristics, table ordering, submission windows or model calls live
here. The caller chooses what to inspect and when to save or revise a mapping.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from copy import deepcopy
from pathlib import Path
from threading import Event, RLock
from collections import Counter

from pydantic import ValidationError

from lightgraph.contracts import digest
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.tools import ToolExecutor
from lightgraph.documents import DocumentCorpus, load_documents
from lightgraph.errors import LightGraphError
from lightgraph.source_values import SOURCE_KEY_SEMANTICS_VERSION
from lightgraph.runtime.config import install_run_query_budget, open_sources

from .models import DraftRecord, GraphDraft, draft_evidence_refs
from .access import access_fingerprint, ensure_supported_access, filter_graph_scopes
from .executor import execute_draft
from .intent import effective_goal_markdown, goal_descriptor, read_goal_material
from .workspace import GraphWorkspace, dump_yaml, write_yaml


SKILL_ROOT = Path(__file__).parent / "skills" / "build-graph"
TERMINAL_QUERY_CODES = frozenset({'query_budget_exhausted', 'db_cu_budget_exhausted',
    'run_time_budget_exhausted', 'wall_time_budget_exhausted', 'construction_time_exhausted',
    'returned_rows_budget_exhausted', 'returned_bytes_budget_exhausted'})
STOP_QUERY_CODES = TERMINAL_QUERY_CODES | {'source_retry_limit_exhausted'}


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
        self._shutdown_requested = Event()
        self._own_adapters = adapters is None
        self.adapters = list(adapters if adapters is not None else open_sources(config,
            metadata_only=config.resolved_model.mode in {"disabled", "metadata_only"},
            isolate_database_failures=True))
        from lightgraph.sources.unavailable import UnavailableSource
        self.unavailable_sources = {adapter.source_id: adapter for adapter in self.adapters
                                    if isinstance(adapter, UnavailableSource)}
        self.initialization_failures = [adapter.initialization_failure
                                        for adapter in self.unavailable_sources.values()]
        self.executors = {}
        self._read_chunks = {}
        try:
            self.query_budget = install_run_query_budget(config, self.adapters)
            self.documents = DocumentCorpus([document for document in load_documents(config.documents)
                if document.document_id not in config.removed_document_ids])
            mapping = {t.source_id: adapter for adapter in self.adapters for t in adapter.describe()}
            self._adapters_by_source = {**mapping, **self.unavailable_sources}
            self._fact_data_denied = {source.id for source in config.sources
                if source.allow_fact_data is False}
            self._fact_data_denied.update(source_id for source_id, adapter in mapping.items()
                if getattr(adapter, 'metadata_only', False))
            budget = BudgetAllocator(Cost(db_cu=config.max_db_queries, llm_calls=None,
                llm_tokens=None, memory_bytes=config.max_working_bytes, seconds=config.max_run_seconds))
            self.budget = budget
            for adapter in self.adapters:
                if isinstance(adapter, UnavailableSource):
                    continue
                executor = ToolExecutor(adapter, config.goal, budget, documents=self.documents,
                    allow_source_samples=config.resolved_model.mode in {"in_environment", "approved_external"}
                        and not any(source_id in self._fact_data_denied
                                    for source_id, current in mapping.items() if current is adapter),
                    source_adapters=mapping, include_document_context=False)
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
                # Database artifacts describe their recorded read window, not
                # current live rows. Stable context permits restart recovery;
                # schema, access, configuration and file/doc checks remain above.
                {'snapshot_semantics': 'validated_snapshot_v1',
                 'connection_bindings': [(s.id, digest(value)) for s in config.sources
                    if not s.path and s.dsn_env and (value := os.getenv(s.dsn_env)) is not None]}
                    if any(not s.path for s in config.sources) else None,
                [(d.document_id, d.revision, d.content_hash) for d in self.documents.documents],
                {'source_key_semantics': SOURCE_KEY_SEMANTICS_VERSION})
            self.workspace = GraphWorkspace(workspace, fingerprint)
            from .result_materials import ResultMaterialStore, result_token_limit
            self.result_materials = ResultMaterialStore(self.workspace.root, fingerprint,
                self._result_access(), result_token_limit(config.resolved_model))
            self.terminal_reason = None
            from .source_circuit import SourceCircuits
            self.source_circuits = SourceCircuits()
            self._sync_source_circuits(self.unavailable_sources)
            # Live control state is reset for this worker; durable attempts remain.
            from lightgraph.runtime.retention import atomic_text
            atomic_text(self.workspace.root / 'query-stop.json', json.dumps({'code': None}))
            self._save_source_circuits()
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

    def request_shutdown(self):
        """Stop active work before its owner closes adapters; never take the writer lock.

        The writer may be blocked in a database operation. Cancellation must be
        observable from another thread, including between CPU-only graph rows.
        The MCP owner waits for that writer to settle before calling ``close``.
        """
        self._shutdown_requested.set()
        # Closing a failed MCP session is cancellation, not evidence that the
        # run's wall clock expired. Keep the deadline intact for reporting.
        self.budget.cancel()
        for adapter in self.adapters:
            budget = getattr(adapter, 'query_budget', None)
            if budget is not None:
                budget.cancel()
            try:
                adapter.cancel()
            except Exception:
                # Cancellation is best effort. The service still waits for
                # active operations; this never authorizes premature close.
                pass

    def close(self):
        if hasattr(self, 'result_materials'):
            self.result_materials.close()
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
                if self._shutdown_requested.is_set():
                    raise LightGraphError('construction_cancelled')
                if tool_name not in methods:
                    raise LightGraphError("unknown_construction_tool")
                if self.terminal_reason and (tool_name == 'inspect_source' and arguments.get('operation')
                        not in {'get_metadata', 'search_knowledge'}):
                    return self._stop_queries(self.terminal_reason)
                result = methods[tool_name](**arguments)
                if isinstance(result, dict):
                    result['query_budget'] = self.query_budget.snapshot()
                return result
            except ValidationError as exc:
                return {"status": "error", "code": "invalid_graph_draft", "diagnostics": [
                    {"path": list(e["loc"]), "constraint": e["type"]}
                    for e in exc.errors(include_input=False, include_url=False)],
                    "next_action": "Read draft_schema and revise this draft. Other drafts are retained."}
            except LightGraphError as exc:
                if exc.code in TERMINAL_QUERY_CODES:
                    return self._stop_queries(exc.code)
                from lightgraph.sources.database_errors import safe_database_diagnostic
                from .failures import failure_guidance
                detail = safe_database_diagnostic(exc)
                error = {'code': exc.code, 'retryable': exc.retryable,
                         **({'database': detail} if detail else {})}
                return {"status": "error", **error,
                        "next_action": "Correct the affected request. Record only unmet user requirements as unresolved; optional suggestions are not blockers. Other drafts remain available.",
                        **failure_guidance([error])}
            except (TypeError, ValueError, KeyError):
                return {"status": "error", "code": "invalid_construction_arguments"}
            except Exception:
                # MCP otherwise reflects exception text, which can contain a
                # backend's SQL or source values. Durable drafts stay available.
                return {"status": "error", "code": "construction_tool_failed",
                        "next_action": "The operation failed. Retain the draft and retry after checking the source or workspace."}

    def _result_access(self):
        """Bind cached values to every value-output permission, without secrets."""
        return digest(access_fingerprint(self.config), self.config.scopes,
            self.config.resolved_model.mode,
            [(source.id, source.allow_fact_data) for source in self.config.sources])

    def bounded_call(self, tool_name, **arguments):
        """Present a recoverable model view; internal callers keep full call()."""
        with self._lock:
            result = self.call(tool_name, **arguments)
            if (tool_name == 'read_material' and isinstance(arguments.get('name'), str)
                    and arguments['name'].startswith('result:')):
                # A material page already has a token-sized wrapper. Offloading
                # that page again would create an unreadable reference chain.
                return result
            return self.result_materials.bound(result, tool_name)

    def _stop_queries(self, code):
        """Tell Harness to finish using saved work, never retry exhausted queries."""
        if not self.terminal_reason:
            self.terminal_reason = code
            from lightgraph.runtime.retention import atomic_text
            atomic_text(self.workspace.root / 'query-stop.json', json.dumps({'code': code}))
        action = ('Repeated requests targeted a source already stopped in this run. '
                  'Do not retry it or remove its mappings to claim the full goal is complete. '
                  if self.terminal_reason == 'source_retry_limit_exhausted' else '')
        from .failures import failure_guidance
        return {'status': 'error', 'code': self.terminal_reason, 'retryable': False,
                'query_budget': self.query_budget.snapshot(),
                'queries_stopped': True, 'finalization_model_calls': 2,
                'next_action': action + 'Source queries cannot continue in this run. Read saved work, publish an already '
                    'successful validation if appropriate, and finish. Do not repeat queries or revise the '
                    'mapping solely because resources were exhausted. Two model calls remain for this closeout, '
                    'subject to the existing model/time limits.',
                **failure_guidance([{'code': self.terminal_reason}])}

    def _save_source_circuits(self):
        from lightgraph.runtime.retention import atomic_text
        atomic_text(self.workspace.root / 'source-circuits.json',
                    json.dumps(self.source_circuits.summary(), ensure_ascii=False))

    def _sync_source_circuits(self, source_ids):
        """Consult adapter state without SQL, before any new instance operation."""
        for source_id in set(source_ids):
            adapter = self._adapters_by_source.get(source_id)
            if adapter is None:
                continue
            code = getattr(adapter, 'terminal_failure_code', None)
            if isinstance(code, str):
                self.source_circuits.fail(source_id, {'code': code,
                    'database': getattr(adapter, 'terminal_failure_diagnostic', None)}, terminal=True, observed=False)

    def _source_gate(self, source_ids):
        source_ids = set(source_ids)
        self._sync_source_circuits(source_ids)
        blocked = self.source_circuits.reject(source_ids)
        if not blocked:
            return None
        self._save_source_circuits()
        common = {'blocked_sources': blocked, 'retryable': False,
                  'new_snapshot_recovery_available': False,
                  'source_circuits': self.source_circuits.summary()}
        if any(s['blocked_requests'] >= self.source_circuits.blocked_request_limit for s in blocked):
            return {**self._stop_queries('source_retry_limit_exhausted'), **common}
        return {'status': 'error', 'code': 'source_circuit_open', **common,
                'failure_class': 'source_execution', 'recovery': 'operator_requalification_required',
                'next_action': 'This source is stopped for this run. No queries or new validation were executed. '
                    'Preserve the original draft and its earlier candidate/validation. Continue metadata, '
                    'structure or independent healthy-source work; do not repeatedly validate this source, '
                    'rename a draft or remove source mappings to claim the full goal is complete. '
                    'Three blocked requests end data exploration and leave two model dispatches for closeout.'}

    def describe_sources(self):
        """Return the authorized physical catalog without inferred business roles."""
        titles = {d.document_id: d.title for d in self.documents.documents}
        return {"status": "ok", "goal": goal_descriptor(self.config),
                "query_budget": self.query_budget.snapshot(),
                "construction_target": self.config.construction_target,
                "output_constraints": {"entity_kinds": self.config.goal.entity_kinds,
                                       "relation_kinds": self.config.goal.relation_kinds},
                "result_visibility": {"scopes": self.config.scopes,
                    "scope_bindings": [binding.model_dump(mode='json', by_alias=True) for binding in self.config.scope_bindings],
                    "unbound_scope": "public", "managed_by": "operator"},
                "tables": [{"table": key, "source": t.source_id, "schema": t.schema_name,
                            "sql_dialect": self.executors[t.source_id].compiler.dialect,
                            "fact_data_allowed": self._fact_data_allowed(t.source_id),
                            "name": t.name, "columns": [c.model_dump(mode="json") for c in t.columns],
                            "declared_unique_keys": t.unique_keys,
                            "declared_foreign_keys": [fk.model_dump(mode="json") for fk in t.foreign_keys]}
                           for key, t in self.tables.items()],
                "documents": list({s["doc_id"]: {"id": s["doc_id"], "title": titles.get(s["doc_id"], s["doc_id"]), "revision": s["revision"]}
                                   for s in self._sections}.values()),
                "unavailable_sources": deepcopy(self.initialization_failures),
                "source_circuits": self.source_circuits.summary(),
                "workspace": self.workspace.read_summary()}

    def _fact_data_allowed(self, source_id):
        return (self.config.resolved_model.mode in {"in_environment", "approved_external"}
                and source_id not in self._fact_data_denied)

    def _require_fact_sources(self, source_ids):
        """Check each participating source before any query, including cross-source probes."""
        if self.config.resolved_model.mode not in {"in_environment", "approved_external"}:
            raise LightGraphError('metadata_only_query_forbidden')
        if any(source_id in self._fact_data_denied for source_id in source_ids):
            raise LightGraphError('source_fact_data_forbidden')

    def _require_draft_sources(self, draft):
        from .models import iter_fact_mappings
        sources = {mapping.source_id for mapping in iter_fact_mappings(draft)}
        if sources:
            self._require_fact_sources(sources)

    def _metadata_evidence_issues(self, draft):
        """Validate issued evidence applicability, not the Agent's semantic claim."""
        from .models import ColumnReference
        issues = []
        catalog = {(t.source_id, t.schema_name, t.name): t for t in self.tables.values()}
        for category in ('entities', 'relations'):
            for index, definition in enumerate(getattr(draft, category)):
                for number, mapping in enumerate(definition.mappings):
                    references = [(mapping, [])] if isinstance(mapping, ColumnReference) else [
                        (value, [name]) for name in ('source_metadata', 'target_metadata')
                        if (value := getattr(mapping, name, None)) is not None]
                    for column, suffix in references:
                        key = (column.source_id, column.schema_name, column.table)
                        table = catalog.get(key)
                        applicable = set()
                        if table is not None and column.column in table.column_names:
                            applicable = {f"{s['doc_id']}@{s['revision']}:{s['section_id']}"
                                for s in self.documents.context_for(table, [column.column])}
                        for position, ref in enumerate(column.evidence_refs):
                            evidence = self.workspace.read_evidence(ref)
                            source = evidence.source_ref if isinstance(evidence.source_ref, dict) else {}
                            metadata_probe = (evidence.kind == 'source_probe'
                                and evidence.details.get('operation') == 'get_metadata'
                                and evidence.details.get('complete') is True
                                and (source.get('source_id'), source.get('schema', 'public'), source.get('table')) == key)
                            document = evidence.kind == 'document_section' and ref in applicable
                            if not (metadata_probe or document):
                                issues.append({'code': 'metadata_evidence_not_applicable',
                                    'path': [category, index, 'mappings', number] + suffix + ['evidence_refs', position],
                                    'source_id': column.source_id, 'table': column.table, 'column': column.column})
        return issues

    def _metadata_evidence_error(self, draft):
        issues = self._metadata_evidence_issues(draft)
        if issues:
            return {'status': 'error', 'code': 'metadata_evidence_not_applicable', 'diagnostics': issues,
                    'next_action': 'Read get_metadata for this authorized table, or read the complete applicable '
                        'document section. Reference its issued evidence for this column. Query results, '
                        'validation records and references for other tables cannot establish column definitions. '
                        'Existing drafts remain available; do not use fact values as column identity.'}
        return None

    def _snapshot_recovery_sources(self, source_ids):
        """Observe recovery eligibility only after the failed snapshot closes."""
        return sorted({source_id for source_id in source_ids
            if not self.source_circuits.blocked([source_id])
            and source_id in self.executors and getattr(self.executors[source_id].adapter,
                'new_snapshot_recovery_allowed', False) is True})

    def inspect_source(self, table: str, operation: str, arguments: dict | None = None):
        """Run a named read-only probe using physical names, not guessed IDs."""
        self._check_current_inputs()
        unavailable = self._unavailable_table_source(table)
        if unavailable:
            if operation in {'get_metadata', 'search_knowledge'}:
                failure = self.unavailable_sources[unavailable].initialization_failure
                return {**failure, 'status': 'error', 'code': 'source_initialization_failed',
                        'cause_code': failure['code']}
            return self._source_gate([unavailable])
        executor, key, physical = self._executor(table)
        arguments = dict(arguments or {})
        is_data_operation = operation not in {'get_metadata', 'search_knowledge'}
        involved = {physical.source_id}
        if is_data_operation:
            self._require_fact_sources([physical.source_id])
            if operation == 'check_reference':
                # The selected source may be allowed while the other endpoint
                # is metadata-only. Reject before either side reads rows.
                for field in ('source_table', 'target_table'):
                    if isinstance(arguments.get(field), str):
                        unavailable = self._unavailable_table_source(arguments[field])
                        if unavailable:
                            return self._source_gate([unavailable])
                        source_id = self._executor(arguments[field])[2].source_id
                        self._require_fact_sources([source_id])
                        involved.add(source_id)
            stopped = self._source_gate(involved)
            if stopped:
                return stopped
        if operation in {"get_metadata", "search_knowledge", "profile_columns", "check_fd"}:
            if "table" in arguments and arguments["table"] not in {key, table}:
                raise LightGraphError("probe_table_mismatch")
            arguments["table"] = key
        result = executor.execute(operation, arguments)
        if is_data_operation:
            reference_failure = result.get('coverage', {}).get('failure') if operation == 'check_reference' else None
            data_completed = result.get('status') == 'ok' and (
                operation != 'check_reference' or len(involved) == 1
                or result.get('coverage', {}).get('complete') is True)
            if data_completed:
                for source_id in involved:
                    self.source_circuits.succeeded(source_id)
            elif len(involved) == 1:
                self.source_circuits.fail(physical.source_id, result)
            elif isinstance(reference_failure, dict) and reference_failure.get('source_id') in involved:
                self.source_circuits.fail(reference_failure['source_id'], reference_failure)
            # Never guess the failed endpoint when a cross-source check cannot
            # attribute it. Adapter terminal state remains an independent signal.
            self._sync_source_circuits(involved)
            self._save_source_circuits()
            if isinstance(reference_failure, dict) and reference_failure.get('source_id') in involved:
                from .failures import failure_guidance
                failed_source = reference_failure['source_id']
                reference_failure.update(
                    source_blocked=bool(self.source_circuits.blocked([failed_source])),
                    new_snapshot_recovery_available=failed_source in self._snapshot_recovery_sources([failed_source]))
                result.update(**failure_guidance([reference_failure]))
            blocked = self.source_circuits.blocked(involved)
            if blocked:
                from .failures import failure_guidance
                result.update(source_circuits=self.source_circuits.summary(),
                    **failure_guidance([{'code': s['cause_code'], 'source_blocked': True}
                                       for s in blocked]))
        if result.get('status') == 'error':
            from .failures import SOURCE_FAILURES, failure_guidance
            if result.get('code') in SOURCE_FAILURES:
                recovery = self._snapshot_recovery_sources(involved)
                result.update(new_snapshot_recovery_available=bool(recovery), recovery_source_ids=recovery)
                result['source_blocked'] = bool(self.source_circuits.blocked(involved))
            result.update(source_id=physical.source_id, **failure_guidance([result]))
            if is_data_operation:
                result['source_circuits'] = self.source_circuits.summary()
        # The generic draft protocol uses physical column names. Legacy probe
        # aliases are implementation details, not references the Agent must copy.
        result.pop('probe_id', None)
        if operation == 'get_metadata' and result.get('status') == 'ok':
            data = result.get('data', {})
            for entry in data.get('tables', []):
                entry['fact_data_allowed'] = self._fact_data_allowed(entry.get('source_id', physical.source_id))
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

    def read_material(self, name: str, page: int = 0, section: str = ""):
        """Read Markdown goal, skill, reference, schema or document on demand."""
        if type(page) is not int or page < 0:
            raise LightGraphError("invalid_material_page")
        if name.startswith('result:'):
            self._check_current_inputs()
            return self.result_materials.read(name, page, section, access=self._result_access(),
                                              query_budget=self.query_budget.snapshot())
        if section:
            raise LightGraphError("result_section_requires_result_material")
        if name == "goal":
            return read_goal_material(self.config, page)
        if name == 'publication_conflicts' or name.startswith(('draft:', 'validation:', 'conflicts:')):
            kind, _, draft_id = name.partition(':')
            record = self.workspace.get_draft(draft_id) if draft_id else None
            if name == 'publication_conflicts':
                from .conflicts import conflict_details
                self._check_current_inputs()
                merged = self.workspace.read_published_graph()
                if merged.get('access_fingerprint') != access_fingerprint(self.config):
                    raise LightGraphError('validated_graph_access_mismatch')
                diagnostics = [{'code': 'same_identity_conflicting_label', 'node_id': node_id}
                    for node_id in merged.get('conflict_isolation', {}).get('node_ids', [])]
                contributors = []
                for graph in self.workspace.read_publication_contributors():
                    detail = conflict_details(graph, diagnostics, self.config.scopes)
                    if detail['nodes']:
                        contributors.append({'draft_id': graph.get('draft_id'),
                            'revision': graph.get('draft_revision'), **detail})
                value = {'basis': 'retained_publication_snapshots', 'contributors': contributors,
                         'requires_database': False,
                         'local_conflicts': 'Read conflicts:<draft_id> for identities already isolated within a draft.'}
            elif kind == 'draft':
                value = self.workspace._draft_record_payload(record)['draft']
            elif kind == 'conflicts':
                from .conflicts import conflict_details
                self._check_current_inputs()
                if record.status == 'stale':
                    raise LightGraphError('stale_graph_draft')
                validation = self.workspace.latest_conflict_validation(draft_id)
                graph = self.workspace.read_validation_graph(draft_id, validation)
                if graph.get('access_fingerprint') != access_fingerprint(self.config):
                    raise LightGraphError('validated_graph_access_mismatch')
                value = conflict_details(graph, validation.diagnostics, self.config.scopes)
                value.update(validation_id=validation.validation_id, observed_revision=validation.revision,
                             current_revision=record.revision, authorizes_current_publication=False)
            elif record.validation is not None:
                value = record.validation.model_dump(mode='json')
            else:
                raise LightGraphError('graph_validation_missing')
            content = dump_yaml(value)
            page_size = 12000
            pages = max(1, (len(content) + page_size - 1) // page_size)
            if page >= pages:
                raise LightGraphError('invalid_material_page')
            return {'status': 'ok', 'format': 'yaml', 'material': name,
                    **({'draft_id': draft_id, 'revision': record.revision, 'draft_state': record.status} if record else {}),
                    'material_revision': digest(content),
                    'page': page, 'pages': pages, 'characters': len(content),
                    'content': content[page * page_size:(page + 1) * page_size],
                    'content_is_fragment': pages > 1,
                    'next_page': page + 1 if page + 1 < pages else None,
                    'next_action': 'Concatenate content pages only when material_revision matches. '
                        'Read the saved draft and validation directly; session history is not their canonical copy.'}
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
                        f'in the {executor.compiler.dialect} dialect over authorized objects on this source.',
                        'using the sql_dialect returned by get_metadata for the selected source. '
                        'Only tables on that source can appear in its SQL.').replace(
                        'tool_table_id and opaque column.id are not SQL identifiers.',
                        'Use physical table and column names, with the supplied SQL quoting.')
            return {"status": "ok", "operations": operations,
                    "query_planning": {
                        "mapping_predicates": "Mappings support typed is_not_null/is_null, comparisons and all/any/not. Read reference:predicate-guide.md; never change code identity to a name merely to avoid NULL.",
                        "column_definitions": "Use metadata_column mappings for evidence-backed catalog column definitions, without reading their fact values. Read reference:metadata-objects.md for row-to-column relations and evidence boundaries.",
                        "profile_columns": "One exact aggregate query per selected column; each may scan the table.",
                        "check_fd": "One key-null aggregate plus one dependency query per selected value column; each may scan the table.",
                        "check_reference": "Actual paired-key coverage; cross-source checks use bounded projections and may be incomplete.",
                        "execute_readonly_sql": "LIMIT bounds returned rows, not scanned rows. GROUP BY, DISTINCT and ORDER BY may process the full selected range.",
                        "sample_boundary": "A small projection suggests roles; it does not prove complete identities or relationships. Save the candidate and its unmet requirements before costly validation.",
                    }}
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

    def _check_metadata_catalog(self, draft):
        """A lost fact connection does not revoke its observed catalog; a changed
        catalog or rejected access does. Never recover definitions from those.
        """
        from .metadata_columns import is_metadata_mapping
        sources = {m.source_id for e in draft.entities for m in e.mappings if is_metadata_mapping(m)}
        self._sync_source_circuits(sources)
        for state in self.source_circuits.blocked(sources):
            if (state.get('cause_code') not in {'database_reconnect_failed', 'database_connection_failed',
                    'read_only_transaction_failed', 'snapshot_close_failed'}
                    or state.get('database', {}).get('category') in {
                        'authentication', 'permission', 'configuration_or_catalog', 'unsupported'}):
                raise LightGraphError('metadata_catalog_requalification_required')

    def save_graph_draft(self, yaml_text: str | None = None, metadata_from_draft: str | None = None):
        from .workspace import parse_graph_draft
        if (yaml_text is None) == (metadata_from_draft is None):
            raise LightGraphError('graph_draft_input_selection_required')
        if metadata_from_draft is not None:
            from .definitions import metadata_definition_draft
            self._check_current_inputs()
            parent = self.workspace.get_draft(metadata_from_draft)
            draft = metadata_definition_draft(parent)
            self._check_metadata_catalog(draft)
        else:
            draft = parse_graph_draft(yaml_text)
        error = self._metadata_evidence_error(draft)
        if error:
            return error
        previous_entries = self.workspace._manifest()['drafts']
        record = self.workspace.save_draft(draft)
        entry = previous_entries.get(record.draft.draft_id)
        previous = DraftRecord.model_validate(self.workspace._read_record(entry['current'])) if entry else None
        from .revision_advice import revision_loss_advice
        advice = revision_loss_advice(previous.draft, record.draft) if previous and (
            previous.context_fingerprint == record.context_fingerprint) else []
        return {"status": "ok", "draft_id": record.draft.draft_id,
                "revision": record.revision, "state": record.status,
                "previous_revision": previous.revision if previous else None,
                "revision_advice": advice,
                **({'derived_from': {'draft_id': parent.draft.draft_id, 'revision': parent.revision,
                                     'fingerprint': parent.fingerprint},
                    'scope': 'metadata_definitions_only', 'fact_data_validation': 'not_run'}
                   if metadata_from_draft is not None else {}),
                "retained_failed_work": self._retained_failed_work(excluding=record.draft.draft_id),
                "next_action": "Validate this draft, or revise it as new evidence arrives."}

    def _retained_failed_work(self, *, excluding=None):
        """Keep execution-blocked candidates visible even when a new draft ID is used.

        This is a pointer to retained work, not an automatic goal-completeness
        judgment: independent drafts and evidence-based scope changes are legal.
        """
        from .failures import SOURCE_FAILURES
        from .models import iter_source_mappings
        records = []
        for item in self.workspace.list_drafts():
            if (item.draft.draft_id == excluding or item.status != 'needs_revision'
                    or item.context_fingerprint != self.workspace.context_fingerprint
                    or item.validation is None):
                continue
            codes = sorted({d.get('code') for d in item.validation.diagnostics
                            if isinstance(d.get('code'), str)})
            if not set(codes).intersection(SOURCE_FAILURES | TERMINAL_QUERY_CODES | {'memory_budget_exhausted'}):
                continue
            records.append({'draft_id': item.draft.draft_id, 'revision': item.revision,
                            'source_ids': sorted({m.source_id for m in iter_source_mappings(item.draft)}),
                            'entity_types': len(item.draft.entities), 'relation_types': len(item.draft.relations),
                            'failure_codes': codes})
        return {'total': len(records), 'items': records[:12], 'omitted': max(0, len(records) - 12),
                'next_action': 'Read these retained drafts before concluding the goal is complete. '
                    'A new or smaller draft does not resolve their source/resource failures. '
                    'These are review pointers, not proof that every candidate belongs in the final graph.'}

    def validate_graph_draft(self, draft_id: str, mode: str | None = None):
        mode = mode or self.config.construction_target
        if mode not in {'instances', 'structure'}:
            raise LightGraphError('invalid_construction_mode')
        if mode == 'structure':
            self._check_current_inputs()
            record = self.workspace.get_draft(draft_id)
            error = self._metadata_evidence_error(record.draft)
            if error:
                return error
            for ref in draft_evidence_refs(record.draft):
                self.workspace.read_evidence(ref)
            from .structure import save_structure_check
            return save_structure_check(self.workspace, draft_id, list(self.tables.values()),
                access=access_fingerprint(self.config), goal=self.config.goal,
                dialects={key: value.compiler.dialect for key, value in self.executors.items()},
                scope_bindings=self.config.scope_bindings)
        if self.config.resolved_model.mode not in {"in_environment", "approved_external"}:
            raise LightGraphError("metadata_only_query_forbidden")
        self._check_current_inputs()
        record = self.workspace.get_draft(draft_id)
        self._check_metadata_catalog(record.draft)
        error = self._metadata_evidence_error(record.draft)
        if error:
            return error
        self._require_draft_sources(record.draft)
        if record.status == "stale":
            raise LightGraphError("stale_graph_draft")
        for ref in draft_evidence_refs(record.draft):
            self.workspace.read_evidence(ref)
        from .models import iter_fact_mappings
        sources = sorted({m.source_id for m in iter_fact_mappings(record.draft)})
        if self.terminal_reason and sources:
            return {**self._stop_queries(self.terminal_reason),
                    'metadata_definitions': self._definition_options(record)}
        stopped = self._source_gate(sources)
        if stopped:
            stopped['metadata_definitions'] = self._definition_options(record)
            return stopped
        started = datetime.now(timezone.utc).isoformat()
        result = execute_draft(record.draft, self.adapters, list(self.tables.values()),
            goal=self.config.goal, max_working_bytes=self.config.max_working_bytes,
            max_rows=self.config.limits.max_result_rows, max_seconds=self.budget.remaining_seconds(),
            scope_bindings=self.config.scope_bindings,
            cancelled=self._shutdown_requested.is_set)
        self._check_current_inputs()
        recovery = self._snapshot_recovery_sources(sources)
        from .failures import SOURCE_FAILURES
        # A healthy mapping can depend on an unavailable endpoint. That is a
        # skipped dependency, not another failed connection to its own source.
        operation_failures = SOURCE_FAILURES - {'mapping_source_unavailable', 'relation_dependency_unavailable'}
        for diagnostic in result['diagnostics']:
            if diagnostic.get('code') in operation_failures:
                diagnostic['new_snapshot_recovery_available'] = diagnostic.get('source_id') in recovery
        # Count a source only once per operation, not once per failed mapping.
        # Unsuccessful mappings on healthy sources are not connection failures.
        for source_id in sources:
            failures = [d for d in result['diagnostics']
                        if d.get('source_id') == source_id and d.get('code') in operation_failures]
            if failures:
                primary = next((d for d in failures if d.get('phase') == 'snapshot_open'), failures[0])
                self.source_circuits.fail(source_id, primary)
            elif any(w['source_id'] == source_id and w['state'] == 'closed'
                     and w.get('completed_projections', 0) > 0
                     for w in result.get('source_read_windows', [])):
                self.source_circuits.succeeded(source_id)
        self._sync_source_circuits(sources)
        self._save_source_circuits()
        blocked_ids = {s['source_id'] for s in self.source_circuits.blocked(sources)}
        for diagnostic in result['diagnostics']:
            if diagnostic.get('source_id') in blocked_ids and diagnostic.get('code') in SOURCE_FAILURES:
                diagnostic.update(source_blocked=True, new_snapshot_recovery_available=False)
        read_window = {'started_at': started, 'finished_at': datetime.now(timezone.utc).isoformat(),
                       'source_ids': sources, 'consistency': 'independent_projection_snapshots',
                       'source_read_windows': result.pop('source_read_windows', []),
                       'publication_semantics': 'validated_snapshot'}
        evidence = self.workspace.issue_evidence("data_validation", details={
            "draft_id": draft_id, "draft_revision": record.revision,
            "passed": result["passed"], "metrics": result["metrics"],
            "diagnostics": result["diagnostics"]})
        graph = {**result.pop('graph'), 'draft_id': draft_id, 'draft_revision': record.revision,
                 'access_fingerprint': access_fingerprint(self.config),
                 'read_window': read_window,
                 'evidence_refs': sorted(draft_evidence_refs(record.draft) | {evidence.ref})}
        current = self.workspace.record_validation(draft_id, revision=record.revision,
            passed=result["passed"], diagnostics=result["diagnostics"], metrics=result["metrics"],
            evidence_refs=[evidence.ref], graph=graph, read_window=read_window)
        stop = next((d['code'] for d in result['diagnostics'] if d.get('code') in TERMINAL_QUERY_CODES), None)
        budget_state = self._stop_queries(stop) if stop else None
        from .statistics import graph_statistics
        from .quality import name_mapping_advice
        visible_graph = filter_graph_scopes(graph, self.config.scopes)
        statistics = graph_statistics(visible_graph)
        from .failures import failure_guidance
        diagnostics = result['diagnostics']
        from .conflicts import subset_available, isolate_validation
        subset = None
        if subset_available(current.validation):
            try:
                clean = isolate_validation(graph, diagnostics)
                subset = {'nodes': len(clean['nodes']), 'edges': len(clean['edges']),
                    'isolated_nodes': clean['conflict_isolation']['nodes'],
                    'isolated_edges': clean['conflict_isolation']['edges']}
            except LightGraphError:
                pass
        # Keep every counterexample in the validation artifact; a model reply
        # need not replay hundreds of identical error shapes on every attempt.
        return {"status": "ok", "state": current.status, **result,
                'metadata_definitions': self._definition_options(current),
                'diagnostics': diagnostics[:32],
                'diagnostic_count': len(diagnostics),
                'diagnostic_codes': dict(Counter(d['code'] for d in diagnostics)),
                'diagnostics_truncated': len(diagnostics) > 32,
                'validation_material': 'validation:' + draft_id,
                'conflicts_material': 'conflicts:' + draft_id,
                'publishable_subset': subset,
                'draft_material': 'draft:' + draft_id, "statistics": statistics,
                'effective_limits': {'max_working_bytes': self.config.max_working_bytes,
                    'max_returned_rows': self.config.max_returned_rows,
                    'max_returned_bytes': self.config.max_returned_bytes,
                    'max_result_rows': self.config.limits.max_result_rows,
                    'query_timeout_ms': self.config.limits.timeout_ms,
                    'run_max_seconds': self.config.max_run_seconds},
                "evidence_ref": evidence.ref, "candidate_graph": current.validation.graph_path,
                "validation_id": current.validation.validation_id, 'read_window': read_window,
                'quality_advice': name_mapping_advice(record.draft, visible_graph),
                'source_circuits': self.source_circuits.summary(),
                **failure_guidance(diagnostics),
                **({'resource_state': budget_state} if budget_state else {}),
                "semantic_assessment": "agent_authored", "published": False}

    def publish_graph_draft(self, draft_id: str, validation_id: str | None = None, mode: str | None = None):
        """Publish an Agent-selected revision only while its checks are current."""
        if not self.allow_publication:
            raise LightGraphError("construction_publication_disabled")
        mode = mode or self.config.construction_target
        if mode not in {'instances', 'structure'}:
            raise LightGraphError('invalid_construction_mode')
        from .models import iter_source_mappings
        record = self.workspace.get_draft(draft_id)
        failed = sorted({m.source_id for m in iter_source_mappings(record.draft)}
                        & self.unavailable_sources.keys())
        if failed:
            return {'status': 'error', 'code': 'source_initialization_failed',
                    'retryable': False, 'blocked_sources': [
                        self.unavailable_sources[source].initialization_failure for source in failed]}
        if mode == 'structure':
            if validation_id is not None:
                raise LightGraphError('structure_validation_selection_unsupported')
            self._check_current_inputs()
            from .structure import publish_structure
            return publish_structure(self.workspace, draft_id, access=access_fingerprint(self.config))
        if self.config.resolved_model.mode not in {"in_environment", "approved_external"}:
            raise LightGraphError("metadata_only_query_forbidden")
        self._check_current_inputs()
        record = self.workspace.get_draft(draft_id)
        self._check_metadata_catalog(record.draft)
        self._require_draft_sources(record.draft)
        selected = self.workspace.select_validation(draft_id, validation_id, isolate_conflicts=True)
        graph = self.workspace.read_validation_graph(draft_id, selected)
        if graph.get('access_fingerprint') != access_fingerprint(self.config):
            raise LightGraphError('validated_graph_access_mismatch')
        publication = self.workspace.publish(draft_id, revision=record.revision, graph=graph,
                                             validation_id=selected.validation_id, isolate_conflicts=True)
        manifest = self.workspace.read_summary()
        merged = filter_graph_scopes(self.workspace.read_published_graph(), self.config.scopes)
        from .statistics import graph_statistics
        return {"status": "ok", "draft_id": draft_id, "publication": publication,
                "graph": "manifest.yaml:current_graph", "publications": len(manifest['publications']),
                "nodes": len(merged['nodes']), "edges": len(merged['edges']),
                'conflict_isolation': merged.get('conflict_isolation'),
                'conflicts_material': 'publication_conflicts',
                "statistics": graph_statistics(merged), "semantic_assessment": "agent_authored"}

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
        if draft_id is None:
            value['retained_failed_work'] = self._retained_failed_work()
            value['source_circuits'] = self.source_circuits.summary()
            value['unavailable_sources'] = deepcopy(self.initialization_failures)
            value['metadata_definition_options'] = [option
                for record in self.workspace.list_drafts()
                if record.context_fingerprint == self.workspace.context_fingerprint
                and (option := self._definition_options(record)).get('available')]
            from .statistics import graph_statistics
            try:
                graph = filter_graph_scopes(self.workspace.read_published_graph(), self.config.scopes)
                value['statistics'] = graph_statistics(graph)
            except LightGraphError as exc:
                if exc.code != 'stale_published_graph':
                    raise
                value['statistics_status'] = 'stale_published_graph'
        content = dump_yaml(value)
        if draft_id and len(content) > 16000:
            record = self.workspace.get_draft(draft_id)
            validation = record.validation
            return {'status': 'ok', 'format': 'yaml', 'content': dump_yaml({
                'draft_id': draft_id, 'revision': record.revision, 'status': record.status,
                'entities': len(record.draft.entities), 'relations': len(record.draft.relations),
                'validation': None if validation is None else {
                    'passed': validation.passed, 'validation_id': validation.validation_id,
                    'diagnostic_codes': dict(Counter(d['code'] for d in validation.diagnostics))}}),
                'summary_only': True, 'draft_material': 'draft:' + draft_id,
                'validation_material': 'validation:' + draft_id if validation else None,
                'next_action': 'Read read_material(name="draft:' + draft_id + '", page=0) and follow next_page '
                    'for the complete saved YAML. Read validation material for all diagnostics. Do not reconstruct it from session searches.'}
        return {"status": "ok", "format": "yaml", "content": content}

    @staticmethod
    def _definition_options(record):
        from .definitions import definition_options
        return definition_options(record)

    def _unavailable_table_source(self, table):
        # Configured identifiers identify an unavailable source, never create a
        # readable table or columns. Ambiguous short names remain unauthorized.
        if any(table in {key, value.name, value.qualified_name,
                         f'{value.source_id}.{value.qualified_name}'}
               for key, value in self.tables.items()):
            return None
        matches = {source_id for source_id, adapter in self.unavailable_sources.items()
                   for name in adapter.allowed_tables
                   if table in {name, name.split('.')[-1], f'{source_id}:{name}', f'{source_id}.{name}'}}
        return next(iter(matches)) if len(matches) == 1 else None
