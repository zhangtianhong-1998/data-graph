"""Only exhaustively corroborated definition overlaps authorize a current-batch rule."""
from copy import deepcopy

import pytest

from lightgraph.construction import overlap_patterns as overlap
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import MergePolicyConfig


def unit(owner, name, key, *, table='items', column='code', field='currency', role='parent'):
    address = {'source_id': 'db', 'schema': 'dm', 'table': table}
    nodes = []
    for i, (code, label) in enumerate((('000071', '测试对象甲'), ('000072', '测试对象乙'))):
        props = {field: [field + '_private_value']}
        nodes.append({'id': f'{owner}_{i}', 'type': name, 'identity': {key: code}, 'identity_types': {key: 'text'},
                      'identity_domain': 'catalog', 'label': label, 'properties': props, 'supports': [{
                          'scope_ref': 'public', 'label': label, 'properties': deepcopy(props),
                          'business_context': {'status': 'global', 'dimensions': {}},
                          'provenance': {'source': address, 'key_columns': [column], 'label_column': 'display_name',
                                         'property_columns': {field: field + '_col'}}}]})
    edge = {'id': owner + '_edge', 'type': role, 'source': nodes[0]['id'], 'target': nodes[1]['id'],
            'supports': [{'scope_ref': 'public', 'provenance': {'source': address,
                          'source_key_columns': [column], 'target_key_columns': ['parent_code']}}]}
    return {'kind': 'unit', 'source_namespace': 'workspace1', 'contributor_id': owner, 'graph': {
        'access_fingerprint': 'authorized', 'nodes': nodes, 'edges': [edge], 'status': 'published',
        'support_visibility_version': 1,
        'definitions': {'entities': [{'type': name, 'key_names': [key],
                                      'identity_domain': 'catalog', 'identity_scope': 'global'}],
                        'relations': [{'type': role, 'source_type': name, 'target_type': name}]}}}


def inputs():
    return [unit('a', 'Metric', 'item_code'), unit('b', 'Indicator', 'metric_code', field='unit')]


def test_pattern_is_per_projection_and_executes_without_exposing_values():
    batch = inputs()
    original = deepcopy(batch)
    base = MergePolicyConfig()
    analysis = overlap.analyze_overlap_patterns(batch, base)
    assert overlap.analyze_overlap_patterns(list(reversed(batch)), base).patterns == analysis.patterns
    entity, = [p for p in analysis.patterns if p['kind'] == 'entity']
    assert entity['eligible_actions'] == ['apply', 'retain']
    assert entity['overlapping_keys'] == 2
    assert {m['property_names'][0] for m in entity['members']} == {'currency', 'unit'}
    assert all(m['roles'] for m in entity['members'])
    relation, = [p for p in analysis.patterns if p['kind'] == 'relation']
    assert relation['sources'][0]['source_key_columns'] == ['code']
    assert relation['sources'][0]['target_key_columns'] == ['parent_code']
    assert relation['eligible_actions'] == ['retain']
    assert all(secret not in str(analysis.patterns) for secret in ['000071', '测试对象甲', '_private_value', 'a_edge'])
    policy = overlap.apply_overlap_decisions(base, analysis, [entity['pattern_id']])
    assert all(len(r.source_bindings) == 1 for r in policy.fusion_policy.entity_rules)
    graph = merge_construction_graphs(batch, merge_policy=policy)
    assert len(graph['nodes']) == 2
    assert all(set(n['properties']) == {'currency', 'unit'} and len(n['supports']) == 2 for n in graph['nodes'])
    assert graph['edges'] and all(e['source'] != e['target'] for e in graph['edges'])
    assert batch == original and base.fusion_policy is None


@pytest.mark.parametrize('change,reason', [
    ('scope', 'different_source_or_scope'), ('labels', 'conflicting_labels'),
    ('foreign_role', 'unverified_relation_role'), ('unknown_namespace', 'unverified_source_namespace'),
    ('variant', 'unverified_identity_contract'),
    ('inherited', 'previous_identity_decision_or_inheritance'), ('wrong_access', 'unverified_access_fingerprint'),
])
def test_patterns_cannot_override_conflicts(change, reason):
    batch = inputs()
    graph = batch[1]['graph']
    node = graph['nodes'][0]
    if change == 'scope':
        node['supports'][0]['scope_ref'] = 'finance'
    elif change == 'labels':
        node['supports'][0]['observed_label'] = '冲突'
    elif change == 'foreign_role':
        graph['edges'] = []
    elif change == 'unknown_namespace':
        batch[1].pop('source_namespace')
    elif change == 'variant':
        graph['definitions']['entities'][0]['definition_variant'] = {'contract_digest': 'different'}
    elif change == 'inherited':
        node['supports'][0]['identity_origins'] = [{'type': 'Other'}]
    elif change == 'wrong_access':
        graph['access_fingerprint'] = 'other'
    analysis = overlap.analyze_overlap_patterns(batch)
    pattern, = [p for p in analysis.patterns if p['kind'] == 'entity']
    assert reason in pattern['reasons'] and not analysis.rules_by_pattern
    with pytest.raises(LightGraphError, match='invalid_overlap_pattern_decision'):
        overlap.apply_overlap_decisions(None, analysis, [pattern['pattern_id']])


def test_cross_table_needs_dictionary_and_observed_unique_member_for_each_key():
    a = unit('a', 'Item', 'sku', table='items')
    b = unit('b', 'SKU', 'sku', table='sales', column='item_code')
    for with_refs in (False, True):
        if with_refs:
            for unit_graph, local, kind in [(a, 'code', 'unique_key'), (b, 'item_code', 'foreign_key')]:
                for node in unit_graph['graph']['nodes']:
                    node['supports'][0]['provenance']['catalog_identity_reference'] = {
                        'kind': kind, 'local_columns': [local], 'target': {
                            'source_id': 'db', 'schema': 'dm', 'table': 'items', 'columns': ['code']}}
        analysis = overlap.analyze_overlap_patterns([a, b])
        logical, = [p for p in analysis.patterns if p['basis'] == 'same_logical_keys_across_sources']
        assert logical['eligible_actions'] == ['retain']
        assert len(analysis.rules_by_pattern) == int(with_refs)
    del a['graph']['nodes'][0]['supports'][0]['provenance']['catalog_identity_reference']
    assert not overlap.analyze_overlap_patterns([a, b]).rules_by_pattern


def test_policy_scope_cannot_extend_to_other_units_or_competing_draft_types():
    batch = inputs()
    extra = unit('c', 'Metric', 'item_code', table='elsewhere')
    assert not overlap.analyze_overlap_patterns([*batch, extra]).rules_by_pattern
    draft = unit('a', 'Facet', 'facet_code')['graph']
    for key in ('nodes', 'edges'):
        batch[0]['graph'][key].extend(draft[key])
    batch[0]['graph']['definitions']['entities'] += draft['definitions']['entities']
    result = overlap.analyze_overlap_patterns(batch)
    physical, = [p for p in result.patterns if p['basis'] == 'same_physical_key']
    assert 'competing_roles_in_one_unit' in physical['reasons']
    assert not result.rules_by_pattern


def test_mixed_incremental_and_excess_budget_are_not_eligible(monkeypatch):
    batch = inputs()
    batch[1]['kind'] = 'base'
    assert overlap.analyze_overlap_patterns(batch).patterns == []
    batch[1]['kind'] = 'unit'
    monkeypatch.setattr(overlap, 'MAX_RECORDS', 1)
    assert overlap.analyze_overlap_patterns(batch).summary['reason'] == 'analysis_budget_exceeded'


def _codes(contributor, codes):
    graph = contributor['graph']
    for node, code in zip(graph['nodes'], codes, strict=True):
        key, = node['identity']
        node['identity'][key] = code
        node['label'] = node['supports'][0]['label'] = 'name ' + code
    return contributor


def test_partially_overlapping_subgraphs_preserve_each_unique_member():
    a, b = inputs()
    batch = [_codes(a, ['A', 'B']), _codes(b, ['B', 'C'])]
    # These projections of one physical primary key have different coverage.
    # No competing relation observation contradicts the actual common key.
    for entry in batch:
        _use_physical_primary_key(entry)
    analysis = overlap.analyze_overlap_patterns(batch)
    card, = [p for p in analysis.patterns if p['kind'] == 'entity']
    assert card['overlapping_keys'] == 1 and card['eligible_actions'] == ['apply', 'retain']
    policy = overlap.apply_overlap_decisions(None, analysis, [card['pattern_id']])
    graph = merge_construction_graphs(batch, merge_policy=policy)
    owners = {n['identity']['metric_code']: {s['contributor_id'] for s in n['supports']} for n in graph['nodes']}
    assert owners == {'A': {'a'}, 'B': {'a', 'b'}, 'C': {'b'}}
    amounts = {n['identity']['metric_code']: set(n['properties']) for n in graph['nodes']}
    assert amounts == {'A': {'currency'}, 'B': {'currency', 'unit'}, 'C': {'unit'}}
    row = b['graph']['nodes'][0]
    row['label'] = row['supports'][0]['label'] = 'different B'
    assert not overlap.analyze_overlap_patterns(batch).rules_by_pattern


def test_three_unit_chain_without_common_observed_key_is_a_candidate_only():
    a, b = inputs()
    c = unit('c', 'Code', 'code')
    batch = [_codes(a, ['A', 'B']), _codes(b, ['B', 'C']), _codes(c, ['C', 'D'])]
    analysis = overlap.analyze_overlap_patterns(batch)
    entity, = [p for p in analysis.patterns if p['kind'] == 'entity']
    assert entity['overlapping_keys'] == 2 and 'no_witness_shared_by_all_projections' in entity['reasons']
    assert not analysis.rules_by_pattern


def _use_physical_primary_key(entry):
    graph = entry['graph']
    graph['edges'] = []
    graph['definitions']['relations'] = []
    for node in graph['nodes']:
        prov = node['supports'][0]['provenance']
        prov['catalog_identity_reference'] = {'kind': 'unique_key', 'local_columns': ['code'],
                                             'target': {**prov['source'], 'columns': ['code']}}


def test_same_table_observed_unique_key_is_a_role_witness_for_two_isolated_projections():
    batch = inputs()
    for entry in batch:
        _use_physical_primary_key(entry)
    assert len(overlap.analyze_overlap_patterns(batch).rules_by_pattern) == 1
    del batch[1]['graph']['nodes'][1]['supports'][0]['provenance']['catalog_identity_reference']
    assert not overlap.analyze_overlap_patterns(batch).rules_by_pattern


def test_opposite_roles_on_the_same_members_block_a_definition_wide_rule():
    batch = inputs()
    edge = batch[1]['graph']['edges'][0]
    edge['source'], edge['target'] = edge['target'], edge['source']
    analysis = overlap.analyze_overlap_patterns(batch)
    card, = [p for p in analysis.patterns if p['kind'] == 'entity']
    assert 'incompatible_member_relation_roles' in card['reasons']
    assert not analysis.rules_by_pattern
    # The schema has the same direction and columns, but no actual directed pair overlaps.
    assert not any(p['kind'] == 'relation' for p in analysis.patterns)


def test_multiple_hierarchy_column_positions_have_unique_pattern_ids():
    batch = inputs()
    for c in batch:
        for node in c['graph']['nodes']:
            another = deepcopy(node['supports'][0])
            another['provenance']['key_columns'] = ['other_level_code']
            node['supports'].append(another)
    result = overlap.analyze_overlap_patterns(batch)
    cards = [p for p in result.patterns if p['basis'] == 'same_physical_key']
    assert len(cards) == 2 and len({p['pattern_id'] for p in cards}) == 2
    assert {tuple(p['witness_binding']['key_columns']) for p in cards} == {('code',), ('other_level_code',)}
    assert all('type_has_other_source_bindings' in p['reasons'] for p in cards)


def test_automatic_rule_cannot_capture_an_unrelated_projection_in_a_participating_owner():
    a, b = inputs()
    other = unit('b', 'Metric', 'item_code', table='other_business_table')['graph']
    other['edges'] = []
    for node in other['nodes']:
        node['id'] += '_elsewhere'
    b['graph']['definitions']['entities'] += other['definitions']['entities']
    b['graph']['nodes'] += other['nodes']
    result = overlap.analyze_overlap_patterns([a, b])
    entity, = [p for p in result.patterns if p['basis'] == 'same_physical_key']
    assert 'type_used_by_other_projection' in entity['reasons']
    assert not result.rules_by_pattern


def test_a_directional_cycle_cannot_confirm_an_opposite_cycle_on_the_same_codes():
    batch = inputs()
    for entry, step in zip(batch, (1, -1), strict=True):
        graph = entry['graph']
        template = graph['nodes'][-1]
        for ordinal in range(2, 4):
            node = deepcopy(template)
            node['id'] = entry['contributor_id'] + '_' + str(ordinal)
            graph['nodes'].append(node)
        _codes(entry, ['A', 'B', 'C', 'D'])
        template_edge, = graph['edges']
        graph['edges'] = [{**template_edge, 'id': template_edge['id'] + str(i), 'source': node['id'],
                           'target': graph['nodes'][(i + step) % 4]['id']} for i, node in enumerate(graph['nodes'])]
    analysis = overlap.analyze_overlap_patterns(batch)
    card, = [p for p in analysis.patterns if p['kind'] == 'entity']
    assert 'incompatible_member_neighbors' in card['reasons']
    assert not analysis.rules_by_pattern


def test_four_batch_inputs_only_participating_owners_set_rule_authority():
    a, b = inputs()
    c = _codes(unit('c', 'Location', 'office'), ['North', 'South'])
    d = _codes(unit('d', 'Office', 'city'), ['North', 'South'])
    c['graph']['access_fingerprint'] = 'offices'
    d['graph']['access_fingerprint'] = 'estates'
    batch = [a, b, c, d]

    def cards():
        analysis = overlap.analyze_overlap_patterns(batch)
        physical = {tuple(m['contributor_id'] for m in p['members']): p
                    for p in analysis.patterns if p['basis'] == 'same_physical_key'}
        return analysis, physical

    analysis, groups = cards()
    assert groups['a', 'b']['eligible_actions'] == ['apply', 'retain']
    assert 'unverified_access_fingerprint' in groups['c', 'd']['reasons']
    policy = overlap.apply_overlap_decisions(None, analysis, [groups['a', 'b']['pattern_id']])
    graph = merge_construction_graphs(batch, merge_policy=policy)
    assert sum(n['type'] == 'Indicator' for n in graph['nodes']) == 2
    assert {n['type'] for n in graph['nodes']} >= {'Indicator', 'Location', 'Office'}

    d['graph']['access_fingerprint'] = 'offices'
    d['source_namespace'] = 'other_workspace'
    _, groups = cards()
    assert groups['a', 'b']['eligible_actions'] == ['apply', 'retain']
    assert 'unverified_source_namespace' in groups['c', 'd']['reasons']


def _customer_tables():
    return [unit('a', 'Customer', 'customer_no', table='customers'),
            unit('b', 'Client', 'client_code', table='invoices', column='client_ref')]


def _cross_table_cards(analysis):
    return [p for p in analysis.patterns if p['basis'] == 'same_typed_keys_and_names_across_sources']


def test_different_logical_keys_with_several_independent_names_are_reviewable_without_fk():
    batch = _customer_tables()
    result = overlap.analyze_overlap_patterns(batch)
    card, = _cross_table_cards(result)
    assert card['overlapping_keys'] == 2 and card['eligible_actions'] == ['retain']
    assert 'cross_source_identity_unverified' in card['reasons']
    assert all(value not in str(result.patterns) for value in ['000071', '000072', '测试对象甲', '测试对象乙'])
    with pytest.raises(LightGraphError, match='invalid_overlap_pattern_decision'):
        overlap.apply_overlap_decisions(None, result, [card['pattern_id']])


@pytest.mark.parametrize('case', ['single_agreement', 'generic_name', 'same_key_as_name', 'wrong_type', 'partial_key'])
def test_alias_candidates_require_independent_names_and_complete_typed_keys(case):
    batch = _customer_tables()
    nodes = batch[1]['graph']['nodes']
    if case == 'single_agreement':
        nodes[1]['supports'][0]['label'] = nodes[1]['label'] = '另一个客户'
    elif case == 'generic_name':
        for c in batch:
            for node in c['graph']['nodes']:
                node['supports'][0]['label'] = node['label'] = '未知'
    elif case == 'same_key_as_name':
        for c in batch:
            for node in c['graph']['nodes']:
                prov = node['supports'][0]['provenance']
                prov['label_column'] = prov['key_columns'][0]
    elif case == 'wrong_type':
        for n in nodes:
            n['identity']['client_code'] = int(n['identity']['client_code'])
            n['identity_types']['client_code'] = 'integer'
    elif case == 'partial_key':
        batch[1]['graph']['definitions']['entities'][0]['key_names'].append('company')
        for n in nodes:
            n['identity']['company'] = 'XYZ'
            n['identity_types']['company'] = 'text'
            n['supports'][0]['provenance']['key_columns'].append('company_code')
    result = overlap.analyze_overlap_patterns(batch)
    assert not _cross_table_cards(result) and not result.rules_by_pattern


def test_frequent_weak_keys_and_budget_limits_cannot_authorize_cross_table_aliases(monkeypatch):
    batch = [unit('unit' + str(i), 'Type' + str(i), 'key' + str(i), table='table' + str(i))
             for i in range(overlap.MAX_ALIAS_FANOUT + 1)]
    result = overlap.analyze_overlap_patterns(batch)
    assert result.summary['cross_table_key_name_search']['high_frequency_keys_suppressed'] == 2
    assert not _cross_table_cards(result) and not result.rules_by_pattern

    for setting, reason in [('MAX_ALIAS_COMPARISONS', 'comparison_budget_exceeded'),
                            ('MAX_ALIAS_OBSERVATIONS', 'observation_budget_exceeded')]:
        with monkeypatch.context() as patch:
            patch.setattr(overlap, setting, 1)
            result = overlap.analyze_overlap_patterns(_customer_tables())
            assert result.summary['cross_table_key_name_search']['status'] == reason
            assert result.summary['truncated'] and not _cross_table_cards(result) and not result.rules_by_pattern
