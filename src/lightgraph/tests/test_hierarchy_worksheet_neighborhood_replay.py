"""Replay two independent hierarchy mappings of one workbook with a scoped relationship."""
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
def scoped_worksheet_units(tmp_path_factory):
    folder = tmp_path_factory.mktemp('scoped-worksheet')
    path = folder / 'tree.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.title = 'tree'
    path_columns = ['parent_code', 'child_code', 'group_code', 'category_code', 'leaf_code']
    sheet.append(['center', 'parent_code', 'parent_name', 'unused', 'child_code', 'child_name',
                  *path_columns[2:]])
    for leaf in ('L1', 'L2'):
        sheet.append(['North', 'P', 'Parent', None, 'C', 'Child', 'G', 'K', leaf])
    book.save(path)
    book.close()

    config = RunConfig(sources=[{'id': 'excel', 'kind': 'excel_workbook', 'path': str(path)}],
                       model={'mode': 'in_environment'})
    units = []
    for number, context in [(2, ['center']), (1, path_columns)]:
        unit = f'extract_{number}'
        output = folder / unit
        tools = ConstructionTools(config, output / 'construction')
        try:
            meta = tools.call('inspect_source', table='excel:public.tree', operation='get_metadata')
            draft = _draft(number, meta['evidence_ref'])
            mapping, = draft['relations'][0]['mappings']
            mapping.update(context_columns=context,
                           business_context={'status': 'scoped', 'columns': {'department': 'center'}})
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
            validation = tools.call('validate_graph_draft', draft_id=draft['draft_id'])
            assert validation['passed'], validation
            assert tools.call('publish_graph_draft', draft_id=draft['draft_id'])['status'] == 'ok'
        finally:
            tools.close()
        units.append(load_unit_contributor(output, unit))
    path.unlink()
    return units


def test_physical_rows_match_center_in_edge_context_to_the_same_typed_scope_observation(scoped_worksheet_units):
    units = scoped_worksheet_units
    left, right = [u['graph']['edges'][0]['supports'] for u in units]
    assert [s['source_row_witness']['rows'] for s in left] == [[[2, 3]]]
    assert sorted(s['source_row_witness']['rows'] for s in right) == [[[2, 2]], [[3, 3]]]
    assert len({s['source_row_witness']['snapshot'] for s in [*left, *right]}) == 1
    for s in [*left, *right]:
        assert s['provenance']['source_column_ordinals']['center'] == 1
        assert s['business_context']['observed_columns'] == {'department': 'center'}
        assert s['business_context']['dimensions'] == {'department': 'North'}
        assert s['business_context']['value_types'] == {'department': 'text'}

    for missing in [['source_row_witness', 'source_column_ordinals'], ['source_row_witness']]:
        legacy = merge_construction_graphs(_without_evidence(units, missing))
        assert len(legacy['nodes']) == 4
        stats = legacy['fusion_diagnostics']['hierarchy_replay_diagnostics']
        assert stats['different_neighborhood_pairs_by_first_difference'] == {'edge_row_context': 2}

    baseline = merge_construction_graphs(units, recognize_source_replays=False)
    merged = merge_construction_graphs(units)
    assert merged['fusion_diagnostics']['hierarchy_replay_collapsed_nodes'] == 2
    assert len(merged['nodes']) == 2 and len(merged['edges']) == 2
    ids = {n['identity']['code']: n['id'] for n in merged['nodes']}
    assert {e['type']: (e['source'], e['target']) for e in merged['edges']} == {
        'link2': (ids['P'], ids['C']), 'link1': (ids['C'], ids['P'])}
    assert _edge_observations(merged) == _edge_observations(baseline)
    assert _node_origins(merged) == _node_origins(baseline)
    assert merge_construction_graphs(list(reversed(units))) == merged


@pytest.mark.parametrize('difference', [
    'scope_value', 'scope_type', 'physical_column', 'conflicting_cell',
    'missing_observed_column', 'constant_dimension', 'unprojected_endpoint',
])
def test_scoped_column_requires_consistent_source_observations_on_every_row(scoped_worksheet_units, difference):
    units = deepcopy(scoped_worksheet_units)
    left, = units[0]['graph']['edges'][0]['supports']
    right = units[1]['graph']['edges'][0]['supports'][0]
    if difference == 'scope_value':
        right['business_context']['dimensions']['department'] = 'South'
    elif difference == 'scope_type':
        right['business_context']['value_types']['department'] = 'date'
    elif difference == 'physical_column':
        right['provenance']['source_column_ordinals']['center'] = 11
    elif difference == 'conflicting_cell':
        left['row_context']['values']['center'] = 'South'
    elif difference == 'missing_observed_column':
        right['business_context']['observed_columns'].clear()
    elif difference == 'unprojected_endpoint':
        left['provenance']['columns'].remove('parent_code')
        left['provenance']['source_column_ordinals'].pop('parent_code')
    else:
        right['business_context']['observed_columns'].clear()
        right['business_context']['declared_dimensions'] = ['department']
        mapping = right['provenance']['business_context_mapping']
        mapping.update(columns={}, values={'department': 'North'})
    rejected = merge_construction_graphs(units)
    assert len(rejected['nodes']) == 4 and len(rejected['edges']) == 2


def test_equal_values_in_two_columns_cannot_rebind_a_business_dimension(scoped_worksheet_units):
    units = deepcopy(scoped_worksheet_units)
    for unit in units:
        for support in unit['graph']['edges'][0]['supports']:
            prov = support['provenance']
            prov['columns'].append('other_center')
            prov['source_column_ordinals']['other_center'] = 10
            context = support['business_context']
            # Each projection records the other column as plain row context.
            # Both values are North on both rows, but the declared scopes use
            # different physical columns to identify the department.
            if unit['contributor_id'] == 'extract_1':
                prov['business_context_mapping']['columns']['department'] = 'other_center'
                context['observed_columns']['department'] = 'other_center'
                column = 'center'
            else:
                column = 'other_center'
            prov['context_columns'].append(column)
            support['row_context']['values'][column] = 'North'
            support['row_context']['value_types'][column] = 'text'
    assert len(merge_construction_graphs(units)['nodes']) == 4


def test_partial_header_keeps_a_strict_match_on_the_same_scoped_row():
    units = [_tree(number, False) for number in (1, 2)]
    for unit in units:
        support = _edge_support(unit, {'owner': 'North'}, [[2, 2]])
        unit['graph']['edges'][0]['supports'] = [support]
        support['provenance']['business_context_mapping'] = {'status': 'scoped', 'columns': {'department': 'owner'}}
        support['business_context'] = {
            'version': 1, 'status': 'scoped', 'origin': 'declared_mapping',
            'dimensions': {'department': 'North'}, 'value_types': {'department': 'text'},
            'observed_columns': {'department': 'owner'},
        }
    units[0]['graph']['edges'][0]['supports'][0]['provenance']['source_column_ordinals'].pop('owner')
    assert len(merge_construction_graphs(units)['nodes']) == 2
