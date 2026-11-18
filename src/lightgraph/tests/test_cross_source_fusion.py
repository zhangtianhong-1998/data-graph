"""Publications and deterministic DAG merges preserve source facts under identity rules."""
from copy import deepcopy

import pytest

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.fusion import prepare_graph
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.node_standardizer import standardize_node_details
from lightgraph.construction.reporting import construction_summary
from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import FusionPolicyConfig
from lightgraph.tests.test_construction_conflict_isolation import conflict_tools


def source_graph(kind, key, code, source, name, property_name, value, *, scope='public'):
    def entity(node_id, identity, label):
        props = {property_name: [value]}
        return {'id': node_id, 'type': kind, 'identity': {key: identity}, 'label': label,
                'properties': props, 'supports': [{'scope_ref': scope, 'label': label, 'properties': props,
                    'provenance': {'definition': kind, 'mapping': 0, 'evidence_refs': ['e1'],
                                   'property_columns': {property_name: 'segment_col'},
                                   'source': {'source_id': source, 'schema': 'dm', 'table': 'products'}}}]}
    prefix = source + '_'
    return {'format_version': 1, 'target': 'graph', 'status': 'published', 'support_visibility_version': 1,
            'definitions': {'entities': [{'type': kind, 'key_names': [key]}],
                            'relations': [{'type': 'contains', 'source_type': kind, 'target_type': kind}]},
            'nodes': [entity(prefix + '1', code, name), entity(prefix + '2', 'B', 'B')],
            'edges': [{'id': prefix + 'edge', 'type': 'contains', 'source': prefix + '1', 'target': prefix + '2',
                       'supports': [{'scope_ref': scope, 'provenance': {'source': {'source_id': source}}}]}],
            'evidence_refs': ['e1']}


def fusion_rule(kind, key, field):
    return {'entity_type': kind, 'canonical_type': 'Product', 'identity_domain': 'catalog',
            'key_mapping': {'code': key}, 'key_normalizers': {'code': ['trim']},
            'property_aliases': {field: 'segment'}, 'property_normalizers': {'segment': ['trim']}}


def test_fusion_merges_three_overlapping_contributors_deterministically_and_scopes_values():
    a = source_graph('Product', 'sku', ' A ', 'master', '主名称', 'category', ' 主类 ')
    b = source_graph('Product', 'item_id', 'A', 'orders', '另一个名称', 'class', '订单类', scope='private')
    g = [{'contributor_id': x, 'graph': graph} for x, graph in [('ab', a), ('ac', a), ('bc', b)]]
    policy = {'fusion_policy': {'entity_rules': [fusion_rule('Product', 'sku', 'category'),
                                                  fusion_rule('Product', 'item_id', 'class')]},
              'authority_sources': ['orders']}
    source_digest = digest(g)
    result = merge_construction_graphs(g, merge_policy=policy)
    assert result == merge_construction_graphs(list(reversed(g)), merge_policy=policy)
    assert digest(g) == source_digest
    assert [n['identity']['code'] for n in sorted(result['nodes'], key=lambda n: n['identity']['code'])] == ['A', 'B']
    assert len(result['edges']) == 1 and len(result['edges'][0]['supports']) == 2
    node = next(n for n in result['nodes'] if n['identity']['code'] == 'A')
    assert result['edges'][0]['source'] == node['id']
    assert result['edges'][0]['target'] in {n['id'] for n in result['nodes']}
    assert node['label'] == '另一个名称' and set(node['label_variants']) == {'另一个名称', '主名称'}
    assert len(node['supports']) == 2 and set(node['properties']['segment']) == {'订单类', '主类'}
    repeated = next(s for s in node['supports'] if s['scope_ref'] == 'public')
    assert {o['contributor_id'] for o in repeated['execution_origins']} == {'ac', 'ab'}
    assert set(repeated['provenance']['evidence_refs']) == {'unit:ac:e1', 'unit:ab:e1'}
    assert repeated['identity_origins'][0]['identity'] == {'sku': ' A '}
    assert repeated['observed_properties'] == {'category': [' 主类 ']}
    replay = merge_construction_graphs([{'kind': 'merged', 'contributor_id': 'inner', 'graph': result}], merge_policy=policy)
    assert replay['nodes'] == result['nodes'] and replay['edges'] == result['edges']
    scoped = filter_graph_scopes(result, ['public'])
    assert '订单类' not in str(scoped) and '另一个名称' not in str(scoped)
    visible = next(n for n in scoped['nodes'] if n['identity']['code'] == 'A')
    assert visible['label'] == '主名称' and visible['label_variants'] == ['主名称']
    attrs = standardize_node_details(visible)['attributes'][0]
    assert attrs['values'] == ['主类']
    observed, = attrs['observations']
    assert (observed['value'], observed['raw_value'], observed['column']) == ('主类', ' 主类 ', 'segment_col')
    assert set(observed['evidence_refs']) == {'unit:ac:e1', 'unit:ab:e1'}


@pytest.mark.parametrize('policy,passed,allowed', [('retain_variants', True, True), ('strict', False, False),
                                                  ('quarantine', False, True)])
def test_name_variants_pass_through_real_validation_and_publication(tmp_path, policy, passed, allowed):
    _, tools, _ = conflict_tools(tmp_path, label_policy=policy)
    try:
        checked = tools.call('validate_graph_draft', draft_id='设备')
        assert checked['passed'] is passed
        reply = tools.call('publish_graph_draft', draft_id='设备')
        assert (reply['status'] == 'ok') is allowed
        if policy == 'retain_variants':
            graph = tools.workspace.read_published_graph()
            node = next(n for n in graph['nodes'] if n['identity']['编码'] == '002')
            assert node['label_conflict'] and set(node['label_variants']) == {'乙旧名', '乙新名'}
            assert len(graph['nodes']) == 3 and len(graph['edges']) == 3
            assert construction_summary([], {}, graph).execution['nodes_with_label_conflicts'] == 1
    finally:
        tools.close()


def test_fusion_requires_entire_typed_identity_and_preserves_relation_constraints():
    g = source_graph('Item', 'sku', ' A ', 'one', 'same', 'category', 'one')
    rule = fusion_rule('Item', 'sku', 'category')
    policy = FusionPolicyConfig(entity_rules=[rule])
    wrong = deepcopy(g)
    wrong['nodes'][0]['identity']['country'] = 'US'
    with pytest.raises(LightGraphError, match='fusion_identity_keys_mismatch'):
        prepare_graph(wrong, policy)
    physical = deepcopy(g)
    physical['definitions']['entities'][0]['identity_origin'] = 'physical_column'
    with pytest.raises(LightGraphError, match='fusion_physical_identity_forbidden'):
        prepare_graph(physical, policy)
    g['nodes'][1]['identity']['sku'] = 'A'
    with pytest.raises(LightGraphError, match='self_link_forbidden'):
        prepare_graph(g, policy)
    rule['key_normalizers'] = {}
    g['nodes'][0]['identity']['sku'] = 7
    g['nodes'][1]['identity']['sku'] = '7'
    merged = merge_construction_graphs([{'graph': g, 'contributor_id': 'u'}], merge_policy={'fusion_policy': {'entity_rules': [rule]}})
    assert len(merged['nodes']) == 2 and len(merged['edges']) == 1


def test_duplicate_draft_observation_keeps_both_validations_and_replays(tmp_path):
    from lightgraph.construction.workspace import dump_yaml
    _, tools, authored = conflict_tools(tmp_path, label_policy='retain_variants')
    try:
        authored['draft_id'] = 'another'
        tools.call('save_graph_draft', yaml_text=dump_yaml(authored))
        checks = {}
        for draft_id in ('设备', 'another'):
            checks[draft_id] = tools.call('validate_graph_draft', draft_id=draft_id)['validation_id']
            assert tools.call('publish_graph_draft', draft_id=draft_id)['status'] == 'ok'
        graph = tools.workspace.read_published_graph()
        node = next(n for n in graph['nodes'] if n['identity']['编码'] == '002')
        assert len(node['supports']) == 2
        for support in node['supports']:
            assert {(o['draft_id'], o['validation_id']) for o in support['execution_origins']} == set(checks.items())
        merged = merge_construction_graphs([{'contributor_id': 'extract_1', 'graph': graph}],
            merge_policy={'shared_entity_types': ['设备'], 'fusion_policy': {}})
        node = next(n for n in merged['nodes'] if n['identity']['编码'] == '002')
        for support in node['supports']:
            assert len(support['execution_origins']) == 2
            assert {o['contributor_id'] for o in support['execution_origins']} == {'extract_1'}
    finally:
        tools.close()


def test_merge_rekeys_an_external_inherited_endpoint_and_its_lookup_witness():
    base = source_graph('Item', 'sku', ' A ', 'master', 'A', 'segment', 'C')
    order = source_graph('Order', 'id', 'first', 'sales', 'First', 'segment', 'C')
    order.update(extends_base_graph=True, nodes=order['nodes'][:1])
    order['definitions']['relations'] = [{'type': 'includes', 'source_type': 'Order', 'target_type': 'Item'}]
    order['edges'] = [{'id': 'old', 'type': 'includes', 'source': 'sales_1', 'target': 'master_1',
                       'supports': [{'scope_ref': 'public', 'lookup_matches': [
                           {'endpoint': 'target', 'inherited_node_id': 'master_1'}]}]}]
    merged = merge_construction_graphs([{'graph': base, 'kind': 'base', 'contributor_id': 'catalog'},
                                        {'graph': order, 'contributor_id': 'extract'}],
        merge_policy={'fusion_policy': {'entity_rules': [fusion_rule('Item', 'sku', 'segment')]}})
    new = next(n['id'] for n in merged['nodes'] if n['identity'].get('code') == 'A')
    edge = next(e for e in merged['edges'] if e['type'] == 'includes')
    assert edge['target'] == new
    assert edge['supports'][0]['lookup_matches'][0]['inherited_node_id'] == new


def test_homonymous_properties_can_keep_separate_meanings_per_source_table():
    budget = source_graph('Item', 'sku', 'A', 'budget', 'A', 'amount', '100')
    actual = source_graph('Item', 'sku', 'A', 'sales', 'A', 'amount', '90')
    rule = fusion_rule('Item', 'sku', 'unused')
    rule['property_aliases'] = {'budget.dm.products.amount': 'budget_amount', 'sales.dm.products.amount': 'actual_amount'}
    merged = merge_construction_graphs([{'contributor_id': 'ab', 'graph': budget}, {'contributor_id': 'ac', 'graph': actual}],
                                       merge_policy={'fusion_policy': {'entity_rules': [rule]}})
    node = next(n for n in merged['nodes'] if n['identity']['code'] == 'A')
    assert node['properties'] == {'budget_amount': ['100'], 'actual_amount': ['90']}
    assert filter_graph_scopes(merged, ['public'])['nodes'][0]['properties'] == node['properties']


def test_homonyms_are_candidates_with_a_reason_and_never_imply_shared_identity():
    a = source_graph('Product', 'code', 'A', 'master', '同名', 'group', 'A')
    b = source_graph('Contract', 'number', 'X', 'orders', '同名', 'group', 'B')
    merged = merge_construction_graphs([{'contributor_id': 'u1', 'graph': a},
                                        {'contributor_id': 'u2', 'graph': {**b, 'edges': [],
                                                   'definitions': {**b['definitions'], 'relations': []}}}])
    candidates = [c for c in merged['candidate_identity_merges'] if c.get('normalized_label') == '同名']
    assert len(candidates) == 1
    assert candidates[0]['reason'] == 'same_label_different_entity_types'
    assert candidates[0]['status'] == 'candidate_only' and len(candidates[0]['node_ids']) == 2
    assert len(merged['nodes']) == 4


def test_value_origins_bind_amount_to_the_context_of_its_own_row():
    rule = fusion_rule('Item', 'sku', 'amount')
    rule.update(property_aliases={'amount': 'income'}, property_normalizers={})
    graphs = []
    for src, amount, region, period in [('income', 100, '华东', 'Q1'), ('orders', 300, '华南', 'Q2')]:
        graph = source_graph('Item', 'sku', 'A', src, '同一产品', 'amount', amount)
        for node in graph['nodes']:
            node['supports'][0]['row_context'] = {'values': {'region': region, 'period': period}}
        graphs.append({'contributor_id': src, 'graph': graph})
    result = merge_construction_graphs(graphs, merge_policy={'fusion_policy': {'entity_rules': [rule]}})
    view = filter_graph_scopes(result, ['public'])
    node = next(n for n in view['nodes'] if n['identity']['code'] == 'A')
    attr, = standardize_node_details(node)['attributes']
    assert sorted((o['value'], o['row_context']['values']['region'], o['row_context']['values']['period'])
                  for o in attr['observations']) == [(100, '华东', 'Q1'), (300, '华南', 'Q2')]
