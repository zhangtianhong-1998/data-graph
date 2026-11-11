"""Exercise real fixed runtime probes across replacement sessions, not real DBs."""
import re
import threading

import pytest

from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.sources.gauss import GaussSource
from lightgraph.sources.database_errors import safe_database_diagnostic
from lightgraph.sources import reconnect
from lightgraph.tests import test_sources_gauss as gauss
from lightgraph.tests import test_sources_mysql as mysql
from lightgraph.tests.test_database_reconnect import Factory, read
from lightgraph.tests.test_database_snapshot_lifecycle import sessions
from lightgraph.tests.test_database_probe_recovery import readback


class PGError(Exception):
    def __init__(self, state):
        self.sqlstate = state
        super().__init__('PRIVATE_DRIVER_DETAIL')


class RuntimeCursor(gauss.Cursor):
    def execute(self, sql, parameters=None):
        result = super().execute(sql, parameters)
        if sql.startswith('SET LOCAL statement_timeout'):
            self.connection.timeout = int(re.search(r"'([0-9]+)ms'", sql).group(1))
            if self.connection.timeout == 1000:
                self.connection.cancel_finished.clear()
                self.connection.cancelled = False
        if sql == 'SELECT pg_catalog.pg_sleep(0.25)':
            if self.connection.timeout == 50:
                if self.connection.timeout_works:
                    raise PGError('57014')
            else:
                assert self.connection.cancel_finished.wait(3)
                if self.connection.cancelled:
                    raise PGError('57014')
        return result


class RuntimeConnection(gauss.Connection):
    def __init__(self):
        super().__init__()
        self.timeout_works = self.cancel_works = True
        self.cancel_finished = threading.Event()

    def cursor(self, name=None):
        return RuntimeCursor(self, name)

    def cancel(self):
        self.cancelled = self.cancel_works
        self.cancel_finished.set()


def runtime_sessions(engine, count=3):
    if engine == 'mysql':
        return sessions(engine, count=count, probe=True)
    data = [RuntimeConnection() for _ in range(count)]
    factory = Factory(data)
    source = GaussSource('fixture-only', allowed_tables=['public.items'],
        connect_factory=factory, probe_runtime_limits=True)
    return source, data, [], factory


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(reconnect, '_recovery_pause', lambda source: None)


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('control,probe', [('timeout_works', 'statement_timeout_unverified'),
                                         ('cancel_works', 'query_cancel_unverified')])
@pytest.mark.parametrize('failures', [1, 4])
def test_runtime_probe_failure_uses_new_session_or_stops_at_bound(engine, control, probe, failures, caplog):
    source, data, controls, factory = runtime_sessions(engine, count=failures + 2)
    for connection in data[1:failures + 1]:
        setattr(connection, control, False)
    try:
        if failures == 1:
            assert read(source) == [('001', 'Expenses')]
            assert source.terminal_failure_code is None
            detail = next(record.database_diagnostic for record in caplog.records
                if getattr(record, 'database_diagnostic', {}).get('recovery_decision') == 'retry_new_session')
        else:
            with pytest.raises(LightGraphError, match='^database_reconnect_failed$') as caught:
                read(source)
            detail = safe_database_diagnostic(caught.value)
            assert detail['terminal'] and detail['recovery_decision'] == 'retry_limit_exhausted'
            calls = factory.calls
            with pytest.raises(LightGraphError, match='^database_reconnect_failed$'):
                read(source)
            assert factory.calls == calls and not data[-1].executed
        assert source._reconnect_attempts == min(failures, 3)
        assert detail['stage'] == 'connection_preflight' and detail['cause_code'] == probe
        # MySQL's rejected cancellation has a driver category; a successful
        # statement without interruption has an explicit qualification result.
        if engine == 'gauss' or control == 'timeout_works':
            assert detail['probe_code'] == probe
        assert all(connection.closed for connection in data[:failures + 1] + controls[:failures + 1])
        assert not any('FROM "public"."items"' in sql or 'FROM `analytics`.`items`' in sql
            for connection in data[1:failures + 1] for sql, _ in connection.executed)
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('occurrence', [2, 3, 4, 5])
def test_readonly_checks_inside_runtime_qualification_have_preflight_stage(engine, occurrence):
    source, data, controls, _ = runtime_sessions(engine)
    readback(data[1], 'read_only_transaction_unverified', [], occurrence=occurrence)
    try:
        assert read(source) == [('001', 'Expenses')]
        assert source._reconnect_attempts == 1 and data[1].closed
        assert all(connection.closed for connection in controls)
    finally:
        source.close()


def inject_sleep_error(connection, error):
    original = connection.cursor
    def cursor(*args, **kwargs):
        result = original(*args, **kwargs)
        execute = result.execute
        def fault(sql, parameters=None):
            if 'SLEEP(0.25)' in sql or 'pg_sleep(0.25)' in sql:
                raise error
            return execute(sql, parameters)
        result.execute = fault
        return result
    connection.cursor = cursor


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('kind', ['permission', 'authentication', 'budget', 'cancelled', 'unknown_driver'])
def test_runtime_probe_preserves_driver_category_and_budget_before_retry(engine, kind):
    source, data, controls, factory = runtime_sessions(engine)
    errors = {'budget': BudgetExceeded('query_budget_exhausted'), 'cancelled': LightGraphError('query_cancelled'),
        'permission': PGError('42501') if engine == 'gauss' else mysql.DBError(1142, 'PRIVATE'),
        'authentication': PGError('28P01') if engine == 'gauss' else mysql.DBError(1045, 'PRIVATE'),
        'unknown_driver': PGError('29003') if engine == 'gauss' else mysql.DBError(999, 'PRIVATE')}
    inject_sleep_error(data[1], errors[kind])
    try:
        if kind == 'unknown_driver':
            assert read(source) == [('001', 'Expenses')]
            assert source._reconnect_attempts == 1
        else:
            with pytest.raises(LightGraphError) as caught:
                read(source)
            assert source._reconnect_attempts == 0 and not data[-1].executed
            if kind in {'budget', 'cancelled'}:
                assert caught.value.code == errors[kind].code
                assert source.terminal_failure_code is None
            else:
                detail = safe_database_diagnostic(caught.value)
                assert detail['category'] == kind
                assert detail['cause_code'] == 'statement_timeout_unverified'
                assert detail['recovery_decision'] == 'deterministic_rejection'
                assert 'PRIVATE' not in str(detail)
        assert data[1].closed and all(connection.closed for connection in controls[:factory.calls // 2])
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('code', ['invalid_capability_manifest', 'qualified_capability_manifest_required', 'PRIVATE_NEW_ERROR'])
def test_unclassified_replacement_retains_safe_cause_without_guessing_recovery(engine, code):
    source, data, _, _ = runtime_sessions(engine)
    def fail(*args):
        raise LightGraphError(code)
    source._reopen = fail
    try:
        with pytest.raises(LightGraphError, match='database_reconnect_failed') as caught:
            read(source)
        detail = safe_database_diagnostic(caught.value)
        assert detail['cause_code'] == ('unclassified_application_error' if code.startswith('PRIVATE') else code)
        assert detail['boundary_recovery_attempts'] == 0 and not data[1].executed
        assert detail['recovery_decision'] == ('unclassified_failure' if code.startswith('PRIVATE') else 'deterministic_rejection')
        assert 'PRIVATE' not in str(detail)
    finally:
        source.close()
