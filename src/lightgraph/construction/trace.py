"""Project recorded Harness events for the workbench without inventing messages.

The untouched native event remains available in each record. Request headers are
not full model inputs: only the native llm/stream hook can emit model_request.
"""
from __future__ import annotations

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
