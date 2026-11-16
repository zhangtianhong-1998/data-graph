"""Opening a Gauss source must requalify a replacement before discovery."""
import pytest

from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import open_sources
from lightgraph.sources import reconnect
from lightgraph.sources.database_errors import qualification_error
from lightgraph.sources.gauss import GaussSource
from lightgraph.sources.unavailable import UnavailableSource
from lightgraph.tests.test_database_reconnect import Factory
from lightgraph.tests.test_database_runtime_probe_recovery import RuntimeConnection
from lightgraph.tests.test_source_initialization_isolation import configuration


@pytest.mark.parametrize('kind,adapter', [('gauss_database', 'GaussSource'), ('mysql_database', 'MySQLSource')])
@pytest.mark.parametrize('isolate', [False, True])
def test_initial_connection_failure_retries_with_same_budget(tmp_path, monkeypatch, kind, adapter, isolate):
    config = configuration(tmp_path)
    config.sources = [config.sources[1]]
    config.sources[0].kind = kind
    budgets, pauses = [], []

    def fail(*args, query_budget, **kwargs):
        budgets.append(query_budget)
        query_budget.charge_query()
        raise LightGraphError('database_connection_failed')

    def pause(attempt, check, *, initial_delay):
        check()
        pauses.append(initial_delay * 2 ** (attempt - 1))

    monkeypatch.setattr('lightgraph.sources.' + adapter, fail)
    monkeypatch.setattr(reconnect, '_wait_for_recovery', pause)
    if isolate:
        [source] = open_sources(config, source_credentials={'offline': 'fixture'}, isolate_database_failures=True)
        assert isinstance(source, UnavailableSource)
        assert source.terminal_failure_diagnostic['recovery_decision'] == 'retry_limit_exhausted'
        source.close()
    else:
        with pytest.raises(LightGraphError, match='^database_connection_failed$'):
            open_sources(config, source_credentials={'offline': 'fixture'})
    assert len(budgets) == reconnect.INITIAL_PREFLIGHT_ATTEMPTS
    assert len({id(budget) for budget in budgets}) == 1
    assert budgets[0].queries == reconnect.INITIAL_PREFLIGHT_ATTEMPTS
    assert pauses == [.25, .5]


@pytest.mark.parametrize('kind,adapter', [('gauss_database', 'GaussSource'), ('mysql_database', 'MySQLSource')])
@pytest.mark.parametrize('cause', ['42501', '28P01', '42601', '0A000', 'source_credential_missing'])
def test_initial_retry_does_not_retry_permanent_failures(tmp_path, monkeypatch, kind, adapter, cause):
    from lightgraph.sources.database_errors import database_error
    config = configuration(tmp_path)
    config.sources = [config.sources[1]]
    config.sources[0].kind = kind
    calls = []

    def reject(*args, **kwargs):
        calls.append(1)
        if cause == 'source_credential_missing':
            raise LightGraphError(cause)
        error = RuntimeError('PRIVATE_DETAILS')
        error.sqlstate = cause
        raise database_error('database_preflight_failed', error,
                             engine='mysql' if kind == 'mysql_database' else 'gauss',
                             stage='connection_preflight')

    monkeypatch.setattr('lightgraph.sources.' + adapter, reject)
    with pytest.raises(LightGraphError):
        open_sources(config, source_credentials={'offline': 'fixture'}, isolate_database_failures=True)
    assert calls == [1]


@pytest.mark.parametrize('misses', [1, 3])
def test_open_sources_requalifies_fresh_connections_before_exposing_catalog(tmp_path, monkeypatch, caplog, misses):
    connections = [RuntimeConnection() for _ in range(misses + 1)]
    for conn in connections[:misses]:
        conn.cancel_works = False
    driver = Factory(connections)
    monkeypatch.setattr('lightgraph.sources.GaussSource',
        lambda dsn, **kw: GaussSource(dsn, connect_factory=driver, **kw))
    monkeypatch.setattr(reconnect, '_wait_for_recovery', lambda attempt, check, **kw: check())
    sources = open_sources(configuration(tmp_path), source_credentials={'offline': 'fixture'},
                           isolate_database_failures=True)
    try:
        csv, database = sources
        attempts = min(misses + 1, 3)
        assert driver.calls == attempts
        assert all(conn.closed for conn in connections[:attempts])
        assert database.query_budget is csv.query_budget
        assert database.stats.runtime_probe_runs == attempts
        assert database.stats.query_cancel_unverified_count == misses
        assert database.stats.catalog_queries == 4 * attempts
        assert not any('FROM "public"."items"' in sql for conn in connections for sql, _ in conn.executed)
        retries = [r.database_diagnostic for r in caplog.records
                   if getattr(r, 'database_diagnostic', {}).get('recovery_decision') == 'retry_new_session']
        assert [d['boundary_recovery_attempts'] for d in retries] == list(range(1, min(misses, 2) + 1))
        if misses == 1:
            assert database.capabilities().actual_database_verified
            assert database.describe()[0].qualified_name == 'public.items'
        else:
            assert isinstance(database, UnavailableSource)
            assert not database.describe()
            assert database.terminal_failure_code == 'query_cancel_unverified'
            terminal = database.terminal_failure_diagnostic
            assert terminal['probe_failure'] == 'interrupt_not_observed'
            assert terminal['terminal'] and terminal['recovery_decision'] == 'retry_limit_exhausted'
            assert terminal['boundary_recovery_attempts'] == 2
            assert not connections[-1].executed
    finally:
        for source in sources:
            source.close()


@pytest.mark.parametrize('stop', ['query_cancelled', 'query_budget_exhausted', 'run_time_budget_exhausted'])
def test_initial_retry_cannot_reset_budget_or_ignore_cancellation(tmp_path, monkeypatch, stop):
    config = configuration(tmp_path)
    config.max_db_queries = 1 if stop == 'query_budget_exhausted' else None
    config.sources = [config.sources[1]]
    calls = []
    def missed(*args, query_budget, **kwargs):
        calls.append(query_budget)
        query_budget.charge_query()
        raise qualification_error('query_cancel_unverified', engine='gauss',
                                  stage='connection_preflight', failure='interrupt_not_observed')
    def stop_during_pause(attempt, check, **kw):
        if stop == 'query_cancelled':
            calls[-1].cancel()
        elif stop == 'run_time_budget_exhausted':
            calls[-1].deadline = 0
        check()
    monkeypatch.setattr('lightgraph.sources.GaussSource', missed)
    monkeypatch.setattr(reconnect, '_wait_for_recovery', stop_during_pause)
    with pytest.raises(LightGraphError, match=f'^{stop}$'):
        open_sources(config, source_credentials={'offline': 'fixture'}, isolate_database_failures=True)
    assert len(calls) == 1 and calls[0].queries == 1
