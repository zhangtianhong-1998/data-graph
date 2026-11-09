"""Exercise real local HTTP requests, cancellation and the workspace writer."""
import json
import threading
import time

import httpx

from lightgraph.construction.mcp_server import serve_local_tools


def request(url, token, method, params=None, *, timeout=3):
    return httpx.post(url, trust_env=False, headers={'Authorization': 'Bearer ' + token,
        'Accept': 'application/json, text/event-stream'},
        json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}, timeout=timeout)


def rpc_result(response):
    """Decode test responses; production parsing belongs to the native MCP SDK."""
    assert response.headers['content-type'].startswith('text/event-stream')
    messages = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert len(messages) == 1
    return messages[0]['result']


def test_slow_validation_keeps_http_responsive_and_serializes_disconnected_calls():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls, errors = [], []

    class Tools:
        def request_shutdown(self): release.set()
        def call(self, name, **arguments):
            calls.append(name)
            if name == 'validate_graph_draft':
                started.set()
                assert release.wait(5)
                finished.set()
            return {'status': 'ok'}

    with serve_local_tools(Tools()) as (url, token):
        def validate():
            try:
                request(url, token, 'tools/call', {'name': 'validate_graph_draft',
                    'arguments': {'draft_id': 'test'}}, timeout=.15)
            except httpx.ReadTimeout:
                errors.append('timed_out')
        worker = threading.Thread(target=validate)
        worker.start()
        assert started.wait(3)
        # A long synchronous projection must not block the server's event loop.
        response = request(url, token, 'tools/list', timeout=1)
        assert response.status_code == 200
        assert 'tools' in rpc_result(response)
        worker.join(3)
        assert errors == ['timed_out'] and not finished.is_set()
        second = threading.Thread(target=lambda: request(url, token, 'tools/call',
            {'name': 'describe_sources', 'arguments': {}}))
        second.start()
        time.sleep(.05)
        assert calls == ['validate_graph_draft']  # caller timeout did not release its writer
        release.set()
        second.join(3)
        assert not second.is_alive()
        assert calls == ['validate_graph_draft', 'describe_sources']
        assert finished.is_set()


def test_server_exit_waits_for_validation_before_owner_closes_sources():
    started, stop, finished = threading.Event(), threading.Event(), threading.Event()
    events = []

    class Tools:
        def request_shutdown(self):
            events.append('stop_requested')
            stop.set()
        def call(self, name, **arguments):
            started.set()
            assert stop.wait(5)
            time.sleep(.15)  # transaction rollback and atomic artifact cleanup
            events.append('validation_settled')
            finished.set()
            return {'status': 'error', 'code': 'construction_cancelled'}

    with serve_local_tools(Tools()) as (url, token):
        def validate():
            try:
                request(url, token, 'tools/call', {'name': 'validate_graph_draft',
                    'arguments': {'draft_id': 'test'}}, timeout=.1)
            except httpx.ReadTimeout:
                pass
        worker = threading.Thread(target=validate)
        worker.start()
        assert started.wait(3)
        worker.join(3)
    events.append('owner_closed_sources')
    assert finished.is_set()
    assert events == ['stop_requested', 'validation_settled', 'owner_closed_sources']


def test_idle_shutdown_does_not_cancel_borrowed_source_budgets():
    events = []
    class Tools:
        def request_shutdown(self): events.append('cancel')
        def call(self, tool_name, **arguments): return {'status': 'ok'}
    with serve_local_tools(Tools()) as (url, token):
        response = request(url, token, 'tools/call', {'name': 'read_material',
            'arguments': {'name': 'draft:test', 'page': 1}})
        assert rpc_result(response)['isError'] is False
    assert events == []


def test_native_sse_sends_headers_and_heartbeat_before_validation_finishes(monkeypatch):
    from sse_starlette.sse import EventSourceResponse
    # Speed up only the heartbeat interval; the actual >300s native kernel test
    # uses the production interval and unmodified Node fetch defaults.
    monkeypatch.setattr(EventSourceResponse, 'DEFAULT_PING_INTERVAL', .1)
    started, release = threading.Event(), threading.Event()
    calls = []
    class Tools:
        def request_shutdown(self): release.set()
        def call(self, name, **arguments):
            calls.append(name)
            started.set()
            assert release.wait(5)
            return {'status': 'ok', 'validation_id': 'validated-once'}

    with serve_local_tools(Tools()) as (url, token):
        assert request(url, 'invalid-token', 'tools/list').status_code == 401
        try:
            with httpx.stream('POST', url, trust_env=False, timeout=2,
                headers={'Authorization': 'Bearer '+token, 'Accept': 'application/json, text/event-stream'},
                json={'jsonrpc':'2.0', 'id':1, 'method':'tools/call',
                      'params':{'name':'validate_graph_draft', 'arguments':{'draft_id':'test'}}}) as response:
                # Entering this block requires headers, while the tool is still blocked.
                assert response.status_code == 200
                assert response.headers['content-type'].startswith('text/event-stream')
                assert started.wait(1) and not release.is_set()
                lines = response.iter_lines()
                assert next(lines).startswith(': ping')
                release.set()
                messages = [json.loads(line[6:]) for line in lines if line.startswith('data: ')]
                assert len(messages) == 1
                assert messages[0]['result']['isError'] is False
                assert 'validated-once' in json.dumps(messages[0])
            assert 'tools' in rpc_result(request(url, token, 'tools/list'))
        finally:
            release.set()
    assert calls == ['validate_graph_draft']
