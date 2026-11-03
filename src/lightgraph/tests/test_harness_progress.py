"""Progress consumes actual native dispatches, not headers or guessed usage."""
from lightgraph.construction.runner import _trace_event
from lightgraph.runtime.progress import RunProgress, bind_progress


def test_native_dispatches_retries_and_summary_are_visible_in_progress(tmp_path):
    progress = RunProgress(tmp_path / "run.progress.json")
    def event(kind, data=None, seq=1):
        return {"method": "session.event", "payload": {"sessionId": "s",
            "event": {"type": kind, "seq": seq, "data": data or {}}}}
    with bind_progress(progress):
        _trace_event({"method": "model.request", "payload": {"type": "model/request", "seq": 1,
            "estimated_input_tokens": 9000, "request": {"messages": []}}})
        _trace_event(event("assistant/attempt"))
        _trace_event({"method": "model.request", "payload": {"type": "model/request", "seq": 2,
            "purpose": "compaction", "request": {"messages": []}}})
        _trace_event(event("compaction/summary", {"llmStreamCall": True}))
        _trace_event({"method": "model.request", "payload": {"type": "model/request", "seq": 3,
            "request": {"messages": []}}})
        _trace_event(event("assistant/message", {"usage": {"inputTokens": 20, "outputTokens": 5}}))
        _trace_event(event("request/header", {"header": {"tools": []}}, seq=100))
    assert progress.snapshot["stage"] == "discovery"
    assert progress.snapshot["counters"] == {"model_calls": 3}
    assert [item["code"] for item in progress.snapshot["events"]] == [
        "model_call_started", "model_call_failed", "model_call_started", "model_call_completed",
        "model_call_started", "model_call_completed"]
