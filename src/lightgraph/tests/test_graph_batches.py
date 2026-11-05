"""All authorized nodes and edges survive transport boundaries and large scenes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from lightgraph.construction.access import access_fingerprint
from lightgraph.construction.workspace import GraphWorkspace
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.web.app import create_app
from lightgraph.web.graph_batches import read_graph_batch


def publish_fixture(config, count=5001):
    """Synthetic display fixture only; no source connections or model calls."""
    workspace = GraphWorkspace(Path(config.output_dir) / 'construction', 'synthetic-canvas-check')
    draft = {'draft_id': 'synthetic', 'entities': [{'type': 'Item', 'key_names': ['code'],
        'mappings': [{'source_id': 'synthetic', 'table': 'items', 'key_columns': ['code']}]}]}
    record = workspace.save_draft(draft)
    nodes = [{'id': f'n{i:05}', 'type': 'Item', 'identity': {'code': f'ITEM-{i:05}'},
              'supports': [{'scope_ref': 'public', 'label': f'模拟节点 {i}', 'aliases': []}]} for i in range(count)]
    # Star makes the whole graph a two-hop view. Chain edges test connections
    # between non-center nodes, including 999 -> 1000 across transport pages.
    pairs = [(0, i) for i in range(1, count)] + [(i, i + 1) for i in range(1, count - 1)]
    edges = [{'id': f'e{i:05}', 'source': f'n{source:05}', 'target': f'n{target:05}',
              'type': 'Connects', 'supports': [{'scope_ref': 'public'}]} for i, (source, target) in enumerate(pairs)]
    nodes.append({'id': 'private-node', 'type': 'Secret', 'identity': {'code': 'SECRET'},
                  'supports': [{'scope_ref': 'private', 'label': 'PRIVATE_SECRET'}]})
    edges.append({'id': 'private-edge', 'source': 'n00000', 'target': 'private-node',
                  'type': 'SecretLink', 'supports': [{'scope_ref': 'private'}]})
    graph = {'nodes': nodes, 'edges': edges, 'definitions': {'entities': [
        {'type': 'Item', 'description': '模拟对象'}, {'type': 'Secret', 'description': 'PRIVATE_TYPE'}],
        'relations': [{'type': 'Connects', 'description': '连接'}, {'type': 'SecretLink'}]},
        'support_visibility_version': 1, 'access_fingerprint': access_fingerprint(config)}
    workspace.record_validation('synthetic', revision=record.revision, passed=True, graph=graph)
    workspace.publish('synthetic', revision=record.revision, graph=graph)
    return count, len(pairs)


@pytest.fixture
def published(tmp_path):
    config = RunConfig(sources=[], output_dir=str(tmp_path / 'output'))
    expected = publish_fixture(config)
    return config, expected


def collect(config, **selection):
    result, version = {'nodes': [], 'edges': []}, ''
    for part in ('nodes', 'edges'):
        offset = 0
        while offset is not None:
            page = read_graph_batch(None, 'p', config=config, part=part, offset=offset,
                                    generation=version, **selection)
            assert len(page[part]) <= 1000
            assert 'PRIVATE' not in str(page)
            assert all('supports' not in item and 'provenance' not in item for item in page[part])
            result[part].extend(page[part]); version = page['generation']; offset = page['next_offset']
    return result, version


def test_full_graph_includes_every_cross_page_edge_and_no_private_supports(published):
    config, (node_count, edge_count) = published
    result, _ = collect(config)
    assert len(result['nodes']) == node_count == 5001
    assert len(result['edges']) == edge_count > 3000
    assert len({node['id'] for node in result['nodes']}) == node_count
    assert len({edge['id'] for edge in result['edges']}) == edge_count
    assert any(edge['source'] == 'n00999' and edge['target'] == 'n01000' for edge in result['edges'])


def test_two_hops_is_complete_above_1000_nodes(published):
    config, expected = published
    result, _ = collect(config, focus='n00000', traversal='all')
    assert (len(result['nodes']), len(result['edges'])) == expected


def test_cursor_rejects_changed_publication_permissions_or_selection(published):
    config, _ = published
    first = read_graph_batch(None, 'p', config=config)
    for change in ({'scopes': []}, {'max_db_queries': 20}):
        with pytest.raises(LightGraphError, match='graph_generation_changed'):
            read_graph_batch(None, 'p', config=config.model_copy(update=change), generation=first['generation'])
    with pytest.raises(LightGraphError, match='graph_generation_changed'):
        read_graph_batch(None, 'p', config=config, generation=first['generation'], focus='n00000')
    publish_fixture(config, 1001)
    with pytest.raises(LightGraphError, match='graph_generation_changed'):
        read_graph_batch(None, 'p', config=config, generation=first['generation'])


@pytest.mark.parametrize('selection', [{'part': 'facts'}, {'limit': 1001}, {'offset': -1}, {'layer': 'all'}, {'hops': 3}])
def test_invalid_batch_selection(tmp_path, selection):
    with pytest.raises(LightGraphError, match='graph_page_invalid'):
        read_graph_batch(None, 'p', config=RunConfig(sources=[], output_dir=str(tmp_path)), **selection)


def test_legacy_batch_uses_support_authorization_and_candidate_selection(tmp_path):
    from lightgraph.tests.test_web_graph_review import seed
    with TestClient(create_app(tmp_path / 'workspace', frontend_dir=tmp_path / 'missing')) as client:
        client.post('/api/projects', json={'project_id': 'p', 'name': 'batch test'})
        seed(client)
        normal = client.get('/api/projects/p/graph/batches').json()
        assert {node['id'] for node in normal['nodes']} == {'a', 'b'}
        candidate = client.get('/api/projects/p/graph/batches?include_candidates=true').json()
        assert {node['id'] for node in candidate['nodes']} == {'a', 'b', 'c'}
        assert 'PRIVATE' not in str([normal, candidate])
        assert client.get('/api/projects/other/graph/batches').status_code == 404


def test_api_discards_batch_when_permissions_change_during_read(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path / 'workspace', frontend_dir=tmp_path / 'missing')) as client:
        client.post('/api/projects', json={'project_id': 'p', 'name': 'batch test'})
        entered, release = Event(), Event()
        def delayed(*args, **kwargs):
            entered.set(); assert release.wait(10)
            return {'nodes': [{'id': 'old', 'label': 'OLD_SCOPE'}], 'edges': []}, 'version'
        monkeypatch.setattr('lightgraph.web.graph_batches._authorized_view', delayed)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(client.get, '/api/projects/p/graph/batches')
            try:
                assert entered.wait(5)
                config = client.get('/api/projects/p/config'); changed = config.json(); changed['scopes'] = []
                assert client.put('/api/projects/p/config', json=changed, headers={'If-Match': config.headers['etag']}).status_code == 200
            finally:
                release.set()
            response = pending.result(timeout=5)
        assert response.status_code == 409 and 'OLD_SCOPE' not in response.text


def test_metadata_batches_request_unbounded_cached_declarations_without_connections(tmp_path, monkeypatch):
    def metadata(*args, **kwargs):
        assert kwargs['full_graph'] is True and not kwargs.get('refresh')
        return {'nodes': [{'id': f'c{i}'} for i in range(1203)], 'edges': []}
    monkeypatch.setattr('lightgraph.web.execution.read_metadata', metadata)
    config = RunConfig(sources=[], output_dir=str(tmp_path))
    result, _ = collect(config, layer='metadata')
    assert len(result['nodes']) == 1203


def test_real_cached_metadata_cursor_is_stable_and_has_no_1000_node_ceiling(tmp_path, monkeypatch):
    from lightgraph.web.workspace import Workspace
    from lightgraph.web.execution import read_metadata
    workspace = Workspace(tmp_path / 'workspace')
    workspace.create('synthetic metadata', 'p')
    workspace.add_file('p', 'wide.csv', (','.join(f'column_{index}' for index in range(1100)) + '\n').encode())
    initial = read_metadata(workspace, 'p', refresh=True)
    assert initial['total_nodes'] > 1000 and len(initial['nodes']) == 1000
    config = workspace.resolved_config('p')
    def forbidden(*args, **kwargs):
        raise AssertionError('batch read must use cached metadata')
    monkeypatch.setattr('lightgraph.runtime.config.open_sources', forbidden)
    first = read_graph_batch(workspace, 'p', config=config, layer='metadata')
    second = read_graph_batch(workspace, 'p', config=config, layer='metadata', offset=1000, generation=first['generation'])
    assert len(first['nodes']) + len(second['nodes']) == initial['total_nodes']
    assert second['next_offset'] is None
    edge_count, offset = 0, 0
    while offset is not None:
        page = read_graph_batch(workspace, 'p', config=config, layer='metadata', part='edges', offset=offset, generation=first['generation'])
        edge_count += len(page['edges']); offset = page['next_offset']
    assert edge_count == initial['total_edges']
