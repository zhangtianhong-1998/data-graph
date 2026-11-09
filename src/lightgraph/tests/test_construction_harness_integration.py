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
from lightgraph.construction.paths import io_path


pytestmark = pytest.mark.skipif(os.environ.get("LIGHTGRAPH_TEST_HARNESS") != "1",
                                reason="real kernel smoke requires scripts/bootstrap_harness.py")


@pytest.mark.parametrize('broken_channel', [False, True])
def test_native_kernel_separates_model_timeout_and_stops_dead_graph_channel(tmp_path, broken_channel):
    """Real MCP/native hooks: slow tools survive, broken tools never replay."""
    from lightgraph.construction.mcp_server import serve_local_tools
    calls, requests = [], []
    class Host:
        def call(self, name, **arguments):
            calls.append(name)
            if broken_channel:
                raise RuntimeError('Session not found')
            time.sleep(.4)
            return {'status': 'ok', 'sources': []}
    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(request)
            if len(requests) == 1 or broken_channel:
                respond(self, {'role':'assistant','tool_calls':[{'index':0,
                    'id':f'call{len(requests)}','type':'function','function':{
                        'name':'mcp__graph__describe_sources','arguments':'{}'}}]}, 'tool_calls')
            else:
                respond(self, {'role':'assistant','content':'Sources read.'}, 'stop')
    model = ThreadingHTTPServer(('127.0.0.1',0),Model)
    thread = threading.Thread(target=model.serve_forever,daemon=True)
    thread.start()
    try:
        config = tmp_path/'config.yaml'
        config.write_text('goal: lifecycle\n')
        with serve_local_tools(Host()) as (url,token):
            result = run_goal('Read authorized sources.',config,tmp_path,
                HarnessOptions(model='loopback-model',base_url=f'http://127.0.0.1:{model.server_port}/v1',
                    timeout_seconds=.2,max_calls=12,max_run_seconds=40),
                mcp_url=url,mcp_token_env='GRAPH_TEST_TOKEN',
                environment={'GRAPH_TEST_TOKEN':token,'OPENAI_API_KEY':'local-test-only'})
        assert calls == ['describe_sources']
        assert len(requests) == (3 if broken_channel else 2)
        assert result['budget']['stop_reason'] == ('construction_tool_transport_failed' if broken_channel else '')
        assert result['finish_reason'] == ('error' if broken_channel else 'completed')
    finally:
        model.shutdown()
        model.server_close()
        thread.join(timeout=5)


def test_native_truncated_summaries_never_replace_history_and_stop_retrying(tmp_path):
    from lightgraph.construction.mcp_server import serve_local_tools
    requests, events = [], []
    marker = 'TRUNCATED_CHECKPOINT_MUST_NOT_BECOME_MEMORY'
    class Host:
        def call(self, tool_name, **arguments):
            return {'status':'ok','material':'x'*100000}
    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(request)
            if 'Graph checkpoint rules:' in json.dumps(request['messages'][-1]):
                respond(self, {'role':'assistant','content':marker}, 'length')
            else:
                respond(self, {'role':'assistant','tool_calls':[{'index':0,
                    'id':f'call{len(requests)}','type':'function','function':{
                        'name':'mcp__graph__describe_sources','arguments':'{}'}}]}, 'tool_calls')
    model = ThreadingHTTPServer(('127.0.0.1',0),Model)
    thread = threading.Thread(target=model.serve_forever,daemon=True)
    thread.start()
    try:
        config = tmp_path/'config.yaml'
        config.write_text('goal: checkpoint\n')
        with serve_local_tools(Host()) as (url,token):
            result = run_goal('Keep the original graph goal.',config,tmp_path,
                HarnessOptions(model='loopback-model',base_url=f'http://127.0.0.1:{model.server_port}/v1',
                    max_calls=20,max_run_seconds=40,working_context_tokens=2048),
                mcp_url=url,mcp_token_env='GRAPH_TEST_TOKEN',
                environment={'GRAPH_TEST_TOKEN':token,'OPENAI_API_KEY':'local-test-only'},
                on_event=events.append)
        summaries = [request for request in requests
            if 'Graph checkpoint rules:' in json.dumps(request['messages'][-1])]
        assert len(summaries) == 3
        assert result['budget']['stop_reason'] == 'construction_compaction_failed'
        assert result['finish_reason'] == 'error'
        # Native compaction rejects truncated model output before installing a
        # checkpoint. Its original event log remains available for retrieval.
        assert not any(event.get('payload',{}).get('event',{}).get('type') == 'compaction/summary'
                       for event in events)
        ordinary = [request for request in requests if request not in summaries]
        assert len(ordinary) >= 3
        assert all(marker not in json.dumps(request) for request in ordinary)
        assert all('Keep the original graph goal.' in json.dumps(request) for request in ordinary)
        assert all('Pinned build-graph skill' in json.dumps(request) for request in ordinary)
    finally:
        model.shutdown()
        model.server_close()
        thread.join(timeout=5)


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


@pytest.mark.parametrize("max_calls, expected_calls, large_result, input_limit, fail_first, long_workspace", [
    (3, 2, False, 1048576, False, False), (1, 1, False, 1048576, False, False),
    (8, None, True, 1048576, False, False),
    pytest.param(8, 1, True, 4096, False, False, id="immutable-contract-exceeds-small-window"),
    pytest.param(8, None, True, 8192, False, False, id="bounded-window-prune-recovery"),
    (3, 0, False, 100, False, False), (3, 2, False, 1048576, True, False),
    pytest.param(3, 2, False, 1048576, False, True, id="deep-unicode-workspace")])
def test_real_kernel_mcp_trace_and_model_budget(tmp_path, max_calls, expected_calls, large_result, input_limit, fail_first, long_workspace):
    narrow_window = large_result and input_limit < 1048576
    # With the complete pinned skill, 4096 tokens cannot fit a tool result plus
    # the native pruner's retained head/tail; depending on the skill's current
    # length even the first request can exceed it. A lossy
    # summary is not permission to remove the original contract. Verify that
    # boundary separately from recovery with enough room for the fixed context.
    input_exhausted = input_limit in {100, 4096}
    if long_workspace:
        # The native project directory repeats an escaped cwd (up to 255
        # characters). Exercise >260-char session IO without also testing an
        # unrelated CreateProcess current-directory length limitation.
        while len(str(tmp_path)) < 180:
            tmp_path /= "中文 workspace " + "depth-" * 6
        io_path(tmp_path).mkdir(parents=True)
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
        io_path(config).write_text("goal: smoke\n", encoding="utf-8")
        events = []
        result = run_goal("发现授权数据中的对象与关系", config, tmp_path,
            HarnessOptions(model="loopback-model", base_url=f"http://127.0.0.1:{model.server_port}/v1",
                           max_calls=max_calls, max_run_seconds=40,
                           max_input_tokens=input_limit,
                           include_stream_usage=not narrow_window,
                           working_context_tokens=(500000 if narrow_window else 2048) if large_result else 32768),
            mcp_url=f"http://127.0.0.1:{mcp_port}/mcp", mcp_token_env="GRAPH_TEST_TOKEN",
            environment={"GRAPH_TEST_TOKEN": token, "OPENAI_API_KEY": "local-test-only"}, on_event=events.append)
        if input_limit == 4096:
            assert len(requests) <= 1
        elif expected_calls is not None:
            assert len(requests) == expected_calls
        else:
            assert 2 <= len(requests) <= max_calls
        assert tool_calls == (["describe_sources"] if requests and input_limit > 100 and not fail_first else [])
        assert result["budget"]["calls"] == len(requests)
        assert result["finish_reason"] == ("completed" if max_calls > 1 and not input_exhausted else "error")
        if max_calls == 1:
            assert result["budget"]["stop_reason"] == "construction_call_budget_exhausted"
        if requests:
            assert all("max_tokens" in request and "max_completion_tokens" not in request for request in requests)
            assert all("store" not in request for request in requests)
            assert all(("stream_options" in request) == (not narrow_window) for request in requests)
            names = {tool["function"]["name"] for tool in requests[0]["tools"]}
            assert names == {"mcp__graph__describe_sources", "skill", "session_search", "session_event_search",
                             "session_trace", "session_event_trace", "session_event_read"}
        if input_exhausted:
            assert result["budget"]["stop_reason"] == "construction_input_limit_exceeded"
            assert result["budget"]["input_diagnostic"]["estimated_input_tokens"] > input_limit
        request_events = [event for event in events if event["method"] == "model.request"]
        assert len(request_events) == len(requests)
        assert all(event['payload']['estimated_input_tokens'] <= input_limit for event in request_events)
        assert all('Graph checkpoint rules:' in json.dumps(event['payload']['request']['messages'][-1])
                   for event in request_events if event['payload']['purpose'] == 'compaction')
        if requests:
            assert request_events[0]["payload"]["request"]["model"] == "loopback-model"
        assert any(event["method"] == "session.event" for event in events)
        if fail_first:
            assert any(event.get("payload", {}).get("event", {}).get("type") == "llm/retry" for event in events)
        assert list(io_path(tmp_path / "harness/sessions").rglob("*.jsonl"))
        assert token not in io_path(tmp_path / "harness/graph.patch.yaml").read_text(encoding="utf-8")
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
        if long_workspace:
            assert all(len(path) > 260 for path in result["trace_files"])
            assert usage["complete"] and usage["malformed_records"] == 0
            # Verify the archived native session can also be read, not just
            # that the extended-length directory scan found its filename.
            native_usage = summarize_harness_usage(result["trace_files"],
                request_trace=result["request_trace"], session_id=result["session_id"])
            assert native_usage["measured_calls"] == len(requests)
            assert native_usage["input_tokens"] == 100 * len(requests)
        assert "vendor/deepseek-harness" in result["sdk_source"].replace("\\", "/")
        if large_result and requests:
            kinds = [e.get("payload", {}).get("event", {}).get("type") for e in events]
            assert "compaction/prune" in kinds
            if not narrow_window:
                assert "compaction/summary" in kinds
                assert len(requests) >= 3  # Summary dispatches are part of the same call budget.
            assert all("RETRIEVABLE_MIDDLE_MARKER" not in json.dumps(r) for r in requests[1:])
            assert any("RETRIEVABLE_MIDDLE_MARKER" in io_path(path).read_text(encoding="utf-8") for path in result["trace_files"])
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
                assert any('MUST_REPLACE_GOAL_WITH_INVENTED_UNRESOLVED' in io_path(path).read_text(encoding="utf-8")
                           for path in result['trace_files'])
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        model.shutdown(); model.server_close(); model_thread.join(timeout=5)
        sock.close()


@pytest.mark.parametrize("through_runner", [False, True])
@pytest.mark.parametrize("column_definition", [False, True], ids=['row-objects', 'row-and-column-objects'])
def test_real_kernel_drives_yaml_validation_and_publication(tmp_path, monkeypatch, through_runner, column_definition):
    """Only the model's tool calls may create, validate or publish this graph."""
    from lightgraph.construction.mcp_server import serve_local_tools
    from lightgraph.construction.tools import ConstructionTools
    from lightgraph.construction.workspace import dump_yaml
    from lightgraph.runtime.config import RunConfig
    data = tmp_path / "input.csv"
    contents = ("甲,乙,丙,丁,戊\n001,指标甲,O1,办事处甲,987654321\n002,指标乙,,,\n" if column_definition else
                "甲,乙,丙,丁\n001,设备甲,G1,制造组\n002,设备乙,G1,制造组\n")
    data.write_text(contents, encoding="utf-8")
    expected_nodes = 4 if column_definition else 3
    config = RunConfig.model_validate({"sources": [{"id": "input", "kind": "csv_file",
        "path": str(data), "table_name": "无命名模板", "encoding": "utf-8", "delimiter": ",",
        "header": True, "null_values": [""], "type_inference": "text"}],
        "model": {"mode": "in_environment"}, "goal": {"purpose":
            "建立指标、办事处及列定义，并仅连接实际非空配对" if column_definition else "发现设备及其所属组织"},
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
                    if column_definition:
                        # Only the scripted model authors these definitions. The
                        # actual kernel must use MCP to validate and publish them.
                        # Nested refs deliberately test publication evidence closure.
                        nonnull = {"column": "丙", "op": "is_not_null"}
                        draft.update(evidence_refs=[], entities=[
                            {"type": "Indicator", "key_names": ["code"], "mappings": [
                                {**source, "key_columns": ["甲"], "label_column": "乙"}]},
                            {"type": "Office", "key_names": ["code"], "mappings": [
                                {**source, "key_columns": ["丙"], "label_column": "丁", "predicate": nonnull}]},
                            {"type": "MeasureDefinition", "key_names": ["source_id", "schema", "table", "column"],
                             "mappings": [{**source, "kind": "metadata_column", "column": "戊",
                                "label": "同期季度完成", "aliases": ["Prior quarter completion"],
                                "evidence_refs": [evidence]}]}], relations=[
                            {"type": "measured_in", "source_type": "Indicator", "target_type": "Office",
                             "mappings": [{**source, "source_key_columns": ["甲"], "target_key_columns": ["丙"],
                                "predicate": nonnull}]},
                            {"type": "uses_measure", "source_type": "Indicator", "target_type": "MeasureDefinition",
                             "mappings": [{**source, "source_key_columns": ["甲"], "target_metadata": {
                                **source, "column": "戊", "evidence_refs": [evidence]},
                                "metadata_basis": "observed_non_null"}]}])
                    name, arguments = "save_graph_draft", {"yaml_text": dump_yaml(draft)}
                elif index == 3:
                    decode_result(request)
                    name, arguments = "validate_graph_draft", {"draft_id": "设备组织"}
                elif index == 4:
                    assert decode_result(request)["passed"]
                    name, arguments = "publish_graph_draft", {"draft_id": "设备组织"}
                else:
                    assert decode_result(request)["nodes"] == expected_nodes
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
            assert report.counts["business_nodes"] == expected_nodes and report.counts["business_edges"] == 2
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
        assert len(graph["nodes"]) == expected_nodes and len(graph["edges"]) == 2
        if column_definition:
            assert {n["label"] for n in graph["nodes"]} == {"指标甲", "指标乙", "办事处甲", "同期季度完成"}
            nodes = {n['id']: n for n in graph['nodes']}
            definition, = [n for n in nodes.values() if n['type'] == 'MeasureDefinition']
            assert definition['identity'] == {'source_id': 'input', 'schema': 'public',
                                               'table': '无命名模板', 'column': '戊'}
            assert definition['label_origin'] == 'metadata_declared'
            assert definition['aliases'] == ['Prior quarter completion']
            assert {edge['type'] for edge in graph['edges']} == {'uses_measure', 'measured_in'}
            assert {nodes[edge['source']]['identity']['code'] for edge in graph['edges']} == {'001'}
            measure_edge, = [e for e in graph['edges'] if e['type'] == 'uses_measure']
            assert measure_edge['target'] == definition['id']
            for support in measure_edge['supports']:
                assert support['provenance']['metadata_endpoint']['non_null_observed'] is True
                assert '戊' not in support['provenance']['columns']
            assert '987654321' not in dump_yaml(graph)
            evidence = next(item['evidence_ref'] for item in observed if 'evidence_ref' in item)
            assert evidence in graph['evidence_refs']
            assert tools.workspace.read_evidence(evidence).kind == 'source_probe'
        else:
            assert {n["label"] for n in graph["nodes"]} == {"设备甲", "设备乙", "制造组"}
        assert all(io_path(path).is_file() for path in result["trace_files"])
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


@pytest.mark.parametrize('large_closeout_result', [False, True])
def test_real_kernel_stops_repeated_blocked_source_requests(tmp_path, large_closeout_result):
    """A real kernel cannot spend its call budget repeating a dead source.

    The model deliberately ignores recovery advice. SQL execution and retained
    validation artifacts are real; only the database snapshot failure is fake.
    Large tool feedback also exercises native compaction during closeout.
    """
    from contextlib import contextmanager
    from lightgraph.construction.mcp_server import serve_local_tools
    from lightgraph.construction.tools import ConstructionTools
    from lightgraph.construction.workspace import dump_yaml
    from lightgraph.contracts import ColumnSpec, TableSpec
    from lightgraph.errors import LightGraphError
    from lightgraph.runtime.config import RunConfig
    from lightgraph.sources import MemorySource

    class FailedSource(MemorySource):
        snapshot_attempts = 0
        @contextmanager
        def snapshot(self):
            self.snapshot_attempts += 1
            raise LightGraphError('database_reconnect_failed')
            yield  # Keep this a context manager without opening a database.

    tables = [TableSpec(source_id=source, name='items',
        columns=[ColumnSpec(name='code'), ColumnSpec(name='name')])
        for source in ('local', 'remote')]
    good = MemorySource([tables[0]], {tables[0].qualified_name: [('A', 'Local A')]})
    bad = FailedSource([tables[1]], {tables[1].qualified_name: [('B', 'Remote B')]})
    config = RunConfig(sources=[], model={'mode': 'in_environment'},
        goal={'purpose': 'Keep the complete mixed-source draft and healthy local evidence'},
        max_run_seconds=60)
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(dump_yaml(config.model_dump(mode='json')), encoding='utf-8')
    workspace = tmp_path / 'work'
    host = ConstructionTools(config, workspace, adapters=[good, bad])
    authored = {'draft_id': 'mixed', 'entities': [
        {'type': source, 'key_names': ['id'], 'mappings': [{
            'source_id': source, 'table': 'items', 'key_columns': ['code'], 'label_column': 'name'}]}
        for source in ('local', 'remote')]}
    assert host.call('save_graph_draft', yaml_text=dump_yaml(authored))['status'] == 'ok'
    original = host.workspace.get_draft('mixed').draft
    requests, events, tool_results = [], [], []
    stopped_at = []
    original_call = host.call

    def observed_call(tool_name, **arguments):
        result = original_call(tool_name, **arguments)
        tool_results.append(result)
        if result.get('code') == 'source_retry_limit_exhausted' and not stopped_at:
            stopped_at.append(len(requests))
            if large_closeout_result:
                result = {**result, 'test_material': 'x' * 100000}
        return result
    host.call = observed_call

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(request)
            if 'Graph checkpoint rules:' in json.dumps(request['messages'][-1]):
                respond(self, {'role': 'assistant', 'content':
                    'The original mixed draft is retained. The remote source requires operator recovery.'}, 'stop')
            else:
                respond(self, {'role': 'assistant', 'tool_calls': [{
                    'index': 0, 'id': f'call{len(requests)}', 'type': 'function', 'function': {
                        'name': 'mcp__graph__validate_graph_draft',
                        'arguments': json.dumps({'draft_id': 'mixed'})}}]}, 'tool_calls')

    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    try:
        with serve_local_tools(host) as (url, token):
            result = run_goal(config.goal.purpose, config_path, workspace,
                HarnessOptions(model='loopback-model', base_url=f'http://127.0.0.1:{model.server_port}/v1',
                    max_calls=32, max_run_seconds=60,
                    working_context_tokens=2048 if large_closeout_result else 32768),
                mcp_url=url, mcp_token_env='GRAPH_TEST_TOKEN',
                environment={'GRAPH_TEST_TOKEN': token, 'OPENAI_API_KEY': 'local-test-only'},
                on_event=events.append)
        assert stopped_at, tool_results
        assert result['finish_reason'] == 'error'
        assert result['budget']['stop_reason'] == 'source_retry_limit_exhausted'
        assert result['budget']['calls'] == len(requests) == stopped_at[0] + 2
        assert bad.snapshot_attempts == 1
        assert host.workspace.get_draft('mixed').draft == original
        retained = host.workspace.get_draft('mixed').validation
        assert not retained.passed
        candidate = host.workspace.read_validation_graph('mixed', retained)
        assert len(candidate['nodes']) == 1 and candidate['nodes'][0]['label'] == 'Local A'
        assert host.workspace.read_published_graph()['nodes'] == []
        if large_closeout_result:
            assert any('Graph checkpoint rules:' in json.dumps(request['messages'][-1])
                       for request in requests[stopped_at[0]:])
    finally:
        model.shutdown(); model.server_close(); thread.join(timeout=5)
        host.close(); good.close(); bad.close()
