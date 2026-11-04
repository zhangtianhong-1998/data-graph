"""Project recorded Harness events for the workbench without inventing messages.

The untouched native event remains available in each record. Request headers are
not full model inputs: only the native llm/stream hook can emit model_request.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def project_harness_event(event: dict) -> tuple[str, dict]:
    payload = _mapping(event.get("payload"))
    native = _mapping(payload.get("event", payload))
    kind = native.get("type", "")
    data = _mapping(native.get("data"))
    result = {"runtime": "deepseek_harness", "native": event}
    for source, target in (("turn", "harness_turn"), ("step", "harness_step")):
        if source in data:
            result[target] = data[source]
    if "seq" in native:
        result["native_seq"] = native["seq"]
    if "sessionId" in payload:
        result["session_id"] = payload["sessionId"]

    if event.get("method") == "model.request" and kind == "model/request":
        # Captured immediately before provider dispatch, not reconstructed from
        # the conversation's event log or a compressed summary.
        result.update(payload=_mapping(payload.get("request")),
                      recording_source="native_llm_stream_hook")
        if "seq" in payload:
            result["dispatch_index"] = payload["seq"]
        if "session_id" in payload:
            result["session_id"] = payload["session_id"]
        for field in ("attempt", "purpose", "estimated_input_tokens", "measurement", "exact"):
            if field in payload:
                result[field] = payload[field]
        return "model_request", result
    if kind == "tool/call":
        result.update(name=data.get("name", ""), call_id=data.get("callId"),
                      arguments=data.get("arguments"))
        return "tool_call", result
    if kind == "tool/result":
        message = _mapping(data.get("message"))
        result.update(name=message.get("name", data.get("name", "")),
                      call_id=message.get("toolCallId", _mapping(message.get("source")).get("callId")),
                      content=message.get("content", []), is_error=message.get("isError", False))
        surface = _mapping(native.get("surfaceOp"))
        if surface.get("op") == "replace":
            # Native pruning appends a replacement event with the original
            # call ID. This is an edit of history, never another tool execution.
            result.update(operation="tool_result_replacement", surface_operation=surface,
                          source_event_seqs=native.get("sourceEventSeqs", []))
            return "context_history_edit", result
        return "tool_result", result
    if kind == "assistant/message":
        message = _mapping(data.get("message"))
        response = {"message": message}
        if "usage" in data:
            response["usage"] = data["usage"]
        recorded_response = _mapping(_mapping(_mapping(message.get("source")).get("replayState")).get("response"))
        if "stopReason" in recorded_response:
            response["finish_reason"] = recorded_response["stopReason"]
        result["response"] = response
        return "model_response", result
    if kind == "assistant/attempt":
        result["diagnostic"] = data
        return "model_error", result
    if kind == "turn/start":
        result["turn_record"] = data
        return "agent_start", result
    if kind == "turn/end":
        result["reason"] = data.get("reason")
        return "agent_end", result
    if isinstance(kind, str) and kind.startswith("compaction/"):
        result.update(operation=kind, details=data)
        return "context_compaction", result
    # Headers/context changes, history edits and native compaction operations
    # remain runtime events. Their presence is not proof of an LLM dispatch.
    result["native_type"] = kind or event.get("method", "")
    return "harness_event", result


def reclassify_trace_event(event: dict) -> dict:
    """Correct old projections at read time without rewriting their archive."""
    if event.get("kind") != "tool_result":
        return event
    data = _mapping(event.get("data"))
    native = _mapping(data.get("native"))
    kind, projected = project_harness_event(native)
    if kind != "context_history_edit":
        return event
    return {**event, "kind": kind, "data": {**data, **projected, "recorded_kind": event["kind"]}}


def summarize_harness_usage(trace_files, *, request_trace=None, usage_trace=None,
                            session_id=None, execution_id=None) -> dict:
    """Sum native provider counters, including compaction and physical retries.

    New runs record the last usage sample at the native dispatch hook. Older
    runs fall back to durable settlements; absent counters remain missing, never
    estimated. Input includes the native contract's disjoint cache buckets.
    """
    malformed = 0
    def records(path):
        nonlocal malformed
        if not path:
            return
        try:
            with Path(path).open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise ValueError
                        yield value
                    except (ValueError, TypeError):
                        malformed += 1
        except OSError:
            malformed += 1

    def matches(record):
        return (session_id is None or record.get("session_id") == session_id) and (
            execution_id is None or record.get("execution_id") == execution_id)

    def identity(record):
        return record.get("execution_id", record.get("session_id")), record.get("seq")

    requests = {identity(r): r for r in records(request_trace) if matches(r)}
    samples = {}
    source = "native_dispatch_usage" if usage_trace else "native_session_usage"
    if usage_trace:
        for r in records(usage_trace):
            if matches(r):
                usage = r.get("usage")
                # Some adapters emit initialized zero counters on HTTP errors
                # without receiving provider usage. Preserve the raw record,
                # but do not call such a failed request measured or free.
                if (r.get("finish_reason") in {"error", "aborted"} and isinstance(usage, dict)
                        and not any(usage.get(k, 0) for k in (
                            "inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens"))):
                    usage = None
                samples[identity(r)] = (r.get("purpose", "agent"), usage)
    else:
        seen = set()
        retries = {}
        for path in trace_files:
            sid = session_id
            for event in records(path):
                if event.get("type") == "session":
                    sid = event.get("id")
                    continue
                if session_id is not None and sid != session_id:
                    continue
                event_key = (sid, event.get("seq"))
                if event_key in seen:
                    continue
                seen.add(event_key)
                data = _mapping(event.get("data"))
                slot = (sid, data.get("turn"), data.get("step"))
                if event.get("type") == "llm/retry-started":
                    retries[slot] = retries.get(slot, 0) + 1
                elif event.get("type") == "compaction/summary" and data.get("llmStreamCall"):
                    samples[(sid, "summary", event.get("seq"))] = ("compaction", data.get("usage"))
                elif event.get("type") in {"assistant/message", "assistant/attempt"}:
                    usage = data.get("usage")
                    if usage is None:
                        for chunk in reversed(data.get("stream", [])):
                            value = _mapping(chunk.get("chunk"))
                            if value.get("type") == "usage":
                                usage = value.get("usage")
                                break
                    # Like native token-meter, later settlements for the same
                    # attempt replace earlier usage; a retry opens a new slot.
                    key = (*slot, retries.get(slot, 0))
                    if usage is not None or key not in samples:
                        samples[key] = ("agent", usage)

    totals = dict(input_tokens=0, uncached_input_tokens=0, cache_read_tokens=0,
                  cache_write_tokens=0, output_tokens=0, total_tokens=0)
    by_purpose = {}
    measured = peak = provider_total = provider_total_calls = 0
    def counter(value):
        return type(value) is int and value >= 0
    for purpose, usage in samples.values():
        if not isinstance(usage, dict) or not all(counter(usage.get(k)) for k in ("inputTokens", "outputTokens")):
            continue
        if any(not counter(usage.get(k, 0)) for k in ("cacheReadTokens", "cacheWriteTokens")):
            continue
        values = {"uncached_input_tokens": usage["inputTokens"],
                  "cache_read_tokens": usage.get("cacheReadTokens", 0),
                  "cache_write_tokens": usage.get("cacheWriteTokens", 0),
                  "output_tokens": usage["outputTokens"]}
        values["input_tokens"] = sum(values[k] for k in (
            "uncached_input_tokens", "cache_read_tokens", "cache_write_tokens"))
        values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
        bucket = by_purpose.setdefault(purpose, {**dict.fromkeys(totals, 0), "measured_calls": 0})
        for key, value in values.items():
            totals[key] += value
            bucket[key] += value
        bucket["measured_calls"] += 1
        measured += 1
        peak = max(peak, values["input_tokens"])
        if counter(usage.get("totalTokens")):
            provider_total += usage["totalTokens"]
            provider_total_calls += 1
    dispatched = len(requests) if request_trace else None
    missing = max(len(samples), dispatched or 0) - measured
    return {"source": source, **totals, "max_input_tokens": peak,
            "measured_calls": measured, "dispatched_calls": dispatched,
            "missing_usage_calls": missing, "malformed_records": malformed,
            "complete": dispatched is not None and missing == 0 and not malformed,
            "provider_total_tokens": provider_total if provider_total_calls else None,
            "provider_total_calls": provider_total_calls, "by_purpose": by_purpose}
