"""Large saved plans remain directly readable after native context pruning."""
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml, safe_load_yaml
from lightgraph.tests.test_construction_tools import config_for, draft


def read_all(tools, name):
    first = tools.call('read_material', name=name)
    assert first['status'] == 'ok', first
    pieces, current = [], first
    while True:
        assert len(current['content']) <= 12000
        assert current['material_revision'] == first['material_revision']
        pieces.append(current['content'])
        if current['next_page'] is None:
            break
        current = tools.call('read_material', name=name, page=current['next_page'])
    return safe_load_yaml(''.join(pieces)), first


def test_large_draft_and_full_diagnostics_remain_readable_without_session_search(tmp_path):
    tools = ConstructionTools(config_for(tmp_path), tmp_path / 'work')
    try:
        raw = draft(tools.describe_sources()['tables'][0])
        raw['entities'][0]['description'] = '需要保留的对象说明' * 300
        raw['entities'][0]['mappings'] *= 75
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(raw))['status'] == 'ok'
        result = tools.call('read_workspace', draft_id='设备')
        assert result['summary_only'] and result['draft_material'] == 'draft:设备'
        restored, first = read_all(tools, result['draft_material'])
        assert len(restored['entities'][0]['mappings']) == 75
        assert restored == tools.workspace.get_draft('设备').draft.model_dump(mode='json', by_alias=True)
        assert first['pages'] > 1 and first['content_is_fragment']
        assert tools.call('read_material', name='draft:设备', page=first['pages'])['code'] == 'invalid_material_page'
        assert tools.call('read_material', name='draft:../secret')['code'] == 'unknown_graph_draft'
        assert tools.call('read_material', name='validation:设备')['code'] == 'graph_validation_missing'
        raw['purpose'] = '修订后的目标说明'
        tools.call('save_graph_draft', yaml_text=dump_yaml(raw))
        later = tools.call('read_material', name='draft:设备')
        assert later['revision'] == 2 and later['material_revision'] != first['material_revision']
    finally:
        tools.close()


def test_validation_reply_bounds_errors_but_keeps_full_artifact_and_actual_limits(tmp_path, monkeypatch):
    tools = ConstructionTools(config_for(tmp_path, max_working_bytes=1024**3), tmp_path / 'work')
    try:
        tools.call('save_graph_draft', yaml_text=dump_yaml(draft(tools.describe_sources()['tables'][0])))
        from lightgraph.construction import tools as module
        actual = module.execute_draft
        def failing(*args, **kwargs):
            value = actual(*args, **kwargs)
            value['passed'] = False
            value['graph']['status'] = 'candidate'
            value['diagnostics'] = [{'code': 'same_identity_conflicting_label', 'node_id': f'node_{i}',
                                    'entity_type': '设备', 'mapping': 0} for i in range(462)]
            return value
        monkeypatch.setattr(module, 'execute_draft', failing)
        result = tools.call('validate_graph_draft', draft_id='设备')
        assert result['status'] == 'ok' and not result['passed']
        assert len(result['diagnostics']) == 32 and result['diagnostics_truncated']
        assert result['diagnostic_count'] == result['diagnostic_codes']['same_identity_conflicting_label'] == 462
        assert result['effective_limits']['max_working_bytes'] == 1024**3
        assert result['effective_limits']['query_timeout_ms'] == tools.config.limits.timeout_ms
        full, info = read_all(tools, result['validation_material'])
        assert len(full['diagnostics']) == 462 and info['pages'] > 1
        assert tools.call('publish_graph_draft', draft_id='设备')['status'] == 'error'
    finally:
        tools.close()
