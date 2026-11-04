"""Refresh/restart credential reuse without plaintext or cross-project leakage."""
import os
import stat

import pytest

from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.tests.web_client import RevisionClient as TestClient
from lightgraph.web.app import create_app
from lightgraph.web.saved_connections import SavedConnections


KEY = 'PRIVATE_MODEL_KEY_9b4b65d74_refresh'


def config(**updates):
    model = {'base_url': ' https://model.example/v1/chat/completions/ ',
             'model': ' test-model ', 'mode': 'metadata_only'}
    model.update(updates)
    return RunConfig(sources=[], model=model)


def session(client, pid='alpha'):
    return {'X-Workbench-Session': client.post(f'/api/projects/{pid}/session').json()['session_id']}


def configure(client):
    client.post('/api/projects', json={'name': 'alpha', 'project_id': 'alpha'})
    value = client.get('/api/projects/alpha/config').json()
    value['model'] = config().model.model_dump(mode='json')
    assert client.put('/api/projects/alpha/config', json=value).status_code == 200


def test_restart_and_fresh_browser_restore_key_without_returning_secret(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'
    with TestClient(create_app(root)) as first:
        configure(first)
        old_headers = session(first)
        response = first.put('/api/projects/alpha/credentials/model', headers=old_headers, json={'api_key': KEY})
        assert response.status_code == 200 and response.json()['model']
        for route in ('/config', '/config/export', '/credentials'):
            assert KEY not in first.get('/api/projects/alpha' + route, headers=old_headers).text
    with TestClient(create_app(root)) as second:
        # The prior page token expires with the old server, but a fresh page
        # session can use the saved key without ever receiving its value.
        assert second.get('/api/projects/alpha/credentials', headers=old_headers).status_code == 400
        fresh = session(second)
        assert second.get('/api/projects/alpha/credentials', headers=fresh).json()['model']
        sent = []
        monkeypatch.setattr(second.app.state.manager, 'start', lambda pid, **kw: sent.append(kw) or {'run_id': 'fake'})
        assert second.post('/api/projects/alpha/runs', headers=fresh).status_code == 202
        assert sent == [{'model_api_key': KEY}]
    for path in root.rglob('*'):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes()
    assert (root/'alpha/.model-credentials/model.enc').exists()


def test_forget_revokes_every_tab_and_does_not_revive_after_restart(tmp_path):
    root = tmp_path/'workspace'
    with TestClient(create_app(root)) as client:
        configure(client)
        first, other = session(client), session(client)
        client.put('/api/projects/alpha/credentials/model', headers=first, json={'api_key': KEY})
        assert client.get('/api/projects/alpha/credentials', headers=other).json()['model']
        assert client.delete('/api/projects/alpha/credentials/model', headers=first).status_code == 200
        for headers in (first, other, session(client)):
            assert not client.get('/api/projects/alpha/credentials', headers=headers).json()['model']
    with TestClient(create_app(root)) as client:
        assert not client.get('/api/projects/alpha/credentials', headers=session(client)).json()['model']
    assert not (root/'alpha/.model-credentials/model.enc').exists()


@pytest.mark.parametrize('changed', [{'base_url': 'https://other.example/v1'}, {'model': 'other-model'}])
def test_normalized_endpoint_binding_survives_limits_but_rejects_other_destinations(tmp_path, changed):
    saved = SavedConnections(tmp_path)
    saved.save_model('alpha', config(), KEY)
    assert saved.load_model('alpha', config(base_url='https://model.example/v1', max_calls=1000)) == KEY
    assert saved.load_model('alpha', config(**changed)) is None
    target = tmp_path/'beta/.model-credentials/model.enc'
    target.parent.mkdir(parents=True)
    target.write_bytes((tmp_path/'alpha/.model-credentials/model.enc').read_bytes())
    assert saved.load_model('beta', config()) is None


@pytest.mark.parametrize('damage', ['missing_key', 'corrupt_key', 'corrupt_profile'])
def test_damaged_profile_does_not_block_settings_and_can_be_cleared(tmp_path, damage, monkeypatch):
    root = tmp_path/'workspace'
    with TestClient(create_app(root)) as client:
        configure(client)
        headers = session(client)
        client.put('/api/projects/alpha/credentials/model', headers=headers, json={'api_key': KEY})
        if damage == 'missing_key':
            (root/'.connection-key').unlink()
        else:
            path = root/'.connection-key' if damage == 'corrupt_key' else root/'alpha/.model-credentials/model.enc'
            path.write_bytes(b'broken encrypted content')
        result = client.get('/api/projects/alpha/credentials', headers=session(client))
        assert result.status_code == 200 and not result.json()['model']
        assert result.json()['model_error'].startswith('saved_connection_')
        assert client.get('/api/projects/alpha/config').status_code == 200
        monkeypatch.setattr('lightgraph.web.app.read_metadata', lambda *args, **kwargs: {'tables': []})
        assert client.post('/api/projects/alpha/metadata/refresh', headers=headers).status_code == 200
        sent = []
        monkeypatch.setattr(client.app.state.manager, 'start', lambda pid, **kw: sent.append(kw) or {'run_id': 'fake'})
        assert client.post('/api/projects/alpha/runs', headers=headers).status_code in (400, 404)
        assert sent == []
        # No silent key rotation while the encrypted model profile exists.
        if damage != 'corrupt_profile':
            assert client.put('/api/projects/alpha/credentials/model', headers=headers, json={'api_key': 'replacement'}).status_code in (400, 404)
        assert client.delete('/api/projects/alpha/credentials/model', headers=headers).status_code == 200
        assert not client.get('/api/projects/alpha/credentials', headers=headers).json()['model']
        assert client.put('/api/projects/alpha/credentials/model', headers=headers, json={'api_key': KEY}).status_code == 200


def test_replacing_key_is_atomic_and_files_are_private(tmp_path, monkeypatch):
    saved = SavedConnections(tmp_path)
    saved.save_model('alpha', config(), KEY)
    import lightgraph.web.saved_connections as storage
    original = storage._atomic_write
    def failed_write(path, data):
        if path.suffix == '.enc':
            raise OSError('PRIVATE_DETAIL')
        original(path, data)
    monkeypatch.setattr(storage, '_atomic_write', failed_write)
    with pytest.raises(LightGraphError, match='^saved_connection_storage_failed$'):
        saved.save_model('alpha', config(), 'new-private-key')
    assert saved.load_model('alpha', config()) == KEY
    if os.name == 'posix':
        assert stat.S_IMODE((tmp_path/'.connection-key').stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path/'alpha/.model-credentials/model.enc').stat().st_mode) == 0o600


def test_project_deletion_removes_saved_model_key(tmp_path):
    root = tmp_path/'workspace'
    with TestClient(create_app(root)) as client:
        configure(client)
        headers = session(client)
        client.put('/api/projects/alpha/credentials/model', headers=headers, json={'api_key': KEY})
        assert client.delete('/api/projects/alpha', headers=headers).status_code == 200
        configure(client)
        assert not client.get('/api/projects/alpha/credentials', headers=session(client)).json()['model']


def test_server_environment_presence_is_distinct_and_never_echoes_key(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_MODEL_CONFIGURED_KEY', KEY)
    with TestClient(create_app(tmp_path/'workspace')) as client:
        configure(client)
        value = client.get('/api/projects/alpha/config').json()
        value['model']['api_key_env'] = 'TEST_MODEL_CONFIGURED_KEY'
        assert client.put('/api/projects/alpha/config', json=value).status_code == 200
        headers = session(client)
        response = client.get('/api/projects/alpha/credentials', headers=headers)
        assert response.json() == {'model': False, 'database_sources': [], 'model_environment': True}
        assert KEY not in response.text
        assert client.delete('/api/projects/alpha/credentials/model', headers=headers).status_code == 200
        assert client.get('/api/projects/alpha/credentials', headers=headers).json()['model_environment']
        monkeypatch.delenv('TEST_MODEL_CONFIGURED_KEY')
        assert not client.get('/api/projects/alpha/credentials', headers=headers).json().get('model_environment')
