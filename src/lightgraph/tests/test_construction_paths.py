"""Session IO must not depend on the Windows LongPathsEnabled registry flag."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lightgraph.construction import harness, paths
from lightgraph.construction.trace import summarize_harness_usage
from lightgraph.errors import LightGraphError


@pytest.mark.parametrize("value, expected", [
    (r"C:\工作目录\会话\session.v4.jsonl", r"\\?\C:\工作目录\会话\session.v4.jsonl"),
    ("C:/工作目录/first/../会话", r"\\?\C:\工作目录\会话"),
    (r"\\server\共享\会话", r"\\?\UNC\server\共享\会话"),
    (r"\\?\C:\工作目录\会话", r"\\?\C:\工作目录\会话"),
    (r"\\?\UNC\server\共享\会话", r"\\?\UNC\server\共享\会话"),
])
def test_windows_io_prefix_handles_drive_unc_and_unicode(value, expected):
    assert paths._extended_windows_path(value) == expected


@pytest.mark.parametrize("value", [r"C:\工作目录\会话", r"\\server\共享\会话"])
def test_windows_io_prefix_is_not_exported_as_a_user_path(monkeypatch, value):
    monkeypatch.setattr(paths, "_WINDOWS", True)
    assert paths.logical_path(paths._extended_windows_path(value)) == Path(value)


@pytest.mark.parametrize("persist", [True, False])
def test_deep_unicode_session_scan_request_tail_and_usage_reads(tmp_path, monkeypatch, persist):
    # Each component remains below NAME_MAX. The entire path exceeds MAX_PATH,
    # including on Windows CI where plain Python glob used to return no files.
    workspace = tmp_path
    while len(str(workspace)) < 330:
        workspace /= "中文 workspace " + "depth-" * 6
    paths.io_path(workspace).mkdir(parents=True)
    config = workspace / "config.yaml"
    paths.io_path(config).write_text("goal: demo\n", encoding="utf-8")
    sid = "graph-long-path"
    session = workspace / "harness/sessions/2026/11/04" / sid / "session.v4.jsonl"
    native_usage = {"inputTokens": 30, "cacheReadTokens": 70, "outputTokens": 20}
    request = {"type": "model/request", "seq": 1, "session_id": sid, "execution_id": "test"}
    dispatch = {**request, "type": "model/usage", "purpose": "agent", "usage": native_usage}
    seen = {}

    class SDK:
        def __init__(self, **kwargs): seen.update(kwargs)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def run(self, prompt, session_id, on_notification):
            home = workspace / "harness"
            if persist:
                paths.io_path(session.parent).mkdir(parents=True)
                paths.io_path(session).write_text("\n".join(json.dumps(row) for row in [
                    {"type": "session", "id": sid},
                    {"type": "assistant/message", "seq": 1,
                     "data": {"turn": 1, "step": 1, "usage": native_usage}},
                ]) + "\n", encoding="utf-8")
            # The runtime writes these ledgers; Python must tail/read them via
            # the same extended-path boundary used for the session glob.
            paths.io_path(home / "model-requests.jsonl").write_text(json.dumps(request) + "\n")
            paths.io_path(home / "model-usage.jsonl").write_text(json.dumps(dispatch) + "\n")
            paths.io_path(home / "budget.yaml").write_text("calls: 1\nexecution_id: test\n")
            on_notification({"method": "session.event", "payload": {"type": "turn/end"}})
            return SimpleNamespace(session_id=sid, final_response="done", finish_reason="completed")

    # Exercise the production persistence guard, not sdk_factory's unit-test
    # exemption. Only the external executable is substituted in this test.
    monkeypatch.setattr(harness, "resolved_options", lambda options: options)
    monkeypatch.setattr(harness, "_base_profile", lambda *args: ())
    monkeypatch.setattr(harness, "_sdk_factory", lambda options: SDK)
    monkeypatch.setattr(harness.subprocess, "check_output", lambda *args, **kwargs: "test-runtime")
    events = []
    options = harness.HarnessOptions(model="test", base_url="http://127.0.0.1:9/v1", dsh_bin="test")
    def run():
        return harness.run_goal("long path regression", config, workspace, options,
            session_id=sid, mcp_url="http://127.0.0.1:9/mcp", mcp_token_env="LOCAL_TOKEN",
            on_event=events.append)
    if not persist:
        with pytest.raises(LightGraphError, match="^harness_session_persistence_failed$"):
            run()
        return
    result = run()
    assert result["trace_files"] == [str(session)]
    assert len(result["trace_files"][0]) > 260
    assert not seen["dsh_home"].startswith("\\\\?\\")
    assert result["budget"]["calls"] == 1
    assert [event["payload"] for event in events if event["method"] == "model.request"] == [request]
    ledger = summarize_harness_usage(result["trace_files"], request_trace=result["request_trace"],
        usage_trace=result["usage_trace"], session_id=sid, execution_id="test")
    legacy = summarize_harness_usage(result["trace_files"], request_trace=result["request_trace"], session_id=sid)
    for usage in (ledger, legacy):
        assert usage["complete"] and usage["malformed_records"] == 0
        assert usage["measured_calls"] == 1 and usage["total_tokens"] == 120
