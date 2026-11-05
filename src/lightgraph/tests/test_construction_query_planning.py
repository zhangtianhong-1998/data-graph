"""Query-cost feedback must preserve authored scope and permit a new probe."""
from lightgraph.construction.failures import failure_guidance
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml, safe_load_yaml
from lightgraph.errors import LightGraphError
from lightgraph.tests.test_construction_tools import config_for, draft


def test_timeout_retains_full_draft_and_a_different_probe_can_continue(tmp_path):
    tools = ConstructionTools(config_for(tmp_path), tmp_path / 'work')
    try:
        table = tools.describe_sources()['tables'][0]
        value = draft(table)
        value['entities'][0]['mappings'][0]['context_columns'] = ['编码', '名称', '归属']
        tools.call('save_graph_draft', yaml_text=dump_yaml(value))
        before = tools.workspace.get_draft(value['draft_id'])
        executor = tools.executors[table['source']]
        original = executor._aggregate
        def timeout(*args, **kwargs):
            raise LightGraphError('query_timeout')
        executor._aggregate = timeout
        response = tools.call('inspect_source', table=table['table'],
                              operation='profile_columns', arguments={'columns': ['编码']})
        assert response['code'] == 'query_timeout'
        assert response['failure_class'] == 'query_timeout'
        assert response['recovery'] == 'inspect_query_scope'
        assert not response['retryable'] and tools.terminal_reason is None
        assert tools.workspace.get_draft(value['draft_id']) == before
        executor._aggregate = original
        sample = tools.call('inspect_source', table=table['table'], operation='execute_readonly_sql',
                            arguments={'sql': 'SELECT "编码", "名称" FROM public."资料"', 'max_rows': 1})
        assert sample['status'] == 'ok' and sample['coverage']['truncated']
        assert not tools.workspace.read_published_graph()['nodes']
        plan = tools.call('read_material', name='source_operations')['query_planning']
        assert 'One exact aggregate query per selected column' in plan['profile_columns']
        assert 'not scanned rows' in plan['execute_readonly_sql']
    finally:
        tools.close()


def test_disconnection_recovery_is_not_misreported_as_an_expensive_query():
    advice = failure_guidance([{'code': 'query_timeout', 'database': {
        'category': 'connection', 'recovery': 'reconnect_required'}}])
    assert advice['failure_class'] == 'source_execution'
    assert advice['recovery'] == 'reconnect_and_requalify'
    for code in ('database_reconnect_failed', 'source_catalog_changed'):
        advice = failure_guidance([{'code': code}])
        assert advice['retryable'] is False
        assert advice['recovery'] == 'operator_requalification_required'


def test_fresh_snapshot_recovery_is_explicit_without_replaying_partial_results(tmp_path):
    tools = ConstructionTools(config_for(tmp_path), tmp_path / 'work')
    try:
        table = tools.describe_sources()['tables'][0]
        source = tools.executors[table['source']].adapter
        source.new_snapshot_recovery_allowed = True
        def disconnected(*args, **kwargs):
            raise LightGraphError('query_execution_failed')
        source.execute = disconnected
        result = tools.call('inspect_source', table=table['table'], operation='profile_columns',
                            arguments={'columns': ['编码']})
        assert result['retryable'] is False
        assert result['new_snapshot_recovery_available'] is True
        assert result['recovery'] == 'new_snapshot_available'
        assert result['recovery_source_ids'] == [table['source']]
        value = draft(table)
        tools.call('save_graph_draft', yaml_text=dump_yaml(value))
        checked = tools.call('validate_graph_draft', draft_id=value['draft_id'])
        assert not checked['passed'] and checked['recovery'] == 'new_snapshot_available'
        assert checked['retryable'] is False
        assert not tools.workspace.read_published_graph()['nodes']
        source.new_snapshot_recovery_allowed = False
        again = tools.call('inspect_source', table=table['table'], operation='profile_columns',
                           arguments={'columns': ['编码']})
        assert not again['new_snapshot_recovery_available']
        assert again['recovery'] != 'new_snapshot_available'
    finally:
        tools.close()


def test_new_draft_id_keeps_failed_work_visible_without_blocking_independent_publication(tmp_path):
    tools = ConstructionTools(config_for(tmp_path), tmp_path / 'work')
    try:
        table = tools.describe_sources()['tables'][0]
        original = draft(table, draft_id='full')
        tools.call('save_graph_draft', yaml_text=dump_yaml(original))
        tools.workspace.record_validation('full', revision=1, passed=False,
            diagnostics=[{'code': 'read_only_transaction_failed', 'message': 'PRIVATE_ERROR_TEXT'}], metrics={})
        independent = draft(table, draft_id='independent')
        saved = tools.call('save_graph_draft', yaml_text=dump_yaml(independent))
        work = saved['retained_failed_work']
        assert saved['status'] == 'ok' and work['total'] == 1
        assert work['items'][0]['draft_id'] == 'full'
        assert work['items'][0]['failure_codes'] == ['read_only_transaction_failed']
        assert 'PRIVATE_ERROR_TEXT' not in str(work)
        assert tools.call('validate_graph_draft', draft_id='independent')['passed']
        assert tools.call('publish_graph_draft', draft_id='independent')['status'] == 'ok'
        summary = safe_load_yaml(tools.call('read_workspace')['content'])
        assert summary['retained_failed_work']['total'] == 1
        assert tools.workspace.get_draft('full').status == 'needs_revision'
        tools.workspace.context_fingerprint = 'changed-access-context'
        assert tools._retained_failed_work()['total'] == 0
    finally:
        tools.close()
