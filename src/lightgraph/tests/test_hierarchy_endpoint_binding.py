"""Verify neighboring projections against the physical slot named on each edge."""
from collections import Counter
from copy import deepcopy

import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.sources import MemorySource
from lightgraph.tests.test_hierarchy_projection_variants import _facts
from lightgraph.tests.test_source_hierarchy_replays import code, contributor


def _projection(unit, changed_slot, *, edition_names=None):
    table = TableSpec(source_id='file', name='catalog', columns=[ColumnSpec(name=c) for c in
                      ['upper', 'upper_name', 'lower', 'lower_name', 'edition']])
    rows = [('R', 'Root', 'X', 'Branch', 'old'), ('X', 'Branch', 'Y', 'Leaf', 'new')]
    source = {'source_id': 'file', 'table': 'catalog'}
    mappings = [{**source, 'key_columns': [slot], 'label_column': f'{slot}_name'} for slot in ('upper', 'lower')]
    if edition_names:
        rows = [('R', 'Root', 'X', name, edition) for name, edition in zip(edition_names, ('old', 'new'))]
        mappings[1]['applicability'] = {'status': 'scoped', 'columns': {'edition': 'edition'}}
    if changed_slot:
        # This is an additional bounded observation about X at one position.
        # Both graphs retain the unfiltered observations of the whole catalog.
        mapping = deepcopy(next(m for m in mappings if m['key_columns'] == [changed_slot]))
        mapping.update(predicate={'column': changed_slot, 'op': 'eq', 'value': 'X'},
                       properties={'edition': 'edition'}, context_columns=['edition'],
                       applicability={'status': 'scoped', 'values': {'edition': 'local'}})
        mappings.append(mapping)
    inverse = unit == 'second'
    src, dst = ('lower', 'upper') if inverse else ('upper', 'lower')
    draft = {'draft_id': unit, 'entities': [{
        'type': unit, 'key_names': ['code'], 'identity_scope': 'global', 'identity_domain': unit,
        'identity_evidence_refs': ['e1'], 'mappings': mappings}],
        'relations': [{'type': 'edge_' + unit, 'source_type': unit, 'target_type': unit,
                       **({'hierarchy_orientation': 'child_to_parent'} if inverse else {}),
                       'mappings': [{**source, 'source_key_columns': [src], 'target_key_columns': [dst]}]}]}
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        result = execute_draft(draft, [adapter], [table], label_conflict_policy='retain_variants')
    assert result['passed'], result['diagnostics']
    result['graph'].update(draft_id=unit, draft_revision=1)
    return contributor(unit, result['graph'])


@pytest.mark.parametrize('changed_slot,expected', [
    ('upper', {'R': 1, 'X': 2, 'Y': 2}),
    ('lower', {'R': 2, 'X': 2, 'Y': 1}),
])
def test_only_the_observed_endpoint_binding_can_supply_direction_and_neighbor_evidence(changed_slot, expected):
    units = [_projection('first', None), _projection('second', changed_slot)]
    before = merge_construction_graphs(units, recognize_source_replays=False)
    assert Counter(map(code, before['nodes'])) == {'R': 2, 'X': 2, 'Y': 2}
    after = merge_construction_graphs(units)
    assert Counter(map(code, after['nodes'])) == expected
    assert {n['id'] for n in after['nodes'] if code(n) == 'X'} == {
        n['id'] for n in before['nodes'] if code(n) == 'X'}
    assert _facts(after) == _facts(before)


@pytest.mark.parametrize('second_names,root_copies', [(('One', 'Two'), 1), (('Two', 'One'), 2)])
def test_endpoint_label_must_belong_to_the_same_observed_scope(second_names, root_copies):
    units = [_projection('first', None, edition_names=('One', 'Two')),
             _projection('second', None, edition_names=second_names)]
    before = merge_construction_graphs(units, recognize_source_replays=False)
    after = merge_construction_graphs(units)
    assert Counter(map(code, after['nodes'])) == {'R': root_copies, 'X': 2}
    assert _facts(after) == _facts(before)
