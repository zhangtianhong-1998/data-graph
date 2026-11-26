"""Large observation lists retain their source order, JSON types and independent edge evidence."""
from copy import deepcopy

import pytest

from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.workspace import _merge_graphs
from lightgraph.contracts import canonical


def contribution(owner, size):
    supports = [{'scope_ref': 'public', 'label': 'Item',
        'business_context': {'status': 'global', 'dimensions': {}},
        'provenance': {'source': {'source_id': 's', 'schema': 'public', 'table': 'items'},
                       'key_columns': ['code'], 'label_column': 'name'},
        'row_context': {'values': {'row': f'{owner}-{i}', 'typed': value}}}
        for i in range(size) for value in (True, 1, 1.0, '1')]
    node = {'id': 'item', 'type': 'Item', 'identity': {'code': 'item'}, 'identity_types': {'code': 'text'},
            'label': 'Item', 'properties': {}, 'supports': supports}
    second = deepcopy(node)
    second.update(id='other', identity={'code': 'other'})
    second['supports'] = deepcopy(supports[:1])
    graph = {'draft_id': owner, 'definitions': {'entities': [{'type': 'Item', 'key_names': ['code']}],
        'relations': [{'type': 'related', 'source_type': 'Item', 'target_type': 'Item'}]},
        'nodes': [node, second], 'edges': [{'id': 'link', 'type': 'related', 'source': 'item', 'target': 'other',
                                         'properties': {}, 'supports': deepcopy(supports)}]}
    return {'contributor_id': owner, 'kind': 'unit', 'graph': graph}


@pytest.mark.parametrize('entrypoint', ['workflow', 'workspace'])
def test_large_support_sequences_preserve_order_types_and_inputs(entrypoint):
    units = [contribution(owner, 250) for owner in ('a', 'b')]
    original = deepcopy(units)
    if entrypoint == 'workflow':
        result = merge_construction_graphs(units,
            merge_policy={'shared_entity_types': ['Item'], 'conflict_mode': 'strict'})
    else:
        result = _merge_graphs([unit['graph'] for unit in units])
    node, = [node for node in result['nodes'] if node['identity']['code'] == 'item']
    expected = [canonical(s['row_context']) for unit in original for s in unit['graph']['nodes'][0]['supports']]
    assert len(expected) == len(set(expected)) == 2000
    assert [canonical(s['row_context']) for s in node['supports']] == expected
    assert len(result['edges']) == 1
    assert [canonical(s['row_context']) for s in result['edges'][0]['supports']] == expected
    assert units == original
