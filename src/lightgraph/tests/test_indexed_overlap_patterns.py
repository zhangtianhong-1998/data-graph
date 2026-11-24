"""A type is reviewed once; only routed evidence pages may be opened."""
from copy import deepcopy
import json

import pytest

from lightgraph.construction import overlap_patterns as overlap
from lightgraph.errors import LightGraphError
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.tests.test_batch_overlap_patterns import unit


def regions(owner, name, key):
    branch = unit(owner, name, key, table='geo', column='region_id')
    nodes = branch['graph']['nodes']
    nodes.append(deepcopy(nodes[1]))
    nodes[2]['id'] = owner + '_third'
    for node, (code, label) in zip(nodes, [('CN11', '北京地区'), ('CN32', '江苏地区'), ('CN12', '天津地区')], strict=True):
        node['identity'][key] = code
        node['label'] = node['supports'][0]['label'] = label
    return branch


def indexed(c):
    index = json.loads(json.dumps(overlap.build_overlap_index(c)))
    return {k: v for k, v in c.items() if k != 'graph'} | {'overlap_index': index}


class NoEvidence(dict):
    def __getitem__(self, key):
        if key in ('evidence', 'aliases'):
            pytest.fail(f'unrelated page was read: {key}')
        return super().__getitem__(key)


def test_three_region_definitions_share_one_rule_and_unrelated_tables_need_no_evidence(monkeypatch):
    batch = [regions('north', 'SalesRegion', 'area'), regions('east', 'ServiceZone', 'zone'),
             regions('shared', 'OfficeRegion', 'location')]
    other = unit('other', 'Calendar', 'month_code', table='dates', column='calendar_id')
    descriptors = [indexed(c) for c in [*batch, other]]
    raw_index = json.dumps(descriptors, ensure_ascii=False)
    assert all(value not in raw_index for value in ('CN11', '北京地区', 'north_0', 'north_edge', 'private_value'))
    directory = descriptors[-1]['overlap_index']
    directory['families'] = [NoEvidence(row) for row in directory['families']]
    directory['relations'] = [NoEvidence(row) for row in directory['relations']]
    # Four graphs exceed the old aggregate raw-record limit. The three
    # candidate type pages (nine keys) and a relation page per peer fit.
    monkeypatch.setattr(overlap, 'MAX_RECORDS', 12)
    result = overlap.analyze_indexed_overlap_patterns(descriptors)
    card, = [p for p in result.patterns if p['kind'] == 'entity']
    assert card['overlapping_keys'] == 3
    assert {m['type'] for m in card['members']} == {'SalesRegion', 'ServiceZone', 'OfficeRegion'}
    assert result.summary['index_sections_read']['family_sections'] == 3
    assert result.summary['index_sections_read']['relation_sections'] == 3
    assert result.summary['index_directories'] == {'families': 4, 'relations': 4}
    policy = overlap.apply_overlap_decisions(None, result, [card['pattern_id']])
    merged = merge_construction_graphs([*batch, other], merge_policy=policy)
    members = [n for n in merged['nodes'] if n['type'] != 'Calendar']
    assert len(members) == 3 and len(merged['nodes']) == 5
    assert {n['label'] for n in members} == {'北京地区', '江苏地区', '天津地区'}
    assert all({origin['contributor_id'] for s in n['supports'] for origin in s['execution_origins']}
               == {'north', 'east', 'shared'} for n in members)


def test_same_region_type_across_branches_yields_one_review_card():
    result = overlap.analyze_indexed_overlap_patterns([indexed(regions(owner, 'Region', 'code'))
                                                       for owner in ('north', 'east', 'shared')])
    card, = [p for p in result.patterns if p['kind'] == 'entity']
    assert card['overlapping_keys'] == 3 and len(card['members']) == 3
    assert {m['type'] for m in card['members']} == {'Region'}
    assert 'type_used_by_other_projection' in card['reasons']


def test_missing_or_old_index_never_asks_for_a_graph():
    branches = [indexed(regions(owner, 'Region', 'code')) for owner in ('a', 'b')]
    del branches[0]['overlap_index']
    branches[1]['overlap_index']['format_version'] = 0
    result = overlap.analyze_indexed_overlap_patterns(branches)
    assert result.summary['missing_index_contributors'] == ['a']
    assert result.summary['incompatible_index_contributors'] == ['b']
    assert result.summary['index_sections_read'] == {'family_sections': 0, 'alias_sections': 0, 'relation_sections': 0}
    assert not result.rules_by_pattern


def test_incomplete_legacy_unit_can_still_commit_while_index_approval_is_unavailable():
    old = {'contributor_id': 'a', 'graph': {'nodes': [{'id': 'legacy'}], 'edges': []}}
    index = overlap.build_overlap_index(old)
    assert index['status'] == 'unavailable' and not index['families']


def test_candidate_evidence_budget_never_approves_a_partial_type(monkeypatch):
    branches = [indexed(regions(owner, name, 'code')) for owner, name in [('a', 'Region'), ('b', 'Zone')]]
    monkeypatch.setattr(overlap, 'MAX_RECORDS', 4)
    result = overlap.analyze_indexed_overlap_patterns(branches)
    assert result.summary['truncated'] and result.summary['index_evidence_budget']['skipped_groups'] > 0
    assert result.summary['index_sections_read']['family_sections'] == 0 and not result.rules_by_pattern


def test_same_serial_codes_with_disjoint_names_need_no_alias_sections():
    a = unit('a', 'Order', 'order_id', table='orders')
    b = unit('b', 'Customer', 'client_id', table='clients')
    for i, n in enumerate(b['graph']['nodes']):
        n['supports'][0]['label'] = n['label'] = '客户' + str(i)
    inputs = [indexed(a), indexed(b)]
    for c in inputs:
        c['overlap_index']['families'] = [NoEvidence(row) for row in c['overlap_index']['families']]
    result = overlap.analyze_indexed_overlap_patterns(inputs)
    assert result.summary['index_sections_read']['alias_sections'] == 0
    assert not result.patterns


def districts_from_table(owner, codes):
    entry = regions(owner, owner + 'District', owner + '_code')
    graph = entry['graph']
    original = graph['nodes'][0]
    graph['nodes'], graph['edges'] = [], []
    for i, (code, name) in enumerate(codes):
        node = deepcopy(original)
        node['id'] = owner + '_' + str(i)
        node['identity'][owner + '_code'] = code
        support, = node['supports']
        node['label'] = support['label'] = name
        support['provenance']['source']['table'] = owner + '_geo'
        graph['nodes'].append(node)
    return entry


def test_three_differently_named_regional_keys_are_one_weak_pattern():
    places = [('11', '北京'), ('12', '天津'), ('32', '江苏')]
    branches = [indexed(districts_from_table(owner, places)) for owner in ('billing', 'sales', 'service')]
    result = overlap.analyze_indexed_overlap_patterns(branches)
    candidate, = result.patterns
    assert candidate['basis'] == 'same_typed_keys_and_names_across_sources'
    assert candidate['eligible_actions'] == ['retain'] and candidate['overlapping_keys'] == 3
    assert candidate['pair_coverage'] == [
        {'member_indexes': [0, 1], 'overlapping_keys': 3}, {'member_indexes': [0, 2], 'overlapping_keys': 3},
        {'member_indexes': [1, 2], 'overlapping_keys': 3}]
    assert not result.rules_by_pattern and result.summary['index_sections_read']['family_sections'] == 0
    assert '北京' not in json.dumps(candidate, ensure_ascii=False)
    with pytest.raises(LightGraphError, match='invalid_overlap_pattern_decision'):
        overlap.apply_overlap_decisions(None, result, [candidate['pattern_id']])


@pytest.mark.parametrize('case, pair_count', [('chain', 2), ('partial', 3), ('scope', 3)])
def test_alias_pairs_need_identical_shared_witnesses_and_compatible_scope_to_group(case, pair_count):
    north = [('11', '北京'), ('12', '天津')]
    east = [('32', '江苏'), ('33', '浙江')]
    rows = [north, north + east, east] if case == 'chain' else [north + east, north + east, north]
    if case == 'scope':
        rows = [north, north, north]
    batch = [districts_from_table(owner, places) for owner, places in zip(('first', 'second', 'third'), rows)]
    if case == 'scope':
        for n in batch[-1]['graph']['nodes']:
            n['supports'][0]['scope_ref'] = 'other-permission'
    result = overlap.analyze_indexed_overlap_patterns([indexed(c) for c in batch])
    assert len(result.patterns) == pair_count
    assert all(len(p['members']) == 2 and p['eligible_actions'] == ['retain'] for p in result.patterns)
    assert not result.rules_by_pattern
