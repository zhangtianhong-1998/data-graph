from collections import Counter
from types import SimpleNamespace

import pytest

from lightgraph.construction.hierarchy_neighborhood import neighborhood_difference_counts
from lightgraph.construction.hierarchy_observations import ObservationBoundary
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.tests.test_hierarchy_projection_variants import _facts
from lightgraph.tests.test_source_hierarchy_replays import contributor, draft_graph


@pytest.mark.parametrize(('annotations', 'can_fold'), [
    ({'parent_position': 'L1', 'child_level': 2, 'source_position': 'L2', 'target_level': 1}, True),
    ({'parent_position': 'section', 'child_position': 'leaf'}, True),
    ({'parent_position': 'leaf'}, False),  # Same physical edge, but the opposite endpoint.
    ({'source_level': 1}, False),  # In the second declaration the source is the child.
    ({'project': 'L2'}, False),
])
def test_reversed_relationships_compare_each_position_in_its_own_endpoint(annotations, can_fold):
    inputs = [contributor('first', draft_graph(1, 'Catalog')),
              contributor('second', draft_graph(2, 'Archive', inverse=True, edge_constants=annotations))]
    before = merge_construction_graphs(inputs, recognize_source_replays=False)
    after = merge_construction_graphs(inputs)
    assert len(after['nodes']) == (3 if can_fold else 6)
    assert _facts(before) == _facts(after)
    if not can_fold:
        stats = after['fusion_diagnostics']['hierarchy_replay_diagnostics']
        assert stats['equal_node_pairs_by_result']['different_neighborhood'] == 3
        assert stats['different_neighborhood_pairs_by_first_difference'] == {'edge_properties': 3}


def _signature(value='A', parent=True, cols=('leaf',), label='Caption', scope='public', predicate=(),
               context='{}', properties='[]'):
    return frozenset({(parent, ('root',), cols, value, frozenset({label}),
                       (ObservationBoundary(scope, context, predicate), properties))})


@pytest.mark.parametrize(('change', 'expected'), [
    ({'value': 'B', 'properties': 'other'}, 'neighbor_values'),
    ({'parent': False, 'cols': ('later',)}, 'direction'),
    ({'cols': ('branch',)}, 'endpoint_columns'),
    ({'label': 'other'}, 'neighbor_observations'),
    ({'scope': 'team'}, 'edge_applicability'),
    ({'predicate': ('edition',)}, 'edge_predicate'),
    ({'context': 'edition'}, 'edge_row_context'),
    ({'properties': 'edition'}, 'edge_properties'),
])
def test_neighborhood_counts_exclude_same_unit_pairs_and_count_only_the_first_difference(change, expected):
    # Six cross-unit pairs overall: A-C, B-C, D-E agree in their respective
    # signatures; A-E, B-E, C-D disagree. Pairs within one unit never count.
    matches = {_signature(): {'A', 'B', 'C'}, _signature(**change): {'D', 'E'}}
    members = {nid: SimpleNamespace(unit=unit) for nid, unit in zip('ABCDE', ('one', 'one', 'two', 'one', 'two'))}
    assert neighborhood_difference_counts(matches, members) == Counter({expected: 3})


def test_correlated_edge_observations_are_compared_on_the_matching_physical_neighbor():
    first = _signature(value='A', scope='north') | _signature(value='B', scope='south')
    second = _signature(value='A', scope='south') | _signature(value='B', scope='north')
    matches = {first: {'x'}, second: {'y'}}
    members = {nid: SimpleNamespace(unit=nid) for nid in ('x', 'y')}
    assert neighborhood_difference_counts(matches, members) == {'edge_applicability': 1}


def test_a_proper_subset_of_observed_neighbors_cannot_stand_in_for_the_whole_hierarchy():
    first = contributor('first', draft_graph(1, 'Collection'))
    second = contributor('second', draft_graph(2, 'Archive'))
    second['graph']['edges'].pop()
    result = merge_construction_graphs([first, second])
    roots = [n for n in result['nodes'] if list(n['identity'].values()) == ['X']]
    assert len(roots) == 2
    stats = result['fusion_diagnostics']['hierarchy_replay_diagnostics']
    assert stats['different_neighborhood_pairs_by_first_difference'] == {'neighbor_values': 1}
