"""Private subprocess entrypoint. Configuration arrives only through stdin."""
from __future__ import annotations

import json
from pathlib import Path
import signal
import sys


def execute(payload, result_path):
    from ..contracts import RunReport
    from ..documents import load_documents
    from ..errors import LightGraphError
    from ..pipeline import load_rules, make_model, run_pipeline
    from ..runtime.config import AgentConfig, RunConfig, open_sources
    from .execution import check_dependencies, write_metadata_context, _safe_code, _safe_report, _save

    config = None
    sources = []
    report = None
    status = "failed"
    error_code = None
    cleanup = "completed"
    cancelled = False
    opening_sources = False

    def stop(_signum, _frame):
        nonlocal cancelled
        cancelled = True
        # A second SIGTERM must not interrupt finally blocks; the parent owns
        # bounded escalation to kill if a driver refuses to release resources.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        for source in sources:
            try:
                source.cancel()
            except Exception:
                pass
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        config = RunConfig.model_validate(payload["config"])
        config._agent = AgentConfig(name=payload.get("agent_name", "lightgraph-discovery"), llm=config.model)
        project = result_path.parent.parent
        check_dependencies(payload["dependencies"], project)
        opening_sources = True
        sources = open_sources(config)
        opening_sources = False
        rules = load_rules(config.rules) if config.rules else None
        documents = load_documents(config.documents)
        metadata_path = Path(config.output_dir) / "metadata.yaml"
        metadata_before = metadata_path.stat().st_mtime_ns if metadata_path.exists() else None
        report = run_pipeline(config, sources, rules=rules, documents=documents, model=make_model(config))
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
