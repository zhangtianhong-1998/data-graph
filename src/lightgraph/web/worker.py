"""Private subprocess entrypoint. Configuration arrives only through stdin."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import signal
import sys
import threading


def execute(payload, result_path):
    from ..contracts import RunReport
    from ..documents import load_documents
    from ..errors import LightGraphError
    from ..construction.runner import run_construction
    from ..runtime.config import AgentConfig, RunConfig, open_sources
    from ..runtime.progress import RunProgress, bind_progress, emit_progress, progress_path
    from .execution import check_dependencies, write_metadata_context, _safe_code, _safe_report, _save, _cancel_path
    from .worker_diagnostics import WorkerDiagnostics, DatabaseDiagnosticHandler

    diagnostics = WorkerDiagnostics(result_path)
    diagnostics.emit('worker_started', stage='initializing')
    stage = 'initializing'
    diagnostic_logger = logging.getLogger('lightgraph.sources')
    diagnostic_handler = DatabaseDiagnosticHandler(diagnostics, lambda: stage)
    previous_log_level = diagnostic_logger.level
    config = None
    sources = []
    report = None
    status = "failed"
    error_code = None
    cleanup = "completed"
    cancelled = False
    opening_sources = False
    closing = threading.Event()
    cancelled_sources = set()
    cancellation_lock = threading.Lock()
    trace_context = None

    def cancel_sources():
        for source in tuple(sources):
            with cancellation_lock:
                if id(source) in cancelled_sources:
                    continue
                cancelled_sources.add(id(source))
            try:
                source.cancel()
            except Exception as exc:
                diagnostics.emit('worker_cancel_error', stage='cleanup', error=exc)

    def stop(_signum, _frame):
        nonlocal cancelled
        # A signal can arrive between any two instructions, including finally's
        # first instruction. Never raise or perform driver I/O from this handler.
        cancelled = True

    def check_cancelled():
        nonlocal cancelled
        if cancelled or _cancel_path(result_path).exists():
            cancelled = True
            cancel_sources()
            # This is a normal control-flow checkpoint inside try, not an
            # asynchronous exception that can escape resource cleanup.
            raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    def watch_cancellation():
        nonlocal cancelled
        while not closing.wait(.05):
            if cancelled or _cancel_path(result_path).exists():
                cancelled = True
                # Cancel driver I/O from outside the main thread so blocked
                # reads get a chance to return before the parent force-kills.
                cancel_sources()
    watcher = threading.Thread(target=watch_cancellation, daemon=True)
    progress_context = bind_progress(RunProgress(progress_path(result_path)))
    progress_context.__enter__()
    emit_progress("initializing", "run_started")
    try:
        diagnostic_logger.addHandler(diagnostic_handler)
        diagnostic_logger.setLevel(min(logging.INFO, diagnostic_logger.getEffectiveLevel()))
        watcher.start()
        config = RunConfig.model_validate(payload["config"])
        config._agent = AgentConfig(name=payload.get("agent_name", "lightgraph-discovery"), llm=config.model)
        from ..runtime.trace_lifecycle import execution_trace
        from ..runtime.trace import trace_path, emit_trace
        trace_context = execution_trace(config,path=trace_path(result_path),
            secrets=(payload.get('source_credentials') or {}).values())
        trace_context.__enter__()
        emit_trace('run_started', {'run_id':result_path.name.removesuffix('.result.json')})
        project = result_path.parent.parent
        check_dependencies(payload["dependencies"], project)
        check_cancelled()
        from ..construction.access import ensure_supported_access
        ensure_supported_access(config)
        if config.rules:
            raise LightGraphError("legacy_rule_input_removed")
        opening_sources = True
        stage = 'opening_sources'
        emit_progress("initializing", "source_open_started")
        # Direct credentials arrive through the parent's private stdin pipe.
        # They never enter the persisted RunConfig or process environment.
        options = {"source_credentials": payload["source_credentials"]} if payload.get("source_credentials") else {}
        sources = open_sources(config,
            metadata_only=config.resolved_model.mode in {"disabled", "metadata_only"},
            isolate_database_failures=True, **options)
        opening_sources = False
        stage = 'construction'
        emit_progress("initializing", "source_open_completed")
        check_cancelled()
        metadata_path = Path(config.output_dir) / "metadata.yaml"
        metadata_before = metadata_path.stat().st_mtime_ns if metadata_path.exists() else None
        report = run_construction(config, sources,
            cancel_requested=lambda: cancelled or _cancel_path(result_path).exists(),
            run_id=result_path.name.removesuffix('.result.json'))
        check_cancelled()
        status = {"COMPLETED": "completed", "COMPLETED_WITH_GAPS": "completed_with_gaps",
                  "PARTIAL": "partial", "FAILED": "failed"}[report.status]
        try:
            check_dependencies(payload["dependencies"], project)
            if metadata_path.exists() and metadata_path.stat().st_mtime_ns != metadata_before:
                write_metadata_context(config, project)
        except LightGraphError:
            status = "partial"
            error_code = "input_dependency_changed"
            report.status = "PARTIAL"
            report.issues.append({"code": error_code})
    except KeyboardInterrupt:
        cancelled = True
    except LightGraphError as exc:
        error_code = _safe_code(exc.code)
        diagnostics.emit('worker_error', stage=stage, error=exc, error_code=error_code)
    except Exception as exc:
        error_code = "worker_execution_failed"
        diagnostics.emit('worker_error', stage=stage, error=exc, error_code=error_code)
    finally:
        stage = 'cleanup'
        # Late cancellation must not interrupt release/report finally blocks.
        emit_progress("cleanup", "cleanup_started")
        closing.set()
        if watcher.ident is not None:
            watcher.join(timeout=.2)
        if watcher.is_alive():
            cleanup = "unknown"
        if opening_sources:
            cleanup = "unknown"
        for source in sources:
            try:
                source.close()
            except Exception as exc:
                cleanup = "unknown"
                diagnostics.emit('worker_cleanup_error', stage='cleanup', error=exc)
        sources.clear()
        payload.clear()
        if cancelled:
            status = "cancelled"
            error_code = "run_cancelled"
        emit_progress("cleanup", "cleanup_completed" if cleanup == "completed" else "issue_detected",
                      level="info" if cleanup == "completed" else "warning")
        try:
            result = {"status": status, "report": _safe_report(report) if report is not None else None,
                      "cleanup_status": cleanup, "error_code": error_code}
            _save(result_path, result)
            diagnostics.emit('worker_finished', stage='finished',
                             **({'error_code': error_code} if error_code else {}))
            from ..runtime.trace import emit_trace
            emit_trace('run_completed', result)
            emit_progress("finished", "run_" + status,
                          level="error" if status == "failed" else "warning" if status in {"partial", "cancelled"} else "info",
                          counters={"published_rules": len(report.rules_published)} if report is not None else None)
        finally:
            try:
                signal.signal(signal.SIGTERM, previous)
            finally:
                try:
                    if trace_context is not None:
                        trace_context.__exit__(None, None, None)
                finally:
                    try:
                        progress_context.__exit__(None, None, None)
                    finally:
                        diagnostic_logger.removeHandler(diagnostic_handler)
                        diagnostic_handler.close()
                        diagnostic_logger.setLevel(previous_log_level)
    # A completed interpretation may legitimately leave unsupported candidates.
    # That is distinct from an interrupted or otherwise unfinished execution.
    return 0 if status in {"completed", "completed_with_gaps"} else 2


def main():
    # The parent creates the path inside its authorized project before launch.
    # A bounded pipe prevents malformed internal requests exhausting memory.
    if len(sys.argv) != 2:
        return 1
    from .worker_diagnostics import WorkerDiagnostics
    diagnostics = WorkerDiagnostics(Path(sys.argv[1]))
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            diagnostics.emit('worker_input_invalid', stage='initializing')
            return 1
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            diagnostics.emit('worker_input_invalid', stage='initializing')
            return 1
        del raw
        return execute(payload, Path(sys.argv[1]))
    except Exception as exc:
        diagnostics.emit('worker_unhandled_error', stage='result', error=exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
