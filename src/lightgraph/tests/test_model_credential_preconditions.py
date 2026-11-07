"""Late credential mutations must never target a newer model configuration."""
import pytest
import yaml
from fastapi.testclient import TestClient

from lightgraph.web.app import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / 'workspace')) as client:
        client.post('/api/projects', json={'name': 'test', 'project_id': 'p'})
        token = client.post('/api/projects/p/session').json()['session_id']
        client.headers['X-Workbench-Session'] = token
        yield client


PREFIX = '/api/projects/p'


def configure(client, *, endpoint='https://a.example/v1', model='model-a'):
    current = client.get(PREFIX + '/config')
    config = current.json()
    config['model'].update(base_url=endpoint, model=model, mode='metadata_only')
    saved = client.put(PREFIX + '/config', json=config, headers={'If-Match': current.headers['etag']})
    assert saved.status_code == 200
    return saved


def key_payload(config, key):
    model = config.json()['model']
    return {'api_key': key, 'expected_model': {'base_url': model['base_url'], 'model': model['model']}}


def saved_key(client):
    return client.app.state.saved_connections.load_model('p', client.app.state.workspace.resolved_config('p'))


def test_late_key_cannot_replace_the_key_for_a_new_endpoint(client):
    original = configure(client)
    newer = configure(client, endpoint='https://b.example/v1', model='model-b')
    assert client.put(PREFIX + '/credentials/model', json=key_payload(newer, 'synthetic-key-b'),
                      headers={'If-Match': newer.headers['etag']}).status_code == 200
    result = client.put(PREFIX + '/credentials/model', json=key_payload(original, 'synthetic-key-a'),
                        headers={'If-Match': original.headers['etag']})
    assert result.status_code == 409
    assert result.json()['error']['code'] == 'config_revision_conflict'
    assert saved_key(client) == 'synthetic-key-b'


@pytest.mark.parametrize('mismatch', ['base_url', 'model'])
def test_current_revision_does_not_authorize_a_different_model_binding(client, mismatch):
    config = configure(client)
    good = key_payload(config, 'synthetic-current-key')
    assert client.put(PREFIX + '/credentials/model', json=good,
                      headers={'If-Match': config.headers['etag']}).status_code == 200
    wrong = key_payload(config, 'synthetic-stale-key')
    wrong['expected_model'][mismatch] = 'https://other.example/v1' if mismatch == 'base_url' else 'other-model'
    result = client.put(PREFIX + '/credentials/model', json=wrong,
                        headers={'If-Match': config.headers['etag']})
    assert result.status_code == 409
    assert result.json()['error']['code'] == 'model_credential_binding_conflict'
    assert saved_key(client) == 'synthetic-current-key'


@pytest.mark.parametrize('method', ['put', 'delete'])
def test_credential_mutations_require_configuration_revision(client, method):
    config = configure(client)
    payload = key_payload(config, 'synthetic-current-key')
    assert client.put(PREFIX + '/credentials/model', json=payload,
                      headers={'If-Match': config.headers['etag']}).status_code == 200
    options = {'json': key_payload(config, 'synthetic-replacement')} if method == 'put' else {}
    result = getattr(client, method)(PREFIX + '/credentials/model', **options)
    assert result.status_code == 428
    assert saved_key(client) == 'synthetic-current-key'


def test_credential_write_requires_an_explicit_model_binding(client):
    config = configure(client)
    result = client.put(PREFIX + '/credentials/model', json={'api_key': 'synthetic-key'},
                        headers={'If-Match': config.headers['etag']})
    assert result.status_code == 422
    assert saved_key(client) is None


def test_late_clear_keeps_new_endpoint_key_but_current_clear_succeeds(client):
    original = configure(client)
    newer = configure(client, endpoint='https://b.example/v1', model='model-b')
    assert client.put(PREFIX + '/credentials/model', json=key_payload(newer, 'synthetic-key-b'),
                      headers={'If-Match': newer.headers['etag']}).status_code == 200
    result = client.delete(PREFIX + '/credentials/model', headers={'If-Match': original.headers['etag']})
    assert result.status_code == 409
    assert saved_key(client) == 'synthetic-key-b'
    assert client.delete(PREFIX + '/credentials/model', headers={'If-Match': newer.headers['etag']}).status_code == 200
    assert saved_key(client) is None


def test_normalized_binding_from_saved_config_is_accepted(client):
    config = configure(client, endpoint=' https://a.example/v1/chat/completions/ ', model=' model-a ')
    result = client.put(PREFIX + '/credentials/model', json=key_payload(config, 'synthetic-key'),
                        headers={'If-Match': config.headers['etag']})
    assert result.status_code == 200
    assert saved_key(client) == 'synthetic-key'


def test_agent_file_change_is_rejected_even_without_a_configuration_revision_change(client):
    configure(client)
    before = client.get(PREFIX + '/config')
    agent = {'name': 'test-agent', 'llm': {'mode': 'metadata_only',
        'base_url': ' https://a.example/v1/chat/completions/ ', 'model': ' model-a '}}
    uploaded = client.post(PREFIX + '/files', files={'file': ('agent.yaml', yaml.safe_dump(agent).encode())},
                           headers={'If-Match': before.headers['etag']})
    assert uploaded.status_code == 201
    current = client.get(PREFIX + '/config')
    config = current.json()
    config.pop('model', None)
    config['agent_config'] = uploaded.json()['relative_path']
    saved = client.put(PREFIX + '/config', json=config, headers={'If-Match': current.headers['etag']})
    assert saved.status_code == 200
    payload = {'api_key': 'synthetic-agent-key', 'expected_model': {'base_url': 'https://a.example/v1', 'model': 'model-a'}}
    assert client.put(PREFIX + '/credentials/model', json=payload,
                      headers={'If-Match': saved.headers['etag']}).status_code == 200
    workspace = client.app.state.workspace
    profile = workspace.project_path('p') / '.model-credentials' / 'model.enc'
    original_profile = profile.read_bytes()
    agent['llm']['base_url'] = 'https://b.example/v1'
    workspace.file_path('p', uploaded.json()['file_id']).write_text(yaml.safe_dump(agent), encoding='utf-8')
    assert client.get(PREFIX + '/config').headers['etag'] == saved.headers['etag']
    result = client.put(PREFIX + '/credentials/model', json={**payload, 'api_key': 'synthetic-late-key'},
                        headers={'If-Match': saved.headers['etag']})
    assert result.status_code == 409
    assert result.json()['error']['code'] == 'model_credential_binding_conflict'
    assert profile.read_bytes() == original_profile
