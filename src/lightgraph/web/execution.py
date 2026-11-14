"""Project-isolated background execution and bounded, published graph views.

Reports, progress events and worker results survive a process. When enabled,
the separate local run trace retains redacted model and tool inputs/outputs
until the user explicitly clears it; subprocess output is not a run transcript.
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
import time
from datetime import datetime, timezone
import uuid
from functools import lru_cache

from ..contracts import RunReport, digest
from ..errors import LightGraphError
from ..runtime.locking import FileLock
from ..runtime.retention import atomic_text
from ..runtime.progress import finish_progress, progress_path, read_progress
from .worker_diagnostics import WorkerDiagnostics, StderrCollector, read_worker_log

ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset({"completed", "completed_with_gaps", "partial", "failed", "cancelled", "interrupted"})
MAX_GRAPH_NODES = 1000
MAX_GRAPH_EDGES = 3000
_RUN_ID = re.compile(r"^run_[0-9a-f]{24}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_ISSUE_ENUMS = {
    "status": frozenset({"unresolved", "deferred", "partial", "resolved", "rejected", "diagnostic"}),
    "category": frozenset({"semantic_decision", "evidence_gap", "proposal_contract", "review_protocol", "internal_error"}),
    "next_action": frozenset({"continue_other_candidates", "gather_evidence", "revise_proposal", "retry_review", "fix_code"}),
}


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


@lru_cache(maxsize=1)
def _diagnostic_vocabulary():
    """Only schema-defined field names and fixed rejection codes may survive."""
    from ..discovery.models import ProposalBatch
    from ..discovery.hierarchy_plan import HierarchyPlan
    from ..discovery.proposal_session import DiscoveryFinish
    from ..discovery.schema_knowledge import ObjectModelDraft
    from ..discovery.repair import REPAIRABLE_PROPOSAL_CODES
    from ..discovery.validation_feedback import _property_names, _JSON_CONSTRAINTS, _SAFE_VALUE_CODES
    from ..semantic_schema import SemanticSchema
    names=set().union(*(_property_names(model.model_json_schema()) for model in
        (ProposalBatch,HierarchyPlan,DiscoveryFinish,ObjectModelDraft,SemanticSchema)))
    codes=REPAIRABLE_PROPOSAL_CODES | _SAFE_VALUE_CODES | {
        'invalid_tool_arguments','invalid_proposal_arguments','dangling_evidence_reference',
        'unauthorized_column_reference','unauthorized_source_reference','unauthorized_selector_reference',
        'unverified_entity_reference','dangling_order_evidence','hierarchy_shape_not_on_table',
        'discovery_session_finished','draft_replacement_changes_obligations','draft_replacement_required',
        'invalid_draft_replacement','invalid_rejection_resolution','unknown_rejection_id',
        'object_model_limit','object_model_mapping_conflict','proposal_draft_limit',
        'semantic_definition_conflict','task_kind_forbidden'}
    return names,codes,_JSON_CONSTRAINTS | {'schema','reference','contract','semantic_contract'}


def _safe_issue(item):
    safe={key:value for key,value in item.items() if key in {'code','table','rule_id'}
        or key in _ISSUE_ENUMS and value in _ISSUE_ENUMS[key]}
    if type(item.get('count')) is int and item['count']>=0:
        safe['count']=item['count']
    categories = {'construction_goal_gap': 'goal_gap', 'construction_decision': 'decision',
                  'construction_suggestion': 'suggestion'}
    if item.get('code') in categories and item.get('category') == categories[item['code']]:
        # These are explicit draft statements, never exception bodies. The
        # source/transport error paths retain their existing closed diagnostics.
        safe['category'] = item['category']
        if isinstance(item.get('message'), str):
            safe['message'] = item['message'][:8000]
    if item.get('code') in {*categories, 'construction_unpublished_draft', 'construction_draft_validation_failed'} and isinstance(item.get('draft_id'), str):
        safe['draft_id'] = item['draft_id'][:80]
    if item.get('code')=='model_protocol_diagnostic':
        from ..discovery.llm import MODEL_PROTOCOL_STAGES, MODEL_PROTOCOL_CODES, MODEL_PROTOCOL_FIELDS
        from ..discovery.validation_feedback import _path
        stage=item.get('stage');constraint=item.get('constraint')
        if isinstance(stage,str) and stage in MODEL_PROTOCOL_STAGES:safe['stage']=stage
        if isinstance(constraint,str) and constraint in MODEL_PROTOCOL_CODES:safe['constraint']=constraint
        raw=item.get('field_path')
        if isinstance(raw,str) and len(raw)<=4096:
            try:path=json.loads(raw)
            except (TypeError,ValueError):path=None
            if isinstance(path,list):
                safe['field_path']=json.dumps(_path(path,MODEL_PROTOCOL_FIELDS-{'<unknown_field>'}),ensure_ascii=False,separators=(',',':'))
    if item.get('code') in {'unexplained_columns','coverage_shared_label_unresolved'}:
        # The coverage stage supplies authorized metadata names, never row values.
        # Keep this field restricted to its two producers and bounded JSON shape.
        raw=item.get('column_names')
        if isinstance(raw,str) and len(raw)<=32768:
            try:names=json.loads(raw)
            except (TypeError,ValueError):names=None
            if (isinstance(names,list) and len(names)<=32 and all(
                    isinstance(name,str) and 0<len(name)<=256 and
                    not any(ord(char)<32 or ord(char)==127 for char in name) for name in names)):
                safe['column_names']=json.dumps(names,ensure_ascii=False,separators=(',',':'))
    if item.get('code') in {'discovery_draft_rejected','discovery_draft_field_diagnostic'}:
        names,codes,constraints=_diagnostic_vocabulary()
        if item.get('reason_code') in codes:
            safe['reason_code']=item['reason_code']
        if item.get('constraint') in constraints:
            safe['constraint']=item['constraint']
        raw=item.get('field_path')
        if isinstance(raw,str) and len(raw)<=4096:
            try:path=json.loads(raw)
            except (TypeError,ValueError):path=None
            if isinstance(path,list):
                from ..discovery.validation_feedback import _path
                safe['field_path']=json.dumps(_path(path,names),ensure_ascii=False,separators=(',',':'))
    return safe


def _safe_report(value):
    """Accept the existing report contract, never arbitrary process output."""
    try:
        report = RunReport.model_validate(value)
    except (ValueError, TypeError):
        raise LightGraphError("run_report_invalid") from None
    # Runtime reports use closed numeric counters and error codes. Keep only
    # supported issue fields rather than allowing arbitrary message/evidence keys.
    report.issues = [_safe_issue(item) for item in report.issues]
    return report.model_dump(mode="json")


def _input_manifest(config, project_path):
    """Hash uploaded dependencies, not database rows or query results."""
    paths = set(config.documents + config.rules)
    if config.agent_config:
        paths.add(config.agent_config)
    for ref in getattr(config, 'inherited_graphs', []) or []:
        if ref.snapshot_path:
            paths.add(ref.snapshot_path)
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
                        finish_progress(progress_path(path), record.get("run_id"), "interrupted")
                        _save(path, record)
                    # Exclusive service ownership proves no surviving worker
                    # still needs a cancellation request from a previous run.
                    _cancel_path(path.with_name(path.stem + ".result.json")).unlink(missing_ok=True)
                except (LightGraphError, OSError):
                    continue

    def start(self, pid, *, source_credentials=None, model_api_key=None, run_mode="continue"):
        with self.workspace.lock(pid), self._guard:
            if self._closed:
                raise LightGraphError("run_service_stopped")
            if self.active(pid):
                raise LightGraphError("project_run_active")
            if run_mode not in {"continue", "fresh"}:
                raise LightGraphError("invalid_run_mode")
            project, runs = self._paths(pid)
            config = self.workspace.resolved_config(pid).model_copy(deep=True)
            from ..construction.intent import effective_goal_markdown
            if not effective_goal_markdown(config).strip():
                raise LightGraphError("extraction_purpose_required")
            if not config.sources and not config.documents and not config.inherited_graphs:
                raise LightGraphError("project_inputs_required")
            for source in config.sources:
                if source.credential_mode == "session" and not (source_credentials or {}).get(source.id):
                    raise LightGraphError("source_credential_missing")
            rid = "run_" + uuid.uuid4().hex[:24]
            previous_generation = None
            if run_mode == "fresh":
                # Upgrades may have only the old generation's catalog. Preserve
                # that exact authorized preview before changing the output path.
                _promote_metadata_preview(config, project)
                previous_generation = self.workspace.output_generation(pid)
                config.output_dir = str(self.workspace.generation_path(pid, rid))
            if config.inherited_graphs:
                from ..construction.inheritance import authorize_frozen_inheritance, freeze_inherited_graphs
                if run_mode == "fresh":
                    self.workspace.select_output_generation(pid, rid, create=True)
                try:
                    construction = Path(config.output_dir) / "construction"
                    if run_mode == "continue" and (construction / "inherited" / "summary.yaml").is_file():
                        authorize_frozen_inheritance(config, self.workspace)
                    else:
                        freeze_inherited_graphs(config, construction, workspace=self.workspace)
                except Exception:
                    if run_mode == "fresh":
                        self.workspace.select_output_generation(pid, previous_generation)
                    raise
            if config.workflow and config.workflow.enabled and config.workflow.units:
                try:
                    return self._start_workflow(
                        pid, config, project, runs, rid,
                        source_credentials=source_credentials,
                        model_api_key=model_api_key,
                        run_mode=run_mode,
                        previous_generation=previous_generation,
                        generation_already_selected=(run_mode == "fresh" and bool(config.inherited_graphs)),
                    )
                except Exception:
                    if run_mode == "fresh" and self.workspace.output_generation(pid) == rid:
                        self.workspace.select_output_generation(pid, previous_generation)
                    raise
            try:
                payload = _resolved_payload(config, project, source_credentials=source_credentials,
                                            model_api_key=model_api_key)
            except Exception:
                if run_mode == "fresh" and config.inherited_graphs:
                    self.workspace.select_output_generation(pid, previous_generation)
                raise
            result_path = runs / (rid + ".result.json")
            record = {"run_id": rid, "project_id": pid, "status": "queued", "stage": "starting",
                      "created_at": _now(), "finished_at": None, "report": None,
                      "error_code": None, "cleanup_status": "pending",
                      "dependencies": payload["dependencies"],
                      "run_mode": run_mode,
                      "output_path": Path(config.output_dir).relative_to(project).as_posix(),
                      "config_revision": self.workspace.config_revision(pid)}
            _save(runs / (rid + ".json"), record)
            env = os.environ.copy()
            source_root = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            diagnostics = WorkerDiagnostics(result_path)
            diagnostics.emit('worker_starting', stage='launch')
            collector = None
            generation_selected = bool(run_mode == "fresh" and config.inherited_graphs)
            try:
                if run_mode == "fresh" and not generation_selected:
                    self.workspace.select_output_generation(pid, rid, create=True)
                    generation_selected = True
                # The inherited lock stays held if this service crashes while
                # its worker is still publishing. Handle inheritance differs
                # between POSIX and Windows and is owned by FileLock.
                with self._service_lock.child_process_options() as child_options:
                    proc = subprocess.Popen([sys.executable, "-m", "lightgraph.web.worker", str(result_path)],
                                            cwd=project, env=env, stdin=subprocess.PIPE,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                            **child_options)
                self._processes[pid] = {"process": proc, "record": record, "result": result_path,
                                        "cancel_requested": False, "forced": False, "config": config,
                                        "diagnostics": diagnostics, "stderr_collector": None}
                collector = StderrCollector(proc.stderr)
                self._processes[pid]['stderr_collector'] = collector
                proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                proc.stdin.close()
                record.update(status="running", stage="extracting")
                _save(runs / (rid + ".json"), record)
            except Exception as exc:
                if pid in self._processes:
                    proc = self._processes.pop(pid)["process"]
                    proc.kill(); proc.wait()
                if collector is not None:
                    diagnostics.emit('worker_stderr_summary', stage='launch', **collector.finish())
                diagnostics.emit('worker_start_failed', stage='launch', error=exc, error_code='worker_start_failed')
                if generation_selected:
                    self.workspace.select_output_generation(pid, previous_generation)
                record.update(status="failed", stage="finished", finished_at=_now(),
                              cleanup_status="unknown", error_code="worker_start_failed")
                _save(runs / (rid + ".json"), record)
                return dict(record)
            threading.Thread(target=self._watch, args=(pid, proc), daemon=True).start()
            return dict(record)

    def _start_workflow(self, pid, config, project, runs, rid, *, source_credentials=None,
                        model_api_key=None, run_mode="continue", previous_generation=None,
                        generation_already_selected=False, selected_unit_ids=None):
        from concurrent.futures import ThreadPoolExecutor
        from ..construction.workspace import safe_load_yaml
        from ..construction.workflow import (
            build_unit_config, copy_frozen_inheritance, load_workflow_state,
            merge_project_workflow, published_unit_attempts, save_workflow_state, unit_output_path,
        )
        if run_mode == "fresh" and not generation_already_selected:
            self.workspace.select_output_generation(pid, rid, create=True)
        wf = config.workflow
        deadline = time.monotonic() + config.max_run_seconds
        target_units = [u for u in wf.units if selected_unit_ids is None or u.unit_id in selected_unit_ids]
        if not target_units:
            raise LightGraphError("workflow_unit_not_found")
        existing_state = load_workflow_state(config.output_dir) if run_mode == "continue" else None
        payload_deps = _input_manifest(config, project)
        from ..construction.access import access_fingerprint
        workflow_input = digest(access_fingerprint(config), config.workflow.model_dump(mode="json"),
                                config.goal_markdown, config.goal.model_dump(mode="json"), payload_deps)
        if selected_unit_ids is not None and (existing_state or {}).get("workflow_input") != workflow_input:
            raise LightGraphError("workflow_input_changed")
        previous_attempts = (published_unit_attempts(config.output_dir)
                             if (existing_state or {}).get("workflow_input") == workflow_input else {})
        units_state = dict((existing_state or {}).get("units") or {}) if selected_unit_ids is not None else {}
        for unit in wf.units:
            if unit.unit_id not in units_state or unit in target_units:
                units_state[unit.unit_id] = {
                    "unit_id": unit.unit_id,
                    "status": "queued" if unit in target_units else units_state.get(unit.unit_id, {}).get("status", "pending"),
                    "run_id": None,
                    "error_code": None,
                    "nodes": 0,
                    "edges": 0,
                    "updated_at": _now(),
                }
        wf_state = {
            "parent_run_id": rid,
            "workflow_input": workflow_input,
            "status": "running",
            "created_at": _now(),
            "finished_at": None,
            "units": units_state,
            "merge": {"status": "pending"},
        }
        save_workflow_state(config.output_dir, wf_state)
        record = {
            "run_id": rid, "project_id": pid, "status": "running", "stage": "extracting",
            "created_at": _now(), "finished_at": None, "report": None,
            "error_code": None, "cleanup_status": "pending",
            "dependencies": payload_deps,
            "run_mode": run_mode,
            "workflow": True,
            "workflow_units": [u.unit_id for u in target_units],
            "output_path": Path(config.output_dir).relative_to(project).as_posix(),
            "config_revision": self.workspace.config_revision(pid),
        }
        _save(runs / (rid + ".json"), record)

        cancel_event = threading.Event()
        done_event = threading.Event()
        entry = {
            "kind": "workflow", "process": None, "record": record,
            "result": runs / (rid + ".result.json"),
            "cancel_requested": False, "forced": False, "config": config,
            "cancel_event": cancel_event, "done_event": done_event,
            "unit_procs": {}, "state_lock": threading.Lock(),
        }
        self._processes[pid] = entry

        env = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

        def run_unit(unit):
            unit_out = unit_output_path(config.output_dir, unit.unit_id, rid)
            unit_out.mkdir(parents=True, exist_ok=True)
            copy_frozen_inheritance(Path(config.output_dir) / "construction", unit_out / "construction")
            unit_config = build_unit_config(config, unit, unit_output_dir=unit_out)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LightGraphError("run_time_budget_exhausted")
            unit_config.max_run_seconds = min(unit_config.max_run_seconds, remaining)
            unit_rid = "run_" + uuid.uuid4().hex[:24]
            unit_result = runs / (unit_rid + ".result.json")
            unit_payload = _resolved_payload(
                unit_config, project,
                source_credentials={k: v for k, v in (source_credentials or {}).items()
                                    if any(s.id == k for s in unit_config.sources)},
                model_api_key=model_api_key,
            )
            with entry["state_lock"]:
                if cancel_event.is_set():
                    units_state[unit.unit_id].update(status="cancelled", error_code="run_cancelled", updated_at=_now())
                    save_workflow_state(config.output_dir, wf_state)
                    return {"unit_id": unit.unit_id, "status": "cancelled", "cleanup_status": "completed"}
                units_state[unit.unit_id].update(status="running", run_id=unit_rid, updated_at=_now())
                save_workflow_state(config.output_dir, wf_state)
                with self._service_lock.child_process_options() as child_options:
                    proc = subprocess.Popen(
                        [sys.executable, "-m", "lightgraph.web.worker", str(unit_result)],
                        cwd=project, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        **child_options,
                    )
                entry["unit_procs"][unit.unit_id] = (proc, unit_result)
            collector = StderrCollector(proc.stderr)
            try:
                proc.stdin.write(json.dumps(unit_payload, ensure_ascii=False).encode("utf-8"))
                proc.stdin.close()
                try:
                    proc.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    # Give the worker its normal cancellation/transaction cleanup window.
                    atomic_text(_cancel_path(unit_result), "cancel\n")
                    try:
                        proc.wait(timeout=self.cancel_timeout)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    raise LightGraphError("run_time_budget_exhausted") from None
            except BaseException:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                raise
            finally:
                collector.finish()
                with entry["state_lock"]:
                    entry["unit_procs"].pop(unit.unit_id, None)
            try:
                res = _json_file(unit_result)
            except LightGraphError:
                res = {"status": "failed", "cleanup_status": "unknown", "error_code": "worker_result_missing"}
            if cancel_event.is_set():
                res["status"] = "cancelled"
                res["error_code"] = "run_cancelled"
            rep = res.get("report") or {}
            counts = rep.get("counts") or {}
            with entry["state_lock"]:
                units_state[unit.unit_id].update(
                    status=res.get("status", "failed"),
                    error_code=res.get("error_code"),
                    nodes=counts.get("delta_nodes", counts.get("business_nodes", 0)),
                    edges=counts.get("delta_edges", counts.get("business_edges", 0)),
                    updated_at=_now(),
                )
                save_workflow_state(config.output_dir, wf_state)
            try:
                _cancel_path(unit_result).unlink(missing_ok=True)
            except OSError:
                pass
            return {"unit_id": unit.unit_id, **res}

        def coordinator():
            unit_results = []
            try:
                with ThreadPoolExecutor(max_workers=wf.max_parallel_workers) as pool:
                    futures = {pool.submit(run_unit, unit): unit.unit_id for unit in target_units}
                    for future, uid in futures.items():
                        try:
                            unit_results.append(future.result())
                        except Exception as exc:
                            code = _safe_code(getattr(exc, "code", None), "workflow_unit_failed")
                            with entry["state_lock"]:
                                units_state[uid].update(status="failed", error_code=code, updated_at=_now())
                                save_workflow_state(config.output_dir, wf_state)
                            unit_results.append({"unit_id": uid, "status": "failed", "error_code": code})
                if cancel_event.is_set():
                    final_status = "cancelled"
                    error_code = "run_cancelled"
                    merge_summary = None
                else:
                    attempts = dict(previous_attempts)
                    for result in unit_results:
                        uid = result["unit_id"]
                        unit_manifest = unit_output_path(config.output_dir, uid, rid) / "construction" / "manifest.yaml"
                        # Keep the last published contribution if its replacement stopped early.
                        usable = (result["status"] in {"completed", "completed_with_gaps"}
                                  or (result["status"] == "partial" and uid not in previous_attempts))
                        if usable and unit_manifest.is_file():
                            if (safe_load_yaml(unit_manifest.read_text("utf-8")) or {}).get("current_graph"):
                                attempts[uid] = rid
                    check_dependencies(payload_deps, project)
                    if config.inherited_graphs:
                        from ..construction.inheritance import authorize_frozen_inheritance
                        authorize_frozen_inheritance(config, self.workspace)
                    if self.workspace.config_revision(pid) != record["config_revision"]:
                        raise LightGraphError("input_dependency_changed")
                    merge_summary = merge_project_workflow(config.output_dir, config, workspace=self.workspace,
                                                          unit_attempts=attempts)
                    wf_state["merge"] = {"status": "completed", **merge_summary}
                    statuses = {u["status"] for u in units_state.values()}
                    if statuses <= {"completed"}:
                        gaps = (merge_summary["skipped_units"]
                                or merge_summary["conflict_isolation"].get("coverage") == "incomplete")
                        final_status, error_code = ("completed_with_gaps" if gaps else "completed"), None
                    elif "completed" in statuses or "completed_with_gaps" in statuses:
                        final_status = "completed_with_gaps" if statuses <= {"completed", "completed_with_gaps"} else "partial"
                        error_code = None if final_status == "completed_with_gaps" else "workflow_unit_partial"
                    elif "partial" in statuses:
                        final_status, error_code = "partial", "workflow_unit_partial"
                    else:
                        final_status, error_code = "failed", "workflow_units_failed"
                wf_state.update(status=final_status, finished_at=_now())
                save_workflow_state(config.output_dir, wf_state)
                total_counts = {
                    "business_nodes": (merge_summary or {}).get("nodes", 0),
                    "business_edges": (merge_summary or {}).get("edges", 0),
                    "workflow_units_total": len(wf.units),
                    "workflow_units_succeeded": sum(u["status"] in {"completed", "completed_with_gaps"} for u in units_state.values()),
                }
                report_obj = RunReport(
                    run_id=rid,
                    status={"completed": "COMPLETED", "completed_with_gaps": "COMPLETED_WITH_GAPS",
                            "partial": "PARTIAL", "failed": "FAILED", "cancelled": "PARTIAL"}[final_status],
                    counts=total_counts,
                    validation={"workflow_units": len(target_units),
                                "merge_status": wf_state["merge"].get("status")},
                )
                with self.workspace.lock(pid), self._guard:
                    record.update(
                        status=final_status,
                        stage="finished",
                        finished_at=_now(),
                        cleanup_status="completed" if all(r.get("cleanup_status") == "completed" for r in unit_results) else "unknown",
                        error_code=error_code,
                        report=_safe_report(report_obj),
                    )
                    _save(runs / (rid + ".json"), record)
                    self._processes.pop(pid, None)
            except Exception as exc:
                code = _safe_code(getattr(exc, "code", None), "workflow_execution_failed")
                wf_state.update(status="failed", finished_at=_now(), merge={"status": "failed", "error_code": code})
                save_workflow_state(config.output_dir, wf_state)
                with self.workspace.lock(pid), self._guard:
                    record.update(status="failed", stage="finished", finished_at=_now(),
                                  cleanup_status="unknown", error_code=code)
                    _save(runs / (rid + ".json"), record)
                    self._processes.pop(pid, None)
            finally:
                done_event.set()

        threading.Thread(target=coordinator, daemon=True).start()
        return dict(record)

    def retry_unit(self, pid, unit_id, *, source_credentials=None, model_api_key=None):
        """Re-run one workflow unit and deterministically replace only its contribution."""
        with self.workspace.lock(pid), self._guard:
            if self._closed:
                raise LightGraphError("run_service_stopped")
            if self.active(pid):
                raise LightGraphError("project_run_active")
            project, runs = self._paths(pid)
            config = self.workspace.resolved_config(pid).model_copy(deep=True)
            if not config.workflow or not config.workflow.enabled:
                raise LightGraphError("workflow_not_enabled")
            if not any(u.unit_id == unit_id for u in config.workflow.units):
                raise LightGraphError("workflow_unit_not_found")
            if config.inherited_graphs:
                from ..construction.inheritance import authorize_frozen_inheritance
                authorize_frozen_inheritance(config, self.workspace)
            rid = "run_" + uuid.uuid4().hex[:24]
            return self._start_workflow(
                pid, config, project, runs, rid,
                source_credentials=source_credentials,
                model_api_key=model_api_key,
                run_mode="continue",
                selected_unit_ids=[unit_id],
            )

    def workflow_status(self, pid):
        with self.workspace.lock(pid):
            config = self.workspace.resolved_config(pid)
            from ..construction.workflow import load_workflow_state
            state = load_workflow_state(config.output_dir)
            return {
                "enabled": bool(config.workflow and config.workflow.enabled),
                "units": [u.unit_id for u in config.workflow.units] if config.workflow else [],
                "state": state,
            }

    def _watch(self, pid, proc):
        proc.wait()
        with self.workspace.lock(pid), self._guard:
            entry = self._processes.get(pid)
            if entry and entry["process"] is proc:
                self._finish(pid, entry)

    def _finish(self, pid, entry):
        record = entry["record"]
        diagnostics = entry.get('diagnostics') or WorkerDiagnostics(entry['result'])
        if entry.get('stderr_collector') is not None:
            diagnostics.emit('worker_stderr_summary', stage='finished', **entry['stderr_collector'].finish())
        if entry.get('process') is not None:
            diagnostics.emit('worker_process_exited', stage='finished',
                exit_code=getattr(entry['process'], 'returncode', None),
                cancel_requested=entry['cancel_requested'], forced=entry['forced'])
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
            diagnostics.emit('worker_error', stage='result', error_code='worker_result_missing')
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
            if changed and record["status"] in {"completed", "completed_with_gaps", "partial"}:
                record.update(status="partial", error_code="input_dependency_changed")
        record.update(stage="finished", finished_at=_now())
        record_path = self._paths(pid, record["run_id"])[1]
        finish_progress(progress_path(record_path), record["run_id"], record["status"])
        _save(record_path, record)
        try:
            # Preserve worker results and trace artifacts until explicit user
            # cleanup. Only the obsolete cancellation control signal is removed.
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
            return self._with_progress(_json_file(path), path)

    @staticmethod
    def _with_progress(record, path):
        # A worker owns only its sibling progress file. Optional telemetry cannot
        # break old records or override the authoritative terminal run status.
        if record.get("run_id") != path.stem:
            return record
        progress = read_progress(progress_path(path), record.get("run_id"))
        if progress is not None:
            record = dict(record, progress=progress)
            if record.get("status") in ACTIVE_STATUSES:
                record["stage"] = progress["stage"]
        diagnostics = read_worker_log(path)
        record = dict(record, worker_diagnostics={key: diagnostics[key] for key in ('status', 'incomplete')},
                      worker_diagnostic_events=len(diagnostics['events']))
        return record

    def list(self, pid):
        with self.workspace.lock(pid):
            _, directory = self._paths(pid)
            records = []
            for path in directory.glob("run_*.json"):
                if _RUN_ID.fullmatch(path.stem):
                    records.append(self._with_progress(_json_file(path), path))
            return sorted(records, key=lambda r: r["created_at"], reverse=True)

    def cancel(self, pid, rid):
        with self.workspace.lock(pid), self._guard:
            record = self.get(pid, rid)
            entry = self._processes.get(pid)
            if not entry or entry["record"]["run_id"] != rid:
                return record
            if entry.get("kind") == "workflow":
                entry["cancel_requested"] = True
                entry["cancel_event"].set()
                with entry["state_lock"]:
                    active_procs = list(entry["unit_procs"].items())
                for _uid, (_proc, unit_result) in active_procs:
                    try:
                        atomic_text(_cancel_path(unit_result), "cancel\n")
                    except OSError:
                        _proc.kill()
            else:
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
        if entry.get("kind") == "workflow":
            if not entry["done_event"].wait(timeout=self.cancel_timeout + 1):
                with entry["state_lock"]:
                    for _uid, (uproc, _ures) in list(entry["unit_procs"].items()):
                        try:
                            uproc.kill()
                        except Exception:
                            pass
                entry["done_event"].wait(timeout=5)
            return self.get(pid, rid)
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


def _bounded_view(view, layer, *, bounded=True):
    if layer not in {"business", "metadata", "provenance", "all"}:
        raise LightGraphError("graph_layer_invalid")
    all_nodes = [n for n in view.get("nodes", []) if layer == "all" or n.get("layer") == layer]
    ids = {n["id"] for n in all_nodes}
    all_edges = [e for e in view.get("edges", []) if e["source"] in ids and e["target"] in ids]
    nodes = all_nodes[:MAX_GRAPH_NODES] if bounded else all_nodes
    # Derive display scope only from already-authorized visible supports.
    fact_entities = {f['id']: f['payload'].get('entity_id') for f in view.get('facts', [])}
    scopes = {}
    for support in view.get('supports', []):
        entity = fact_entities.get(support.get('fact_id'))
        if entity and support.get('scope_ref'):
            scopes.setdefault(entity, set()).add(support['scope_ref'])
    nodes = [{**node, 'scopes': sorted(scopes.get(node['id'], set()))} for node in nodes]
    ids = {n["id"] for n in nodes}
    edges = [e for e in all_edges if e["source"] in ids and e["target"] in ids]
    if bounded:
        edges = edges[:MAX_GRAPH_EDGES]
    return {"nodes": nodes, "edges": edges, "layer": layer,
            "total_nodes": len(all_nodes), "total_edges": len(all_edges),
            "truncated": len(nodes) != len(all_nodes) or len(edges) != len(all_edges),
            "limits": {"nodes": MAX_GRAPH_NODES, "edges": MAX_GRAPH_EDGES}, "complete_batches": True}


def read_graph_consistently(workspace, pid, reader, **options):
    """Read a frozen view without blocking configuration behind graph parsing.

    A concurrent configuration change or project replacement invalidates the
    result before it reaches the response, including any now-hidden supports.
    """
    with workspace.lock(pid):
        project = workspace.get(pid)
        revision = project['config_revision']
        config = workspace.resolved_config(pid).model_copy(deep=True)
    result = reader(workspace, pid, config=config, **options)
    with workspace.lock(pid):
        current = workspace.get(pid)
        if (current['created_at'] != project['created_at']
                or current['config_revision'] != revision
                or workspace.resolved_config(pid) != config):
            raise LightGraphError('graph_read_conflict')
    return result


def read_graph(workspace, pid, layer="business", *, include_candidates=False,offset=0,limit=240,q='',kind='',focus='',hops=1,traversal='all',config=None):
    """Publication heads, not stale graph.json exports, determine current results."""
    from ..store import GraphStore
    if layer == "metadata":
        return read_metadata(workspace, pid, focus=focus, hops=hops, traversal=traversal, config=config)
    config = config if config is not None else workspace.resolved_config(pid)
    from . import construction_graph
    if construction_graph.available(config):
        return construction_graph.graph_page(config, layer=layer, offset=offset, limit=limit,
            q=q, kind=kind, focus=focus, hops=hops, traversal=traversal, include_candidates=include_candidates)
    path = Path(config.output_dir) / "graph.sqlite"
    if path.is_symlink():
        raise LightGraphError("workspace_path_invalid")
    if not path.exists():
        return {**_bounded_view({}, layer), "include_candidates": include_candidates}
    with GraphStore(path) as store:
        from ..store.paging import graph_page
        view = graph_page(store,set(config.scopes),config.isolation_domain,layer=layer,
                          include_candidates=include_candidates,offset=offset,limit=limit,q=q,kind=kind,focus=focus,hops=hops,traversal=traversal)
        from .graph_display import display_schema
        vocabulary = display_schema(store, view)
    result = _bounded_view(view, layer)
    result.update({key:view[key] for key in ('total_nodes','total_edges','matched_nodes','boundary_edges',
        'next_offset','offset','page_size','generation','query_strategy','kinds','kind_counts','focus_node_id','focus_neighbor_count',
        'focus_hops','focus_traversal','neighborhood_truncated')})
    result['truncated'] = result['truncated'] or view['truncated']
    result["include_candidates"] = include_candidates
    result["display_schema"] = vocabulary
    return result


def read_graph_details(workspace, pid, *, entity_id=None, fact_id=None, include_candidates=False, offset=0, config=None):
    """Return bounded fact-level provenance only after support authorization."""
    from ..store import GraphStore
    if bool(entity_id) == bool(fact_id) or offset < 0:
        raise LightGraphError("graph_selection_invalid")
    config = config if config is not None else workspace.resolved_config(pid)
    from . import construction_graph
    if construction_graph.available(config):
        return construction_graph.graph_details(config, entity_id=entity_id, fact_id=fact_id,
            offset=offset, include_candidates=include_candidates)
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
        from .graph_display import display_schema, endpoint_captions
        captions = endpoint_captions(view['nodes'], facts)
        vocabulary = display_schema(store, {'nodes': captions, 'facts': facts, 'supports': supports})
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
                "namespace_columns": list(rule.scope.namespaces),
                "hierarchy_columns": [level.model_dump(mode='json') for level in rule.hierarchy.levels] if rule.hierarchy else [],
                "reference_columns": {key:getattr(rule.relation,key) for key in ('source_columns','target_columns','source_level','target_level')} if rule.relation else {},
                "concept_columns": [{key:getattr(component,key) for key in ('role','column','key_column','namespace_columns')} for component in rule.concept_mapping.components] if rule.concept_mapping else [],
                "semantic_basis": list(rule.evidence.semantic_basis)})
    return {"facts": facts, "supports": supports[:500], "rules": rules,
            "nodes": captions, "display_schema": vocabulary,
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
    declarations = {source.id: {**source.model_dump(mode="json"), **source.file_scope_revision}
                    for source in config.sources}
    return {"source_revision": digest(list(declarations.values())),
            "file_revisions": files,
            "source_revisions": {source.id: digest(declarations[source.id],
                {str(Path(p).resolve().relative_to(project_path.resolve())): files[str(Path(p).resolve().relative_to(project_path.resolve()))]
                 for p in (source.path, source.capability_manifest) if p}) for source in config.sources}}


def write_metadata_context(config, project_path):
    """Bind cached declarations to source authorization and uploaded revisions."""
    _save(Path(config.output_dir) / "metadata-context.json", _metadata_context(config, project_path))


def _metadata_preview_path(project):
    path = project / 'metadata' / 'catalog-preview.yaml'
    if any(entry.is_symlink() for entry in (path, path.parent)):
        raise LightGraphError('workspace_path_invalid')
    return path


def _save_metadata_preview(project, context, declared):
    """Keep successful declarations apart from run-specific output directories."""
    import yaml
    path = _metadata_preview_path(project)
    path.parent.mkdir(exist_ok=True)
    atomic_text(path, yaml.safe_dump({'context': context,
        'declared': declared.model_dump(mode='json', by_alias=True)}, allow_unicode=True, sort_keys=False))


def _read_metadata_preview_entry(project):
    import yaml
    from ..catalog import DeclaredCatalog
    path = _metadata_preview_path(project)
    if not path.exists():
        return None
    try:
        value = yaml.safe_load(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or not isinstance(value.get('context'), dict):
            return None
        return DeclaredCatalog.model_validate(value['declared']), value['context']
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
        return None


def _read_metadata_preview(project, context):
    entry = _read_metadata_preview_entry(project)
    return entry[0] if entry is not None and entry[1] == context else None


def _promote_metadata_preview(config, project):
    """Migrate a valid legacy catalog without reading a source or old run logs."""
    import yaml
    from ..catalog import DeclaredCatalog
    output = Path(config.output_dir)
    path = output / 'metadata.yaml'
    if path.is_symlink():
        raise LightGraphError('workspace_path_invalid')
    if not path.exists():
        return
    context = _metadata_context(config, project)
    try:
        if _json_file(output / 'metadata-context.json') != context:
            return
        declared = DeclaredCatalog.model_validate(yaml.safe_load(path.read_text(encoding='utf-8'))['declared'])
    except (LightGraphError, OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
        return
    try:
        _save_metadata_preview(project, context, declared)
    except OSError:
        raise LightGraphError('metadata_preview_persistence_failed') from None


def _metadata_neighborhood(view, focus, hops, traversal='all'):
    """Select the neighborhood before the response cap, using authorized metadata."""
    nodes={n['id']:n for n in view['nodes'] if n['layer']=='metadata'}
    from ..graph_neighborhood import neighborhood_paths
    ordered, _ = neighborhood_paths(nodes, view['edges'], focus, hops, traversal)
    found = set(ordered)
    subset={**view,'nodes':[nodes[nid] for nid in ordered],
        'edges':[e for e in view['edges'] if e['source'] in found and e['target'] in found]}
    graph=_bounded_view(subset,'metadata')
    graph.update(focus_node_id=focus if focus in nodes else None,focus_hops=hops,focus_traversal=traversal,
        focus_neighbor_count=len(found-{focus}),matched_nodes=len(found-{focus}),
        next_offset=None,neighborhood_truncated=len(found)>MAX_GRAPH_NODES,
        total_nodes=len(nodes),total_edges=sum(e['source'] in nodes and e['target'] in nodes for e in view['edges']))
    return graph


def read_metadata(workspace, pid, refresh=False, *, source_credentials=None, source_ids=None, focus='', hops=1, traversal='all', config=None, full_graph=False):
    """Cached GETs never connect; explicit refresh reads authorized declarations."""
    import yaml
    from ..catalog import CATALOG_VERSION, DeclaredCatalog, declared_catalog, semantic_catalog, write_catalog, read_semantic_catalog
    from ..contracts import CapabilityManifest
    from ..metadata import publish_metadata
    from ..runtime.config import open_sources
    from ..store import GraphStore
    config = config if config is not None else workspace.resolved_config(pid)
    output = Path(config.output_dir)
    path = output / "metadata.yaml"
    adapters = []
    project = workspace.project_path(pid)
    context = _metadata_context(config, project)
    prior_context = None
    preview = None
    refresh_failed = False
    cache_warning = None
    def unread(stale=False):
        return {**_bounded_view({}, "metadata"), "tables": [], "revision": None,
                "coverage": "not_read", "instance_queries": 0, "requires_refresh": True,
                "stale_engine_metadata": stale}
    if not refresh:
        try:
            cached_context = _json_file(output / "metadata-context.json")
        except LightGraphError:
            cached_context = None
        if not path.exists() or cached_context != context:
            preview = _read_metadata_preview(project, context)
            if preview is None:
                return unread(stale=path.exists())
            refresh_failed = cached_context == {"refresh_pending": True}
    else:
        try:
            prior_context = _json_file(output / "metadata-context.json")
        except LightGraphError:
            pass
        _promote_metadata_preview(config, project)
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
                declared = preview if preview is not None else DeclaredCatalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))["declared"])
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
            if selected is not None:
                # Reuse only declarations tied to unchanged source authorization
                # and file revisions. A fresh generation may only have the
                # project preview; adding a DB must preserve other valid sources.
                saved = _read_metadata_preview_entry(project)
                previous = [saved] if saved is not None else []
                if path.exists():
                    try:
                        if path.is_symlink():
                            raise ValueError
                        prior = DeclaredCatalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))["declared"])
                        previous.append((prior, prior_context or {}))
                    except (LightGraphError, OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
                        pass
                retained_tables, retained_sources = {}, {}
                for prior, saved_context in previous:
                    retained = {sid for sid, revision in context["source_revisions"].items()
                                if sid not in selected and saved_context.get("source_revisions", {}).get(sid) == revision}
                    retained_tables.update({(t.source_id, t.schema_name, t.name): t for t in prior.tables if t.source_id in retained})
                    retained_sources.update({s.source_id: s for s in prior.sources if s.source_id in retained})
                tables = sorted(declared.tables + list(retained_tables.values()),
                                key=lambda t: (t.source_id, t.schema_name, t.name))
                descriptions = sorted(declared.sources + list(retained_sources.values()), key=lambda s: s.source_id)
                declared = DeclaredCatalog(tables=tables, sources=descriptions,
                    revision=digest(CATALOG_VERSION, [t.model_dump(by_alias=True) for t in tables],
                                    [s.model_dump() for s in descriptions]))
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
        # Promote legacy successful caches on GET too. Never reconnect or copy
        # business graph/evidence into this declaration-only project cache.
        if preview is None:
            try:
                _save_metadata_preview(project, context, declared)
            except OSError:
                if refresh:
                    raise LightGraphError('metadata_preview_persistence_failed') from None
                # This optional copy cannot make an authorized catalog unreadable.
                # Path-policy errors remain fatal; do not expose filesystem details.
                cache_warning = 'metadata_preview_persistence_failed'
        graph = _metadata_neighborhood(view,focus,hops,traversal) if focus else _bounded_view(view, "metadata", bounded=not full_graph)
        tables = [{"id": t.source_id + "." + t.qualified_name,
                   **t.model_dump(mode="json", by_alias=True)} for t in declared.tables]
        missing_sources = sorted({s.id for s in config.sources} - {s.source_id for s in declared.sources})
        return {**graph, "tables": tables, "revision": declared.revision,
                "sources": [s.model_dump(mode="json") for s in declared.sources],
                "coverage": "declarations_partial" if missing_sources else "declarations_only", "instance_queries": 0,
                "requires_refresh": bool(missing_sources) or refresh_failed, "missing_sources": missing_sources,
                "using_saved_metadata": preview is not None, "refresh_failed": refresh_failed,
                "metadata_cache_warning": cache_warning,
                "document_context_deferred": bool(missing_sources),
                "stale_engine_metadata": stale}
    finally:
        for adapter in adapters:
            adapter.close()
