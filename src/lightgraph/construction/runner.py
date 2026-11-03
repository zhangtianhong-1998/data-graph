"""Product entrypoint: one Harness session owns discovery and graph publication."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

from lightgraph.contracts import RunReport
from lightgraph.errors import LightGraphError
from lightgraph.runtime.progress import emit_progress
from lightgraph.runtime.trace import emit_trace, current_trace

from .harness import HarnessOptions, run_goal
from .mcp_server import serve_local_tools
from .tools import ConstructionTools
from .workspace import write_yaml


def runtime_options(config):
    """Translate effective settings once; the old per-table scheduler is unused."""
    settings = config.resolved_model
    if settings.mode == "disabled":
        return None
    if settings.max_calls == 0:
        raise LightGraphError("construction_call_budget_exhausted")
    host = urlsplit(settings.base_url)
    if host.scheme == "http" and host.hostname not in {"127.0.0.1", "localhost", "::1"} and not settings.allow_insecure_http:
        raise LightGraphError("insecure_model_endpoint")
    capacity = settings.max_prompt_tokens
    if settings.context_window_tokens:
        available = settings.context_window_tokens - settings.max_output_tokens
        capacity = min(capacity, available) if capacity is not None else available
    return HarnessOptions(model=settings.model, base_url=settings.base_url,
        api_key_env=settings.api_key_env, max_input_tokens=capacity,
        max_output_tokens=settings.max_output_tokens,
        working_context_tokens=settings.working_context_tokens,
        timeout_seconds=settings.timeout_seconds,
        max_calls=settings.max_calls if settings.max_calls is not None else 2**53 - 1,
        max_run_seconds=config.max_run_seconds, retries=settings.retries,
        include_stream_usage=settings.stream_options_include_usage,
        dsh_bin=os.environ.get("LIGHTGRAPH_DSH_BIN"),
        sdk_source=os.environ.get("LIGHTGRAPH_HARNESS_SDK_SOURCE"))


def _trace_event(event):
    from .trace import project_harness_event
    event_kind, recorded = project_harness_event(event)
    emit_trace(event_kind, recorded)
    payload = event.get("payload", {})
    native = payload.get("event", payload)
    kind = native.get("type", "") if isinstance(native, dict) else ""
    if event.get("method") == "model.request" and kind == "model/request":
        count = payload.get("seq")
        counters = {"model_calls": count} if type(count) is int and count > 0 else None
        emit_progress("discovery", "model_call_started", counters=counters)
    elif kind == "assistant/message":
        emit_progress("discovery", "model_call_completed")
    elif kind == "assistant/attempt":
        emit_progress("discovery", "model_call_failed", level="warning")
    elif kind == "compaction/summary" and native.get("data", {}).get("llmStreamCall") is True:
        emit_progress("discovery", "model_call_completed")


def run_construction(config, adapters=None, *, discover_only=False, cancel_requested=None,
                     runtime=None, options=None):
    """Let the Agent choose tools and publish; report actual durable outcomes.

    ``runtime`` is an explicit test seam, not a second production Agent engine.
    Sources and their credentials stay in this worker behind loopback MCP.
    """
    started = time.monotonic()
    output = Path(config.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    workspace = output / "construction"
    report = RunReport(run_id="run_" + uuid.uuid4().hex[:20])
    tools = None
    outcome = None
    try:
        tools = ConstructionTools(config, workspace, adapters=adapters,
                                  allow_publication=not discover_only)
        deadline = started + config.max_run_seconds
        tools.budget.tighten_deadline(deadline)
        # Keep counters and the earlier deadline when borrowing already-open
        # adapters. Preparation and later validation share this run boundary.
        for adapter in tools.adapters:
            budget = getattr(adapter, "query_budget", None)
            if budget is not None:
                budget.deadline = min(budget.deadline, deadline)
        metadata = tools.describe_sources()
        write_yaml(output / "physical_catalog.yaml", {"tables": metadata["tables"], "documents": metadata["documents"]})
        if config.resolved_model.mode == "disabled":
            report.issues.append({"code": "model_disabled", "status": "diagnostic"})
            report.counts["metadata_tables"] = len(tools.tables)
            return report
        if not config.record_trace:
            raise LightGraphError("harness_session_persistence_required")
        if config.resolved_model.max_calls == 0:
            raise LightGraphError("construction_call_budget_exhausted")
        selected = options or runtime_options(config)
        deadline = min(deadline, started + selected.max_run_seconds)
        tools.budget.tighten_deadline(deadline)
        for adapter in tools.adapters:
            budget = getattr(adapter, "query_budget", None)
            if budget is not None:
                budget.deadline = min(budget.deadline, deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LightGraphError("construction_time_budget_exhausted")
        selected = replace(selected, max_run_seconds=remaining)
        settings = config.resolved_model
        key = settings.api_key.get_secret_value() if settings.api_key else os.getenv(settings.api_key_env, "")
        # This private credential only reaches the subprocess environment; the
        # tool service itself continues to own all database credentials.
        environment = {settings.api_key_env: key} if key else {}
        config_path = workspace / "config.yaml"
        write_yaml(config_path, {"target": "graph", "goal": config.goal.model_dump(mode="json"),
                                "input_token_capacity": selected.max_input_tokens,
                                "output_token_limit": selected.max_output_tokens})
        from .intent import effective_goal_markdown, initial_intent
        from lightgraph.runtime.retention import atomic_text
        atomic_text(workspace / "goal.md", effective_goal_markdown(config))
        intent = initial_intent(config, metadata["documents"], discover_only=discover_only)
        emit_progress("discovery", "stage_started")
        with serve_local_tools(tools) as (url, token):
            token_env = "LIGHTGRAPH_CONSTRUCTION_TOKEN"
            environment[token_env] = token
            sink = current_trace()
            if sink:
                sink.add_secrets([key, token])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LightGraphError("construction_time_budget_exhausted")
            selected = replace(selected, max_run_seconds=remaining)
            if cancel_requested and cancel_requested():
                raise LightGraphError("construction_cancelled")
            outcome = (runtime or run_goal)(intent, config_path=config_path, workspace=workspace,
                options=selected, on_event=_trace_event, mcp_url=url, mcp_token_env=token_env,
                environment=environment, cancel_requested=cancel_requested)
        outcome = {**outcome, "execution_kind": "test_double" if runtime else "model_runtime"}
        write_yaml(workspace / "runtime-result.yaml", outcome)
        reason = outcome.get("finish_reason")
        calls = outcome.get("budget", {}).get("calls", 0)
        report.counts["llm_calls"] = calls if runtime is None else 0
        if runtime is not None:
            report.counts["simulated_model_calls"] = calls
            report.validation["D"] = "test_double"
        if reason not in {"completed", "stop", "end_turn"}:
            report.status = "PARTIAL"
            cause = outcome.get("budget", {}).get("stop_reason", "")
            code = cause if cause in {"construction_input_limit_exceeded", "construction_call_budget_exhausted",
                                     "construction_time_budget_exhausted"} else "harness_run_interrupted"
            report.issues.append({"code": code, "status": "partial"})
    except LightGraphError as exc:
        report.status = "PARTIAL"
        report.issues.append({"code": exc.code, "status": "partial"})
    except Exception as exc:
        report.status = "PARTIAL"
        report.issues.append({"code": "harness_runtime_failed", "status": "partial"})
        emit_trace("harness_error", {"error_type": type(exc).__name__})
    finally:
        try:
            if tools is not None:
                summary = tools.workspace.read_summary()
                records = tools.workspace.list_drafts()
                publications = {p["draft_id"]: p for p in summary["publications"]}
                # A new candidate revision does not remove its previous valid
                # publication. Count the published versions, not current drafts.
                report.rules_published = list(publications)
                report.counts.update(discovered_drafts=len(records), rules_published=len(publications))
                try:
                    graph = tools.workspace.read_published_graph()
                except LightGraphError as exc:
                    if exc.code != 'stale_published_graph':
                        raise
                    # An input change invalidates old checks; the historical
                    # artifact stays on disk and must not count as current work.
                    graph = {'nodes': [], 'edges': []}
                    report.issues.append({'code': 'stale_published_graph', 'status': 'unresolved'})
                from .access import filter_graph_scopes
                had_published_nodes = bool(graph.get("nodes"))
                graph = filter_graph_scopes(graph, config.scopes)
                if had_published_nodes and not graph["nodes"]:
                    report.validation['business_graph_reason'] = 'scope_filtered'
                    report.issues.append({'code': 'business_graph_filtered_by_scopes', 'status': 'diagnostic'})
                report.counts.update(business_nodes=len(graph.get("nodes", [])),
                                     business_edges=len(graph.get("edges", [])))
                if discover_only:
                    remaining_work = not records or any(
                        r.status not in {"validated", "published"} or r.draft.unresolved for r in records)
                else:
                    remaining_work = not records or any(r.draft.unresolved or
                        publications.get(r.draft.draft_id, {}).get("revision") != r.revision for r in records)
                if (report.status == "COMPLETED" and remaining_work
                        and config.resolved_model.mode != "disabled"):
                    report.status = "COMPLETED_WITH_GAPS"
                    report.issues.append({"code": "construction_unresolved_work", "status": "unresolved"})
                write_yaml(output / "construction-summary.yaml", summary)
        except LightGraphError as exc:
            report.status = "PARTIAL"
            report.issues.append({"code": exc.code, "status": "partial"})
        finally:
            if tools is not None:
                try:
                    tools.close()
                except Exception as exc:
                    report.status = "PARTIAL"
                    report.issues.append({"code": "construction_source_close_failed", "status": "partial"})
                    emit_trace("construction_source_close_error", {"error_type": type(exc).__name__})
            report.counts["elapsed_seconds"] = time.monotonic() - started
            write_yaml(output / "report.yaml", report)
    return report
