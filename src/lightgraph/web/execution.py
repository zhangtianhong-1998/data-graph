"""Project-isolated background execution and bounded, published graph views.

Only typed run reports and hashes of user-uploaded files survive a process. Model
transcripts, database query results and subprocess output never become run logs.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
import uuid

from ..contracts import RunReport, digest
from ..errors import LightGraphError
from ..runtime.locking import FileLock
from ..runtime.retention import atomic_text

ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset({"completed", "partial", "failed", "cancelled", "interrupted"})
MAX_GRAPH_NODES = 1000
MAX_GRAPH_EDGES = 3000
_RUN_ID = re.compile(r"^run_[0-9a-f]{24}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe_code(code, fallback="run_failed"):
    return code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else fallback


def _json_file(path):
    try:
        if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise LightGraphError("run_record_invalid") from None


def _save(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, sort_keys=True))


def _cancel_path(result_path):
    # Only a cancellation request is persisted, never a query or source value.
    return result_path.with_suffix(".cancel")


def _safe_report(value):
    """Accept the existing report contract, never arbitrary process output."""
    try:
        report = RunReport.model_validate(value)
    except (ValueError, TypeError):
        raise LightGraphError("run_report_invalid") from None
    # Runtime reports use closed numeric counters and error codes. Keep only
    # supported issue fields rather than allowing arbitrary message/evidence keys.
    report.issues = [{k: v for k, v in item.items()
                      if k in {"code", "table", "rule_id", "count"}}
                     for item in report.issues]
    return report.model_dump(mode="json")


def _input_manifest(config, project_path):
    """Hash uploaded dependencies, not database rows or query results."""
    paths = set(config.documents + config.rules)
    if config.agent_config:
        paths.add(config.agent_config)
    for source in config.sources:
        if source.path:
            paths.add(source.path)
        if source.capability_manifest:
            paths.add(source.capability_manifest)
    return _file_manifest(paths, project_path)


def _file_manifest(paths, project_path):
    manifest = {}
    for raw in sorted(paths):
        path = Path(raw)
        try:
            relative = str(path.resolve().relative_to(project_path.resolve()))
            if path.is_symlink() or not path.is_file():
                raise ValueError
            before = path.stat()
            value = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(block)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError
            manifest[relative] = value.hexdigest()
        except (OSError, ValueError):
            raise LightGraphError("input_dependency_changed") from None
    return manifest


def check_dependencies(expected, project_path):
    current = _file_manifest([project_path / name for name in expected], project_path)
    if current != expected:
        raise LightGraphError("input_dependency_changed")


def _resolved_payload(config, project_path, *, source_credentials=None, model_api_key=None):
    # Private resolved AgentConfig is not serialized by RunConfig. Flatten it
    # for this worker only; credentials travel through a pipe, never a file.
    data = config.model_dump(mode="json", by_alias=True)
    data["agent_config"] = None
    data["model"] = config.resolved_model.model_dump(mode="json")
    secret = config.resolved_model.api_key
    if secret is not None:
        data["model"]["api_key"] = secret.get_secret_value()
    if model_api_key is not None:
        data["model"]["api_key"] = model_api_key
    payload = {"config": data, "agent_name": config.agent_name,
               "dependencies": _input_manifest(config, project_path)}
    if source_credentials:
        payload["source_credentials"] = dict(source_credentials)
    return payload


class RunManager:
    """One child writer per project; no PID restored from disk is ever signalled."""
    def __init__(self, workspace, *, cancel_timeout=5.0):
        self.workspace = workspace
        self.cancel_timeout = cancel_timeout
        self._processes = {}
        self._guard = threading.RLock()
        self._closed = False
        self._service_lock = FileLock(self.workspace.root / ".service.lock",
            busy_code="workspace_service_active", invalid_code="workspace_service_lock_invalid").acquire()
        try:
            self._recover()
        except BaseException:
            self._service_lock.close()
            self._service_lock = None
            raise

    def _paths(self, pid, rid=None):
        project = self.workspace.project_path(pid)
        runs = project / "runs"
        if runs.is_symlink():
            raise LightGraphError("workspace_path_invalid")
        runs.mkdir(exist_ok=True)
        if rid is None:
            return project, runs
        if not _RUN_ID.fullmatch(rid):
            raise LightGraphError("run_not_found")
        return project, runs / (rid + ".json")

    def _recover(self):
        # Service restart has no authority over an old PID; preserve its result
        # as interrupted until a subsequent explicit extraction reconciles it.
        for project in self.workspace.root.iterdir():
            if not project.is_dir() or project.is_symlink():
                continue
            try:
                _, runs = self._paths(project.name)
            except LightGraphError:
                continue
            for path in runs.glob("run_*.json"):
                if not _RUN_ID.fullmatch(path.stem):
                    continue
                try:
                    record = _json_file(path)
                    if record.get("status") in ACTIVE_STATUSES:
                        record.update(status="interrupted", stage="finished", finished_at=_now(),
                                      cleanup_status="unknown", error_code="service_restarted")
                        _save(path, record)
                    # Exclusive service ownership proves no surviving worker
                    # still needs a cancellation request from a previous run.
                    _cancel_path(path.with_name(path.stem + ".result.json")).unlink(missing_ok=True)
                except (LightGraphError, OSError):
                    continue

    def start(self, pid, *, source_credentials=None, model_api_key=None):
        with self.workspace.lock(pid), self._guard:
            if self._closed:
                raise LightGraphError("run_service_stopped")
            if self.active(pid):
                raise LightGraphError("project_run_active")
            project, runs = self._paths(pid)
            config = self.workspace.resolved_config(pid).model_copy(deep=True)
            if not config.goal.purpose.strip():
                raise LightGraphError("extraction_purpose_required")
            if not config.sources and not config.documents:
                raise LightGraphError("project_inputs_required")
            for source in config.sources:
                if source.credential_mode == "session" and not (source_credentials or {}).get(source.id):
                    raise LightGraphError("source_credential_missing")
            payload = _resolved_payload(config, project, source_credentials=source_credentials,
                                        model_api_key=model_api_key)
            rid = "run_" + uuid.uuid4().hex[:24]
            result_path = runs / (rid + ".result.json")
            record = {"run_id": rid, "project_id": pid, "status": "queued", "stage": "starting",
                      "created_at": _now(), "finished_at": None, "report": None,
                      "error_code": None, "cleanup_status": "pending",
                      "dependencies": payload["dependencies"],
                      "config_revision": self.workspace.config_revision(pid)}
            _save(runs / (rid + ".json"), record)
            env = os.environ.copy()
            source_root = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            try:
                # The inherited lock stays held if this service crashes while
                # its worker is still publishing. Handle inheritance differs
                # between POSIX and Windows and is owned by FileLock.
                with self._service_lock.child_process_options() as child_options:
                    proc = subprocess.Popen([sys.executable, "-m", "lightgraph.web.worker", str(result_path)],
                                            cwd=project, env=env, stdin=subprocess.PIPE,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            **child_options)
                self._processes[pid] = {"process": proc, "record": record, "result": result_path,
                                        "cancel_requested": False, "forced": False, "config": config}
                proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                proc.stdin.close()
                record.update(status="running", stage="extracting")
                _save(runs / (rid + ".json"), record)
            except Exception:
                if pid in self._processes:
                    proc = self._processes.pop(pid)["process"]
                    proc.kill(); proc.wait()
                record.update(status="failed", stage="finished", finished_at=_now(),
                              cleanup_status="unknown", error_code="worker_start_failed")
                _save(runs / (rid + ".json"), record)
                return dict(record)
            threading.Thread(target=self._watch, args=(pid, proc), daemon=True).start()
            return dict(record)

    def _watch(self, pid, proc):
        proc.wait()
        with self.workspace.lock(pid), self._guard:
            entry = self._processes.get(pid)
            if entry and entry["process"] is proc:
                self._finish(pid, entry)

    def _finish(self, pid, entry):
        record = entry["record"]
        result = None
        try:
            result = _json_file(entry["result"])
            if result.get("status") not in TERMINAL_STATUSES:
                raise LightGraphError("run_record_invalid")
            report = _safe_report(result["report"]) if result.get("report") is not None else None
            record.update(status=result["status"], report=report,
                          cleanup_status="completed" if result.get("cleanup_status") == "completed" else "unknown",
                          error_code=_safe_code(result.get("error_code")) if result.get("error_code") else None)
        except LightGraphError:
            record.update(status="failed", report=None, cleanup_status="unknown", error_code="worker_result_missing")
        if entry["cancel_requested"]:
            record.update(status="cancelled", error_code="run_cancelled")
        if entry["forced"]:
            record.update(cleanup_status="unknown", error_code="run_force_stopped")
        if not entry["cancel_requested"]:
            try:
                changed = (_input_manifest(entry["config"], self.workspace.project_path(pid)) != record["dependencies"]
                           or self.workspace.config_revision(pid) != record["config_revision"]
                           or self.workspace.resolved_config(pid) != entry["config"])
            except LightGraphError:
                changed = True
            if changed and record["status"] in {"completed", "partial"}:
                record.update(status="partial", error_code="input_dependency_changed")
        record.update(stage="finished", finished_at=_now())
        _save(self._paths(pid, record["run_id"])[1], record)
        try:
            entry["result"].unlink(missing_ok=True)
            _cancel_path(entry["result"]).unlink(missing_ok=True)
        except OSError:
            pass
        self._processes.pop(pid, None)

    def active(self, pid):
        with self._guard:
            return pid in self._processes

    def get(self, pid, rid):
        with self.workspace.lock(pid):
            _, path = self._paths(pid, rid)
            if not path.exists():
                raise LightGraphError("run_not_found")
            return _json_file(path)

    def list(self, pid):
        with self.workspace.lock(pid):
            _, directory = self._paths(pid)
            records = []
            for path in directory.glob("run_*.json"):
                if _RUN_ID.fullmatch(path.stem):
                    records.append(_json_file(path))
            return sorted(records, key=lambda r: r["created_at"], reverse=True)

    def cancel(self, pid, rid):
        with self.workspace.lock(pid), self._guard:
            record = self.get(pid, rid)
            entry = self._processes.get(pid)
            if not entry or entry["record"]["run_id"] != rid:
                return record
            proc = entry["process"]
            if proc.poll() is not None:
                self._finish(pid, entry)
                return self.get(pid, rid)
            entry["cancel_requested"] = True
            try:
                # Windows terminate() is a force kill. A private marker lets
                # every worker run its normal source/transaction cleanup first.
                atomic_text(_cancel_path(entry["result"]), "cancel\n")
            except OSError:
                entry["forced"] = True
                proc.kill()
        try:
            proc.wait(timeout=self.cancel_timeout)
        except subprocess.TimeoutExpired:
            with self._guard:
                entry["forced"] = True
            proc.kill(); proc.wait()
        with self.workspace.lock(pid), self._guard:
            if self._processes.get(pid) is entry:
                self._finish(pid, entry)
            return self.get(pid, rid)

    def shutdown(self):
        with self._guard:
            self._closed = True
            entries = [(pid, e["record"]["run_id"]) for pid, e in self._processes.items()]
        for pid, rid in entries:
            self.cancel(pid, rid)
        with self._guard:
            if self._service_lock is not None:
                self._service_lock.close()
                self._service_lock = None


def _bounded_view(view, layer):
    if layer not in {"business", "metadata", "provenance", "all"}:
        raise LightGraphError("graph_layer_invalid")
    all_nodes = [n for n in view.get("nodes", []) if layer == "all" or n.get("layer") == layer]
    ids = {n["id"] for n in all_nodes}
    all_edges = [e for e in view.get("edges", []) if e["source"] in ids and e["target"] in ids]
    nodes = all_nodes[:MAX_GRAPH_NODES]
    # Derive display scope only from already-authorized visible supports.
    fact_entities = {f['id']: f['payload'].get('entity_id') for f in view.get('facts', [])}
    scopes = {}
    for support in view.get('supports', []):
        entity = fact_entities.get(support.get('fact_id'))
        if entity and support.get('scope_ref'):
            scopes.setdefault(entity, set()).add(support['scope_ref'])
    nodes = [{**node, 'scopes': sorted(scopes.get(node['id'], set()))} for node in nodes]
    ids = {n["id"] for n in nodes}
    edges = [e for e in all_edges if e["source"] in ids and e["target"] in ids][:MAX_GRAPH_EDGES]
    return {"nodes": nodes, "edges": edges, "layer": layer,
            "total_nodes": len(all_nodes), "total_edges": len(all_edges),
            "truncated": len(nodes) != len(all_nodes) or len(edges) != len(all_edges),
            "limits": {"nodes": MAX_GRAPH_NODES, "edges": MAX_GRAPH_EDGES}}


def read_graph(workspace, pid, layer="business", *, include_candidates=False,offset=0,limit=240,q='',kind=''):
    """Publication heads, not stale graph.json exports, determine current results."""
    from ..store import GraphStore
    if layer == "metadata":
        return read_metadata(workspace, pid)
    config = workspace.resolved_config(pid)
    path = Path(config.output_dir) / "graph.sqlite"
    if path.is_symlink():
        raise LightGraphError("workspace_path_invalid")
    if not path.exists():
        return {**_bounded_view({}, layer), "include_candidates": include_candidates}
    with GraphStore(path) as store:
        from ..store.paging import graph_page
        view = graph_page(store,set(config.scopes),config.isolation_domain,layer=layer,
                          include_candidates=include_candidates,offset=offset,limit=limit,q=q,kind=kind)
    result = _bounded_view(view, layer)
    result.update({key:view[key] for key in ('total_nodes','total_edges','matched_nodes','boundary_edges',
        'next_offset','offset','page_size','generation','query_strategy','kinds')})
    result["include_candidates"] = include_candidates
    return result


def read_graph_details(workspace, pid, *, entity_id=None, fact_id=None, include_candidates=False, offset=0):
    """Return bounded fact-level provenance only after support authorization."""
    from ..store import GraphStore
    if bool(entity_id) == bool(fact_id) or offset < 0:
        raise LightGraphError("graph_selection_invalid")
    config = workspace.resolved_config(pid)
    path = Path(config.output_dir) / "graph.sqlite"
    if path.is_symlink():
        raise LightGraphError("workspace_path_invalid")
    if not path.exists():
        raise LightGraphError("graph_item_not_found")
    with GraphStore(path) as store:
        view = store.view(set(config.scopes), domain=config.isolation_domain, include_candidates=include_candidates)
        if entity_id:
            if not any(n["id"] == entity_id for n in view["nodes"]):
                raise LightGraphError("graph_item_not_found")
            selected = [f for f in view["facts"] if f["payload"].get("entity_id") == entity_id
                        or entity_id in (f["payload"].get("source"), f["payload"].get("target"))]
        else:
            selected = [f for f in view["facts"] if f["id"] == fact_id]
            if not selected:
                raise LightGraphError("graph_item_not_found")
        # Page facts, rather than silently discarding all provenance beyond the canvas cap.
        facts = selected[offset:offset + 50]
        ids = {f["id"] for f in facts}
        supports = [v for v in view["supports"] if v["fact_id"] in ids]
        visible_rules = {(v["rule_id"], v["rule_revision"]) for v in supports}
        rules = []
        for rule in store.get_rules(include_stale=True):
            if (rule.rule_id, rule.revision) not in visible_rules:
                continue
            # Source column mappings and evidence references, never runtime samples or predicates.
            rules.append({"rule_id": rule.rule_id, "revision": rule.revision, "kind": rule.kind,
                "source": rule.source.model_dump(mode="json", by_alias=True),
                "identity_columns": list(rule.identity.key_columns) if rule.identity else [],
                "attribute_columns": rule.attributes.model_dump(mode="json") if rule.attributes else {},
                "semantic_basis": list(rule.evidence.semantic_basis)})
    return {"facts": facts, "supports": supports[:500], "rules": rules,
            "total_facts": len(selected), "total_supports": len(supports),
            "supports_truncated": len(supports) > 500,
            "next_offset": offset + len(facts) if offset + len(facts) < len(selected) else None,
            "include_candidates": include_candidates}


def identity_review(workspace, pid, *, offset=0, decision_offset=0):
    """Show original endpoints and authorized evidence before identity decisions."""
    from ..store import GraphStore
    from ..identity.reconciliation import list_candidates
    if offset < 0 or decision_offset < 0:
        raise LightGraphError("graph_selection_invalid")
    config = workspace.resolved_config(pid)
    path = Path(config.output_dir) / "graph.sqlite"
    if path.is_symlink():
        raise LightGraphError("workspace_path_invalid")
    if not path.exists():
        return {"revision": 0, "candidates": [], "decisions": [], "total_candidates": 0, "next_offset": None, "total_decisions": 0, "next_decision_offset": None}
    with GraphStore(path) as store:
        scopes = set(config.scopes)
        listing = list_candidates(store, scopes, config.isolation_domain)
        graph = store.view(scopes, domain=config.isolation_domain, include_candidates=True, apply_resolutions=False)
        nodes = {n["id"]: n for n in graph["nodes"]}
        facts = {f["id"]: f for f in graph["facts"]}
        candidates = []
        for item in listing["candidates"][offset:offset + 50]:
            evidence = set(item["evidence"])
            supports = [s for s in graph["supports"] if s["fact_id"] in evidence]
            candidates.append({**item, "has_active_decision": any(d["candidate_id"] == item["candidate_id"] and d["status"] == "active" for d in listing["decisions"]), "supports_truncated": len(supports) > 500, "nodes": [nodes[e] for e in item["members"] if e in nodes],
                "facts": [facts[f] for f in item["evidence"] if f in facts],
                "supports": supports[:500]})
        decisions = [{**d, "nodes": [nodes[e] for e in d["members"] if e in nodes]}
                     for d in list(reversed(listing["decisions"]))[decision_offset:decision_offset + 50]]
    total = len(listing["candidates"])
    return {"revision": listing["revision"], "candidates": candidates, "decisions": decisions,
            "total_candidates": total, "next_offset": offset + len(candidates) if offset + len(candidates) < total else None,
            "total_decisions": len(listing["decisions"]),
            "next_decision_offset": decision_offset + len(decisions) if decision_offset + len(decisions) < len(listing["decisions"]) else None}


def resolve_identity(workspace, pid, *, candidate_id=None, verdict=None, decision_id=None, expected_revision):
    """The caller serializes this mutation with runs and project configuration."""
    from ..store import GraphStore
    from ..identity.reconciliation import decide, revoke
    config = workspace.resolved_config(pid)
    path = Path(config.output_dir) / "graph.sqlite"
    if path.is_symlink():
        raise LightGraphError("workspace_path_invalid")
    if not path.exists():
        raise LightGraphError("identity_candidate_not_found")
    with GraphStore(path) as store:
        options = {"scopes": set(config.scopes), "domain": config.isolation_domain, "expected_revision": expected_revision}
        if decision_id:
            return revoke(store, decision_id, **options)
        return decide(store, candidate_id, verdict, **options)


def _metadata_context(config, project_path):
    paths = {p for source in config.sources for p in (source.path, source.capability_manifest) if p}
    files = _file_manifest(paths, project_path)
    return {"source_revision": digest([source.model_dump(mode="json") for source in config.sources]),
            "file_revisions": files,
            "source_revisions": {source.id: digest(source.model_dump(mode="json"),
                {str(Path(p).resolve().relative_to(project_path.resolve())): files[str(Path(p).resolve().relative_to(project_path.resolve()))]
                 for p in (source.path, source.capability_manifest) if p}) for source in config.sources}}


def write_metadata_context(config, project_path):
    """Bind cached declarations to source authorization and uploaded revisions."""
    _save(Path(config.output_dir) / "metadata-context.json", _metadata_context(config, project_path))


def read_metadata(workspace, pid, refresh=False, *, source_credentials=None, source_ids=None):
    """Cached GETs never connect; explicit refresh reads authorized declarations."""
    import yaml
    from ..catalog import CATALOG_VERSION, DeclaredCatalog, declared_catalog, semantic_catalog, write_catalog, read_semantic_catalog
    from ..contracts import CapabilityManifest
    from ..metadata import publish_metadata
    from ..runtime.config import open_sources
    from ..store import GraphStore
    config = workspace.resolved_config(pid)
    output = Path(config.output_dir)
    path = output / "metadata.yaml"
    adapters = []
    project = workspace.project_path(pid)
    context = _metadata_context(config, project)
    prior_context = None
    def unread(stale=False):
        return {**_bounded_view({}, "metadata"), "tables": [], "revision": None,
                "coverage": "not_read", "instance_queries": 0, "requires_refresh": True,
                "stale_engine_metadata": stale}
    if not refresh:
        if not path.exists():
            return unread()
        try:
            cached_context = _json_file(output / "metadata-context.json")
        except LightGraphError:
            return unread(stale=True)
        if cached_context != context:
            return unread(stale=True)
    else:
        try:
            prior_context = _json_file(output / "metadata-context.json")
        except LightGraphError:
            pass
        # Invalidate the read marker before any artifact can be replaced. A
        # failed graph render or connection rollback must never associate new
        # declarations with the previous source authorization context.
        _save(output / "metadata-context.json", {"refresh_pending": True})

    class CatalogAdapter:
        # No in-memory database or source rows are needed to render declarations.
        def __init__(self, source, tables):
            self.source, self.tables = source, tables
        def describe(self):
            return self.tables
        def capabilities(self):
            return CapabilityManifest(product=self.source.product, version=self.source.version,
                                      driver=self.source.driver, capabilities=self.source.capabilities)

    try:
        if not refresh:
            if path.is_symlink():
                raise LightGraphError("workspace_path_invalid")
            try:
                declared = DeclaredCatalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))["declared"])
            except (OSError, ValueError, KeyError, TypeError):
                raise LightGraphError("metadata_artifact_invalid") from None
        else:
            options = {"source_credentials": source_credentials} if source_credentials else {}
            selected = set(source_ids) if source_ids is not None else None
            source_config = config
            if selected is not None:
                if not selected or selected - {s.id for s in config.sources}:
                    raise LightGraphError("source_scope_missing")
                source_config = config.model_copy(deep=True)
                source_config.sources = [s for s in config.sources if s.id in selected]
            adapters = open_sources(source_config, metadata_only=True, **options)
            declared = declared_catalog(adapters)
            if selected is not None and path.exists():
                # Reuse only declarations tied to unchanged source authorization
                # and file revisions. Adding a DB never reconnects other DBs.
                try:
                    if path.is_symlink():
                        raise ValueError
                    prior = DeclaredCatalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))["declared"])
                    retained = {sid for sid, revision in context["source_revisions"].items()
                                if sid not in selected and (prior_context or {}).get("source_revisions", {}).get(sid) == revision}
                    tables = sorted(declared.tables + [t for t in prior.tables if t.source_id in retained],
                                    key=lambda t: (t.source_id, t.schema_name, t.name))
                    descriptions = sorted(declared.sources + [s for s in prior.sources if s.source_id in retained],
                                          key=lambda s: s.source_id)
                    declared = DeclaredCatalog(tables=tables, sources=descriptions,
                        revision=digest(CATALOG_VERSION, [t.model_dump(by_alias=True) for t in tables],
                                        [s.model_dump() for s in descriptions]))
                except (LightGraphError, OSError, ValueError, KeyError, TypeError):
                    pass
            from ..documents import load_documents
            missing = {s.id for s in config.sources} - {s.source_id for s in declared.sources}
            # Table-bound documents cannot be validated against an incomplete
            # catalog. Defer interpretations until a full refresh/run, keeping
            # the source declarations available for configuration preview.
            documents = [] if missing else load_documents(config.documents)
            write_catalog(output, declared, semantic_catalog(
                declared, config.goal, documents=documents, previous=read_semantic_catalog(output)))
        with GraphStore(":memory:") as store:
            for source in declared.sources:
                local = [t for t in declared.tables if t.source_id == source.source_id]
                publish_metadata(store, CatalogAdapter(source, local), domain=config.isolation_domain)
            view = store.view(set(config.scopes), domain=config.isolation_domain)
        # A graph publication from a previous run must not overwrite a freshly
        # refreshed declaration. Semantic edges are reusable only when all
        # catalog revisions still match; otherwise show the new base graph.
        graph_path = output / "graph.sqlite"
        stale = False
        if graph_path.exists():
            if graph_path.is_symlink():
                raise LightGraphError("workspace_path_invalid")
            with GraphStore(graph_path) as store:
                published_sources = {row[0].removeprefix("catalog:") for row in store.connection.execute(
                    "SELECT DISTINCT logical_rule_id FROM partition_head WHERE valid=1 AND logical_rule_id LIKE 'catalog:%'")}
                declared_sources = {source.source_id for source in declared.sources}
                stale = not declared_sources or published_sources != declared_sources
                for source in declared.sources:
                    local = [t.model_dump(mode="json", by_alias=True) for t in declared.tables
                             if t.source_id == source.source_id]
                    if (store.current_versions("catalog:" + source.source_id) or {}).get("schema") != digest(local):
                        stale = True
                        break
                if not stale:
                    published = store.view(set(config.scopes), domain=config.isolation_domain, include_candidates=True)
                    known = {n["id"] for n in view["nodes"]}
                    view["nodes"].extend(n for n in published["nodes"] if n["layer"] == "metadata" and n["id"] not in known)
                    known_edges = {e["id"] for e in view["edges"]}
                    view["edges"].extend(e for e in published["edges"] if e["id"] not in known_edges)
        if refresh:
            if _metadata_context(workspace.resolved_config(pid), project) != context:
                raise LightGraphError("input_dependency_changed")
            _save(output / "metadata-context.json", context)
        graph = _bounded_view(view, "metadata")
        tables = [{"id": t.source_id + "." + t.qualified_name,
                   **t.model_dump(mode="json", by_alias=True)} for t in declared.tables]
        missing_sources = sorted({s.id for s in config.sources} - {s.source_id for s in declared.sources})
        return {**graph, "tables": tables, "revision": declared.revision,
                "sources": [s.model_dump(mode="json") for s in declared.sources],
                "coverage": "declarations_partial" if missing_sources else "declarations_only", "instance_queries": 0,
                "requires_refresh": bool(missing_sources), "missing_sources": missing_sources,
                "document_context_deferred": bool(missing_sources),
                "stale_engine_metadata": stale}
    finally:
        for adapter in adapters:
            adapter.close()
