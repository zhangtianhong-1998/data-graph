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


def _resolved_payload(config, project_path):
    # Private resolved AgentConfig is not serialized by RunConfig. Flatten it
    # for this worker only; credentials travel through a pipe, never a file.
    data = config.model_dump(mode="json", by_alias=True)
    data["agent_config"] = None
    data["model"] = config.resolved_model.model_dump(mode="json")
    secret = config.resolved_model.api_key
    if secret is not None:
        data["model"]["api_key"] = secret.get_secret_value()
    return {"config": data, "agent_name": config.agent_name,
            "dependencies": _input_manifest(config, project_path)}


class RunManager:
    """One child writer per project; no PID restored from disk is ever signalled."""
    def __init__(self, workspace, *, cancel_timeout=5.0):
        self.workspace = workspace
        self.cancel_timeout = cancel_timeout
        self._processes = {}
        self._guard = threading.RLock()
        self._closed = False
        self._service_fd = self._claim_service_lock()
        try:
            self._recover()
        except BaseException:
            os.close(self._service_fd)
            self._service_fd = None
            raise

    def _claim_service_lock(self):
        # The worker inherits this *open file description*. A crashed service
        # cannot release its exclusivity while a surviving child still writes.
        if os.name != "posix":
            raise LightGraphError("workspace_service_lock_unsupported")
        import fcntl
        path = self.workspace.root / ".service.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            raise LightGraphError("workspace_service_lock_invalid") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise LightGraphError("workspace_service_active") from None
        return fd

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
                except LightGraphError:
                    continue

    def start(self, pid):
        with self.workspace.lock(pid), self._guard:
            if self._closed:
                raise LightGraphError("run_service_stopped")
            if self.active(pid):
                raise LightGraphError("project_run_active")
            project, runs = self._paths(pid)
            config = self.workspace.resolved_config(pid)
            if not config.goal.purpose.strip():
                raise LightGraphError("extraction_purpose_required")
            if not config.sources and not config.documents:
                raise LightGraphError("project_inputs_required")
            payload = _resolved_payload(config, project)
            rid = "run_" + uuid.uuid4().hex[:24]
            result_path = runs / (rid + ".result.json")
            record = {"run_id": rid, "project_id": pid, "status": "queued", "stage": "starting",
                      "created_at": _now(), "finished_at": None, "report": None,
                      "error_code": None, "cleanup_status": "pending",
                      "dependencies": payload["dependencies"],
                      "config_revision": digest(config.model_dump(mode="json", by_alias=True))}
            _save(runs / (rid + ".json"), record)
            env = os.environ.copy()
            source_root = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            try:
                proc = subprocess.Popen([sys.executable, "-m", "lightgraph.web.worker", str(result_path)],
                                        cwd=project, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        start_new_session=True, pass_fds=(self._service_fd,))
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
                           or digest(self.workspace.resolved_config(pid).model_dump(mode="json", by_alias=True)) != record["config_revision"])
            except LightGraphError:
                changed = True
            if changed and record["status"] in {"completed", "partial"}:
                record.update(status="partial", error_code="input_dependency_changed")
        record.update(stage="finished", finished_at=_now())
        _save(self._paths(pid, record["run_id"])[1], record)
        try:
            entry["result"].unlink(missing_ok=True)
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
            proc.terminate()
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
            if self._service_fd is not None:
                os.close(self._service_fd)
                self._service_fd = None


def _bounded_view(view, layer):
    if layer not in {"business", "metadata", "provenance", "all"}:
        raise LightGraphError("graph_layer_invalid")
    all_nodes = [n for n in view.get("nodes", []) if layer == "all" or n.get("layer") == layer]
    ids = {n["id"] for n in all_nodes}
    all_edges = [e for e in view.get("edges", []) if e["source"] in ids and e["target"] in ids]
    nodes = all_nodes[:MAX_GRAPH_NODES]
    ids = {n["id"] for n in nodes}
    edges = [e for e in all_edges if e["source"] in ids and e["target"] in ids][:MAX_GRAPH_EDGES]
    return {"nodes": nodes, "edges": edges, "layer": layer,
            "total_nodes": len(all_nodes), "total_edges": len(all_edges),
            "truncated": len(nodes) != len(all_nodes) or len(edges) != len(all_edges),
            "limits": {"nodes": MAX_GRAPH_NODES, "edges": MAX_GRAPH_EDGES}}


def read_graph(workspace, pid, layer="business"):
    """Publication heads, not stale graph.json exports, determine current results."""
    from ..store import GraphStore
    if layer == "metadata":
        return read_metadata(workspace, pid)
    config = workspace.resolved_config(pid)
    path = Path(config.output_dir) / "graph.sqlite"
    if path.is_symlink():
        raise LightGraphError("workspace_path_invalid")
    if not path.exists():
        return _bounded_view({}, layer)
    with GraphStore(path) as store:
        view = store.view(set(config.scopes), domain=config.isolation_domain, include_candidates=True)
    return _bounded_view(view, layer)


def _metadata_context(config, project_path):
    paths = {p for source in config.sources for p in (source.path, source.capability_manifest) if p}
    return {"source_revision": digest([source.model_dump(mode="json") for source in config.sources]),
            "file_revisions": _file_manifest(paths, project_path)}


def write_metadata_context(config, project_path):
    """Bind cached declarations to source authorization and uploaded revisions."""
    _save(Path(config.output_dir) / "metadata-context.json", _metadata_context(config, project_path))


def read_metadata(workspace, pid, refresh=False):
    """Cached GETs never connect; explicit refresh reads authorized declarations."""
    import yaml
    from ..catalog import DeclaredCatalog, declared_catalog, semantic_catalog, write_catalog, read_semantic_catalog
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
            adapters = open_sources(config, metadata_only=True)
            declared = declared_catalog(adapters)
            from ..documents import load_documents
            documents = load_documents(config.documents)
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
        return {**graph, "tables": tables, "revision": declared.revision,
                "coverage": "declarations_only", "instance_queries": 0,
                "requires_refresh": False, "stale_engine_metadata": stale}
    finally:
        for adapter in adapters:
            adapter.close()
