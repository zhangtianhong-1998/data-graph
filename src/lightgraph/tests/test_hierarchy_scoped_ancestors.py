"""Historical projections must agree on which ancestor belongs to which scope."""
from collections import Counter

import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.construction.hierarchy_replay import _ancestor_paths_agree, _complementary_paths_agree
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.sources import MemorySource
from lightgraph.tests.test_source_hierarchy_replays import code, contributor


def _catalog_copy(number, first_root, second_root, capture, declared_scope):
    levels = ['route', 'section', 'item']
    columns = ['edition', *levels, 'route_caption', 'section_caption', 'item_caption']
    table = TableSpec(source_id='archive', name='catalog', columns=[ColumnSpec(name=k) for k in columns])
    rows = [('green', first_root, 'Shelf', 'Book', first_root, 'Shelf', 'Book'),
            ('blue', second_root, 'Shelf', 'Book', second_root, 'Shelf', 'Book')]
    source = {'source_id': 'archive', 'table': 'catalog'}
    if declared_scope:
        source['applicability'] = {'status': 'scoped', 'columns': {'edition': 'edition'}}
    edition_columns = [] if declared_scope else ['edition']
    node_source = {**source, 'context_columns': edition_columns + (levels if capture == 'nodes' else [])}
    edge_source = {**source, 'context_columns': edition_columns + (levels if capture == 'edges' else [])}
    kind = f'Entry{number}'
    draft = {'draft_id': f'tree{number}', 'entities': [{'type': kind, 'key_names': ['id'],
             'identity_scope': 'global', 'identity_domain': f'catalog{number}', 'identity_evidence_refs': [f'e{number}'],
             'mappings': [{**node_source, 'key_columns': [key], 'label_column': key + '_caption'} for key in levels]}],
             'relations': [{'type': f'contains{number}', 'source_type': kind, 'target_type': kind,
             'hierarchy_orientation': 'parent_to_child', 'mappings': [{**edge_source, 'source_key_columns': [parent],
             'target_key_columns': [child]} for parent, child in zip(levels, levels[1:])]}]}
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        result = execute_draft(draft, [adapter], [table])
    assert result['passed'], result['diagnostics']
    return contributor(f'u{number}', dict(result['graph'], draft_id=draft['draft_id'], draft_revision=1))


@pytest.mark.parametrize('second_roots, copies', [(('East', 'West'), 1), (('West', 'East'), 2)])
@pytest.mark.parametrize('first, second, declared_scope', [
    ('nodes', 'nodes', True), ('edges', 'edges', False), ('nodes', 'edges', True), ('edges', 'nodes', False)])
def test_ancestor_and_edition_must_be_witnessed_together(second_roots, copies, first, second, declared_scope):
    units = [_catalog_copy(1, 'East', 'West', first, declared_scope),
             _catalog_copy(2, *second_roots, second, declared_scope)]
    merged = merge_construction_graphs(units)
    assert Counter(map(code, merged['nodes']))['Book'] == copies


def test_disjoint_recorded_windows_do_not_hide_different_observed_ancestors():
    location = {'source_id': 'archive', 'schema': 'public', 'table': 'catalog'}

    def member(edition, root):
        def observation(scope):
            cells = {'route': root} if scope == edition else {}
            return {'scope_ref': scope, 'provenance': {'source': location, 'key_columns': ['item']},
                    'row_context': {'values': cells, 'value_types': dict.fromkeys(cells, 'text')}}
        return {'type': 'Entry', 'supports': [observation(s) for s in ('green', 'blue')]}

    nodes = {'first': member('green', 'East'), 'second': member('blue', 'West')}
    layouts = {('Entry', ('archive', 'public', 'catalog'), ('item',)): {'route': 1, 'item': 2}}
    assert not _ancestor_paths_agree(nodes.keys(), nodes, layouts, {})


def test_edge_row_can_witness_a_subset_of_the_member_paths():
    node_rows = {'green': {True: [(('route', 'section'), {('East', 'Shelf'), ('West', 'Shelf')})]}}
    edge_rows = {'green': {False: [(('route',), {('East',)})]}}
    assert _complementary_paths_agree(node_rows, edge_rows)
