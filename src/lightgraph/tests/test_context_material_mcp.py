"""Real MCP requests recover bounded results without replaying source work."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lightgraph.construction.mcp_server import serve_local_tools
from lightgraph.construction.tools import ConstructionTools
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.discovery.token_estimation import estimate_tokens
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.tests.test_construction_mcp_lifecycle import request, rpc_result


def tool_result(url, token, name, arguments):
    envelope = rpc_result(request(url, token, 'tools/call', {
        'name': name, 'arguments': arguments}))
    assert not envelope['isError'], envelope
    text = ''.join(block['text'] for block in envelope['content'] if block['type'] == 'text')
    value = envelope.get('structuredContent') or json.loads(text)
    limit = value.get('estimated_token_limit') or value.get('result_presentation', {}).get('estimated_token_limit')
    if limit and not value.get('presentation_error'):
        # Test the actual pretty-printed MCP text, not an artificially compact
        # JSON serialization with fewer whitespace tokens.
        assert estimate_tokens(text) <= limit
    return value


def test_http_result_sections_preserve_sample_coverage_and_do_not_query_again(tmp_path):
    table = TableSpec(source_id='s', name='items', columns=[
        ColumnSpec(name='code'), ColumnSpec(name='name')])
    values = [(str(i), f'条目{i}:' + '用于核对来源与上下文' * 80) for i in range(40)]
    config = RunConfig(sources=[], model={'mode': 'in_environment'})
    with MemorySource([table], {table.qualified_name: values}) as adapter:
        tools = ConstructionTools(config, tmp_path/'work', adapters=[adapter])
        try:
            with serve_local_tools(tools) as (url, token):
                overview = tool_result(url, token, 'inspect_source', {
                    'table': 's.public.items', 'operation': 'execute_readonly_sql',
                    'arguments': {'sql': 'SELECT code, name FROM public.items'}})
                assert overview['status'] == 'ok'
                assert overview['result_presentation']['summary_only']
                assert overview['result_presentation']['presentation_complete'] is False
                assert overview['result_presentation']['source_complete'] is False
                assert overview['coverage']['truncated'] is True
                allowance = overview['result_presentation']['estimated_token_limit']
                assert estimate_tokens(json.dumps(overview, ensure_ascii=False,
                    sort_keys=True, separators=(',', ':'))) <= allowance
                usage = tools.query_budget.snapshot()
                parts, page, revision = [], 0, None
                while True:
                    fragment = tool_result(url, token, 'read_material', {
                        'name': overview['result_ref'], 'section': '/data/rows/0', 'page': page})
                    assert fragment['status'] == 'ok', fragment
                    assert fragment['requires_database'] is False
                    assert fragment['source_complete'] is False
                    revision = revision or fragment['material_revision']
                    assert fragment['material_revision'] == revision
                    assert estimate_tokens(json.dumps(fragment, ensure_ascii=False,
                        sort_keys=True, separators=(',', ':'))) <= allowance
                    parts.append(fragment['content'])
                    if fragment['next_page'] is None:
                        break
                    page = fragment['next_page']
                assert json.loads(''.join(parts)) == list(values[0])
                current = tools.query_budget.snapshot()
                assert {k: current[k] for k in ('queries', 'returned_rows', 'returned_bytes')} == {
                    k: usage[k] for k in ('queries', 'returned_rows', 'returned_bytes')}
                denied = tool_result(url, token, 'read_material', {
                    'name': overview['result_ref'], 'section': '/missing'})
                assert denied['status'] == 'error' and denied['code'] == 'unknown_result_section'
        finally:
            tools.close()


@pytest.mark.skipif(os.environ.get('LIGHTGRAPH_TEST_HARNESS') != '1',
                   reason='requires the installed native Harness')
def test_native_requests_use_result_reference_then_only_the_requested_section(tmp_path):
    from lightgraph.construction.harness import HarnessOptions, run_goal
    from lightgraph.tests.test_construction_harness_integration import respond

    table = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name='code'), ColumnSpec(name='name')])
    values = [(str(i), f'条目{i}:' + '只按当前问题读取数据' * 80) for i in range(40)]
    requests, returned, errors = [], [], []

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            try:
                if len(requests) == 1:
                    name, arguments = 'inspect_source', {'table': 's.public.items',
                        'operation': 'execute_readonly_sql',
                        'arguments': {'sql': 'SELECT code, name FROM public.items'}}
                else:
                    message = next(m for m in reversed(body['messages']) if m['role'] == 'tool')
                    content = message['content']
                    if isinstance(content, list):
                        content = ''.join(b.get('text', '') for b in content)
                    value = json.loads(content)
                    returned.append(value)
                    if len(requests) == 2:
                        name, arguments = 'read_material', {'name': value['result_ref'],
                            'section': '/data/rows/0'}
                    else:
                        assert json.loads(value['content']) == list(values[0])
                        respond(self, {'role': 'assistant', 'content': 'Requested row inspected.'}, 'stop')
                        return
                respond(self, {'role': 'assistant', 'tool_calls': [{'index': 0,
                    'id': f'call-{len(requests)}', 'type': 'function', 'function': {
                        'name': 'mcp__graph__' + name, 'arguments': json.dumps(arguments)}}]}, 'tool_calls')
            except Exception as exc:
                errors.append(repr(exc))
                respond(self, {'role': 'assistant', 'content': 'Test failed.'}, 'stop')

    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    config = RunConfig(sources=[], model={'mode': 'in_environment'})
    try:
        with MemorySource([table], {table.qualified_name: values}) as adapter:
            tools = ConstructionTools(config, tmp_path/'work', adapters=[adapter])
            try:
                path = tmp_path/'config.yaml'
                path.write_text('goal: inspect a sample and retrieve only one row\n')
                with serve_local_tools(tools) as (url, token):
                    outcome = run_goal('Inspect a sample, then read only row zero from its retained result.',
                        path, tmp_path/'runtime', HarnessOptions(model='local-test',
                            base_url=f'http://127.0.0.1:{model.server_port}/v1',
                            max_calls=6, max_run_seconds=45),
                        mcp_url=url, mcp_token_env='GRAPH_TEST_TOKEN',
                        environment={'GRAPH_TEST_TOKEN': token, 'OPENAI_API_KEY': 'local-test-only'})
                assert errors == []
                assert outcome['finish_reason'] == 'completed'
                assert len(requests) == 3
                assert returned[0]['result_presentation']['summary_only']
                assert returned[0]['coverage']['truncated'] is True
                assert returned[1]['requires_database'] is False
                assert values[8][1] not in json.dumps(requests, ensure_ascii=False)
                assert returned[1]['query_budget']['queries'] == returned[0]['query_budget']['queries']
            finally:
                tools.close()
    finally:
        model.shutdown()
        model.server_close()
        thread.join(timeout=5)
