"""Slow graph reads release the project lock without returning revoked data."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from lightgraph.web.app import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / 'workspace', frontend_dir=tmp_path / 'missing')) as client:
        assert client.post('/api/projects', json={'project_id': 'p', 'name': 'concurrent'}).status_code == 201
        yield client


def slow_graph(monkeypatch, endpoint):
    entered, release = Event(), Event()
    observed = []

    def read(config, **kwargs):
        observed.append(config.model_copy(deep=True))
        entered.set()
        assert release.wait(10), 'test did not release the graph read'
        return {'nodes': [{'id': 'visible-before-change', 'label': 'OLD_AUTHORIZED_VALUE'}]}

    monkeypatch.setattr('lightgraph.web.construction_graph.available', lambda config: True)
    if endpoint == '/graph?layer=metadata':
        monkeypatch.setattr('lightgraph.web.execution.read_metadata',
                            lambda workspace, pid, *, config, **kw: read(config, **kw))
    else:
        target = 'graph_details' if '/details' in endpoint else 'graph_page'
        monkeypatch.setattr('lightgraph.web.construction_graph.' + target, read)
    return entered, release, observed


@pytest.mark.parametrize('endpoint', ['/graph', '/graph/details?entity_id=a', '/graph?layer=metadata'])
def test_slow_graph_does_not_block_configuration_or_credentials(client, monkeypatch, endpoint):
    entered, release, observed = slow_graph(monkeypatch, endpoint)
    with ThreadPoolExecutor(max_workers=2) as pool:
        graph = pool.submit(client.get, '/api/projects/p' + endpoint)
        try:
            assert entered.wait(5)
            # Must finish while graph parsing is still blocked, rather than
            # measuring an unreliable timing difference on a fast fixture.
            config = pool.submit(client.get, '/api/projects/p/config').result(timeout=5)
            assert config.status_code == 200
            credentials = pool.submit(client.get, '/api/projects/p/credentials').result(timeout=5)
            assert credentials.status_code == 200
            updated = config.json()
            updated['model']['max_output_tokens'] = 65536
            saved = pool.submit(client.put, '/api/projects/p/config', json=updated,
                                headers={'If-Match': config.headers['etag']}).result(timeout=5)
            assert saved.status_code == 200, saved.text
            assert not graph.done()
        finally:
            release.set()
        response = graph.result(timeout=5)
    assert observed[0].resolved_model.max_output_tokens != 65536
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'graph_read_conflict'
    assert 'OLD_AUTHORIZED_VALUE' not in response.text


@pytest.mark.parametrize('endpoint', ['/graph', '/graph/details?entity_id=a'])
def test_restricting_scopes_during_read_discards_the_old_authorized_result(client, monkeypatch, endpoint):
    entered, release, observed = slow_graph(monkeypatch, endpoint)
    with ThreadPoolExecutor(max_workers=2) as pool:
        graph = pool.submit(client.get, '/api/projects/p' + endpoint)
        try:
            assert entered.wait(5)
            current = pool.submit(client.get, '/api/projects/p/config').result(timeout=5)
            limited = current.json()
            limited['scopes'] = []
            changed = pool.submit(client.put, '/api/projects/p/config', json=limited,
                                  headers={'If-Match': current.headers['etag']}).result(timeout=5)
            assert changed.status_code == 200, changed.text
        finally:
            release.set()
        response = graph.result(timeout=5)
    assert observed[0].scopes == ['public']
    assert response.status_code == 409 and 'OLD_AUTHORIZED_VALUE' not in response.text


@pytest.mark.parametrize('recreate', [False, True])
def test_deleted_or_recreated_project_never_receives_old_graph(client, monkeypatch, recreate):
    entered, release, observed = slow_graph(monkeypatch, '/graph')
    original = client.get('/api/projects/p/config')
    session = client.post('/api/projects/p/session').json()['session_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        graph = pool.submit(client.get, '/api/projects/p/graph')
        try:
            assert entered.wait(5)
            deleted = pool.submit(client.delete, '/api/projects/p',
                                  headers={'If-Match': original.headers['etag'],
                                           'X-Workbench-Session': session}).result(timeout=5)
            assert deleted.status_code == 200, deleted.text
            if recreate:
                created = client.post('/api/projects', json={'project_id': 'p', 'name': 'replacement'})
                assert created.status_code == 201
                assert client.get('/api/projects/p/config').headers['etag'] == original.headers['etag']
        finally:
            release.set()
        response = graph.result(timeout=5)
    assert response.status_code == (409 if recreate else 404)
    assert 'OLD_AUTHORIZED_VALUE' not in response.text


def test_unchanged_project_returns_graph_without_forcing_retry(client, monkeypatch):
    entered, release, observed = slow_graph(monkeypatch, '/graph')
    release.set()
    response = client.get('/api/projects/p/graph')
    assert response.status_code == 200
    assert response.json()['nodes'][0]['label'] == 'OLD_AUTHORIZED_VALUE'
