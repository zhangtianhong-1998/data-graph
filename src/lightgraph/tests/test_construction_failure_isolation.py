"""Execution faults preserve healthy candidates and all authored semantics."""
from contextlib import contextmanager
from copy import deepcopy

from lightgraph.contracts import ColumnSpec, TableSpec, canonical
from lightgraph.construction.executor import execute_draft
from lightgraph.errors import LightGraphError
from lightgraph.sources import MemorySource


def test_failed_source_does_not_prevent_independent_mapping_or_implicate_healthy_snapshot():
    class Disconnected(MemorySource):
        @contextmanager
        def snapshot(self):
            error = LightGraphError('read_only_transaction_failed')
            error.database_diagnostic = {'engine': 'gauss', 'stage': 'snapshot_start', 'category': 'connection',
                'sqlstate': '08006', 'recovery': 'reconnect_required', 'rollback_succeeded': False}
            raise error
            yield self
    local = TableSpec(source_id='a_local', name='items', columns=[ColumnSpec(name='key')])
    remote = local.model_copy(update={'source_id': 'z_dead'})
    draft = {'draft_id': 'mixed', 'entities': [
        {'type': 'Local', 'key_names': ['id'], 'mappings': [{'source_id': 'a_local', 'table': 'items', 'key_columns': ['key']}]},
        {'type': 'Remote', 'key_names': ['id'], 'mappings': [{'source_id': 'z_dead', 'table': 'items', 'key_columns': ['key']}]}],
        'relations': [{'type': 'links', 'source_type': 'Local', 'target_type': 'Remote',
            'mappings': [{'source_id': 'a_local', 'table': 'items', 'source_key_columns': ['key'], 'target_key_columns': ['key']}]}]}
    with MemorySource([local], {local.qualified_name: [('A',)]}) as good, Disconnected([remote], {}) as bad:
        result = execute_draft(draft, [good, bad], [local, remote])
        assert good.stats.queries > 0 and bad.stats.queries == 0
    assert not result['passed'] and result['graph']['status'] == 'candidate'
    assert len(result['graph']['nodes']) == 1 and result['graph']['nodes'][0]['type'] == 'Local'
    assert result['graph']['edges'] == []
    problem = next(d for d in result['diagnostics'] if d['code'] == 'read_only_transaction_failed')
    assert problem['source_id'] == 'z_dead' and problem['phase'] == 'snapshot_open'
    assert problem['database']['sqlstate'] == '08006' and problem['retryable'] is False
    assert 'relation_dependency_unavailable' in {d['code'] for d in result['diagnostics']}
    states = {w['source_id']: w['state'] for w in result['source_read_windows']}
    assert states == {'a_local': 'closed', 'z_dead': 'failed'}


def test_one_use_projections_are_released_without_removing_names_or_evidence():
    columns = ['code', 'p', 'q', 'r']
    table = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name=c) for c in columns])
    rows = [(str(i), *([str(i) + ':' + 'x' * 8192] * 3)) for i in range(64)]
    draft = {'draft_id': 'long_names', 'entities': [{'type': 'Object', 'key_names': ['id'], 'mappings': [
        {'source_id': 's', 'table': 'items', 'key_columns': ['code'], 'label_column': c,
         'context_columns': ['code'], 'constant_properties': {'position': c}} for c in columns[1:]]}]}
    original = deepcopy(draft)
    with MemorySource([table], {table.qualified_name: rows}) as source:
        result = execute_draft(draft, [source], [table], max_working_bytes=3 * 1024 * 1024)
    assert result['passed'], result['diagnostics']
    assert draft == original
    assert result['metrics']['unique_projections'] == 3
    assert len(result['graph']['nodes']) == 64
    assert len(canonical(result['graph']).encode()) < 3 * 1024 * 1024
    for node in result['graph']['nodes']:
        assert len(node['supports']) == 3 and node['label'].endswith('x' * 8192)
        assert node['properties']['position'] == ['p', 'q', 'r']
        assert all(s['row_context']['values']['code'] == node['identity']['id'] for s in node['supports'])


def test_missing_name_is_not_a_conflicting_name_but_two_real_names_still_conflict():
    table = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name=c) for c in ['key', 'name', 'other']])
    draft = {'draft_id': 'names', 'entities': [{'type': 'Item', 'key_names': ['id'], 'mappings': [
        {'source_id': 's', 'table': 'items', 'key_columns': ['key'], 'label_column': c} for c in ['name', 'other']]}]}
    with MemorySource([table], {table.qualified_name: [('A', '', 'Known')]}) as source:
        result = execute_draft(draft, [source], [table])
    assert result['passed'] and result['graph']['nodes'][0]['label'] == 'Known'
    with MemorySource([table], {table.qualified_name: [('A', 'First', 'Second')]}) as source:
        result = execute_draft(draft, [source], [table])
    assert not result['passed']
    assert 'same_identity_conflicting_label' in {d['code'] for d in result['diagnostics']}


def test_statement_failure_stops_later_mappings_on_the_same_snapshot():
    class Broken(MemorySource):
        attempts = 0
        def execute(self, *args, **kwargs):
            self.attempts += 1
            raise LightGraphError('query_execution_failed')
    table = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name=c) for c in ['key', 'name']])
    draft = {'draft_id': 'broken', 'entities': [{'type': 'Item', 'key_names': ['id'], 'mappings': [
        {'source_id': 's', 'table': 'items', 'key_columns': ['key'], 'label_column': name}
        for name in [None, 'name']]}]}
    with Broken([table], {table.qualified_name: [('A', 'Name')]}) as source:
        result = execute_draft(draft, [source], [table])
        assert source.attempts == 1
    assert not result['passed']
    assert {d['code'] for d in result['diagnostics']} == {'query_execution_failed', 'mapping_source_unavailable'}


def test_mapping_losses_and_source_faults_reach_the_agent_without_raw_driver_text(tmp_path):
    from lightgraph.construction.tools import ConstructionTools
    from lightgraph.construction.workspace import dump_yaml
    from lightgraph.sources.database_errors import database_error
    from lightgraph.tests.test_construction_tools import config_for, draft
    tools = ConstructionTools(config_for(tmp_path), tmp_path / 'work')
    try:
        value = draft(tools.describe_sources()['tables'][0])
        old = deepcopy(value)
        old['entities'][0]['mappings'][0]['context_columns'] = ['名称']
        first = tools.call('save_graph_draft', yaml_text=dump_yaml(old))
        assert first['revision_advice'] == []
        current = deepcopy(old)
        mapping = current['entities'][0]['mappings'][0]
        mapping['alias_columns'] = [mapping.pop('label_column')]
        mapping['context_columns'] = []
        revised = tools.call('save_graph_draft', yaml_text=dump_yaml(current))
        assert revised['previous_revision'] == first['revision']
        assert {a['code'] for a in revised['revision_advice']} == {
            'revision_primary_name_role_changed', 'revision_context_columns_removed'}
        assert len(tools.workspace._manifest()['drafts'][value['draft_id']]['history']) == 2
        class DriverFault(Exception):
            sqlstate = '08006'
        error = database_error('read_only_transaction_failed', DriverFault('password=SECRET'),
                               engine='gauss', stage='snapshot_start', rollback_succeeded=False,
                               recovery='reconnect_required')
        def broken(*a, **kw):
            raise error
        tools.adapters[0].snapshot = broken
        checked = tools.call('validate_graph_draft', draft_id=value['draft_id'])
        assert not checked['passed'] and checked['failure_class'] == 'source_execution'
        assert checked['recovery'] == 'reconnect_and_requalify'
        assert 'SECRET' not in str(checked)
        assert checked['diagnostics'][0]['database']['sqlstate'] == '08006'
        probe = tools.call('inspect_source', table=next(iter(tools.tables)),
                           operation='profile_columns', arguments={'columns': ['名称']})
        # The failure is now classified rather than erased as tool_execution_failed.
        assert probe['code'] == 'read_only_transaction_failed'
        assert probe['failure_class'] == 'source_execution' and probe['database']['sqlstate'] == '08006'
        assert 'SECRET' not in str(probe)
    finally:
        tools.close()
