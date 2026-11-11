"""Cleanup cannot turn authorization or budget failures into retryable faults."""
import pytest

from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.sources import reconnect
from lightgraph.sources.database_errors import safe_database_diagnostic
from lightgraph.tests.test_database_reconnect import read
from lightgraph.tests.test_database_runtime_probe_recovery import PGError, runtime_sessions
from lightgraph.tests.test_sources_mysql import DBError


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(reconnect, '_recovery_pause', lambda source: None)


def fail_readonly_then_cleanup(connection, failure):
    original_cursor = connection.cursor
    readbacks = 0

    def cursor(*args, **kwargs):
        result = original_cursor(*args, **kwargs)
        execute, close = result.execute, result.close
        failed = False

        def wrapped(sql, parameters=None):
            nonlocal readbacks, failed
            if 'current_setting' in sql or sql.startswith('SELECT @@session.transaction_read_only'):
                readbacks += 1
                # The initial readonly check passes; fail inside runtime
                # qualification on the same newly acquired physical session.
                if readbacks == 2:
                    failed = True
                    raise failure
            return execute(sql, parameters)

        def fail_close():
            close()
            if failed:
                raise RuntimeError('PRIVATE_CLEANUP_DETAIL')

        result.execute, result.close = wrapped, fail_close
        return result

    connection.cursor = cursor


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('kind', ['permission', 'budget'])
def test_readonly_primary_failure_survives_cursor_close_failure(engine, kind):
    source, data, controls, factory = runtime_sessions(engine)
    failure = (BudgetExceeded('query_budget_exhausted') if kind == 'budget' else
               PGError('42501') if engine == 'gauss' else DBError(1142, 'PRIVATE_PERMISSION'))
    fail_readonly_then_cleanup(data[1], failure)
    try:
        with pytest.raises(LightGraphError) as caught:
            read(source)
        if kind == 'budget':
            assert caught.value.code == 'query_budget_exhausted'
            assert source.terminal_failure_code is None
        else:
            detail = safe_database_diagnostic(caught.value)
            assert detail['category'] == 'permission'
            assert detail['stage'] == 'connection_preflight'
            assert detail['sqlstate'] == ('42501' if engine == 'gauss' else 'unknown')
            assert detail['errno'] == (None if engine == 'gauss' else 1142)
            assert detail['recovery_decision'] == 'deterministic_rejection'
            assert 'PRIVATE' not in str(detail)
        assert source._reconnect_attempts == 0 and source.stats.queries == 0
        assert factory.calls == (2 if engine == 'gauss' else 4)
        assert all(connection.closed for connection in data[:2] + controls[:2])
        assert not data[2].executed
        assert not any('FROM "public"."items"' in sql or 'FROM `analytics`.`items`' in sql
                       for sql, _ in data[1].executed)
    finally:
        source.close()


def test_mysql_kill_permission_survives_control_cursor_close_failure():
    source, data, controls, factory = runtime_sessions('mysql')
    data[1].cancel_works = False
    original_cursor = controls[1].cursor

    def cursor():
        result = original_cursor()
        execute, close = result.execute, result.close
        attempted_kill = False

        def wrapped(sql, parameters=None):
            nonlocal attempted_kill
            attempted_kill = sql.startswith('KILL QUERY')
            try:
                # The fixture signals the waiting query when cancellation
                # finishes, including rejection, so no timing race is needed.
                return execute(sql, parameters)
            except DBError:
                if attempted_kill:
                    raise DBError(1142, 'PRIVATE_KILL_PERMISSION') from None
                raise

        def fail_close():
            close()
            if attempted_kill:
                raise RuntimeError('PRIVATE_CONTROL_CLEANUP')

        result.execute, result.close = wrapped, fail_close
        return result

    controls[1].cursor = cursor
    try:
        with pytest.raises(LightGraphError) as caught:
            read(source)
        detail = safe_database_diagnostic(caught.value)
        assert detail['category'] == 'permission' and detail['errno'] == 1142
        assert detail['stage'] == 'connection_preflight'
        assert detail['recovery_decision'] == 'deterministic_rejection'
        assert 'PRIVATE' not in str(detail)
        assert source._reconnect_attempts == 0 and source.stats.queries == 0
        assert factory.calls == 4 and not data[2].executed
        assert all(connection.closed for connection in data[:2] + controls[:2])
        assert not any('FROM `analytics`.`items`' in sql for sql, _ in data[1].executed)
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_cancel_callback_permission_survives_main_probe_cursor_close_failure(engine):
    source, data, controls, factory = runtime_sessions(engine)
    failed_connection = data[1]
    if engine == 'gauss':
        def cancel_denied():
            failed_connection.cancel_finished.set()
            raise PGError('42501')
        failed_connection.cancel = cancel_denied
    else:
        failed_connection.cancel_works = False
        original_control_cursor = controls[1].cursor

        def control_cursor():
            result = original_control_cursor()
            execute = result.execute
            def permission_denied(sql, parameters=None):
                try:
                    # Still signal probe completion through the fixture's
                    # real cancellation callback, without cancelling the SQL.
                    return execute(sql, parameters)
                except DBError:
                    if sql.startswith('KILL QUERY'):
                        raise DBError(1142, 'PRIVATE_CALLBACK_PERMISSION') from None
                    raise
            result.execute = permission_denied
            return result

        controls[1].cursor = control_cursor
    original_data_cursor = failed_connection.cursor

    def data_cursor(*args, **kwargs):
        result = original_data_cursor(*args, **kwargs)
        execute, close = result.execute, result.close
        cancel_probe = False

        def wrapped(sql, parameters=None):
            nonlocal cancel_probe
            cancel_probe = (sql in {'SELECT SLEEP(0.25), 1', 'SELECT pg_catalog.pg_sleep(0.25)'}
                            and failed_connection.timeout == 1000)
            return execute(sql, parameters)

        def fail_close():
            close()
            if cancel_probe:
                raise RuntimeError('PRIVATE_MAIN_CURSOR_CLEANUP')

        result.execute, result.close = wrapped, fail_close
        return result

    failed_connection.cursor = data_cursor
    try:
        with pytest.raises(LightGraphError) as caught:
            read(source)
        detail = safe_database_diagnostic(caught.value)
        assert detail['category'] == 'permission'
        assert detail['stage'] == 'connection_preflight'
        assert detail['sqlstate'] == ('42501' if engine == 'gauss' else 'unknown')
        assert detail['errno'] == (None if engine == 'gauss' else 1142)
        assert detail['recovery_decision'] == 'deterministic_rejection'
        assert 'PRIVATE' not in str(detail)
        assert source._reconnect_attempts == 0 and source.stats.queries == 0
        assert factory.calls == (2 if engine == 'gauss' else 4)
        assert all(connection.closed for connection in data[:2] + controls[:2])
        assert not data[2].executed
    finally:
        source.close()
