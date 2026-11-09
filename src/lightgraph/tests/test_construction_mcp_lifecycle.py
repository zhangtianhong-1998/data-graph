"""Exercise real local HTTP requests, cancellation and the workspace writer."""
import threading
import time

import httpx

from lightgraph.construction.mcp_server import serve_local_tools


def request(url, token, method, params=None, *, timeout=3):
    return httpx.post(url, trust_env=False, headers={'Authorization': 'Bearer ' + token,
        'Accept': 'application/json, text/event-stream'},
        json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}, timeout=timeout)


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
        assert 'tools' in response.json()['result']
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
        assert response.json()['result']['isError'] is False
    assert events == []
