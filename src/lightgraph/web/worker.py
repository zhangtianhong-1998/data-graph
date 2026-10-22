"""Private subprocess entrypoint. Configuration arrives only through stdin."""
from __future__ import annotations

import json
from pathlib import Path
import signal
import sys
import threading


def execute(payload, result_path):
    from ..contracts import RunReport
    from ..documents import load_documents
    from ..errors import LightGraphError
    from ..pipeline import load_rules, make_model, run_pipeline
    from ..runtime.config import AgentConfig, RunConfig, open_sources
    from .execution import check_dependencies, write_metadata_context, _safe_code, _safe_report, _save, _cancel_path

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

    def cancel_sources():
        for source in tuple(sources):
            with cancellation_lock:
                if id(source) in cancelled_sources:
                    continue
                cancelled_sources.add(id(source))
            try:
                source.cancel()
            except Exception:
                pass

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
    try:
        watcher.start()
        config = RunConfig.model_validate(payload["config"])
        config._agent = AgentConfig(name=payload.get("agent_name", "lightgraph-discovery"), llm=config.model)
        project = result_path.parent.parent
        check_dependencies(payload["dependencies"], project)
        check_cancelled()
        opening_sources = True
        # Direct credentials arrive through the parent's private stdin pipe.
        # They never enter the persisted RunConfig or process environment.
        options = {"source_credentials": payload["source_credentials"]} if payload.get("source_credentials") else {}
        sources = open_sources(config, **options)
        opening_sources = False
        check_cancelled()
        rules = load_rules(config.rules) if config.rules else None
        check_cancelled()
        documents = load_documents(config.documents)
        check_cancelled()
        metadata_path = Path(config.output_dir) / "metadata.yaml"
        metadata_before = metadata_path.stat().st_mtime_ns if metadata_path.exists() else None
        model = make_model(config)
        check_cancelled()
        report = run_pipeline(config, sources, rules=rules, documents=documents, model=model)
        check_cancelled()
        status = {"COMPLETED": "completed", "COMPLETED_WITH_GAPS": "partial",
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
    except Exception:
        error_code = "worker_execution_failed"
    finally:
        # Late cancellation must not interrupt release/report finally blocks.
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
            except Exception:
                cleanup = "unknown"
        sources.clear()
        payload.clear()
        if cancelled:
            status = "cancelled"
            error_code = "run_cancelled"
        try:
            result = {"status": status, "report": _safe_report(report) if report is not None else None,
                      "cleanup_status": cleanup, "error_code": error_code}
            _save(result_path, result)
        finally:
            signal.signal(signal.SIGTERM, previous)
    return 0 if status == "completed" else 2


def main():
    # The parent creates the path inside its authorized project before launch.
    # A bounded pipe prevents malformed internal requests exhausting memory.
    if len(sys.argv) != 2:
        return 1
    raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        return 1
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return 1
    del raw
    return execute(payload, Path(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
