from copy import deepcopy
from pathlib import Path
from runpy import run_path

import pytest

analyze_nodes = run_path(str(Path(__file__).resolve().parents[3] / 'scripts/fusion_census_nodes.py'))['analyze_nodes']


def _node(nid, code, unit, table, name='Shared', kind='Region', axis='code'):
    return {'id': nid, 'type': kind, 'original_type': 'Region', 'identity': {axis: code},
            'supports': [{'scope_ref': 'public', 'contributor_id': unit, 'observed_label': name,
                          'provenance': {'source': {'source_id': 'db', 'schema': 's', 'table': table},
                                         'key_columns': ['raw_code'], 'label_column': 'raw_name'}}]}


def test_full_node_census_partitions_overlap_without_authorizing_transitive_joins():
    a = _node('a', '100', 'unit_a', 'one')
    b = _node('b', '100', 'unit_a', 'one', kind='Region__def_1', axis='region')
    c = _node('c', '100', 'unit_b', 'two')
    d = _node('d', 100, 'unit_b', 'two', name='Unrelated')
    e = _node('e', '200', 'unit_b', 'two', name='Different')
    e['supports'][0]['aliases'] = ['Shared']
    graph = {'nodes': [a, b, c, d, e], 'candidate_identity_merges': [
        {'node_ids': ['a', 'c'], 'reason': 'previous'},
        {'node_ids': ['c', 'missing'], 'reason': 'stale'}]}
    original = deepcopy(graph)
    result = analyze_nodes(graph)
    assert graph == original
    assert result == analyze_nodes({**graph, 'nodes': graph['nodes'][::-1]})
    summary = result['summary']
    assert [summary[k] for k in ('total_nodes', 'candidate_nodes', 'nodes_outside_candidate_blocks')] == [5, 4, 1]
    group = lambda basis: [g for g in result['groups'] if g['basis'] == basis]
    physical, = group('physical_typed_key')
    assert (physical['node_ids'], physical['unit_span']) == (['a', 'b'], 'within')
    exact, = group('same_type_typed_identity')
    assert exact['node_ids'] == ['a', 'c']
    values, = group('typed_key_values')
    assert (values['node_ids'], values['unit_span']) == (['a', 'b', 'c'], 'within_and_across')
    label, = group('observed_name_or_alias')
    assert label['node_ids'] == ['a', 'b', 'c', 'e']
    assert label['distinct_typed_key_values'] == 2
    assert not label['all_have_same_single_observed_name']
    assert result['unresolved_recorded_groups'][0]['missing_node_ids'] == ['missing']
    assert len(result['components']) == 1
    assert result['components'][0]['node_ids'] == ['a', 'b', 'c', 'e']
    pattern, = result['projection_patterns']
    assert pattern['overlapping_keys'] == 1
    assert len(pattern['projections']) == 3
    assert all(row['status'] == 'candidate_only' for row in result['groups'])


def test_composite_physical_keys_retain_order_and_original_types_after_remap():
    a = _node('a', '', 'x', 'one')
    b = _node('b', '', 'y', 'one', kind='Region__def_b')
    c = deepcopy(b)
    c['id'] = 'c'
    for n in (a, b, c):
        n['identity'] = {'uuid': n['id'], 'tag': 'separate'}
        s = n['supports'][0]
        s['provenance']['key_columns'] = ['cat', 'order']
        s['original_key_values'] = {'cat': 'fruit', 'order': '1.0'}
        s['original_key_types'] = {'cat': 'text', 'order': 'decimal'}
    b['supports'][0]['original_key_values']['order'] = '1.00'
    del c['supports'][0]['original_key_types']
    result = analyze_nodes({'nodes': [a, b, c]})
    physical, = [g for g in result['groups'] if g['basis'] == 'physical_typed_key']
    assert physical['node_ids'] == ['a', 'b']
    assert result['summary']['evidence_gaps']['nodes_without_comparable_physical_binding'] == 1


def test_raw_checkpoint_owner_fallback_and_derived_display_are_separate_from_source_names():
    nodes = [_node(str(i), 'X', None, 'table', name=None) for i in range(2)]
    for node in nodes:
        node.update(label='Label from edges', label_origin='relations')
        del node['supports'][0]['scope_ref']
    merged = analyze_nodes({'nodes': nodes})
    raw = analyze_nodes({'nodes': nodes}, contributor_id='raw')
    for report, span in ((merged, 'unknown'), (raw, 'within')):
        assert report['summary']['evidence_gaps']['nodes_without_observed_name'] == 2
        assert report['summary']['by_basis']['display_label'] == 1
        assert 'observed_name_or_alias' not in report['summary']['by_basis']
        for group in report['groups']:
            assert group['unit_span'] == span
            assert group['scope_evidence']['members_with_incomplete_scope'] == 2


def test_reject_non_unique_node_id_denominator():
    with pytest.raises(ValueError, match='Duplicate node IDs'):
        analyze_nodes({'nodes': [{'id': 'same'}, {'id': 'same'}]})


def test_conflicting_type_metadata_never_makes_a_typed_key_candidate():
    nodes = [_node(str(i), '2', 'u', 'one') for i in range(3)]
    nodes[1]['identity_types'] = {'code': 'integer'}
    nodes[2]['identity'] = {'code': 'broken-decimal'}
    nodes[2]['identity_types'] = {'code': 'decimal'}
    result = analyze_nodes({'nodes': nodes})
    assert result['summary']['by_basis'] == {'observed_name_or_alias': 1}
    assert result['summary']['evidence_gaps']['nodes_without_complete_ordered_key'] == 2
    assert result['summary']['evidence_gaps']['nodes_without_comparable_physical_binding'] == 2


def _hypothesis_graph():
    rows = [_node(str(i), 'X', owner, 'geo', name='Beijing', kind=kind) for i, (owner, kind) in
            enumerate([('one', 'Region'), ('one', 'Region__def_b'), ('two', 'Region')])]
    d = {'key_names': ['code'], 'identity_domain': 'registered-geography', 'identity_scope': 'global'}
    for i, n in enumerate(rows):
        s = n['supports'][0]
        s['execution_origins'] = [{'draft_id': str(i), 'draft_revision': 1}]
        s['business_context'] = {'status': 'global', 'dimensions': {}}
    relation = {'type': 'located_in', 'source_type': 'Region', 'target_type': 'Root'}
    prov = {'source': {'source_id': 'db', 'schema': 's', 'table': 'geo'},
            'source_key_columns': ['raw_code'], 'target_key_columns': ['parent']}
    edges = [{'id': 'e'+n['id'], 'source': n['id'], 'target': 'root', 'type': 'located_in',
              'supports': [{'provenance': prov}]} for n in rows]
    rows.append({'id': 'root', 'type': 'Root', 'identity': {'p': 'parent'}})
    return {'nodes': rows, 'edges': edges, 'definitions': {
        'entities': [{'type': k, **d} for k in ('Region', 'Region__def_b')], 'relations': [relation]}}


def test_physical_what_if_never_adds_per_lane_counts_and_skips_contested_cross_unit_pairing():
    graph = _hypothesis_graph()
    exp = analyze_nodes(graph)['physical_hypothesis']
    assert [exp[k]['hypothetical_node_reduction'] for k in ('within_only', 'cross_only', 'joint')] == [1, 0, 2]
    assert exp['joint']['groups'][0]['node_ids'] == ['0', '1', '2']
    assert exp['rejection_counts'] == {}
    graph['nodes'][1]['supports'][0]['contributor_id'] = 'three'
    exp = analyze_nodes(graph)['physical_hypothesis']
    assert [exp[k]['hypothetical_node_reduction'] for k in ('within_only', 'cross_only', 'joint')] == [0, 2, 2]
    assert analyze_nodes(graph, excluded_values=['X'])['physical_hypothesis']['rejection_counts'] == {
        'excluded_key_value': 1}


@pytest.mark.parametrize(('change', 'reason'), [
    ('draft', 'ambiguous_or_unversioned_same_unit_projection'), ('audience', 'different_audiences'),
    ('context', 'different_business_scopes'), ('unnamed', 'missing_observed_name_binding'),
    ('role', 'unverified_incident_role'), ('label', 'different_observed_names'),
    ('row', 'contradictory_key_or_name_observation'), ('linked', 'distinct_endpoints_of_recorded_relationship')])
def test_incompatible_third_member_blocks_entire_physical_hypothesis(change, reason):
    g = _hypothesis_graph()
    s = g['nodes'][1]['supports'][0]
    if change == 'draft':
        s['execution_origins'][0]['draft_id'] = '0'
    elif change == 'audience':
        s['required_scope_refs'] = ['private']
    elif change == 'context':
        s['business_context'] = {'status': 'scoped', 'dimensions': {'tenant': 'one'}}
    elif change == 'unnamed':
        del s['provenance']['label_column']
    elif change == 'role':
        g['edges'] = g['edges'][:1] + g['edges'][2:]
    elif change == 'label':
        s['observed_label'] = 'Other'
    elif change == 'row':
        s['row_context'] = {'values': {'raw_code': 'Y'}}
    else:
        g['edges'][0]['target'] = '1'
    exp = analyze_nodes(g)['physical_hypothesis']
    assert exp['joint']['hypothetical_node_reduction'] == 0
    assert exp['rejection_counts'] == {reason: 1}


def test_cross_family_hypothesis_needs_explicit_shared_role_and_complete_incident_roles():
    g = _hypothesis_graph()
    g['nodes'][1]['original_type'] = 'ReportingTerritory'
    assert analyze_nodes(g)['physical_hypothesis']['rejection_counts'] == {'cross_family_without_declared_shared_role': 1}
    for n in g['nodes'][:3]:
        n['node_role'] = 'territorial_unit'
    assert analyze_nodes(g)['physical_hypothesis']['joint']['hypothetical_node_reduction'] == 2


def test_overlapping_key_block_never_bridges_separate_projections():
    g = _hypothesis_graph()
    for n in g['nodes'][:2]:
        s = deepcopy(n['supports'][0])
        s['provenance']['key_columns'] = ['other_code']
        n['supports'].append(s)
    exp = analyze_nodes(g)['physical_hypothesis']
    assert exp['physical_key_blocks'] == 2
    assert exp['distinct_member_sets'] == 2
    assert exp['rejection_counts'] == {'overlapping_competing_member_sets': 2}
    assert exp['joint']['hypothetical_node_reduction'] == 0
