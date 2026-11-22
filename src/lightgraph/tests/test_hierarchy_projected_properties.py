"""A property and a row-context cell can be two records of the same source observation."""
from copy import deepcopy

import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.construction.hierarchy_observations import observation_boundary, observation_properties
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.sources import MemorySource
from lightgraph.tests.test_source_hierarchy_replays import contributor


def _tree(unit, with_property):
    table = TableSpec(source_id='archive', name='catalog', columns=[
        ColumnSpec(name=c) for c in ['folder_id', 'folder', 'item_id', 'item', 'collection']])
    entity = 'Entry' + str(unit)
    binding = {'source_id': table.source_id, 'table': table.name, 'context_columns': ['collection']}
    if with_property:
        binding['properties'] = {'owner': 'collection'}
    draft = {
        'draft_id': f'd{unit}',
        'entities': [dict(type=entity, key_names=['code'], identity_scope='global', identity_domain=f'domain{unit}',
                          identity_evidence_refs=[f'e{unit}'], mappings=[
                              {**binding, 'key_columns': [key], 'label_column': label}
                              for key, label in [('folder_id', 'folder'), ('item_id', 'item')]])],
        'relations': [dict(type=f'r{unit}', source_type=entity, target_type=entity,
                           hierarchy_orientation='parent_to_child', mappings=[{
                               **binding, 'source_key_columns': ['folder_id'], 'target_key_columns': ['item_id']}])],
    }
    with MemorySource([table], {table.qualified_name: [('F', 'Folder', 'I', 'Item', 'A')]}) as source:
        result = execute_draft(draft, [source], [table])
    assert result['passed'], result['diagnostics']
    graph = result['graph']
    graph.update(draft_id=draft['draft_id'], draft_revision=1)
    return contributor(f'u{unit}', graph)


def test_shared_typed_attribute_matches_a_row_only_projection_and_survives_the_merge():
    units = [_tree(1, True), _tree(2, False)]
    graph = merge_construction_graphs(units)
    assert graph['fusion_diagnostics']['hierarchy_replay_collapsed_nodes'] == 2
    assert len(graph['edges']) == 2
    for node in graph['nodes']:
        assert node['properties']['owner'] == ['A']
        assert {s['contributor_id'] for s in node['supports']} == {'u1', 'u2'}
    for record in graph['nodes'] + graph['edges']:
        for s in record['supports']:
            assert s['row_context']['values'] == {'collection': 'A'}
            assert s['row_context']['value_types'] == {'collection': 'text'}
            assert bool(s['properties'].get('owner')) == (s['contributor_id'] == 'u1')


def _observation():
    return {'scope_ref': 'public', 'properties': {'owner': ['A']},
            'provenance': {'property_columns': {'owner': 'collection'},
                           'columns': ['id', 'collection'], 'context_columns': ['collection']},
            'row_context': {'values': {'collection': 'A'}, 'value_types': {'collection': 'text'}}}


def _comparison(s, positions=None):
    positions = positions or {}
    return observation_boundary(s, ['id'], positions), observation_properties(s, ['id'], positions)


@pytest.mark.parametrize(('section', 'changes'), [
    ('row_context', {'value_types': {}}),
    ('row_context', {'value_types': {'collection': 'integer'}}),
    ('row_context', {'values': {}}),
    ('properties', {'owner': ['B']}),
    ('properties', {'owner': ['A', 'B']}),
    ('provenance', {'property_columns': {}}),
    ('provenance', {'property_columns': {'owner': 'collection_alias'}}),
    ('provenance', {'columns': ['id']}),
    ('provenance', {'context_columns': []}),
    ('provenance', {'property_columns': {}, 'constant_properties': {'owner': 'A'}}),
])
def test_property_without_a_corroborating_typed_cell_remains_binding(section, changes):
    attributed = _observation()
    attributed[section].update(changes)
    plain = deepcopy(attributed)
    plain['properties'].clear()
    assert observation_properties(attributed, ['id'], {}) != observation_properties(plain, ['id'], {})


@pytest.mark.parametrize(('scope', 'collection'), [('other', 'A'), ('public', 'B')])
def test_row_only_projection_must_witness_the_same_scope_and_value(scope, collection):
    attributed = _observation()
    plain = deepcopy(attributed)
    plain['properties'].clear()
    plain['provenance'].pop('property_columns')
    plain['scope_ref'] = scope
    plain['row_context']['values']['collection'] = collection
    assert _comparison(attributed) != _comparison(plain)


def test_mapped_attribute_cannot_disappear_with_its_structural_context_column():
    first = _observation()
    second = deepcopy(first)
    second['properties']['owner'] = ['B']
    second['row_context']['values']['collection'] = 'B'
    assert _comparison(first, {'id': 2, 'collection': 1}) != _comparison(second, {'id': 2, 'collection': 1})
