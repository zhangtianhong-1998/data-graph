"""Evidence, display and raw storage must give separate whole-graph tallies."""
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


spec = spec_from_file_location('fusion_census_edges', Path(__file__).resolve().parents[3] / 'scripts/fusion_census_edges.py')
census = module_from_spec(spec)
spec.loader.exec_module(census)


def _edge(eid, rel, source, target, levels, *, rows=None, group='public'):
    support = {'scope_ref': group}
    if levels:
        src, dst = levels
        support['provenance'] = {
            'source': {'source_id': 'orders', 'schema': 'sales', 'table': 'Sheet1'},
            'source_key_columns': [f'AREA_L{src}_ID'], 'target_key_columns': [f'AREA_L{dst}_ID'],
        }
    if rows is not None:
        support['source_row_witness'] = {'kind': 'excel_rows_v1', 'snapshot': 'a' * 64, 'rows': rows}
    return {'id': eid, 'type': rel, 'source': source, 'target': target, 'supports': [support]}


def _publication(edges):
    nodes = [{'id': key, 'label': label} for key, label in [('p', '地区'), ('p2', '地区'), ('c', '城市'), ('c2', '城市')]]
    return {'nodes': nodes, 'definitions': {'relations': [{'type': rel} for rel in sorted({e['type'] for e in edges})]},
            'edges': edges}


def test_all_edges_counted_without_collapsing_distinct_slots_labels_scopes_or_nonhierarchy_loops():
    raw = _publication([
        _edge('h1', 'area_parent_child', 'p', 'c', (1, 2)),
        _edge('h2', 'area_parent_of_area', 'p', 'c', (1, 2)),
        _edge('h3', 'area_parent_of_area', 'p', 'c', (1, 2), group='narrow'),
        _edge('h4', 'area_l3_in_l2', 'c', 'p', (3, 2)),
        _edge('h5', 'area_l5_in_l4', 'c', 'p', (5, 4)),
        _edge('h6', 'area_parent_child', 'p2', 'c2', (1, 2)),
        _edge('self-hierarchy', 'area_parent_child', 'p', 'p', (1, 2)),
        _edge('self-ref', 'cites', 'p', 'p', None),
    ])
    before = deepcopy(raw)
    result = census.analyze_edges(raw)
    assert before == raw
    summary = result['summary']
    assert (summary['raw_edges'], summary['directed_endpoint_pairs'], summary['multi_endpoint_pairs'],
            summary['multi_endpoint_edges'], summary['extra_edges_at_same_endpoints']) == (8, 4, 3, 7, 4)
    assert summary['exact_typed_endpoint_groups'] == summary['exact_typed_endpoint_excess'] == 1
    assert summary['same_base_type_and_endpoint_label_excess'] == 2
    assert summary['different_observed_level_binding_sets_at_same_endpoints'] == 1
    assert summary['grouped_lines_by_row_evidence'] == {'no_recorded_physical_rows': 1}
    assert (summary['default_canvas_lines'], summary['self_loops'], summary['hierarchy_self_loops'],
            summary['hierarchy_line_edge_savings']) == (6, 2, 1, 1)
    assert summary['hierarchy_answer_pairs'] == 2
    assert result['multi_edge_hierarchy_answer_candidates'] == [
        {'parent': 'p', 'child': 'c', 'edge_ids': ['h1', 'h2', 'h3', 'h4', 'h5']}]
    lines = result['edge_to_canvas_line']
    assert len(lines) == 8 and lines['self-hierarchy'] is None and lines['self-ref']
    assert lines['h1'] == lines['h2'] and lines['h1'] != lines['h3'] and lines['h4'] != lines['h5']
    assert {k: summary[k] for k in census.canvas_counts(raw)} == census.canvas_counts(raw)
    observed = {}
    census.canvas_counts(raw, observe_line=observed.__setitem__)
    assert observed == lines and observed['self-hierarchy'] is None
    raw['edges'].reverse()
    assert census.analyze_edges(raw) == result


def test_physical_row_evidence_compares_full_intervals_source_and_completeness():
    e = _edge('one', 'foo_parent_child', 'p', 'c', (1, 2), rows=[[7, 12]])
    other = _edge('two', 'foo_parent_of_foo', 'p', 'c', (1, 2), rows=[[7, 12]])
    assert census.analyze_edges(_publication([e, other]))['summary']['grouped_lines_by_row_evidence'] == {
        'same_recorded_physical_rows': 1}
    def outcome():
        return census.analyze_edges(_publication([e, other]))['multi_endpoint_groups'][0]['row_evidence']
    obs = other['supports'][0]
    obs['source_row_witness']['rows'] = [[7, 10], [11, 12]]
    assert outcome() == 'same_recorded_physical_rows'
    obs['provenance']['source']['schema'] = 'other'
    assert outcome() == 'different_recorded_row_sets'
    obs['provenance']['source']['schema'] = 'sales'
    obs['source_row_witness']['rows'] = [[7, 10]]
    assert outcome() == 'different_recorded_row_sets'
    obs['source_row_witness']['rows'] = [[10, 7]]
    assert outcome() == 'invalid_locator_or_source_address'
    del obs['source_row_witness']
    assert outcome() == 'partial_physical_rows'


def test_duplicate_edge_id_cannot_overwrite_a_line_binding():
    one = _edge('duplicate', 'cites', 'p', 'p', None)
    other = _edge('duplicate', 'related', 'p', 'c', None)
    with pytest.raises(ValueError, match='duplicate relation ID'):
        census.analyze_edges(_publication([one, other]))


FAMILIES = [{'family': 'containment', 'types': ['area_parent_child', 'area_parent_of_area', 'contains_area']}]


def _verified_edge(edge_id, relation, rows):
    edge = _edge(edge_id, relation, 'p', 'c', (1, 2), rows=rows)
    obs = edge['supports'][0]
    obs['business_context'] = {'status': 'global', 'dimensions': {}}
    obs['properties'] = {'basis': ['actual']}
    obs['provenance'].update(definition=relation, mapping=0, scope_columns=[], business_context_mapping={'status': 'global'},
                             constant_properties={'basis': 'actual'},
                             source_column_ordinals={'AREA_L1_ID': 1, 'AREA_L2_ID': 4})
    return edge


def test_declared_facts_coalesce_only_fully_witnessed_subgroups_preserving_every_id():
    a = _verified_edge('a', 'area_parent_child', [[2, 4]])
    b = _verified_edge('b', 'area_parent_of_area', [[2, 2], [3, 4]])
    unknown = _verified_edge('u', 'contains_area', [[2, 4]])
    del unknown['supports'][0]['source_row_witness']
    unlisted = _verified_edge('n', 'has_child', [[2, 4]])
    raw = _publication([a, b, unknown, unlisted])
    before = deepcopy(raw)
    output = census.analyze_relation_facts(raw, FAMILIES)
    assert raw == before
    assert output['summary']['raw_edges'] == 4 and output['summary']['hypothetical_distinct_assertions'] == 3
    assert output['summary']['confirmed_reduction'] == 1
    assert output['confirmed_groups'][0]['original_edge_ids'] == ['a', 'b']
    proof, = output['confirmed_groups'][0]['physical_row_proofs']
    assert proof['source']['table'] == 'Sheet1' and proof['rows'] == ((2, 4),)
    assert (proof['source_columns_with_ordinals'], proof['target_columns_with_ordinals']) == (
        (('AREA_L1_ID', 1),), (('AREA_L2_ID', 4),))
    assert output['candidate_groups'][0]['incomplete_edges_by_reason'] == {'missing_or_invalid_physical_rows': ['u']}
    assert output['candidate_groups'][0]['confirmed_subgroups'] == [output['edge_to_fact']['a']]
    assert len(set(output['edge_to_fact'].values())) == 3
    assert census.analyze_relation_facts(raw, [])['summary']['hypothetical_distinct_assertions'] == 4
    raw['edges'].reverse()
    assert output == census.analyze_relation_facts(raw, FAMILIES)


@pytest.mark.parametrize('different', ['scope', 'business', 'properties', 'missing_property', 'predicate', 'level', 'snapshot',
                                       'row', 'source', 'ordinal', 'orientation', 'unaccepted', 'unknown_business_scope'])
def test_relation_synonym_declaration_alone_does_not_confirm_equivalence(different):
    a = _verified_edge('a', 'area_parent_child', [[2, 4]])
    b = _verified_edge('b', 'area_parent_of_area', [[2, 4]])
    raw = _publication([a, b])
    obs = b['supports'][0]
    if different == 'scope':
        obs['required_scope_refs'] = ['private']
    if different == 'business':
        obs['business_context'] = {'status': 'scoped', 'dimensions': {'org': 'team'}}
    if different == 'unknown_business_scope':
        del obs['business_context']
    if different == 'properties':
        obs['properties']['basis'] = ['budget']
    if different == 'missing_property':
        del obs['properties']['basis']
    if different == 'predicate':
        obs['provenance']['predicate'] = {'op': 'eq', 'column': 'active', 'value': True}
    if different == 'level':
        obs['provenance']['target_key_columns'] = ['AREA_L3_ID']
        obs['provenance']['source_column_ordinals']['AREA_L3_ID'] = 7
    if different == 'snapshot':
        obs['source_row_witness']['snapshot'] = 'b' * 64
    if different == 'row':
        obs['source_row_witness']['rows'] = [[3, 4]]
    if different == 'source':
        obs['provenance']['source']['table'] = 'Sheet2'
    if different == 'ordinal':
        obs['provenance']['source_column_ordinals']['AREA_L1_ID'] = 3
    if different == 'orientation':
        raw['definitions']['relations'][1]['hierarchy_orientation'] = 'child_to_parent'
    if different == 'unaccepted':
        b['status'] = 'candidate'
    out = census.analyze_relation_facts(raw, FAMILIES)
    assert out['summary']['confirmed_reduction'] == 0 and out['summary']['hypothetical_distinct_assertions'] == 2
    assert out['candidate_groups'][0]['incomplete_edges_by_reason'] or out['candidate_groups'][0]['differing_evidence']


def test_physical_rows_must_match_per_observation_not_just_globally():
    a = _verified_edge('a', 'area_parent_child', [[10, 10]])
    b = _verified_edge('b', 'area_parent_of_area', [[20, 20]])
    for edge, other_row in [(a, 20), (b, 10)]:
        obs = deepcopy(edge['supports'][0])
        obs['scope_ref'] = 'operations'
        obs['source_row_witness']['rows'] = [[other_row, other_row]]
        edge['supports'].append(obs)
    result = census.analyze_relation_facts(_publication([a, b]), FAMILIES)
    assert result['summary']['confirmed_reduction'] == 0
    assert result['candidate_groups'][0]['differing_evidence'] == ['row_to_qualifier_binding']


@pytest.mark.parametrize('bad', [
    FAMILIES + FAMILIES, [{'family': 'x', 'types': ['a', 'a']}],
    [{'family': 'x', 'types': ['a', 'b']}, {'family': 'y', 'types': ['b', 'c']}],
])
def test_overlapping_relation_families_never_double_count(bad):
    with pytest.raises(ValueError):
        census.analyze_relation_facts(_publication([]), bad)


def test_projection_tuple_facts_never_claim_a_physical_row_or_database_snapshot():
    edges = [_verified_edge('a', 'area_parent_child', [[2, 4]]),
             _verified_edge('b', 'area_parent_of_area', [[2, 4]])]
    for edge in edges:
        support = edge['supports'][0]
        del support['source_row_witness']
        del support['provenance']['source_column_ordinals']
        support['provenance']['columns'] = ['AREA_L1_ID', 'AREA_L2_ID']
        support['source_projection_witness'] = {
            'kind': 'source_projection_v1', 'basis': 'complete_distinct_projection', 'complete': True,
            'source_binding': 'a' * 64, 'projection': 'b' * 64, 'tuple': 'c' * 64,
            'columns': ['AREA_L1_ID', 'AREA_L2_ID']}
    graph = _publication(edges)
    before = deepcopy(graph)
    result = census.analyze_relation_facts(graph, FAMILIES)
    assert graph == before and result['summary']['confirmed_reduction'] == 1
    group, = result['confirmed_groups']
    assert group['physical_row_proofs'] == []
    proof, = group['source_proofs']
    assert proof['kind'] == 'source_projection_v1' and proof['tuple'] == 'c' * 64
    assert 'rows' not in proof and 'snapshot' not in proof
    edges[1]['supports'][0]['source_projection_witness']['tuple'] = 'd' * 64
    assert census.analyze_relation_facts(graph, FAMILIES)['summary']['confirmed_reduction'] == 0
