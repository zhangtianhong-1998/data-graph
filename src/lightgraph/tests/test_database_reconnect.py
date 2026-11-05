"""Reconnection contract with local DB-API substitutes, never real databases."""
import traceback
from types import SimpleNamespace

import pytest

from lightgraph.contracts import QueryLimits
from lightgraph.errors import LightGraphError
from lightgraph.sources.database_errors import connection_is_closed, safe_database_diagnostic
from lightgraph.sources.gauss import GaussSource
from lightgraph.sources.mysql import MySQLSource
from lightgraph.tests import test_sources_gauss as gauss
from lightgraph.tests import test_sources_mysql as mysql
from lightgraph.tests.test_database_snapshot_recovery import GAUSS_QUALIFIED, GaussFailure


class Factory:
    def __init__(self, connections):
        self.connections = list(connections)
        self.calls = 0
    def __call__(self, *args, **kwargs):
        self.calls += 1
        if not self.connections:
            raise RuntimeError('PRIVATE_UNEXPECTED_RECONNECT')
        return self.connections.pop(0)


def environment(engine, *, qualified=True, **options):
    if engine == 'gauss':
        old, new = gauss.Connection(), gauss.Connection()
        factory = Factory([old, new])
        source = GaussSource('fixture-private-credential', allowed_tables=['public.items'],
            connect_factory=factory, verified_capabilities=GAUSS_QUALIFIED if qualified else None, **options)
        return source, old, new, factory, []
    old, new = mysql.Connection(), mysql.Connection()
    controls = [mysql.Connection(old), mysql.Connection(new)]
    factory = Factory([old, controls[0], new, controls[1]])
    source = MySQLSource(mysql.DSN, allowed_tables=['analytics.items'], connect_factory=factory,
        verified_capabilities=mysql.QUALIFIED if qualified else None, **options)
    return source, old, new, factory, controls


def arm_start_failure(connection, engine, error, *, closed=False):
    original = connection.cursor
    triggered = []
    def cursor(*args, **kwargs):
        current = original(*args, **kwargs)
        execute = current.execute
        def wrapped(sql, parameters=None):
            if not triggered and sql.startswith('BEGIN' if engine == 'gauss' else 'START TRANSACTION'):
                triggered.append(sql)
                connection.closed = closed
                raise error
            return execute(sql, parameters)
        current.execute = wrapped
        return current
    connection.cursor = cursor
    return triggered


def read(source):
    rows = []
    for batch in source.project(source.describe()[0], ['code', 'name']):
        with batch:
            rows.extend(batch.rows())
    return rows


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('fault', ['closed_before_start', 'broken_nonzero', 'known_disconnect', 'unknown_but_driver_closed'])
def test_reconnects_once_before_rows_with_same_budget_and_full_readonly_setup(engine, fault):
    source, old, new, factory, controls = environment(engine)
    try:
        budget, stats = source.query_budget, source.stats
        before = budget.queries
        if fault in {'closed_before_start', 'broken_nonzero'}:
            old.closed = 2 if fault == 'broken_nonzero' else True
        else:
            error = (GaussFailure('08006') if engine == 'gauss' else mysql.DBError(2006, 'PRIVATE'))
            if fault == 'unknown_but_driver_closed':
                error = RuntimeError('PRIVATE_UNKNOWN_DRIVER')
            arm_start_failure(old, engine, error, closed=fault == 'unknown_but_driver_closed')
        assert read(source) == [('001', 'Expenses')]
        assert source.query_budget is budget and source.stats is stats
        assert budget.queries > before and source.stats.queries == 1
        assert source.connection_generation == source._reconnect_attempts == 1
        assert factory.calls == (2 if engine == 'gauss' else 4)
        assert old.closed and not new.closed
        if controls:
            assert controls[0].closed and not controls[1].closed
            assert any('@@server_uuid' in sql for sql, _ in controls[1].executed)
        commands = [sql for sql, _ in new.executed]
        assert any('READ ONLY' in sql for sql in commands)
        assert any('REPEATABLE READ' in sql for sql in commands)
        assert any('information_schema' in sql for sql in commands)
        assert any('current_setting' in sql or '@@session.transaction_read_only' in sql for sql in commands)
    finally:
        source.close()
    assert source._reopen is None and new.closed


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('fault', ['unknown', 'permission', 'permission_closed'])
def test_unknown_open_connection_and_permission_errors_do_not_reconnect(engine, fault):
    source, old, new, factory, _ = environment(engine)
    try:
        error = RuntimeError('PRIVATE_UNKNOWN') if fault == 'unknown' else (
            GaussFailure('42501') if engine == 'gauss' else mysql.DBError(1142, 'PRIVATE_PERMISSION'))
        arm_start_failure(old, engine, error, closed=fault == 'permission_closed')
        with pytest.raises(LightGraphError, match='read_only_transaction_failed') as caught:
            read(source)
        assert safe_database_diagnostic(caught.value)['category'] == ('unknown' if fault == 'unknown' else 'permission')
        assert not caught.value.retryable
        assert source._reconnect_attempts == 0
        assert factory.calls == (1 if engine == 'gauss' else 2)
        assert not new.executed
        if fault == 'permission_closed':
            with pytest.raises(LightGraphError, match='source_closed'):
                read(source)
            assert source._reconnect_attempts == 0
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_failed_requalification_closes_new_connections_and_latches_retry_budget(engine):
    source, old, new, factory, controls = environment(engine)
    try:
        old.closed = True
        arm_start_failure(new, engine, GaussFailure('42501') if engine == 'gauss'
                          else mysql.DBError(1142, 'PRIVATE_NEW_PERMISSION'))
        with pytest.raises(LightGraphError) as caught:
            read(source)
        assert safe_database_diagnostic(caught.value)['recovery'] == 'reconnect_failed'
        assert source._reconnect_attempts == 1 and source.connection_generation == 0
        assert not source.new_snapshot_recovery_allowed
        assert new.closed and (not controls or controls[1].closed)
        calls = factory.calls
        for _ in range(3):
            with pytest.raises(LightGraphError, match='source_closed'):
                read(source)
        assert factory.calls == calls and source._reconnect_attempts == 1
        assert source.stats.queries == 0
        assert 'PRIVATE' not in ''.join(traceback.format_exception(caught.value))
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_changed_catalog_cannot_adopt_new_connection(engine):
    source, old, new, factory, _ = environment(engine)
    original = new.cursor
    def cursor(*args, **kwargs):
        current = original(*args, **kwargs)
        execute = current.execute
        def wrapped(sql, parameters=None):
            result = execute(sql, parameters)
            if 'information_schema.columns' in sql.lower():
                current.result = [row for row in current.result if row[0] != 'name']
            return result
        current.execute = wrapped
        return current
    new.cursor = cursor
    try:
        old.closed = True
        with pytest.raises(LightGraphError, match='source_catalog_changed'):
            read(source)
        assert source.stats.queries == 0 and source.connection_generation == 0 and new.closed
        assert 'name' in source.describe()[0].column_names
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_partial_query_failure_is_not_replayed_or_reconnected_inside_snapshot(engine):
    source, old, new, factory, _ = environment(engine, limits=QueryLimits(max_batch_rows=1))
    original = old.cursor
    def cursor(*args, **kwargs):
        current = original(*args, **kwargs)
        execute, fetchone = current.execute, current.fetchone
        emitted = 0
        data = False
        def wrapped(sql, parameters=None):
            nonlocal data
            result = execute(sql, parameters)
            data = ('FROM "public"."items"' in sql or 'FROM `analytics`.`items`' in sql)
            if data: current.result = [('001', 'A'), ('002', 'B'), ('003', 'C')]
            return result
        def fetch():
            nonlocal emitted
            if data:
                emitted += 1
                if emitted == 3:
                    old.closed = True
                    raise RuntimeError('PRIVATE_CLOSED_AFTER_BATCH')
            return fetchone()
        current.execute, current.fetchone = wrapped, fetch
        return current
    old.cursor = cursor
    try:
        with source.snapshot():
            iterator = source.project(source.describe()[0], ['code', 'name'])
            with next(iterator) as batch:
                assert list(batch.rows()) == [('001', 'A')]
            with pytest.raises(LightGraphError, match='query_execution_failed'):
                next(iterator)
            assert source._reconnect_attempts == 0
            assert not source.new_snapshot_recovery_allowed
            with pytest.raises(LightGraphError, match='source_closed'):
                read(source)
            assert source._reconnect_attempts == 0 and not new.executed
        # This is a separate, explicitly requested query after the failed
        # snapshot ended. It restarts its own result, never appends old rows.
        calls, queries = factory.calls, source.query_budget.queries
        assert source.new_snapshot_recovery_allowed
        assert factory.calls == calls and source.query_budget.queries == queries
        assert read(source) == [('001', 'Expenses')]
        assert source.stats.queries == 2 and source.connection_generation == 1
        assert not source.new_snapshot_recovery_allowed
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_closed_or_metadata_only_source_does_not_reconnect(engine):
    source, old, new, factory, _ = environment(engine, metadata_only=True)
    try:
        old.closed = True
        with pytest.raises(LightGraphError, match='metadata_only_query_forbidden'):
            read(source)
        assert source._reconnect_attempts == 0
    finally:
        source.close()
    with pytest.raises(LightGraphError, match='source_closed'):
        read(source)
    assert not new.executed and source._reopen is None


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_reconnect_cannot_reset_exhausted_query_budget(engine):
    source, old, new, factory, _ = environment(engine)
    try:
        old.closed = True
        source.query_budget.max_queries = source.query_budget.queries
        calls = factory.calls
        with pytest.raises(LightGraphError, match='query_budget_exhausted'):
            read(source)
        assert factory.calls == calls and source._reconnect_attempts == 0
        assert not source.new_snapshot_recovery_allowed
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_previously_probed_runtime_controls_are_rechecked_on_new_connection(engine, monkeypatch):
    source, old, new, factory, _ = environment(engine, qualified=False)
    probes = []
    def probe(self, *, cancel):
        probes.append((self._connection, cancel))
        self._manifest.capabilities['query_cancel' if cancel else 'statement_timeout'] = 'verified'
    cls = GaussSource if engine == 'gauss' else MySQLSource
    monkeypatch.setattr(cls, '_probe_runtime_control', probe)
    try:
        source.verify_runtime_controls()
        assert source.capabilities().actual_database_verified
        old.closed = True
        read(source)
        assert probes == [(old, False), (old, True), (new, False), (new, True)]
        assert source.capabilities().actual_database_verified
    finally:
        source.close()


@pytest.mark.parametrize('value,expected', [(True, True), (1, True), (2, True),
    (0, False), (False, False), (-1, False), ('closed', False), (None, False)])
def test_only_explicit_driver_closed_states_trigger_reconnection(value, expected):
    assert connection_is_closed(SimpleNamespace(closed=value)) is expected


def test_mysql_timeout_can_requalify_next_snapshot_without_replaying_failed_query():
    source, old, new, _, _ = environment('mysql')
    try:
        old.fail_errno = 3024
        with pytest.raises(LightGraphError, match='query_timeout'):
            read(source)
        assert source.stats.queries == 1 and source.connection_generation == 0
        assert source.new_snapshot_recovery_allowed
        assert read(source) == [('001', 'Expenses')]
        assert source.stats.queries == 2 and source.connection_generation == 1
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_late_cancel_callback_stays_bound_to_retired_connection(engine, monkeypatch):
    import threading
    timers = []
    class Timer:
        ident = 1
        def __init__(self, interval, callback):
            self.callback = callback
            self.daemon = False
            timers.append(self)
        def start(self): pass
        def cancel(self): pass
        def join(self, timeout=None): pass
        def is_alive(self): return False
    source, old, new, _, _ = environment(engine)
    monkeypatch.setattr('lightgraph.sources.' + engine + '.threading', SimpleNamespace(
        Timer=Timer, Event=threading.Event, RLock=threading.RLock, Lock=threading.Lock))
    try:
        read(source)
        callback = timers[0].callback
        old.closed = True
        read(source)
        callback()
        if engine == 'gauss':
            assert old.cancelled and not new.cancelled
        else:
            assert old.cancelled.is_set() and not new.cancelled.is_set()
    finally:
        source.close()
