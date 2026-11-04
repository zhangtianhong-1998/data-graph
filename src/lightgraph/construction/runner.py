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
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
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
    elif (kind == "compaction/summary" and isinstance(native.get("data"), dict)
          and native["data"].get("llmStreamCall") is True):
        emit_progress("discovery", "model_call_completed")


def run_construction(config, adapters=None, *, discover_only=False, cancel_requested=None,
                     runtime=None, options=None, run_id=None):
    """Let the Agent choose tools and publish; report actual durable outcomes.

    ``runtime`` is an explicit test seam, not a second production Agent engine.
    Sources and their credentials stay in this worker behind loopback MCP.
    """
    started = time.monotonic()
    output = Path(config.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    workspace = output / "construction"
    report = RunReport(run_id=run_id or "run_" + uuid.uuid4().hex[:24])
    tools = None
    outcome = None
    usage = {}
    observed_calls = 0

    def record_event(event):
        nonlocal observed_calls
        payload = event.get('payload')
        payload = payload if isinstance(payload, dict) else {}
        if event.get('method') == 'model.request' and type(payload.get('seq')) is int:
            observed_calls = max(observed_calls, payload['seq'])
        _trace_event(event)
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
                options=selected, on_event=record_event, mcp_url=url, mcp_token_env=token_env,
                environment=environment, cancel_requested=cancel_requested)
        outcome = {**outcome, "execution_kind": "test_double" if runtime else "model_runtime"}
        budget = outcome.get('budget')
        budget = budget if isinstance(budget, dict) else {}
        if runtime is None:
            from .trace import summarize_harness_usage
            usage = summarize_harness_usage(outcome.get('trace_files', []),
                request_trace=outcome.get('request_trace'), usage_trace=outcome.get('usage_trace'),
                session_id=outcome.get('session_id'), execution_id=budget.get('execution_id'))
            outcome['usage'] = usage
        write_yaml(workspace / "runtime-result.yaml", outcome)
        reason = outcome.get("finish_reason")
        calls = budget.get('calls')
        calls = max(calls if type(calls) is int and calls >= 0 else 0, observed_calls)
        report.counts["llm_calls"] = calls if runtime is None else 0
        if usage:
            for key in ('input_tokens', 'output_tokens', 'total_tokens', 'cache_read_tokens',
                        'cache_write_tokens', 'uncached_input_tokens', 'max_input_tokens',
                        'measured_calls', 'missing_usage_calls'):
                report.counts[key] = usage[key]
            for purpose in ('agent', 'compaction'):
                bucket = usage.get('by_purpose', {}).get(purpose, {})
                report.counts[purpose + '_tokens'] = bucket.get('total_tokens', 0)
            report.validation['model_usage'] = 'complete' if usage['complete'] else 'incomplete'
        if runtime is not None:
            report.counts["simulated_model_calls"] = calls
            report.validation["D"] = "test_double"
        if reason not in {"completed", "stop", "end_turn"}:
            report.status = "PARTIAL"
            cause = budget.get("stop_reason", "")
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
        if runtime is None and observed_calls:
            report.counts['llm_calls'] = max(report.counts.get('llm_calls', 0), observed_calls)
            report.validation.setdefault('model_usage', 'incomplete')
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
                from .reporting import construction_summary, draft_is_ready, draft_issues
                execution_kind = ('test_double' if runtime and outcome else
                                  'executed' if outcome else 'interrupted' if observed_calls else 'not_run')
                if outcome and outcome.get('finish_reason') not in {'completed', 'stop', 'end_turn'}:
                    execution_kind = 'interrupted'
                report.construction = construction_summary(records, publications, graph, execution_kind=execution_kind)
                report.construction.usage = usage
                report.issues.extend(draft_issues(records, publications, discover_only=discover_only))
                report.validation.update(business_graph='nonempty' if graph.get('nodes') else 'empty',
                    data_checks=report.construction.execution['data_validation'], model_execution=execution_kind)
                report.counts.update(published_drafts=len(publications),
                    entity_types=report.construction.summary['entity_type_count'],
                    relation_types=report.construction.summary['relation_type_count'],
                    mappings=report.construction.summary['mapping_count'],
                    db_queries=sum(getattr(getattr(a, 'stats', None), 'queries', 0) for a in tools.adapters))
                emit_progress(None, 'stage_completed', counters={
                    **{target: report.counts[source] for source, target in
                       [('llm_calls', 'model_calls'), ('input_tokens', 'input_tokens'), ('output_tokens', 'output_tokens')]
                       if source in report.counts},
                    'published_rules': len(publications), 'db_queries': report.counts['db_queries'],
                    'business_nodes': report.counts['business_nodes'], 'business_edges': report.counts['business_edges']})
                remaining_work = not records or any(r.draft.unresolved or not draft_is_ready(
                    r, publications, discover_only=discover_only) for r in records)
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
