"""Names have explicit mapping roles, including on arbitrary physical columns."""
from copy import deepcopy

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.executor import execute_draft
from lightgraph.construction.quality import name_mapping_advice
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.sources import MemorySource
from lightgraph.tests.test_construction_tools import config_for, draft
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml


def test_display_only_names_receive_nonblocking_actionable_feedback(tmp_path):
    tools = ConstructionTools(config_for(tmp_path), tmp_path / 'work')
    try:
        value = draft(tools.describe_sources()['tables'][0])
        mapping = value['entities'][0]['mappings'][0]
        mapping.pop('label_column')
        mapping['display_columns'] = {'名称': '名称'}
        tools.save_graph_draft(dump_yaml(value))
        result = tools.call('validate_graph_draft', draft_id=value['draft_id'])
        assert result['passed'] and not result['diagnostics']
        advice = result['quality_advice'][0]
        assert advice['path'] == ['entities', 0, 'mappings', 0, 'label_column']
        assert advice['display_columns'] == ['名称']
        assert tools.call('publish_graph_draft', draft_id=value['draft_id'])['status'] == 'ok'
        mapping['label_column'] = '名称'
        tools.save_graph_draft(dump_yaml(value))
        checked = tools.call('validate_graph_draft', draft_id=value['draft_id'])
        assert checked['passed'] and checked['quality_advice'] == []
        tools.call('publish_graph_draft', draft_id=value['draft_id'])
        assert {n['label'] for n in tools.workspace.read_published_graph()['nodes']} == {'设备甲', '设备乙'}
    finally:
        tools.close()


def test_attribute_enrichment_does_not_require_another_primary_name():
    value = {'draft_id': 'items', 'entities': [{'type': 'Item', 'key_names': ['id'], 'mappings': [
        {'source_id': 'a', 'table': 'objects', 'key_columns': ['x'], 'label_column': 'y'},
        {'source_id': 'b', 'table': 'more', 'key_columns': ['m'], 'display_columns': {'备注': 'n'}}]}]}
    assert name_mapping_advice(value) == []
    value['entities'][0]['mappings'][0].pop('label_column')
    assert name_mapping_advice(value)[0]['display_columns'] == ['n']


def test_name_field_schema_explains_roles_without_column_name_heuristics():
    from lightgraph.construction.models import EntityMapping
    props = EntityMapping.model_json_schema()['properties']
    for name in ['label_column', 'alias_columns', 'display_columns']:
        assert props[name]['description']


def named_path_example(rows, *, secondary_label=False):
    table = TableSpec(source_id='s', name='arbitrary', columns=[ColumnSpec(name=c)
        for c in ['x', 'y', 'z', 'w']])
    value = {'draft_id': 'positions', 'entities': [{'type': 'Item', 'key_names': ['code'], 'mappings': [
        {'source_id': 's', 'table': 'arbitrary', 'key_columns': ['x'], 'label_column': 'y'},
        {'source_id': 's', 'table': 'arbitrary', 'key_columns': ['z'],
         **({'label_column': 'w'} if secondary_label else {'alias_columns': ['w']})}]}]}
    with MemorySource([table], {table.qualified_name: rows}) as source:
        result = execute_draft(value, [source], [table])
    return value, result


def test_primary_name_on_one_position_does_not_cover_nodes_only_in_other_positions():
    value, result = named_path_example([('A', '甲', 'B', '乙'), ('A', '甲', 'A', '甲')])
    assert result['passed'], result['diagnostics']
    before = deepcopy(result['graph'])
    advice = name_mapping_advice(value, result['graph'])
    assert len(advice) == 1
    item = advice[0]
    assert item['code'] == 'primary_name_coverage_incomplete'
    assert item['severity'] == 'suggestion'
    assert item['observed_nodes'] == 2 and item['nodes_with_primary_name'] == 1
    assert item['nodes_without_primary_name'] == item['unnamed_nodes_with_aliases'] == 1
    assert item['mapping_coverage'] == [{'mapping': 1, 'path': ['entities', 0, 'mappings', 1, 'label_column'],
        'nodes_without_primary_name': 1, 'label_column': None, 'alias_columns': ['w'], 'display_columns': []}]
    assert result['graph'] == before
    unnamed = next(node for node in result['graph']['nodes'] if node['identity']['code'] == 'B')
    assert unnamed['label_origin'] == 'identity' and unnamed['aliases'] == ['乙']
    assert '乙' not in str(advice)  # Feedback retains roles and counts, not source values.
    _, repaired = named_path_example([('A', '甲', 'B', '乙'), ('A', '甲', 'A', '甲')], secondary_label=True)
    repaired_value = deepcopy(value)
    repaired_value['entities'][0]['mappings'][1].update(label_column='w', alias_columns=[])
    assert repaired['passed'] and name_mapping_advice(repaired_value, repaired['graph']) == []


def test_empty_primary_values_and_generated_labels_are_not_name_evidence():
    value, result = named_path_example([('A', '', 'B', '别名')])
    assert result['passed']
    advice = name_mapping_advice(value, result['graph'])
    assert advice[0]['nodes_without_primary_name'] == 2
    assert {item['mapping'] for item in advice[0]['mapping_coverage']} == {0, 1}
    assert advice[0]['mapping_coverage'][0]['label_column'] == 'y'


def test_named_elsewhere_enrichment_and_legitimate_unnamed_objects_remain_valid():
    value, result = named_path_example([('A', '甲', 'B', '乙'), ('B', '乙', 'A', '甲')])
    assert result['passed'] and name_mapping_advice(value, result['graph']) == []
    unnamed = deepcopy(value)
    unnamed['entities'][0]['mappings'][0].pop('label_column')
    unnamed['entities'][0]['mappings'][1].pop('alias_columns')
    assert name_mapping_advice(unnamed, result['graph']) == []
    # Two real primary names for the same identity still fail; same name on
    # distinct identities is legitimate and must not be renamed or merged.
    _, conflict = named_path_example([('A', '甲', 'A', '不同主名称')], secondary_label=True)
    assert not conflict['passed']
    assert 'same_identity_conflicting_label' in {item['code'] for item in conflict['diagnostics']}
    value, same_name = named_path_example([('A', '相同名称', 'B', '相同名称')], secondary_label=True)
    assert same_name['passed'] and len(same_name['graph']['nodes']) == 2
    assert name_mapping_advice(value, same_name['graph']) == []


def test_name_coverage_uses_only_authorized_supports():
    value, result = named_path_example([('A', '公开主名', 'B', '公开别名')])
    hidden = deepcopy(result['graph']['nodes'][0])
    hidden.update(id='hidden', identity={'code': 'hidden'})
    for support in hidden['supports']:
        support['scope_ref'] = 'private'
    result['graph']['nodes'].append(hidden)
    unnamed = next(node for node in result['graph']['nodes'] if node['identity']['code'] == 'B')
    private_support = deepcopy(unnamed['supports'][0])
    private_support.update(scope_ref='private', label='秘密主名')
    unnamed['supports'].append(private_support)
    visible = filter_graph_scopes(result['graph'], ['public'])
    advice = name_mapping_advice(value, visible)
    assert advice[0]['observed_nodes'] == 2 and advice[0]['nodes_without_primary_name'] == 1
    assert '秘密主名' not in str(advice)
