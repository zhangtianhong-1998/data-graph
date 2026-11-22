"""Explicit identity fusion preserves references and independent column observations."""
from copy import deepcopy

import pytest

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.fusion import prepare_graph
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.metadata_columns import merge_column_supports
from lightgraph.errors import LightGraphError
from lightgraph.tests.test_cross_source_fusion import source_graph, fusion_rule


def test_delta_with_local_id_collision_keeps_its_inherited_lookup_on_the_base():
    base = source_graph('Item', 'sku', 'A', 'store', 'A', 'category', 'one')
    delta = source_graph('Item', 'sku', 'X', 'store', 'X', 'category', 'two')
    delta['extends_base_graph'] = True
    edge = delta['edges'][0]
    edge['source'], edge['target'] = 'store_2', 'store_1'
    edge['supports'][0]['lookup_matches'] = [{'endpoint': 'target', 'inherited_node_id': 'store_1'}]
    policy = {'entity_rules': [fusion_rule('Item', 'sku', 'category')]}
    result = merge_construction_graphs([{'graph': base, 'kind': 'base', 'contributor_id': 'base'},
                                        {'graph': delta, 'contributor_id': 'delta'}],
                                       merge_policy={'fusion_policy': policy})
    by_code = {n['identity']['code']: n['id'] for n in result['nodes']}
    inherited = next(e for e in result['edges'] if e['source'] == by_code['B'])
    assert inherited['target'] == by_code['A']
    assert inherited['supports'][0]['lookup_matches'][0]['inherited_node_id'] == by_code['A']
    assert inherited['supports'][0]['endpoint_origins'][0]['target'] == 'store_1'


def test_pending_candidates_still_point_into_the_rekeyed_graph():
    graph = source_graph('Item', 'sku', 'A', 'store', 'A', 'category', 'one')
    graph['unresolved_relations'] = [{'id': 'pending', 'candidate_endpoints': {
        'source': ['store_1'], 'target': ['store_1', 'store_2']}, 'node_ids': ['store_1', 'store_2']}]
    prepared = prepare_graph(graph, {'entity_rules': [fusion_rule('Item', 'sku', 'category')]})
    by_code = {n['identity']['code']: n['id'] for n in prepared['nodes']}
    pending, = prepared['unresolved_relations']
    assert pending['candidate_endpoints']['source'] == [by_code['A']]
    assert pending['candidate_endpoints']['target'] == pending['node_ids'] == sorted(by_code.values())


def test_new_rule_applies_to_declared_domain_after_unrelated_fusion():
    graph = source_graph('Item', 'sku', 'A', 'store', 'A', 'category', 'one')
    definition, = graph['definitions']['entities']
    definition['identity_domain'] = 'catalog'
    for node in graph['nodes']:
        node['identity_domain'] = 'catalog'
    graph = prepare_graph(graph, {'entity_rules': []})
    result = prepare_graph(graph, {'entity_rules': [fusion_rule('Item', 'sku', 'category')]})
    assert result['definitions']['relations'][0]['target_type'] == 'Product'
    assert {n['type'] for n in result['nodes']} == {'Product'}
    assert {tuple(n['identity']) for n in result['nodes']} == {('code',)}


@pytest.mark.parametrize('source_type,key', [('Product', 'code'), ('Item', 'sku')])
def test_digest_only_fusion_does_not_reapply_changed_normalizers_to_old_keys(source_type, key):
    graph = source_graph('Item', 'sku', ' A ', 'store', 'A', 'category', 'one')
    original = {'entity_rules': [fusion_rule('Item', 'sku', 'category')]}
    legacy = prepare_graph(graph, original)
    del legacy['fusion_policy']
    replay = prepare_graph(legacy, original)
    assert replay['nodes'] == legacy['nodes']

    changed = fusion_rule(source_type, key, 'category')
    changed['key_normalizers'] = {'code': ['casefold']}
    with pytest.raises(LightGraphError, match='fusion_policy_requires_original_contributions'):
        merge_construction_graphs([{'kind': 'merged', 'contributor_id': 'old', 'graph': legacy}],
                                  merge_policy={'fusion_policy': {'entity_rules': [changed]}})

    fresh = source_graph('Sample', 'ref', 'X', 'labs', 'X', 'field', 'two')
    legacy['nodes'] += fresh['nodes']
    legacy['definitions']['entities'] += fresh['definitions']['entities']
    added = {**fusion_rule('Sample', 'ref', 'field'), 'canonical_type': 'Specimen', 'identity_domain': 'samples'}
    extended = prepare_graph(legacy, {'entity_rules': [added]})
    assert extended['nodes'][:2] == replay['nodes']
    assert {n['type'] for n in extended['nodes'][2:]} == {'Specimen'}
    with pytest.raises(LightGraphError, match='fusion_policy_requires_original_contributions'):
        prepare_graph(extended, {'entity_rules': [changed]})


def test_repeated_column_merges_keep_conflicting_declarations_on_their_own_supports():
    def node(name, value, scope):
        properties = {'axis': [value]}
        return {'id': 'column', 'type': 'Column', 'identity_origin': 'physical_column',
                'identity': {'source_id': 'source', 'schema': 's', 'table': 't', 'column': 'c'},
                'aliases': [], 'properties': properties, 'provenance': [], 'supports': [
                    {'source_kind': 'declared', 'scope_ref': scope, 'properties': properties, 'label': name}]}
    a, b = node('first', True, 'public'), node('second', 1, 'internal')
    with pytest.raises(LightGraphError, match='metadata_column_mapping_conflict'):
        merge_column_supports(deepcopy(a), b)
    merge_column_supports(a, b, retain_variants=True)
    assert len(a['properties']['axis']) == 2
    assert {type(value) for value in a['properties']['axis']} == {bool, int}
    assert len(a['supports']) == 2
    graph = {'nodes': [a], 'edges': [], 'label_conflict_policy': 'retain_variants',
             'support_visibility_version': 1}
    pub, = filter_graph_scopes(graph, ['public'])['nodes']
    assert pub['label_variants'] == ['first']
    assert pub['properties']['axis'] == [True]

    catalog = node('catalog name', 'base', 'public')
    catalog['supports'][0]['source_kind'] = 'catalog'
    merge_column_supports(catalog, b, retain_variants=True)
    public_only, = filter_graph_scopes({**graph, 'nodes': [catalog]}, ['public'])['nodes']
    assert public_only['label'] == 'catalog name'
