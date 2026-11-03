"""Workbench projections preserve native Harness events and recorded content."""
from copy import deepcopy

from lightgraph.construction.runner import _trace_event
from lightgraph.construction.trace import project_harness_event
from lightgraph.runtime.trace import bind_trace
from lightgraph.web.run_traces import AGENT_EVENTS


def native(kind, data, seq=7):
    return {"method": "session.event", "payload": {"sessionId": "session-a", "event": {
        "type": kind, "seq": seq, "time": 1234, "data": data}}}


def test_tool_arguments_and_result_are_visible_in_agent_filter_and_keep_call_id():
    call = native("tool/call", {"turn": 1, "step": 2, "callId": "call-1", "name": "mcp__graph__inspect_source",
                                "arguments": '{"table":"input:public.设备","operation":"get_metadata"}'})
    result = native("tool/result", {"turn": 1, "step": 2, "message": {
        "role": "tool", "toolCallId": "call-1", "content": [{"type": "text", "text": '{"rows":3}'}],
        "isError": False}})
    call_kind, call_data = project_harness_event(call)
    result_kind, result_data = project_harness_event(result)
    assert call_kind == "tool_call" and result_kind == "tool_result"
    assert call_kind in AGENT_EVENTS and result_kind in AGENT_EVENTS
    assert call_data["call_id"] == result_data["call_id"] == "call-1"
    assert call_data["arguments"] == call["payload"]["event"]["data"]["arguments"]
    assert result_data["content"] == [{"type": "text", "text": '{"rows":3}'}]
    assert call_data["native"] == call and result_data["native"] == result


def test_native_model_output_keeps_content_and_usage_without_synthesizing_reasoning():
    event = native("assistant/message", {"turn": 1, "step": 3, "message": {
        "role": "assistant", "content": [{"type": "text", "text": "已保存图谱。"}],
        "source": {"replayState": {"response": {"stopReason": "stop"}}}},
        "usage": {"inputTokens": 100, "outputTokens": 20}})
    before = deepcopy(event)
    kind, data = project_harness_event(event)
    assert kind == "model_response" and kind in AGENT_EVENTS
    assert data["response"]["message"]["content"] == [{"type": "text", "text": "已保存图谱。"}]
    assert "reasoning_content" not in data["response"]["message"]
    assert data["response"]["finish_reason"] == "stop"
    assert data["response"]["usage"] == {"inputTokens": 100, "outputTokens": 20}
    assert event == before


def test_only_dispatch_hook_can_be_rendered_as_full_model_request():
    header = native("request/header", {"header": {"config": {"model": "local"}, "tools": []}})
    assert project_harness_event(header)[0] == "harness_event"
    request = {"provider": "graph-model", "model": "local", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "发现设备"}]}], "tools": []}
    event = {"method": "model.request", "payload": {"type": "model/request", "seq": 1,
        "request": request, "attempt": 2, "purpose": "compaction", "estimated_input_tokens": 18,
        "measurement": {"kind": "estimate"}}}
    kind, data = project_harness_event(event)
    assert kind == "model_request" and kind in AGENT_EVENTS
    assert data["payload"] == request
    assert data["recording_source"] == "native_llm_stream_hook"
    assert data["purpose"] == "compaction" and data["attempt"] == 2
    assert data["native"] == event


def test_compaction_is_a_runtime_operation_with_its_original_material():
    event = native("compaction/summary", {"summary": "先前已确认设备编号", "sourceEventSeqs": [1, 3]})
    kind, data = project_harness_event(event)
    assert kind == "context_compaction" and kind not in AGENT_EVENTS
    assert data["operation"] == "compaction/summary"
    assert data["details"]["summary"] == "先前已确认设备编号"
    assert data["native"] == event


def test_run_callback_emits_existing_workbench_kinds_in_native_order():
    recorded = []
    with bind_trace(lambda kind, data: recorded.append((kind, data))):
        _trace_event(native("turn/start", {"turn": 1}))
        _trace_event(native("assistant/attempt", {"turn": 1, "step": 1, "error": {"code": "timeout"}}))
        _trace_event(native("turn/end", {"turn": 1, "reason": {"kind": "failed"}}))
    recorded = [(kind, data) for kind, data in recorded if kind in AGENT_EVENTS]
    assert [kind for kind, _ in recorded] == ["agent_start", "model_error", "agent_end"]
    assert recorded[1][1]["diagnostic"]["error"]["code"] == "timeout"
    assert all(data["native"] for _, data in recorded)
