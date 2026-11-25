"""An extracting Agent can compare definitions before validation and revise endpoints."""
from copy import deepcopy

from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.tests.test_construction_tools import config_for, draft


def test_branch_can_inspect_unsaved_draft_revise_and_publish_the_checked_graph(tmp_path):
    tools = ConstructionTools(config_for(tmp_path), tmp_path / 'work')
    try:
        table, = tools.describe_sources()['tables']
        schema = tools.call('inspect_branch_overlap')
        assert (schema['stage'], schema['table_count'], schema['draft_count']) == ('source_schema', 1, 0)
        assert len(schema['tables'][0]['column_signature']) == 64
        raw = draft(table)
        asset = raw['entities'][0]
        duplicate = {**deepcopy(asset), 'type': '设备记录'}
        mapping = asset['mappings'][0]
        source = {k: mapping[k] for k in ('source_id', 'schema', 'table')}
        raw['entities'] += [duplicate, {'type': '归属组', 'key_names': ['归属'],
                                       'mappings': [{**source, 'key_columns': ['归属']}]}]
        raw['relations'] = [{'type': '设备归属', 'source_type': duplicate['type'], 'target_type': '归属组',
                             'mappings': [{**source, 'source_key_columns': ['编码'], 'target_key_columns': ['归属']}]}]
        request = dump_yaml(raw)
        proposed = tools.call('inspect_branch_overlap', yaml_text=request)
        assert tools.workspace.list_drafts() == []
        group, = proposed['patterns']
        assert group['basis'] == 'same_physical_key' and group['member_count'] == 2
        assert {m['label_column'] for m in group['members']} == {'名称'}
        assert '设备甲' not in str(proposed)
        role, = [r for m in group['members'] for r in m['roles']]
        assert role == {'relation_type': '设备归属', 'end': 'source', 'opposite_type': '归属组',
                        'opposite_columns': ['归属']}

        saved = tools.call('save_graph_draft', yaml_text=request)
        preview = saved['branch_overlap_preview']
        assert preview['pattern_count'] == 1 and 'inspection_id' not in preview
        detail = tools.call('inspect_branch_overlap', draft_id=raw['draft_id'])
        assert detail['inspection_id'] != proposed['inspection_id']
        assert detail['inspection_id'] == tools.call('inspect_branch_overlap', draft_id=raw['draft_id'])['inspection_id']
        assert tools.call('validate_graph_draft', draft_id=raw['draft_id'])['passed']
        record = tools.workspace.get_draft(raw['draft_id'])
        before = tools.workspace.read_validation_graph(raw['draft_id'], record.validation)
        assert len(before['nodes']) == 5 and len(before['edges']) == 2

        # Script the Agent's confirmed choice: reuse the existing definition and move its endpoint.
        raw['entities'].remove(duplicate)
        raw['relations'][0]['source_type'] = asset['type']
        raw['decisions'] = ['两处映射均取本表同一编码和名称列，归属关系的起点复用设备。']
        saved = tools.call('save_graph_draft', yaml_text=dump_yaml(raw))
        assert saved['revision'] == 2 and saved['branch_overlap_preview']['pattern_count'] == 0
        assert tools.call('validate_graph_draft', draft_id=raw['draft_id'])['passed']
        published = tools.call('publish_graph_draft', draft_id=raw['draft_id'])
        assert published['nodes'] == 3 and published['edges'] == 2
        graph = tools.workspace.read_published_graph()
        nodes = {n['id']: n for n in graph['nodes']}
        assert {nodes[e['source']]['label'] for e in graph['edges']} == {'设备甲', '设备乙'}
        assert all(nodes[e['source']]['type'] == asset['type'] and nodes[e['target']]['type'] == '归属组'
                   for e in graph['edges'])
    finally:
        tools.close()


def test_branch_compares_two_wide_tables_before_any_draft_then_compares_saved_siblings(tmp_path):
    config = config_for(tmp_path)
    other = tmp_path / 'other.csv'
    other.write_text('编码,名称,区域\n甲,重复名称,北\n', encoding='utf-8')
    config.sources.append(config.sources[0].model_copy(update={'id': 'other', 'path': str(other),
                                                                 'table_name': '明细'}))
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        initial = tools.call('inspect_branch_overlap')
        assert {p['column'] for p in initial['patterns']} == {'编码', '名称'}
        assert '重复名称' not in str(initial)
        first, second = sorted(tools.describe_sources()['tables'], key=lambda t: t['source'])
        a, b = draft(first, draft_id='A'), draft(second, draft_id='B', kind='明细设备')
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(a))['branch_overlap_preview']['pattern_count'] == 0
        b['entities'][0]['mappings'][0]['label_column'] = '区域'
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(b))['branch_overlap_preview']['pattern_count'] == 1
        report = tools.call('inspect_branch_overlap', draft_id='B')
        pattern, = report['patterns']
        assert pattern['basis'] == 'same_physical_key_columns' and pattern['member_count'] == 2
        assert 'label_column' in pattern['different_fields']
        assert {m['draft_id'] for m in pattern['members']} == {'A', 'B'}
        assert tools.call('inspect_branch_overlap', draft_id='B', offset=1)['patterns'] == []
        assert tools.call('inspect_branch_overlap', draft_id='missing')['code'] == 'unknown_graph_draft'
        assert tools.call('inspect_branch_overlap', draft_id='B', yaml_text=dump_yaml(b))['status'] == 'error'
    finally:
        tools.close()
