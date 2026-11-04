"""Opt-in real vendored kernel tests with a loopback-only scripted model.

Run after bootstrap with LIGHTGRAPH_TEST_HARNESS=1. These verify the kernel,
MCP transport and dispatch budget, not autonomous semantic discovery quality.
"""
import json
import os
from pathlib import Path
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import uvicorn
import yaml
from mcp.server.fastmcp import FastMCP

from lightgraph.construction.harness import HarnessOptions, run_goal


pytestmark = pytest.mark.skipif(os.environ.get("LIGHTGRAPH_TEST_HARNESS") != "1",
                                reason="real kernel smoke requires scripts/bootstrap_harness.py")


def respond(handler, delta, reason):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    for chunk in [
        {"id": "request1", "object": "chat.completion.chunk", "created": 1,
         "model": "loopback-model", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {"id": "request1", "object": "chat.completion.chunk", "created": 1,
         "model": "loopback-model", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                   "prompt_tokens_details": {"cached_tokens": 60}}},
    ]:
        handler.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


@pytest.mark.parametrize("max_calls, expected_calls, large_result, input_limit, fail_first", [
    (3, 2, False, 1048576, False), (1, 1, False, 1048576, False), (8, None, True, 1048576, False),
    (8, None, True, 4096, False), (3, 0, False, 100, False), (3, 2, False, 1048576, True)])
def test_real_kernel_mcp_trace_and_model_budget(tmp_path, max_calls, expected_calls, large_result, input_limit, fail_first):
    requests = []
    tool_calls = []
    token = "local-test-bridge-token"

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if fail_first and len(requests) == 1:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"temporary fixture failure"}}')
                return
            if len(requests) == 1:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call1",
                    "type": "function", "function": {"name": "mcp__graph__describe_sources", "arguments": "{}"}}]}
                reason = "tool_calls"
            elif 'Graph checkpoint rules:' in json.dumps(request['messages'][-1]):
                # An intentionally bad checkpoint tests that generated notes
                # cannot replace the original goal/skill in the next request.
                delta = {"role": "assistant", "content": "## Pending Jobs\nMUST_REPLACE_GOAL_WITH_INVENTED_UNRESOLVED"}
                reason = "stop"
            else:
                delta = {"role": "assistant", "content": "已读取数据源清单。"}
                reason = "stop"
            respond(self, delta, reason)

    domain = FastMCP("graph", stateless_http=True, json_response=True)
    @domain.tool()
    def describe_sources() -> dict:
        tool_calls.append("describe_sources")
        return {"sources": [], "material": "x" * 50000 + "RETRIEVABLE_MIDDLE_MARKER" + "x" * 50000} if large_result else {"sources": []}
    app = domain.streamable_http_app()
    class Auth:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and dict(scope["headers"]).get(b"authorization") != ("Bearer " + token).encode():
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"denied"})
            else:
                await app(scope, receive, send)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    mcp_port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(Auth(), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    model_thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start(); model_thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        config = tmp_path / "config.yaml"
        config.write_text("goal: smoke\n")
        events = []
        result = run_goal("发现授权数据中的对象与关系", config, tmp_path,
            HarnessOptions(model="loopback-model", base_url=f"http://127.0.0.1:{model.server_port}/v1",
                           max_calls=max_calls, max_run_seconds=40,
                           max_input_tokens=input_limit,
                           include_stream_usage=input_limit != 4096,
                           working_context_tokens=(500000 if input_limit == 4096 else 2048) if large_result else 32768),
            mcp_url=f"http://127.0.0.1:{mcp_port}/mcp", mcp_token_env="GRAPH_TEST_TOKEN",
            environment={"GRAPH_TEST_TOKEN": token, "OPENAI_API_KEY": "local-test-only"}, on_event=events.append)
        if expected_calls is not None:
            assert len(requests) == expected_calls
        else:
            assert 2 <= len(requests) <= max_calls
        assert tool_calls == (["describe_sources"] if input_limit > 100 and not fail_first else [])
        assert result["budget"]["calls"] == len(requests)
        assert result["finish_reason"] == ("completed" if max_calls > 1 and input_limit > 100 else "error")
        if max_calls == 1:
            assert result["budget"]["stop_reason"] == "construction_call_budget_exhausted"
        if requests:
            assert all("max_tokens" in request and "max_completion_tokens" not in request for request in requests)
            assert all("store" not in request for request in requests)
            assert all(("stream_options" in request) == (input_limit != 4096) for request in requests)
            names = {tool["function"]["name"] for tool in requests[0]["tools"]}
            assert names == {"mcp__graph__describe_sources", "skill", "session_search", "session_event_search",
                             "session_trace", "session_event_trace", "session_event_read"}
        else:
            assert result["budget"]["stop_reason"] == "construction_input_limit_exceeded"
            assert result["budget"]["input_diagnostic"]["estimated_input_tokens"] > input_limit
        request_events = [event for event in events if event["method"] == "model.request"]
        assert len(request_events) == len(requests)
        assert all('Graph checkpoint rules:' in json.dumps(event['payload']['request']['messages'][-1])
                   for event in request_events if event['payload']['purpose'] == 'compaction')
        if requests:
            assert request_events[0]["payload"]["request"]["model"] == "loopback-model"
        assert any(event["method"] == "session.event" for event in events)
        if fail_first:
            assert any(event.get("payload", {}).get("event", {}).get("type") == "llm/retry" for event in events)
        assert list((tmp_path / "harness/sessions").rglob("*.jsonl"))
        assert token not in (tmp_path / "harness/graph.patch.yaml").read_text()
        from lightgraph.construction.trace import summarize_harness_usage, project_harness_event
        usage = summarize_harness_usage(result["trace_files"], request_trace=result["request_trace"],
            usage_trace=result["usage_trace"], session_id=result["session_id"],
            execution_id=result["budget"]["execution_id"])
        successful = len(requests) - int(fail_first)
        assert usage["input_tokens"] == 100 * successful
        assert usage["cache_read_tokens"] == 60 * successful
        assert usage["output_tokens"] == 20 * successful
        assert usage["total_tokens"] == 120 * successful
        assert usage["missing_usage_calls"] == int(fail_first)
        assert "vendor/deepseek-harness" in result["sdk_source"].replace("\\", "/")
        if large_result:
            kinds = [e.get("payload", {}).get("event", {}).get("type") for e in events]
            assert "compaction/prune" in kinds
            if input_limit != 4096:
                assert "compaction/summary" in kinds
                assert len(requests) >= 3  # Summary dispatches are part of the same call budget.
            assert all("RETRIEVABLE_MIDDLE_MARKER" not in json.dumps(r) for r in requests[1:])
            assert any("RETRIEVABLE_MIDDLE_MARKER" in Path(path).read_text() for path in result["trace_files"])
            replacements = [project_harness_event(e) for e in events if e.get('method') == 'session.event']
            assert any(kind == 'context_history_edit' for kind, _ in replacements)
            compacted = [i for i, request in enumerate(requests)
                         if 'Graph checkpoint rules:' in json.dumps(request['messages'][-1])]
            if compacted:
                assert usage['by_purpose']['compaction']['measured_calls'] == len(compacted)
                after = requests[compacted[0] + 1]
                system = '\n'.join(str(m.get('content')) for m in after['messages'] if m['role'] == 'system')
                assert '发现授权数据中的对象与关系' in system
                assert 'Pinned build-graph skill (verbatim)' in system
                assert 'Unresolved may be empty' in system
                assert 'not new user' in system
                # The bad checkpoint remains recorded and visible as notes;
                # the original higher-priority contract is preserved verbatim.
                assert 'MUST_REPLACE_GOAL_WITH_INVENTED_UNRESOLVED' in json.dumps(after)
                first_system = '\n'.join(str(m.get('content')) for m in requests[0]['messages'] if m['role'] == 'system')
                assert system == first_system
                assert any('MUST_REPLACE_GOAL_WITH_INVENTED_UNRESOLVED' in Path(path).read_text()
                           for path in result['trace_files'])
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        model.shutdown(); model.server_close(); model_thread.join(timeout=5)
        sock.close()


@pytest.mark.parametrize("through_runner", [False, True])
def test_real_kernel_drives_yaml_validation_and_publication(tmp_path, monkeypatch, through_runner):
    """Only the model's tool calls may create, validate or publish this graph."""
    from lightgraph.construction.mcp_server import serve_local_tools
    from lightgraph.construction.tools import ConstructionTools
    from lightgraph.construction.workspace import dump_yaml
    from lightgraph.runtime.config import RunConfig
    data = tmp_path / "input.csv"
    data.write_text("甲,乙,丙,丁\n001,设备甲,G1,制造组\n002,设备乙,G1,制造组\n", encoding="utf-8")
    config = RunConfig.model_validate({"sources": [{"id": "input", "kind": "csv_file",
        "path": str(data), "table_name": "无命名模板", "encoding": "utf-8", "delimiter": ",",
        "header": True, "null_values": [""], "type_inference": "text"}],
        "model": {"mode": "in_environment"}, "goal": {"purpose": "发现设备及其所属组织"},
        "output_dir": str(tmp_path / "output"), "max_run_seconds": 60})
    config_path = tmp_path / "config.yaml"
    config_path.write_text(dump_yaml(config.model_dump(mode="json")))
    tools = None if through_runner else ConstructionTools(config, tmp_path / "work")
    calls = []
    failures = []
    observed = []
    events = []
    def decode_result(request):
        content = next(m["content"] for m in reversed(request["messages"]) if m["role"] == "tool")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content)
        result = json.loads(content)
        if "content" in result:
            result = json.loads("".join(part.get("text", "") for part in result["content"]))
        observed.append(result)
        assert result.get("status") == "ok", result
        return result
    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                index = len(calls)
                if index == 0:
                    name, arguments = "describe_sources", {}
                elif index == 1:
                    table = decode_result(request)["tables"][0]
                    name, arguments = "inspect_source", {"table": table["table"], "operation": "get_metadata"}
                elif index == 2:
                    evidence = decode_result(request)["evidence_ref"]
                    source = {"source_id": "input", "schema": "public", "table": "无命名模板"}
                    draft = {"draft_id": "设备组织", "purpose": config.goal.purpose, "evidence_refs": [evidence],
                        "entities": [
                            {"type": "设备", "key_names": ["编号"], "mappings": [
                                {**source, "key_columns": ["甲"], "label_column": "乙"}]},
                            {"type": "组织", "key_names": ["编号"], "mappings": [
                                {**source, "key_columns": ["丙"], "label_column": "丁"}]}],
                        "relations": [{"type": "隶属", "source_type": "设备", "target_type": "组织",
                            "mappings": [{**source, "source_key_columns": ["甲"], "target_key_columns": ["丙"]}]}]}
                    name, arguments = "save_graph_draft", {"yaml_text": dump_yaml(draft)}
                elif index == 3:
                    decode_result(request)
                    name, arguments = "validate_graph_draft", {"draft_id": "设备组织"}
                elif index == 4:
                    assert decode_result(request)["passed"]
                    name, arguments = "publish_graph_draft", {"draft_id": "设备组织"}
                else:
                    assert decode_result(request)["nodes"] == 3
                    calls.append("final")
                    respond(self, {"role": "assistant", "content": "已保存图谱。"}, "stop")
                    return
                calls.append(name)
                respond(self, {"role": "assistant", "tool_calls": [{"index": 0, "id": f"call{index}",
                    "type": "function", "function": {"name": "mcp__graph__" + name,
                    "arguments": json.dumps(arguments, ensure_ascii=False)}}]}, "tool_calls")
            except BaseException as exc:
                failures.append(repr(exc))
                respond(self, {"role": "assistant", "content": "fixture failure"}, "stop")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if through_runner:
            from lightgraph.construction import runner
            config.model.base_url = f" http://127.0.0.1:{server.server_port}/v1/chat/completions/ "
            config.model.model = " loopback-model "
            config.model.max_calls = 8
            monkeypatch.setenv(config.model.api_key_env, "local-test-only")
            monkeypatch.setattr(runner, "_trace_event", events.append)
            report = runner.run_construction(config)
            assert report.status == "COMPLETED", report
            assert report.counts["business_nodes"] == 3 and report.counts["business_edges"] == 2
            result = yaml.safe_load((Path(config.output_dir) / "construction/runtime-result.yaml").read_text())
            assert result["execution_kind"] == "model_runtime"
            tools = ConstructionTools(config, Path(config.output_dir) / "construction")
        else:
            with serve_local_tools(tools) as (url, token):
                result = run_goal(config.goal.purpose, config_path, tmp_path / "work",
                    HarnessOptions(model="loopback-model", base_url=f"http://127.0.0.1:{server.server_port}/v1",
                                   max_calls=8, max_run_seconds=60), mcp_url=url, mcp_token_env="GRAPH_TEST_TOKEN",
                    environment={"GRAPH_TEST_TOKEN": token, "OPENAI_API_KEY": "local-test-only"},
                    on_event=events.append)
        assert not failures, failures
        assert calls == ["describe_sources", "inspect_source", "save_graph_draft", "validate_graph_draft",
                         "publish_graph_draft", "final"]
        assert result["finish_reason"] == "completed"
        graph = tools.workspace.read_published_graph()
        assert len(graph["nodes"]) == 3 and len(graph["edges"]) == 2
        assert {n["label"] for n in graph["nodes"]} == {"设备甲", "设备乙", "制造组"}
        assert all(Path(path).is_file() for path in result["trace_files"])
        assert list(tmp_path.rglob("*.yaml"))
        (tmp_path / "notifications.json").write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
        assert any(event["method"] == "session.event" for event in events)
        skill_messages = [e["payload"]["event"]["data"] for e in events
                          if e.get("payload", {}).get("event", {}).get("type") == "user/message"]
        assert any('<skill_content name="build-graph">' in json.dumps(m, ensure_ascii=False).replace('\\"', '"')
                   for m in skill_messages)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        if tools is not None:
            tools.close()
