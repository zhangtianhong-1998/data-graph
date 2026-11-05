"""Names have explicit mapping roles, including on arbitrary physical columns."""
from lightgraph.construction.quality import name_mapping_advice
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
