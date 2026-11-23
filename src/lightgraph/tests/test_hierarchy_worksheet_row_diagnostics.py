"""Count supports at the point where replay actually compares the worksheet rows."""
import pytest

from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.tests.test_hierarchy_worksheet_rows import _copies, _edge_support, _tree


def _outcomes(units):
    graph = merge_construction_graphs(units)
    return graph['fusion_diagnostics']['hierarchy_replay_diagnostics']['worksheet_edge_supports_by_row_result']


@pytest.mark.parametrize('case,expected', [
    ('complete', {'normalized_to_physical_rows': 3}),
    ('legacy', {'no_row_witness': 3}),
    ('one_owner', {'no_row_witness': 1, 'single_witnessed_projection': 2}),
    ('invalid', {'invalid_row_witness': 1, 'normalized_to_physical_rows': 2}),
    ('conflict', {'conflicting_cells_or_columns': 2, 'normalized_to_physical_rows': 1}),
    ('budget', {'comparison_budget_exceeded': 3}),
])
def test_disposition_counts_each_entering_edge_support_once(case, expected, monkeypatch):
    first, second = _copies()
    left, = first['graph']['edges'][0]['supports']
    right, other = second['graph']['edges'][0]['supports']
    if case == 'legacy':
        for support in (left, right, other):
            support.pop('source_row_witness')
    elif case == 'one_owner':
        left.pop('source_row_witness')
    elif case == 'invalid':
        right['source_row_witness']['rows'] = [[5, 4]]
    elif case == 'conflict':
        # The first span has conflicting column coordinates. It overlaps one
        # long support, whose second span still has no conflicting cells.
        right['provenance']['source_column_ordinals']['folder_id'] = 8
    elif case == 'budget':
        from lightgraph.construction import hierarchy_observations
        monkeypatch.setattr(hierarchy_observations, '_MAX_EDGE_ROW_COMPARISONS', 2)
    assert _outcomes([first, second]) == expected


def test_original_boundaries_and_incomplete_headers_have_distinct_counts():
    units = [_tree(n, False) for n in (1, 2)]
    for unit in units:
        unit['graph']['edges'][0]['supports'] = [_edge_support(unit, {'owner': 'EBG'}, [[2, 2]])]
    assert _outcomes(units) == {'identical_witnessed_projections': 2}
    left, = units[0]['graph']['edges'][0]['supports']
    left['provenance'].pop('source_column_ordinals')
    assert _outcomes(units) == {'kept_exact_for_legacy_or_partial_header': 2}
    left['source_row_witness']['rows'] = [[3, 3]]
    assert _outcomes(units) == {'incomplete_column_ordinals': 1, 'normalized_to_physical_rows': 1}
