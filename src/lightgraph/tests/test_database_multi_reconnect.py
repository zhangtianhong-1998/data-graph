"""Fresh snapshot sessions recover boundedly before rows, never during reads."""
import pytest

from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.sources.database_errors import safe_database_diagnostic
from lightgraph.sources.gauss import GaussSource
from lightgraph.sources.mysql import MySQLSource
from lightgraph.sources import reconnect
from lightgraph.tests import test_sources_gauss as gauss, test_sources_mysql as mysql
from lightgraph.tests.test_database_reconnect import read
from lightgraph.tests.test_database_snapshot_lifecycle import sessions, fail_begin
from lightgraph.tests.test_database_snapshot_recovery import GAUSS_QUALIFIED, GaussFailure


class FaultFactory:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def source_with_connect_failures(engine, failures):
    old = gauss.Connection() if engine == 'gauss' else mysql.Connection()
    new = gauss.Connection() if engine == 'gauss' else mysql.Connection()
    controls = [] if engine == 'gauss' else [mysql.Connection(old), mysql.Connection(new)]
    factory = FaultFactory([old, *controls[:1], *failures, new, *controls[1:]])
    cls = GaussSource if engine == 'gauss' else MySQLSource
    source = cls('private fixture' if engine == 'gauss' else mysql.DSN,
        allowed_tables=['public.items' if engine == 'gauss' else 'analytics.items'],
        connect_factory=factory, verified_capabilities=GAUSS_QUALIFIED if engine == 'gauss' else mysql.QUALIFIED)
    return source, old, new, controls, factory


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('classified', [False, True])
def test_transient_connect_failure_recovers_multiple_times_without_agent_retry(engine, classified, monkeypatch):
    pauses = []
    monkeypatch.setattr(reconnect, '_recovery_pause', lambda source: pauses.append(source._boundary_recoveries))
    def failure():
        if not classified:
            return RuntimeError('PRIVATE_CONNECT_FAILURE')
        return GaussFailure('08006') if engine == 'gauss' else mysql.DBError(2006, 'PRIVATE')
    source, old, new, controls, factory = source_with_connect_failures(engine, [failure(), failure()])
    try:
        budget = source.query_budget
        assert old.closed and read(source) == [('001', 'Expenses')]
        assert source.query_budget is budget
        assert source._reconnect_attempts == source._boundary_recoveries == 2
        assert source.connection_generation == 1 and source.stats.queries == 1
        assert source.terminal_failure_code is None and pauses == [1, 2]
        assert factory.calls == (4 if engine == 'gauss' else 6)
        assert new.closed and all(control.closed for control in controls)
        assert any('information_schema' in sql for sql, _ in new.executed)
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_unknown_connect_failure_exhausts_bounded_recovery_and_retains_unknown(engine, monkeypatch):
    monkeypatch.setattr(reconnect, '_recovery_pause', lambda source: None)
    source, _, new, _, factory = source_with_connect_failures(engine,
        [RuntimeError('PRIVATE_PASSWORD') for _ in range(4)])
    try:
        with pytest.raises(LightGraphError, match='database_reconnect_failed') as caught:
            read(source)
        detail = safe_database_diagnostic(caught.value)
        assert detail['category'] == 'unknown'  # unknown must not be asserted as network
        assert detail['boundary_recovery_attempts'] == detail['recovery_attempt_limit'] == 3
        assert detail['terminal'] and 'PRIVATE' not in str(detail)
        calls = factory.calls
        for _ in range(5):
            with pytest.raises(LightGraphError, match='database_reconnect_failed'):
                read(source)
        assert factory.calls == calls and not new.executed
        assert source.stats.queries == 0
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_authentication_failure_never_consumes_recovery_attempts(engine, monkeypatch):
    pauses = []
    monkeypatch.setattr(reconnect, '_recovery_pause', lambda source: pauses.append(True))
    failure = GaussFailure('28P01') if engine == 'gauss' else mysql.DBError(1045, 'PRIVATE')
    source, _, new, _, factory = source_with_connect_failures(engine, [failure])
    try:
        with pytest.raises(LightGraphError) as caught:
            read(source)
        assert safe_database_diagnostic(caught.value)['category'] == 'authentication'
        assert source._reconnect_attempts == 0 and pauses == [] and not new.executed
        assert factory.calls == (2 if engine == 'gauss' else 3)
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_qualification_and_begin_share_one_recovery_limit(engine, monkeypatch):
    monkeypatch.setattr(reconnect, '_recovery_pause', lambda source: None)
    source, data, controls, factory = sessions(engine, count=5)
    # First replacement fails in qualification, second at final BEGIN. Both
    # failures precede fact rows; the third session can safely start from zero.
    fail_begin(data[1], engine, begin_number=1, rollback_fails=True)
    fail_begin(data[2], engine, begin_number=2, rollback_fails=True)
    try:
        assert read(source) == [('001', 'Expenses')]
        assert source._reconnect_attempts == source._boundary_recoveries == 2
        assert source.stats.queries == 1 and source.terminal_failure_code is None
        assert all(item.closed for item in data[:4]) and not data[4].executed
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('stop', ['cancel', 'time', 'queries'])
def test_backoff_respects_shared_budget_and_cancellation(engine, stop, monkeypatch):
    original_pause = reconnect._recovery_pause
    def pause(source):
        if stop == 'cancel': source.cancel()
        elif stop == 'time': source.query_budget.deadline = 0
        else: source.query_budget.max_queries = source.query_budget.queries
        original_pause(source)
    monkeypatch.setattr(reconnect, '_recovery_pause', pause)
    source, _, new, _, factory = source_with_connect_failures(engine, [RuntimeError('PRIVATE')])
    try:
        expected = {'cancel': 'query_cancelled', 'time': 'run_time_budget_exhausted',
                    'queries': 'query_budget_exhausted'}[stop]
        with pytest.raises(LightGraphError, match=expected):
            read(source)
        assert factory.calls == (2 if engine == 'gauss' else 3)
        assert source.terminal_failure_code is None and not new.executed
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_cancel_during_qualification_does_not_install_or_revive_new_session(engine, monkeypatch):
    source, _, new, controls, factory = source_with_connect_failures(engine, [])
    reopen = source._reopen
    def cancelled_reopen(*args):
        replacement = reopen(*args)
        source.cancel()
        return replacement
    source._reopen = cancelled_reopen
    try:
        with pytest.raises(LightGraphError, match='query_cancelled'):
            read(source)
        assert source._cancelled and source.connection_generation == 0
        assert new.closed and all(control.closed for control in controls)
        assert source.stats.queries == 0 and source.terminal_failure_code is None
    finally:
        source.close()
