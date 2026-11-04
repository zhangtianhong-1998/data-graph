"""Configuration failures identify safe fields without echoing submitted values."""
from fastapi.testclient import TestClient
import pytest

from lightgraph.web.app import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path/'workspace', frontend_dir=tmp_path/'missing')) as client:
        client.post('/api/projects', json={'name': 'diagnostics', 'project_id': 'p'})
        yield client


def test_invalid_model_reports_field_and_preserves_config(client):
    current = client.get('/api/projects/p/config')
    value = current.json()
    value['model']['max_output_tokens'] = 'PRIVATE_BAD_TOKEN_VALUE'
    response = client.put('/api/projects/p/config', json=value, headers={'If-Match': current.headers['etag']})
    assert response.status_code == 400
    assert response.json()['error']['diagnostics'] == [
        {'path': 'model.max_output_tokens', 'type': 'int_parsing', 'hint': '请填写整数。'}]
    assert 'PRIVATE_BAD_TOKEN_VALUE' not in response.text
    assert client.get('/api/projects/p/config').json() == current.json()
    assert client.get('/api/projects/p/config').headers['etag'] == current.headers['etag']


def test_unknown_config_key_is_not_echoed_as_a_path(client):
    current = client.get('/api/projects/p/config')
    value = current.json()
    value['model']['SECRET_UNRECOGNIZED_FIELD'] = 'SECRET_VALUE'
    response = client.put('/api/projects/p/config', json=value, headers={'If-Match': current.headers['etag']})
    assert response.json()['error']['diagnostics'][0]['path'] == 'model.<field>'
    assert 'SECRET' not in response.text


def test_window_constraint_is_actionable_in_both_editors(client):
    current = client.get('/api/projects/p/config')
    value = current.json()
    value['model'].update(max_output_tokens=100, context_window_tokens=100)
    for path, body in [('/config', value), ('/model-limits', {'max_output_tokens': 100, 'context_window_tokens': 100})]:
        response = client.put('/api/projects/p'+path, json=body, headers={'If-Match': current.headers['etag']})
        assert response.status_code == 400
        assert response.json()['error']['diagnostics'][0]['hint'] == '总上下文窗口须大于最大输出 tokens。'
