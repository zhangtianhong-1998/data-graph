"""A broken source cannot consume unlimited validations or erase healthy work.

Only connection failures are synthetic. The local source, SQL guard, mapping
execution, immutable validation history and publication use production paths.
These checks do not certify a live Gauss/MySQL server.
"""
from contextlib import contextmanager
from copy import deepcopy
import json

import pytest

from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource


class FaultyDatabase(MemorySource):
    failure_code = 'read_only_transaction_failed'
    failure_category = 'unknown'
    terminal_failure_code = None
    latch_terminal = False
    fail = True
    snapshot_attempts = 0

    @contextmanager
    def snapshot(self):
        self.snapshot_attempts += 1
        if self.fail:
            if self.latch_terminal:
                self.terminal_failure_code = self.failure_code
            error = LightGraphError(self.failure_code, retryable=False)
            error.database_diagnostic = {'engine': 'gauss', 'stage': 'snapshot_start',
                'category': self.failure_category, 'sqlstate': 'unknown', 'errno': None,
                'rollback_succeeded': True, 'connection_closed': False, 'recovery': 'rolled_back'}
            raise error
        with super().snapshot():
            yield self


@pytest.fixture
def fixture(tmp_path):
    local = TableSpec(source_id='a_local', name='local_items', columns=[ColumnSpec(name=col) for col in ('code', 'name')])
    remote = TableSpec(source_id='z_database', name='database_items', columns=[ColumnSpec(name=col) for col in ('code', 'name')])
    good = MemorySource([local], {local.qualified_name: [('A', 'Local A')]})
    bad = FaultyDatabase([remote], {remote.qualified_name: [('A', 'Remote A')]})
    config = RunConfig(sources=[], model={'mode': 'in_environment'}, max_run_seconds=300,
                       goal={'purpose': 'Preserve complete mixed-source graph and independent local work'})
    tools = ConstructionTools(config, tmp_path / 'work', adapters=[good, bad])
    mixed = {'draft_id': 'complete', 'entities': [
        {'type': 'Local', 'key_names': ['id'], 'mappings': [{'source_id': 'a_local', 'table': 'local_items',
             'key_columns': ['code'], 'label_column': 'name'}]},
        {'type': 'Remote', 'key_names': ['id'], 'mappings': [{'source_id': 'z_database', 'table': 'database_items',
             'key_columns': ['code'], 'label_column': 'name'}]}],
        'relations': [{'type': 'links', 'source_type': 'Local', 'target_type': 'Remote', 'mappings': [
            {'source_id': 'a_local', 'table': 'local_items', 'source_key_columns': ['code'], 'target_key_columns': ['code']}]}]}
    assert tools.call('save_graph_draft', yaml_text=dump_yaml(mixed))['status'] == 'ok'
    try:
        yield tools, good, bad, mixed
    finally:
        tools.close(); good.close(); bad.close()


def validate(tools, draft_id='complete', **options):
    return tools.call('validate_graph_draft', draft_id=draft_id, **options)


def query(tools, *, local=False, sql=None):
    table = 'local_items' if local else 'database_items'
    return tools.call('inspect_source', table=table, operation='execute_readonly_sql', arguments={
        'sql': sql or f'SELECT "code", "name" FROM public."{table}"', 'max_rows': 10})


def assert_blocked(result, cause='read_only_transaction_failed'):
    assert result['status'] == 'error' and result['code'] == 'source_circuit_open', result
    assert result['retryable'] is False
    assert any(item['source_id'] == 'z_database' and item['cause_code'] == cause for item in result['blocked_sources'])


def open_circuit(tools, bad):
    for _ in range(3):
        result = validate(tools)
        assert result['status'] == 'ok' and not result['passed'], result
    assert bad.snapshot_attempts == 3


def test_repeated_failure_blocks_new_validation_without_overwriting_healthy_candidate(fixture):
    tools, good, bad, authored = fixture
    open_circuit(tools, bad)
    prior = tools.workspace.get_draft('complete')
    assert prior.validation.metrics['mappings_checked'] > 0
    candidate = tools.workspace.read_validation_graph('complete', prior.validation)
    assert len(candidate['nodes']) == 1 and candidate['nodes'][0]['type'] == 'Local'
    assert candidate['edges'] == [] and candidate['status'] == 'candidate'
    history = deepcopy(tools.workspace._manifest()['drafts']['complete']['history'])
    queries = good.stats.queries
    blocked = validate(tools)
    assert_blocked(blocked)
    assert bad.snapshot_attempts == 3 and good.stats.queries == queries
    assert tools.workspace.get_draft('complete') == prior
    assert tools.workspace._manifest()['drafts']['complete']['history'] == history
    assert prior.draft.model_dump(mode='json', exclude_defaults=True)['draft_id'] == authored['draft_id']
    assert tools.call('publish_graph_draft', draft_id='complete')['code'] == 'graph_validation_required'
    assert tools.workspace.read_published_graph()['nodes'] == []


def test_adapter_terminal_failure_blocks_immediately_but_keeps_structure_and_local_publication(fixture):
    tools, good, bad, authored = fixture
    bad.latch_terminal = True
    first = validate(tools)
    assert not first['passed'] and bad.snapshot_attempts == 1
    assert first['source_circuits']['sources'][0]['consecutive_failures'] == 1
    assert_blocked(validate(tools))
    assert bad.snapshot_attempts == 1
    structure = validate(tools, mode='structure')
    assert structure['passed'] and structure['data_validation'] == 'not_run'
    independent = deepcopy(authored); independent['draft_id'] = 'local_only'
    independent['entities'] = independent['entities'][:1]; independent['relations'] = []
    assert tools.call('save_graph_draft', yaml_text=dump_yaml(independent))['status'] == 'ok'
    assert validate(tools, 'local_only')['passed']
    publication = tools.call('publish_graph_draft', draft_id='local_only')
    assert publication['status'] == 'ok' and publication['nodes'] == 1
    assert tools.workspace.get_draft('complete').validation.passed is False
    assert bad.snapshot_attempts == 1


def test_cross_source_reference_checks_both_sources_before_any_query(fixture):
    tools, good, bad, _ = fixture
    open_circuit(tools, bad)
    before = good.stats.queries, bad.stats.queries, bad.snapshot_attempts
    tables = {table['source']: table['table'] for table in tools.call('describe_sources')['tables']}
    result = tools.call('inspect_source', table=tables['a_local'], operation='check_reference', arguments={
        'source_table': tables['a_local'], 'source_columns': ['code'],
        'target_table': tables['z_database'], 'target_columns': ['code']})
    assert_blocked(result)
    assert (good.stats.queries, bad.stats.queries, bad.snapshot_attempts) == before


def test_changing_draft_id_or_sql_cannot_evade_circuit_and_repeated_requests_trigger_closeout(fixture):
    tools, good, bad, authored = fixture
    open_circuit(tools, bad)
    assert_blocked(query(tools, sql='SELECT "code" FROM public."database_items" WHERE "code" = :code'))
    alternate = deepcopy(authored); alternate['draft_id'] = 'renamed'
    assert tools.call('save_graph_draft', yaml_text=dump_yaml(alternate))['status'] == 'ok'
    assert_blocked(validate(tools, 'renamed'))
    stopped = query(tools, sql='SELECT "name" FROM public."database_items"')
    assert stopped['code'] == 'source_retry_limit_exhausted' and stopped['queries_stopped']
    assert stopped['finalization_model_calls'] == 2 and not stopped['retryable']
    assert bad.snapshot_attempts == 3
    assert json.loads((tools.workspace.root / 'query-stop.json').read_text())['code'] == 'source_retry_limit_exhausted'
    assert tools.call('read_workspace')['status'] == 'ok'
    assert validate(tools, mode='structure')['passed']


def test_metadata_does_not_reset_failure_count_but_successful_data_read_does(fixture):
    tools, _, bad, _ = fixture
    assert not validate(tools)['passed']; assert not validate(tools)['passed']
    metadata = tools.call('inspect_source', table='database_items', operation='get_metadata')
    assert metadata['status'] == 'ok' and bad.snapshot_attempts == 2
    bad.fail = False
    assert query(tools)['status'] == 'ok'
    bad.fail = True
    for _ in range(2):
        assert not validate(tools)['passed']
    metadata = tools.call('inspect_source', table='database_items', operation='get_metadata')
    assert metadata['status'] == 'ok'
    third = validate(tools)
    assert third['status'] == 'ok' and not third['passed']
    assert_blocked(validate(tools))
    assert tools.terminal_reason is None


def test_timeout_is_query_planning_feedback_not_a_connection_circuit(fixture):
    tools, _, bad, _ = fixture
    bad.failure_code = 'query_timeout'; bad.failure_category = 'interrupted'
    for _ in range(5):
        checked = validate(tools)
        assert checked['status'] == 'ok' and not checked['passed'], checked
        assert checked['failure_class'] == 'query_timeout'
    assert bad.snapshot_attempts == 5 and tools.terminal_reason is None
    bad.fail = False
    assert validate(tools)['passed']


def test_sql_guard_and_mapping_errors_do_not_open_a_connection_circuit(fixture):
    tools, good, bad, _ = fixture
    bad.fail = False
    before = good.stats.queries
    for _ in range(4):
        assert query(tools, local=True, sql='DELETE FROM public."local_items"')['code'] == 'unsafe_sql'
        denied = query(tools, sql='SELECT "unknown_column" FROM public."database_items"')
        assert denied['code'] == 'unauthorized_column'
    assert good.stats.queries == before and bad.snapshot_attempts == 0
    assert query(tools)['status'] == 'ok'
    assert validate(tools)['passed'] and tools.terminal_reason is None


def test_prior_successful_snapshot_remains_publishable_after_circuit_without_new_reads(fixture):
    tools, good, bad, _ = fixture
    bad.fail = False
    success = validate(tools)
    assert success['passed']
    bad.fail = True
    for _ in range(3):
        assert not validate(tools)['passed']
    assert_blocked(validate(tools))
    before = good.stats.queries, bad.stats.queries, bad.snapshot_attempts
    published = tools.call('publish_graph_draft', draft_id='complete', validation_id=success['validation_id'])
    assert published['status'] == 'ok' and published['nodes'] == 2 and published['edges'] == 1
    assert (good.stats.queries, bad.stats.queries, bad.snapshot_attempts) == before


def test_failure_response_and_retained_history_do_not_expose_driver_text(fixture):
    tools, _, bad, _ = fixture
    # Only allowlisted diagnostics reach the circuit and its public response.
    original = bad.snapshot
    @contextmanager
    def private_fault():
        try:
            with original():
                yield bad
        except LightGraphError as exc:
            exc.database_diagnostic['message'] = 'password=PRIVATE_SECRET SELECT private_values'
            raise
    bad.snapshot = private_fault
    open_circuit(tools, bad)
    blocked = validate(tools)
    assert_blocked(blocked)
    assert 'PRIVATE_SECRET' not in str(blocked)
    assert 'PRIVATE_SECRET' not in tools.call('read_workspace', draft_id='complete')['content']
    assert 'PRIVATE_SECRET' not in str(tools.workspace._manifest())
    assert 'PRIVATE_SECRET' not in (tools.workspace.root / 'source-circuits.json').read_text()


def test_metadata_and_structure_checks_alone_cannot_reset_two_prior_source_failures(fixture):
    tools, _, bad, _ = fixture
    for _ in range(2):
        assert not validate(tools)['passed']
    assert tools.call('inspect_source', table='database_items', operation='get_metadata')['status'] == 'ok'
    assert validate(tools, mode='structure')['passed']
    assert tools.call('read_workspace')['status'] == 'ok'
    assert not validate(tools)['passed']
    assert_blocked(validate(tools))
    assert bad.snapshot_attempts == 3


def test_opening_a_snapshot_without_any_successful_projection_cannot_reset_failures(fixture):
    tools, _, bad, authored = fixture
    for _ in range(2):
        assert not validate(tools)['passed']
    invalid = deepcopy(authored); invalid['draft_id'] = 'invalid_mapping'
    invalid['entities'][1]['mappings'][0]['label_column'] = 'not_an_authorized_column'
    assert tools.call('save_graph_draft', yaml_text=dump_yaml(invalid))['status'] == 'ok'
    bad.fail = False
    before = bad.stats.queries
    checked = validate(tools, 'invalid_mapping')
    assert not checked['passed']
    assert 'unauthorized_column' in {item['code'] for item in checked['diagnostics']}
    assert bad.stats.queries == before
    bad.fail = True
    assert not validate(tools)['passed']
    assert_blocked(validate(tools))


def test_new_worker_clears_run_local_circuit_but_retains_draft_history(fixture):
    tools, good, bad, _ = fixture
    open_circuit(tools, bad)
    assert_blocked(validate(tools))
    history = deepcopy(tools.workspace._manifest()['drafts']['complete']['history'])
    config, path = tools.config, tools.workspace.root
    tools.close()
    resumed = ConstructionTools(config, path, adapters=[good, bad])
    try:
        assert resumed.source_circuits.summary()['sources'] == []
        assert json.loads((path / 'source-circuits.json').read_text())['sources'] == []
        assert resumed.workspace._manifest()['drafts']['complete']['history'] == history
        checked = validate(resumed)
        assert checked['status'] == 'ok' and not checked['passed']
        assert bad.snapshot_attempts == 4
    finally:
        resumed.close()


def test_mixed_snapshot_close_failure_is_attributed_only_to_its_source(fixture):
    tools, good, bad, authored = fixture
    bad.fail = False
    original = bad.snapshot
    @contextmanager
    def fail_on_close():
        with original():
            yield bad
        error = LightGraphError('snapshot_close_failed', retryable=False)
        error.database_diagnostic = {'engine': 'gauss', 'stage': 'snapshot_close',
            'category': 'unknown', 'sqlstate': 'unknown', 'errno': None,
            'rollback_succeeded': False, 'connection_closed': False, 'recovery': 'reconnect_required'}
        raise error
    bad.snapshot = fail_on_close
    for _ in range(3):
        result = validate(tools)
        assert not result['passed'] and result['metrics']['mappings_checked'] == 3
        failures = [item for item in result['diagnostics'] if item['code'] == 'snapshot_close_failed']
        assert len(failures) == 1 and failures[0]['source_id'] == 'z_database'
        assert not any(item.get('source_id') == 'a_local' for item in result['diagnostics'])
        windows = {item['source_id']: item['state'] for item in result['read_window']['source_read_windows']}
        assert windows['z_database'] == 'failed'
        assert windows['a_local'] == 'closed_after_interruption'
    summary = tools.call('describe_sources')['source_circuits']
    assert len(summary['sources']) == 1 and summary['sources'][0]['source_id'] == 'z_database'
    assert summary['sources'][0]['consecutive_failures'] == 3
    assert_blocked(validate(tools), cause='snapshot_close_failed')
    assert tools.call('publish_graph_draft', draft_id='complete')['code'] == 'graph_validation_required'
    independent = deepcopy(authored); independent['draft_id'] = 'local_after_close_failure'
    independent['entities'] = independent['entities'][:1]; independent['relations'] = []
    tools.call('save_graph_draft', yaml_text=dump_yaml(independent))
    assert validate(tools, independent['draft_id'])['passed']
    assert tools.call('publish_graph_draft', draft_id=independent['draft_id'])['nodes'] == 1


def test_unresolved_cross_source_probe_is_not_success_and_cannot_reset_failures(fixture):
    tools, _, bad, _ = fixture
    for _ in range(2):
        assert not validate(tools)['passed']
    tables = {table['source']: table['table'] for table in tools.call('describe_sources')['tables']}
    result = tools.call('inspect_source', table=tables['a_local'], operation='check_reference', arguments={
        'source_table': tables['a_local'], 'source_columns': ['code'],
        'target_table': tables['z_database'], 'target_columns': ['code']})
    assert result['status'] == 'ok' and not result['coverage']['complete'], result
    assert 'read_only_transaction_failed' in result['coverage']['reasons']
    failure = result['coverage']['failure']
    assert failure['source_id'] == 'z_database' and failure['code'] == 'read_only_transaction_failed'
    assert failure['phase'] == 'snapshot_open'
    states = tools.call('describe_sources')['source_circuits']['sources']
    assert len(states) == 1 and states[0]['consecutive_failures'] == 3 and states[0]['blocked']
    assert_blocked(validate(tools))
    assert bad.snapshot_attempts == 3


def test_cross_source_failures_alone_open_only_the_failed_source_circuit(fixture):
    tools, good, bad, _ = fixture
    tables = {table['source']: table['table'] for table in tools.call('describe_sources')['tables']}
    for _ in range(3):
        result = tools.call('inspect_source', table=tables['a_local'], operation='check_reference', arguments={
            'source_table': tables['a_local'], 'source_columns': ['code'],
            'target_table': tables['z_database'], 'target_columns': ['code']})
        assert result['status'] == 'ok' and not result['coverage']['complete']
        assert result['coverage']['failure']['source_id'] == 'z_database'
    before = good.stats.queries, bad.stats.queries, bad.snapshot_attempts
    blocked = tools.call('inspect_source', table=tables['a_local'], operation='check_reference', arguments={
        'source_table': tables['a_local'], 'source_columns': ['code'],
        'target_table': tables['z_database'], 'target_columns': ['code']})
    assert_blocked(blocked)
    assert (good.stats.queries, bad.stats.queries, bad.snapshot_attempts) == before
    assert query(tools, local=True)['status'] == 'ok'
    states = tools.call('describe_sources')['source_circuits']['sources']
    assert len(states) == 1 and states[0]['source_id'] == 'z_database'
