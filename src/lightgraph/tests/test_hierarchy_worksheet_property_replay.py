"""A mapped edge property can corroborate a typed cell on the same worksheet rows."""
from copy import deepcopy

import pytest
from openpyxl import Workbook

from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workflow import load_unit_contributor
from lightgraph.construction.workspace import dump_yaml
from lightgraph.runtime.config import RunConfig
from lightgraph.tests.test_excel_hierarchy_checkpoint_merge import (
    _draft, _edge_observations, _node_origins, _without_evidence,
)
from lightgraph.tests.test_hierarchy_worksheet_rows import _edge_support, _tree


@pytest.fixture(scope='module')
def worksheet_units(tmp_path_factory):
    folder = tmp_path_factory.mktemp('worksheet-property')
    path = folder / 'tree.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.title = 'tree'
    sheet.append(['center', 'parent_code', 'parent_name', 'unused', 'child_code', 'child_name', 'leaf_code'])
    for leaf in ['X', 'Y']:
        sheet.append(['EBG', 'P', 'Parent', None, 'C', 'Child', leaf])
    book.save(path)
    book.close()

    config = RunConfig(sources=[{'id': 'excel', 'kind': 'excel_workbook', 'path': str(path)}],
                       model={'mode': 'in_environment'})
    units = []
    for number in (2, 1, 3):
        unit = f'extract_{number}'
        output = folder / unit
        tools = ConstructionTools(config, output / 'construction')
        try:
            meta = tools.call('inspect_source', table='excel:public.tree', operation='get_metadata')
            draft = _draft(number, meta['evidence_ref'])
            binding, = draft['relations'][0]['mappings']
            if number == 1:
                binding.update(context_columns=['parent_code', 'child_code', 'leaf_code'],
                               properties={'department': 'center'})
            elif number == 3:
                binding.update(context_columns=['center'])
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
            validation = tools.call('validate_graph_draft', draft_id=draft['draft_id'])
            assert validation['passed'], validation
            assert tools.call('publish_graph_draft', draft_id=draft['draft_id'])['status'] == 'ok'
        finally:
            tools.close()
        units.append(load_unit_contributor(output, unit))
    path.unlink()
    return units


@pytest.fixture
def property_units(worksheet_units):
    return worksheet_units[:2]


def test_observed_property_matches_its_typed_cell_over_the_complete_row_set(property_units):
    left, right = [unit['graph']['edges'][0]['supports'] for unit in property_units]
    assert [s['source_row_witness']['rows'] for s in left] == [[[2, 3]]]
    assert left[0]['row_context']['values'] == {'center': 'EBG'}
    assert left[0]['row_context']['value_types'] == {'center': 'text'}
    assert sorted(s['source_row_witness']['rows'] for s in right) == [[[2, 2]], [[3, 3]]]
    for s in right:
        assert 'center' not in s['row_context']['values']
        assert s['properties'] == {'department': ['EBG']}
        assert s['provenance']['property_columns'] == {'department': 'center'}
        assert s['provenance']['source_column_ordinals']['center'] == 1

    before = merge_construction_graphs(property_units, recognize_source_replays=False)
    merged = merge_construction_graphs(property_units)
    assert merged['fusion_diagnostics']['hierarchy_replay_collapsed_nodes'] == 2
    ids = {n['identity']['code']: n['id'] for n in merged['nodes']}
    assert len(merged['nodes']) == 2 and len(merged['edges']) == 2
    assert {e['type']: (e['source'], e['target']) for e in merged['edges']} == {
        'link2': (ids['P'], ids['C']), 'link1': (ids['C'], ids['P'])}
    assert _edge_observations(merged) == _edge_observations(before)
    assert _node_origins(merged) == _node_origins(before)
    assert merge_construction_graphs(list(reversed(property_units))) == merged


@pytest.mark.parametrize('missing', [['source_row_witness'], ['source_row_witness', 'source_column_ordinals']])
def test_property_and_row_context_stay_separate_without_worksheet_evidence(property_units, missing):
    assert len(merge_construction_graphs(_without_evidence(property_units, missing))['nodes']) == 4


@pytest.mark.parametrize('conflict', ['edge_only', 'empty', 'several', 'value', 'other_row', 'other_snapshot',
                                      'other_column', 'no_ordinal', 'constant', 'date_text', 'boolean_integer'])
def test_property_must_be_a_single_observation_of_this_typed_cell(property_units, conflict):
    units = deepcopy(property_units)
    row, = units[0]['graph']['edges'][0]['supports']
    edge, = units[1]['graph']['edges']
    support, other = edge['supports']
    if conflict == 'edge_only':
        assert edge['properties']['department'] == ['EBG']
        support['properties'].clear()
    elif conflict == 'empty':
        support['properties']['department'] = []
    elif conflict == 'several':
        support['properties']['department'].append('Other')
    elif conflict == 'value':
        support['properties']['department'] = ['North']
    elif conflict == 'other_row':
        support['source_row_witness']['rows'] = [[99, 99]]
    elif conflict == 'other_snapshot':
        support['source_row_witness']['snapshot'] = 'a' * 64
    elif conflict == 'other_column':
        support['provenance']['source_column_ordinals']['center'] = 10
    elif conflict == 'no_ordinal':
        support['provenance']['source_column_ordinals'].pop('center')
    elif conflict == 'constant':
        support['provenance']['constant_properties'] = {'department': 'EBG'}
    else:
        cell, kind, property_value = (('2023-06-07', 'date', '2023-06-07') if conflict == 'date_text'
                                      else (True, 'boolean', 1))
        row['row_context']['values']['center'] = cell
        row['row_context']['value_types']['center'] = kind
        for s in [support, other]:
            s['properties']['department'] = [property_value]
    assert len(merge_construction_graphs(units)['nodes']) == 4


@pytest.mark.parametrize(('third', 'expected'), [
    ('same', 2), ('other_scope', 0), ('other_predicate', 0),
    ('missing_mapped_value', 0), ('contradictory_mapped_value', 0),
])
def test_third_unit_must_supply_a_consistent_and_applicable_witness(worksheet_units, third, expected):
    units = deepcopy(worksheet_units)
    row, = units[0]['graph']['edges'][0]['supports']
    row['provenance']['context_columns'] = ['parent_code']
    row['row_context']['values'] = {'parent_code': 'P'}
    row['row_context']['value_types'] = {'parent_code': 'text'}
    # Only the third unit still observes the typed center cell on the two rows.
    witness, = units[2]['graph']['edges'][0]['supports']
    if third == 'other_scope':
        witness['scope_ref'] = 'other'
    elif third == 'other_predicate':
        witness['provenance']['predicate'] = {'column': 'center', 'op': 'eq', 'value': 'EBG'}
    elif third in ('missing_mapped_value', 'contradictory_mapped_value'):
        witness['provenance']['property_columns'] = {'department': 'center'}
        if third == 'contradictory_mapped_value':
            witness['properties']['department'] = ['North']
    stats = merge_construction_graphs(units)['fusion_diagnostics']['hierarchy_replay_diagnostics']
    assert stats.get('worksheet_property_corroborated_supports', 0) == expected


@pytest.mark.parametrize('left_repeats_its_attribute_as_context', [False, True])
def test_same_named_property_cannot_change_its_source_column_on_identical_rows(property_units, left_repeats_its_attribute_as_context):
    first, second = deepcopy(property_units)
    left, = first['graph']['edges'][0]['supports']
    left['provenance']['property_columns'] = {'department': 'other_center'}
    left['properties'] = {'department': ['EBG']}
    for unit in (first, second):
        for support in unit['graph']['edges'][0]['supports']:
            prov = support['provenance']
            prov['columns'].append('other_center')
            prov['source_column_ordinals']['other_center'] = 4
    # Each side independently observes both equal cells, but writes its
    # department attribute from a different worksheet column.
    for support in second['graph']['edges'][0]['supports']:
        support['provenance']['context_columns'].append('other_center')
        support['row_context']['values']['other_center'] = 'EBG'
        support['row_context']['value_types']['other_center'] = 'text'
    if left_repeats_its_attribute_as_context:
        # One attribute was already proved by its own row context. Its declared
        # physical column must still constrain a newly corroborated attribute.
        left['provenance']['context_columns'].append('other_center')
        left['row_context']['values']['other_center'] = 'EBG'
        left['row_context']['value_types']['other_center'] = 'text'
    graph = merge_construction_graphs([first, second])
    assert len(graph['nodes']) == 4 and len(graph['edges']) == 2


def test_extra_mapping_in_one_unit_cannot_replace_a_previously_matching_pair():
    first, second, witness = [_tree(number, False) for number in (1, 2, 3)]
    for unit in (first, second):
        support = _edge_support(unit, {'leaf': 'L'}, [[2, 2]])
        prov = support['provenance']
        prov['columns'].append('owner')
        prov['source_column_ordinals']['owner'] = 6
        prov['property_columns'] = {'department': 'owner'}
        support['properties'] = {'department': ['EBG']}
        unit['graph']['edges'][0].update(supports=[support], properties=support['properties'], provenance=[prov])
    support = _edge_support(witness, {'owner': 'EBG'}, [[2, 2]])
    witness['graph']['edges'][0].update(supports=[support], provenance=[support['provenance']])
    # The third mapping shares the first unit. Its typed cell independently
    # corroborates only the second unit's property on this physical row.
    for key in ('entities', 'relations'):
        first['graph']['definitions'][key].extend(witness['graph']['definitions'][key])
    for key in ('nodes', 'edges', 'evidence_refs'):
        first['graph'][key].extend(witness['graph'][key])
    first['evidence_records'].update(witness['evidence_records'])

    baseline = merge_construction_graphs([first, second], recognize_source_replays=False)
    original = {(n['type'], n['identity']['code']): n['id'] for n in baseline['nodes']}
    expected = set()
    for code in ('F', 'I'):
        expected.add(frozenset((original['Entry1', code], original['Entry2', code])))
        expected.add(frozenset((original['Entry3', code],)))
    fused = merge_construction_graphs([first, second])
    represented = set()
    for node in fused['nodes']:
        ids = {o['id'] for s in node['supports'] for o in s.get('identity_origins', [])}
        represented.add(frozenset(ids | {node['id']}))
    assert represented == expected
    assert merge_construction_graphs([second, first]) == fused
