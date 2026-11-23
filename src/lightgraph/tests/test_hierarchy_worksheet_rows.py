"""A workbook snapshot and the complete physical row set can qualify different edge projections."""
from copy import deepcopy

import pytest

from lightgraph.contracts import canonical
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.source_values import scalar_kind
from lightgraph.tests.test_hierarchy_projected_properties import _tree


def _edge_support(unit, cells, ranges, *, snapshot='a' * 64, **fields):
    edge, = unit['graph']['edges']
    support = deepcopy(edge['supports'][0])
    support.update(fields)
    prov = support['provenance']
    prov.update(context_columns=list(cells),
                columns=sorted(set(prov['source_key_columns'] + prov['target_key_columns']) | set(cells)))
    header = ['folder_id', 'folder', 'item_id', 'item', 'collection', 'owner', 'leaf']
    prov['source_column_ordinals'] = {c: header.index(c) + 1 for c in prov['columns']}
    support['row_context'] = {'values': cells, 'value_types': {k: scalar_kind(v) for k, v in cells.items()}}
    support['source_row_witness'] = {'kind': 'excel_rows_v1', 'snapshot': snapshot, 'rows': ranges}
    return support


def _copies():
    first, second = _tree(1, False), _tree(2, False)
    first['graph']['edges'][0]['supports'] = [_edge_support(first, {'owner': 'EBG'}, [[2, 3]])]
    second['graph']['edges'][0]['supports'] = [
        _edge_support(second, {'folder_id': 'F', 'item_id': 'I', 'leaf': name}, [[n, n]])
        for n, name in [(2, 'L3-A'), (3, 'L3-B')]]
    return first, second


def test_worksheet_row_set_matches_different_edge_context_projections_without_changing_edges():
    first, second = _copies()
    original = deepcopy([first, second])
    g = merge_construction_graphs([first, second])
    assert [first, second] == original
    assert len(g['nodes']) == 2 and len(g['edges']) == 2
    assert len({(e['source'], e['target']) for e in g['edges']}) == 1
    def rows(edge):
        return sorted(canonical([s['row_context'], s['source_row_witness']]) for s in edge['supports'])
    assert {e['type']: rows(e) for e in g['edges']} == {
        e['type']: rows(e) for u in original for e in u['graph']['edges']}
    assert merge_construction_graphs([second, first]) == g


@pytest.mark.parametrize('difference', [
    'legacy', 'new_and_old', 'overlap_only', 'snapshot', 'missing_row', 'typed_cell', 'value',
    'malformed_range', 'missing_type', 'different_header', 'missing_ordinal', 'property', 'mapped_property', 'predicate', 'scope', 'direction', 'endpoint',
])
def test_worksheet_proof_cannot_override_a_conflict_or_substitute_for_an_unobserved_row(difference):
    first, second = _copies()
    left, = first['graph']['edges'][0]['supports']
    right, another = second['graph']['edges'][0]['supports']
    if difference in ('legacy', 'overlap_only'):
        for s in (left, right, another):
            del s['source_row_witness']
        if difference == 'overlap_only':
            for s in (left, right, another):
                s['row_context']['values']['shared'] = 'same'
                s['row_context']['value_types']['shared'] = 'text'
                s['provenance']['context_columns'].append('shared')
    elif difference == 'new_and_old':
        left.pop('source_row_witness')
    elif difference == 'snapshot':
        another['source_row_witness']['snapshot'] = 'b' * 64
    elif difference == 'missing_row':
        second['graph']['edges'][0]['supports'].pop()
    elif difference in ('typed_cell', 'value'):
        right['provenance']['context_columns'].append('owner')
        right['provenance']['columns'].append('owner')
        right['provenance']['source_column_ordinals']['owner'] = 6
        right['row_context']['values']['owner'] = 'EBG' if difference == 'typed_cell' else 'Other'
        right['row_context']['value_types']['owner'] = 'date' if difference == 'typed_cell' else 'text'
    elif difference == 'malformed_range':
        another['source_row_witness']['rows'] = [[3, 2]]
    elif difference == 'missing_type':
        del another['row_context']['value_types']['leaf']
    elif difference == 'different_header':
        right['provenance']['source_column_ordinals']['folder_id'] = 10
    elif difference == 'missing_ordinal':
        right['provenance']['source_column_ordinals'].pop('item_id')
    elif difference == 'property':
        left.update(properties={'version': ['one']})
        left['provenance']['constant_properties'] = {'version': 'one'}
    elif difference == 'mapped_property':
        left['provenance']['property_columns'] = {'department': 'owner'}
        left['properties'] = {'department': ['EBG']}
    elif difference == 'predicate':
        right['provenance']['predicate'] = {'op': 'eq', 'column': 'year', 'value': 2024}
    elif difference == 'scope':
        right['scope_ref'] = 'private'
    elif difference == 'direction':
        second['graph']['definitions']['relations'][0]['hierarchy_orientation'] = 'child_to_parent'
    elif difference == 'endpoint':
        right['row_context']['values']['folder_id'] = 'another'
    graph = merge_construction_graphs([first, second])
    assert len(graph['nodes']) == 4
    assert len({(e['source'], e['target']) for e in graph['edges']}) == 2


def test_legacy_member_and_equivalent_new_projection_keep_their_strict_match():
    first, second = _copies()
    legacy = _tree(3, False)
    support = _edge_support(legacy, {'owner': 'EBG'}, [[2, 3]])
    support.pop('source_row_witness')
    legacy['graph']['edges'][0]['supports'] = [support]
    graph = merge_construction_graphs([first, second, legacy])
    assert len(graph['nodes']) == 4
    ends = {e['type']: (e['source'], e['target']) for e in graph['edges']}
    assert ends['r1'] == ends['r3'] and ends['r1'] != ends['r2']


def test_isolated_role_uses_the_original_scope_of_each_witnessed_anchor():
    from lightgraph.tests.test_hierarchy_isolated_roles import _unit
    units = [_unit(k) for k in ('Entry', 'Term', 'Work')]
    for i, unit in enumerate(units):
        edge, = unit['graph']['edges']
        support, = edge['supports']
        support['source_row_witness'] = {'kind': 'excel_rows_v1', 'snapshot': 'a' * 64, 'rows': [[3, 3]]}
        if i == 2:
            # Same physical row and direct edge. The extra context has no match
            # on this third unit's endpoint mappings and cannot teach S its role.
            support['provenance']['context_columns'].append('edition')
            support['provenance']['columns'].append('edition')
            support['row_context']['values']['edition'] = 'special'
            support['row_context']['value_types']['edition'] = 'text'
        header = ['upper', 'upper_title', 'lower', 'lower_title', 'collection', 'edition']
        prov = support['provenance']
        prov['source_column_ordinals'] = {c: header.index(c) + 1 for c in prov['columns']}
    graph = merge_construction_graphs(units)
    by_value = {}
    for n in graph['nodes']:
        by_value.setdefault(next(iter(n['identity'].values())), []).append(n)
    assert len(by_value['P']) == len(by_value['C']) == 1
    assert len(by_value['S']) == 2
    assert {frozenset(o['contributor_id'] for s in n['supports'] for o in s['execution_origins'])
            for n in by_value['S']} == {
        frozenset({'Entry', 'Term'}), frozenset({'Work'})}


@pytest.mark.parametrize('observed_root, agrees', [('East', True), ('West', False)])
def test_relaxed_edge_projection_keeps_complementary_ancestor_checks(observed_root, agrees):
    from lightgraph.construction.hierarchy_observations import ObservationBoundary, observation_scope
    from lightgraph.construction.hierarchy_replay import _ancestor_paths_agree
    address = ('db', 'public', 'catalog')
    layout = {'root': 1, 'parent': 2, 'leaf': 3}
    base = {'scope_ref': 'public', 'provenance': {
        'source': dict(zip(('source_id', 'schema', 'table'), address)), 'key_columns': ['leaf']}}
    # Unit A recorded ancestry on the member. Unit B recorded it only on
    # its incident edge whose row boundary has been physically aligned.
    left = dict(base, row_context={'values': {'root': 'East', 'dept': 'x'},
                                  'value_types': {'root': 'text', 'dept': 'text'}})
    nodes = {'a': {'type': 'A', 'supports': [left]}, 'b': {'type': 'B', 'supports': [base]}}
    layouts = {(k, address, ('leaf',)): layout for k in ('A', 'B')}
    row = {'values': {'root': observed_root}, 'value_types': {'root': 'text'}}
    fact = (ObservationBoundary(observation_scope(base), ('excel_rows_v1', 'a' * 64, 2, 2, ()), ()), '[]')
    edge_rows = {'b': [(('leaf',), (('parent',), 'P'), row, layout, fact)]}
    assert _ancestor_paths_agree(nodes.keys(), nodes, layouts, edge_rows) is agrees


def test_budget_exhaustion_retains_nodes_and_reports_unverified_physical_edge(monkeypatch):
    from lightgraph.construction import hierarchy_observations
    monkeypatch.setattr(hierarchy_observations, '_MAX_EDGE_ROW_COMPARISONS', 2)
    graph = merge_construction_graphs(_copies())
    assert len(graph['nodes']) == 4 and len(graph['edges']) == 2
    stats = graph['fusion_diagnostics']['hierarchy_replay_diagnostics']
    assert stats['worksheet_edges_exceeding_row_comparison_budget'] == 1


@pytest.mark.parametrize('second_row, expected', [(2, 2), (3, 4)])
def test_partial_header_keeps_historical_exact_match_and_respects_known_row_conflict(second_row, expected):
    units = [_tree(1, False), _tree(2, False)]
    for unit, number in zip(units, (2, second_row), strict=True):
        support = _edge_support(unit, {'owner': 'EBG'}, [[number, number]])
        unit['graph']['edges'][0]['supports'] = [support]
    units[0]['graph']['edges'][0]['supports'][0]['provenance'].pop('source_column_ordinals')
    assert len(merge_construction_graphs(units)['nodes']) == expected
