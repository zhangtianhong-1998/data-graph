"""Incremental canvas transport is bounded by bytes as well as record count."""
from copy import deepcopy

from lightgraph.contracts import canonical
from lightgraph.runtime.config import RunConfig
from lightgraph.web.graph_batches import read_graph_batch
from lightgraph.web.graph_payload import MAX_ITEM_BYTES, MAX_PAGE_BYTES, canvas_item
from lightgraph.web import construction_graph


def test_large_property_pages_keep_every_record_and_monotonic_cursor(tmp_path, monkeypatch):
    nodes = [{'id': f'n{i}', 'kind': 'Item', 'label': f'节点{i}',
              'properties': {'comment': ['文' * 6000]}} for i in range(205)]
    edges = [{'id': f'e{i}', 'source': f'n{i}', 'target': f'n{i+1}',
              'properties': {'comment': ['关' * 6000]}} for i in range(204)]
    graph = {'nodes': nodes, 'edges': edges}
    monkeypatch.setattr('lightgraph.web.graph_batches._authorized_view', lambda *args: (graph, 'fixed'))
    config = RunConfig(sources=[], output_dir=str(tmp_path))
    collected = {'nodes': [], 'edges': []}
    version = ''
    for part in collected:
        cursor = 0
        pages = 0
        while cursor is not None:
            page = read_graph_batch(None, 'p', config=config, part=part, offset=cursor, generation=version)
            version = page['generation']
            assert 0 < len(page[part]) < 1000
            assert page['item_payload_bytes'] <= MAX_PAGE_BYTES
            collected[part].extend(page[part])
            assert page['next_offset'] is None or page['next_offset'] > cursor
            cursor = page['next_offset']; pages += 1
        assert pages > 1
    assert collected == graph


def test_single_large_record_has_explicit_preview_without_mutating_full_details(tmp_path, monkeypatch):
    node = {'id': 'n', 'type': 'Item', 'kind': 'Item', 'label': '节点', 'identity': {'code': 'I1'},
            'properties': {'comment': ['FULL_VALUE_' * 10000]}, 'aliases': [],
            'supports': [{'scope_ref': 'public', 'label': '节点'}]}
    before = deepcopy(node)
    preview = canvas_item(node)
    assert len(canonical(preview).encode()) < MAX_ITEM_BYTES
    assert preview['id'] == 'n' and preview['label'] == '节点' and preview['identity'] == {'code': 'I1'}
    assert preview['overview_only'] and preview['detail_fields_deferred']
    assert 'properties' not in preview and 'supports' not in preview
    assert node == before
    graph = {'nodes': [node], 'edges': []}
    monkeypatch.setattr(construction_graph, 'load_graph', lambda config: graph)
    result = construction_graph.graph_details(RunConfig(sources=[], output_dir=str(tmp_path)), entity_id='n')
    assert result['nodes'][0]['properties'] == before['properties']
    assert 'supports' not in result['nodes'][0]
    assert any(f['payload'].get('value') == before['properties']['comment'][0] for f in result['facts'])


def test_initial_canvas_page_also_bounds_large_records(tmp_path, monkeypatch):
    graph = {'nodes': [{'id': str(i), 'kind': 'Item', 'label': str(i), 'properties': {'note': ['字' * 6000]}}
                       for i in range(1000)], 'edges': []}
    monkeypatch.setattr(construction_graph, 'load_graph', lambda config: graph)
    page = construction_graph.graph_page(RunConfig(sources=[], output_dir=str(tmp_path)), limit=1000)
    assert 0 < len(page['nodes']) < 1000 and page['next_offset'] == len(page['nodes'])
    assert page['truncated'] and page['total_nodes'] == 1000
    assert sum(len(canonical(n).encode()) + 1 for n in page['nodes']) <= MAX_PAGE_BYTES


def test_focused_byte_pages_keep_center_and_connecting_paths(tmp_path, monkeypatch):
    ids = ['center'] + [f'p{i}' for i in range(60)] + [f'c{i}' for i in range(60)]
    graph = {'nodes': [{'id': key, 'kind': 'Item', 'label': key, 'properties': {'note': ['字' * 6000]}} for key in ids],
             'edges': [{'id': f'e{i}', 'source': a, 'target': b, 'relation': 'part'}
                       for i, (a, b) in enumerate([('center', f'p{i}') for i in range(60)] + [(f'p{i}', f'c{i}') for i in range(60)])]}
    monkeypatch.setattr(construction_graph, 'load_graph', lambda config: graph)
    config = RunConfig(sources=[], output_dir=str(tmp_path))
    seen, cursor, pages = set(), 0, 0
    while cursor is not None:
        page = construction_graph.graph_page(config, focus='center', hops=2, limit=1000, offset=cursor)
        current = {n['id'] for n in page['nodes']}
        assert 'center' in current
        assert all('p' + node[1:] in current for node in current if node.startswith('c') and node != 'center')
        assert sum(len(canonical(n).encode()) + 1 for n in page['nodes']) <= MAX_PAGE_BYTES
        seen.update(current); pages += 1
        assert page['next_offset'] is None or page['next_offset'] > cursor
        cursor = page['next_offset']
    assert seen == set(ids) and pages > 1
