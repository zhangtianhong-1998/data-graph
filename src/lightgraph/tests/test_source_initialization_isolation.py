"""Construction startup isolates outages, not authorization or run budgets."""
from contextlib import contextmanager
from copy import deepcopy
import json

import pytest

from lightgraph.construction import runner
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml, safe_load_yaml
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.runtime.config import RunConfig, open_sources
from lightgraph.sources import CSVSource
from lightgraph.sources.database_errors import database_error
from lightgraph.sources.unavailable import UnavailableSource


def configuration(tmp_path):
    path = tmp_path / 'healthy.csv'
    path.write_text('code,name\n001,Alpha\n002,Beta\n', encoding='utf-8')
    return RunConfig.model_validate({
        'sources': [
            {'id': 'healthy', 'kind': 'csv_file', 'path': str(path),
             'encoding': 'utf-8', 'delimiter': ',', 'header': True,
             'null_values': [], 'type_inference': 'text'},
            {'id': 'offline', 'kind': 'gauss_database', 'dsn_env': 'ISOLATION_TEST_DSN',
             'allowed_tables': ['public.items'], 'allow_fact_data': True},
        ],
        'model': {'mode': 'in_environment', 'model': 'scripted',
                  'base_url': 'http://127.0.0.1:9999/v1'},
        'output_dir': str(tmp_path / 'output'),
    })


@pytest.fixture
def outage(monkeypatch):
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs['source_id'])
        raise database_error('database_connection_failed', RuntimeError('PRIVATE_SQL_PASSWORD'),
                             engine='gauss', stage='connection_preflight')
    monkeypatch.setenv('ISOLATION_TEST_DSN', 'PRIVATE_DSN')
    monkeypatch.setattr('lightgraph.sources.GaussSource', fail)
    return calls


def healthy_draft(tools):
    table = tools.describe_sources()['tables'][0]
    return {'draft_id': 'healthy', 'entities': [{'type': 'Item', 'key_names': ['code'],
        'mappings': [{'source_id': table['source'], 'schema': table['schema'],
                      'table': table['name'], 'key_columns': ['code'], 'label_column': 'name'}]}]}


def publish_healthy(tools):
    draft = healthy_draft(tools)
    assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
    assert tools.call('validate_graph_draft', draft_id='healthy')['passed']
    assert tools.call('publish_graph_draft', draft_id='healthy')['nodes'] == 2


def test_strict_callers_still_fail_and_release_previously_opened_sources(tmp_path, monkeypatch, outage):
    created = []
    def create(*args, **kwargs):
        source = CSVSource(*args, **kwargs)
        created.append(source)
        return source
    monkeypatch.setattr('lightgraph.sources.CSVSource', create)
    with pytest.raises(LightGraphError, match='^database_connection_failed$'):
        open_sources(configuration(tmp_path))
    assert created and created[0]._closed


def test_isolation_preserves_shared_budget_and_never_invents_a_catalog(tmp_path, outage):
    sources = open_sources(configuration(tmp_path), isolate_database_failures=True)
    try:
        healthy, failed = sources
        assert isinstance(failed, UnavailableSource)
        assert failed.describe() == [] and not failed.capabilities().consistent_snapshot
        assert failed.query_budget is healthy.query_budget
        assert failed.initialization_failure['catalog_available'] is False
        for _ in range(3):
            with pytest.raises(LightGraphError, match='^database_connection_failed$'):
                with failed.snapshot():
                    pytest.fail('unavailable source entered a snapshot')
        assert outage == ['offline']
        assert 'PRIVATE' not in json.dumps(failed.initialization_failure)
        assert not hasattr(failed, '_reopen')
    finally:
        for source in sources:
            source.close()


@pytest.mark.parametrize('error', [
    LightGraphError('source_credential_missing'),
    LightGraphError('authorized_column_unavailable'),
    LightGraphError('read_only_transaction_unverified'),
    LightGraphError('capability_manifest_binding_mismatch'),
    BudgetExceeded('query_budget_exhausted'),
    BudgetExceeded('run_time_budget_exhausted'),
    RuntimeError('PRIVATE_UNEXPECTED_ERROR'),
])
def test_configuration_security_and_budget_failures_remain_fatal(tmp_path, monkeypatch, outage, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr('lightgraph.sources.GaussSource', fail)
    with pytest.raises(type(error)):
        open_sources(configuration(tmp_path), isolate_database_failures=True)


@pytest.mark.parametrize('sqlstate', ['42501', '28P01', '42601', '0A000'])
def test_driver_access_and_configuration_errors_are_not_isolated(tmp_path, monkeypatch, outage, sqlstate):
    class DriverError(Exception):
        pass
    error = DriverError('PRIVATE_PASSWORD')
    error.sqlstate = sqlstate
    def fail(*args, **kwargs):
        raise database_error('database_preflight_failed', error, engine='gauss', stage='connection_preflight')
    monkeypatch.setattr('lightgraph.sources.GaussSource', fail)
    with pytest.raises(LightGraphError, match='^database_preflight_failed$'):
        open_sources(configuration(tmp_path), isolate_database_failures=True)


def test_initial_failure_cannot_reset_an_exhausted_run_budget(tmp_path, monkeypatch, outage):
    config = configuration(tmp_path)
    config.max_db_queries = 1
    def fail(*args, **kwargs):
        kwargs['query_budget'].charge_query()
        raise LightGraphError('database_connection_failed')
    monkeypatch.setattr('lightgraph.sources.GaussSource', fail)
    with pytest.raises(BudgetExceeded, match='^query_budget_exhausted$'):
        open_sources(config, isolate_database_failures=True)


@pytest.mark.parametrize('kind,adapter', [('gauss_database', 'GaussSource'), ('mysql_database', 'MySQLSource')])
@pytest.mark.parametrize('code', ['database_connection_failed', 'database_preflight_failed',
                                 'runtime_capability_unverified', 'query_cancel_unverified'])
def test_failed_first_source_does_not_block_later_source_or_grant_fact_access(tmp_path, monkeypatch, kind, adapter, code):
    config = configuration(tmp_path)
    config.sources[1].kind = kind
    config.sources[1].allow_fact_data = False
    config.sources.reverse()
    monkeypatch.setenv('ISOLATION_TEST_DSN', 'PRIVATE_DSN')
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise LightGraphError(code)
    monkeypatch.setattr('lightgraph.sources.' + adapter, fail)
    tools = ConstructionTools(config, tmp_path / 'construction')
    try:
        assert calls[0]['metadata_only'] is True
        assert 'probe_runtime_limits' not in calls[0]
        assert tools.describe_sources()['unavailable_sources'][0]['code'] == code
        metadata = tools.call('inspect_source', table='offline:public.items', operation='get_metadata')
        assert metadata['code'] == 'source_initialization_failed' and metadata['cause_code'] == code
        publish_healthy(tools)
        assert len(calls) == 1
    finally:
        tools.close()


def test_tools_keep_failed_source_visible_and_publish_only_independent_work(tmp_path, outage):
    tools = ConstructionTools(configuration(tmp_path), tmp_path / 'construction')
    try:
        metadata = tools.call('describe_sources')
        assert {table['source'] for table in metadata['tables']} == {'healthy'}
        assert metadata['unavailable_sources'][0]['source_id'] == 'offline'
        assert metadata['source_circuits']['sources'][0]['blocked'] is True
        publish_healthy(tools)
        mixed = deepcopy(healthy_draft(tools))
        mixed['draft_id'] = 'mixed'
        mixed['entities'][0]['mappings'].append({'source_id': 'offline', 'schema': 'public',
            'table': 'items', 'key_columns': ['code'], 'label_column': 'name'})
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(mixed))['status'] == 'ok'
        queries = tools.adapters[0].stats.queries
        validation = tools.call('validate_graph_draft', draft_id='mixed')
        assert validation['code'] == 'source_circuit_open'
        assert tools.adapters[0].stats.queries == queries
        assert tools.call('publish_graph_draft', draft_id='mixed')['code'] == 'source_initialization_failed'
        assert tools.call('publish_graph_draft', draft_id='mixed', mode='structure')['code'] == 'source_initialization_failed'
        assert tools.workspace.get_draft('mixed').validation is None
        assert len(tools.workspace.read_published_graph()['nodes']) == 2
        ref = tools.call('inspect_source', table='healthy:public.healthy', operation='check_reference', arguments={
            'source_table': 'healthy:public.healthy', 'source_columns': ['code'],
            'target_table': 'offline:public.items', 'target_columns': ['code']})
        assert ref['code'] == 'source_circuit_open'
        stopped = tools.call('inspect_source', table='offline:public.items', operation='profile_columns',
                             arguments={'columns': ['code']})
        assert stopped['code'] == 'source_retry_limit_exhausted'
        assert outage == ['offline']
        assert 'offline' in tools.call('read_workspace')['content']
    finally:
        tools.close()


@pytest.fixture
def scripted_bridge(monkeypatch):
    bound = {}
    @contextmanager
    def serve(tools):
        bound['tools'] = tools
        yield 'http://127.0.0.1:9998/mcp', 'local-token'
    monkeypatch.setattr(runner, 'serve_local_tools', serve)
    return bound


def test_runner_reaches_runtime_and_reports_partial_even_after_healthy_publication(tmp_path, outage, scripted_bridge):
    config = configuration(tmp_path)
    called = []
    def runtime(*args, **kwargs):
        called.append(True)
        publish_healthy(scripted_bridge['tools'])
        return {'finish_reason': 'completed', 'budget': {'calls': 1}}
    report = runner.run_construction(config, runtime=runtime)
    assert called and report.status == 'PARTIAL'
    assert report.counts['business_nodes'] == 2
    assert report.counts['sources_failed_initialization'] == report.counts['sources_blocked'] == 1
    assert report.validation['source_initialization_failures'][0]['code'] == 'database_connection_failed'
    assert any(issue['code'] == 'source_initialization_failed' for issue in report.issues)
    catalog = safe_load_yaml((tmp_path / 'output/physical_catalog.yaml').read_text())
    assert catalog['unavailable_sources'][0]['source_id'] == 'offline'


def test_worker_uses_real_startup_and_tools_with_session_credentials(tmp_path, monkeypatch, outage, scripted_bridge):
    from lightgraph.web.execution import _resolved_payload
    from lightgraph.web.worker import execute
    config = configuration(tmp_path)
    config.sources[1].credential_mode = 'session'
    config.sources[1].dsn_env = None
    called = []
    def runtime(*args, **kwargs):
        called.append(True)
        publish_healthy(scripted_bridge['tools'])
        return {'finish_reason': 'completed', 'budget': {'calls': 1}}
    original = runner.run_construction
    monkeypatch.setattr(runner, 'run_construction', lambda *args, **kwargs:
                        original(*args, runtime=runtime, **kwargs))
    path = tmp_path / 'runs/result.json'
    code = execute(_resolved_payload(config, tmp_path, source_credentials={'offline': 'PRIVATE_DSN'}), path)
    saved = json.loads(path.read_text())
    assert code == 2 and saved['status'] == 'partial' and called
    assert saved['cleanup_status'] == 'completed'
    assert saved['report']['counts']['business_nodes'] == 2
    assert saved['report']['validation']['source_initialization_failures'][0]['source_id'] == 'offline'
    assert 'PRIVATE' not in path.read_text()
    assert outage == ['offline']
