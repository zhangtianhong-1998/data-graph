"""Saved model settings reach real workers without restarting the workbench."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from lightgraph.web.app import create_app


@pytest.fixture
def local_model():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == '/v1/chat/completions'
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            # Leave work pending so a second run asks the model again. This
            # fixture verifies configuration delivery, not graph completeness.
            result = {'choices': [{'message': {'role': 'assistant', 'content': json.dumps({
                'action': 'finish_discovery', 'pending': True})}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 40, 'completion_tokens': 20}}
            body = json.dumps(result).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def wait_finished(client, run_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run = client.get(f'/api/projects/settings/runs/{run_id}').json()
        if run['status'] not in {'queued', 'running'}:
            return run
        time.sleep(.03)
    pytest.fail('The local model worker did not finish')


@pytest.mark.parametrize('standalone_agent', [False, True])
def test_saved_limits_reach_next_worker_and_request_without_restart(
        standalone_agent, tmp_path, monkeypatch, local_model):
    endpoint, model_requests = local_model
    monkeypatch.setenv('SETTINGS_RUNTIME_TEST_KEY', 'synthetic-settings-runtime-secret')
    limits_versions = [
        {'max_discovery_steps': 12, 'max_prompt_tokens': 100000,
         'max_output_tokens': 2048, 'timeout_seconds': 17.5, 'working_context_tokens': 24576,
         'max_prompt_bytes': 1048576, 'max_response_bytes': 1048576},
        {'max_discovery_steps': 24, 'max_prompt_tokens': 200000,
         'max_output_tokens': 4096, 'timeout_seconds': 29.25, 'working_context_tokens': None,
         'max_prompt_bytes': None, 'max_response_bytes': None},
    ]
    prefix = '/api/projects/settings'
    with TestClient(create_app(tmp_path/'workspace', frontend_dir=tmp_path/'missing')) as client:
        assert client.post('/api/projects', json={
            'name': '配置保存运行验证', 'project_id': 'settings'}).status_code == 201
        workspace = client.app.state.workspace
        workspace.add_file('settings', 'items.csv', 'item_code,item_name\nA,收入\n'.encode('utf-8'))
        config = client.get(prefix+'/config').json()
        config['goal']['purpose'] = '检查指标表的对象模型'
        config['max_discovery_rounds'] = 1
        config['max_identity_reviews'] = 0
        config['max_run_seconds'] = 30
        model = {'mode': 'metadata_only', 'base_url': endpoint, 'model': 'local-settings-fixture',
                 'allow_insecure_http': True, 'api_key_env': 'SETTINGS_RUNTIME_TEST_KEY',
                 'max_calls': 10, 'max_tokens': None, 'retries': 0}
        original = asset = None
        if standalone_agent:
            original = yaml.safe_dump({'framework': 'agentscope', 'name': 'settings-agent',
                                       'llm': model}).encode('utf-8')
            asset = workspace.add_file('settings', 'original-agent.yaml', original)
            config.pop('model', None)
            config['agent_config'] = asset['relative_path']
        else:
            config['model'] = model
        saved = client.put(prefix+'/config', json=config, headers={
            'If-Match': client.get(prefix+'/config').headers['etag']})
        assert saved.status_code == 200, saved.text

        first_trace_path = None
        first_trace_bytes = None
        for desired in limits_versions:
            before = client.get(prefix+'/model-limits')
            saved = client.put(prefix+'/model-limits', json=desired,
                               headers={'If-Match': before.headers['etag']})
            assert saved.status_code == 200, saved.text
            assert saved.headers['etag'] != before.headers['etag']
            assert all(saved.json()[key] == value for key, value in desired.items())
            assert client.get(prefix+'/model-limits').json() == saved.json()
            # The prior revision cannot start a run against a changed config.
            assert client.post(prefix+'/runs', json={}, headers={
                'If-Match': before.headers['etag']}).status_code == 409
            request_count = len(model_requests)
            started = client.post(prefix+'/runs', json={}, headers={'If-Match': saved.headers['etag']})
            assert started.status_code == 202, started.text
            run_id = started.json()['run_id']
            run = wait_finished(client, run_id)
            assert run['error_code'] is None, run
            assert run['status'] == 'partial', run  # Intentional pending response above.
            assert str(run['config_revision']) == saved.headers['etag'].strip('"')
            actual_requests = model_requests[request_count:]
            assert actual_requests, 'The saved settings must reach an actual HTTP model request'
            for request in actual_requests:
                assert request['max_tokens'] == desired['max_output_tokens']
                packet = json.loads(next(m['content'] for m in request['messages'] if m['role'] == 'user'))
                assert packet['runtime_control']['max_turns'] == desired['max_discovery_steps']

            trace_path = workspace.project_path('settings')/'runs'/(run_id+'.trace.jsonl')
            events = [json.loads(line) for line in trace_path.read_text(encoding='utf-8').splitlines()]
            effective = next(e['data']['effective_model'] for e in events if e['kind'] == 'run_configuration')
            assert all(effective[key] == value for key, value in desired.items())
            assert effective['max_calls'] == 10 and effective['max_tokens'] is None
            for event in (e['data'] for e in events if e['kind'] == 'model_request'):
                assert event['timeout_seconds'] == desired['timeout_seconds']
                assert event['max_discovery_steps'] == desired['max_discovery_steps']
            if first_trace_path is None:
                first_trace_path, first_trace_bytes = trace_path, trace_path.read_bytes()
            else:
                assert first_trace_path.read_bytes() == first_trace_bytes
            if standalone_agent:
                assert workspace.file_path('settings', asset['file_id']).read_bytes() == original
                assert workspace.get_config('settings')['agent_config'] != asset['relative_path']
        assert len(client.get(prefix+'/runs').json()['runs']) == 2
