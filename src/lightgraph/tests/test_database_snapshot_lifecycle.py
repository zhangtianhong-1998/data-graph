"""Public DB lifecycle tests with independent DB-API sessions, no real server."""
import pytest

from lightgraph.errors import LightGraphError
from lightgraph.sources.gauss import GaussSource
from lightgraph.sources.mysql import MySQLSource
from lightgraph.sources.database_errors import safe_database_diagnostic
from lightgraph.tests import test_sources_gauss as gauss
from lightgraph.tests import test_sources_mysql as mysql
from lightgraph.tests.test_database_reconnect import Factory, read
from lightgraph.tests.test_database_snapshot_recovery import GAUSS_QUALIFIED, GaussFailure


def sessions(engine, count=4, *, probe=False, metadata_only=False):
    data = [(gauss.Connection() if engine == 'gauss' else mysql.Connection()) for _ in range(count)]
    controls = [] if engine == 'gauss' else [mysql.Connection(item) for item in data]
    factory = Factory(data if engine == 'gauss' else [part for pair in zip(data, controls) for part in pair])
    cls = GaussSource if engine == 'gauss' else MySQLSource
    source = cls('fixture-only' if engine == 'gauss' else mysql.DSN,
        allowed_tables=['public.items' if engine == 'gauss' else 'analytics.items'],
        connect_factory=factory,
        verified_capabilities=None if probe else GAUSS_QUALIFIED if engine == 'gauss' else mysql.QUALIFIED,
        probe_runtime_limits=probe, metadata_only=metadata_only)
    return source, data, controls, factory


def fail_begin(connection, engine, *, begin_number, failure=None, rollback_fails=False):
    """Leave driver closed=False, even when a lost session cannot rollback."""
    original_cursor, original_rollback = connection.cursor, connection.rollback
    state = {'begins': 0, 'failed': False}
    def cursor(*args, **kwargs):
        result = original_cursor(*args, **kwargs)
        execute = result.execute
        def wrapped(sql, parameters=None):
            if sql.startswith('BEGIN' if engine == 'gauss' else 'START TRANSACTION'):
                state['begins'] += 1
                if state['begins'] == begin_number:
                    state['failed'] = True
                    raise failure or RuntimeError('PRIVATE_HALF_OPEN_SQL')
            return execute(sql, parameters)
        result.execute = wrapped
        return result
    def rollback():
        if state['failed'] and rollback_fails:
            raise RuntimeError('PRIVATE_ROLLBACK_PASSWORD')
        return original_rollback()
    connection.cursor, connection.rollback = cursor, rollback
    return state


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_public_constructor_and_each_outer_snapshot_release_connections(engine):
    source, data, controls, factory = sessions(engine)
    try:
        assert data[0].closed and all(control.closed for control in controls[:1])
        assert source.describe() and source.capabilities().consistent_snapshot is False
        assert source.stats.queries == 0 and source._connection_acquisitions == 1
        assert not source.new_snapshot_recovery_allowed
        budget, queries_before = source.query_budget, source.query_budget.queries
        with source.snapshot():
            assert not data[1].closed
            assert source.capabilities().consistent_snapshot
            assert read(source) == [('001', 'Expenses')]
            assert read(source) == [('001', 'Expenses')]
            assert factory.calls == (2 if engine == 'gauss' else 4)
            assert source._depth == 1
        assert data[1].closed
        assert not source.capabilities().consistent_snapshot
        assert read(source) == [('001', 'Expenses')]
        assert data[2].closed and data[2] is not data[1]
        assert source.query_budget is budget and budget.queries > queries_before
        assert source.stats.queries == 3 and source.connection_generation == 2
        assert source._reconnect_attempts == 0
        for physical in data[1:3]:
            assert any('information_schema' in sql for sql, _ in physical.executed)
            assert any('READ ONLY' in sql for sql, _ in physical.executed)
        if controls: assert all(control.closed for control in controls[:3])
    finally: source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('begin_number', [1, 2])
def test_unknown_half_open_begin_with_failed_rollback_rebuilds_once_before_rows(engine, begin_number):
    source, data, controls, factory = sessions(engine)
    state = fail_begin(data[1], engine, begin_number=begin_number, rollback_fails=True)
    try:
        assert read(source) == [('001', 'Expenses')]
        assert state['failed'] and source._reconnect_attempts == 1
        assert source._connection_acquisitions == 3
        assert source.stats.queries == 1 and source.terminal_failure_code is None
        assert all(item.closed for item in data[:3])
        if controls: assert all(item.closed for item in controls[:3])
        assert not any('FROM "public"."items"' in sql or 'FROM `analytics`.`items`' in sql
                       for sql, _ in data[1].executed)
    finally: source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('begin_number', [1, 2])
def test_repeated_half_open_failure_latches_original_safe_terminal_state(engine, begin_number):
    source, data, controls, factory = sessions(engine, count=6)
    for connection in data[1:5]:
        fail_begin(connection, engine, begin_number=begin_number, rollback_fails=True)
    try:
        with pytest.raises(LightGraphError, match='^database_reconnect_failed$') as caught:
            read(source)
        diagnostic = safe_database_diagnostic(caught.value)
        assert diagnostic['category'] == 'unknown' and diagnostic['rollback_succeeded'] is False
        assert diagnostic['terminal'] and diagnostic['recovery'] == 'reconnect_failed'
        assert diagnostic['boundary_recovery_attempts'] == diagnostic['recovery_attempt_limit'] == 3
        assert source.terminal_failure_code == 'database_reconnect_failed'
        assert not source.new_snapshot_recovery_allowed
        saved = source.terminal_failure_diagnostic
        saved['sqlstate'] = 'PRIVATE'
        calls, queries = factory.calls, source.query_budget.queries
        for _ in range(8):
            with pytest.raises(LightGraphError, match='^database_reconnect_failed$') as repeated:
                read(source)
            assert repeated.value is not caught.value
            assert safe_database_diagnostic(repeated.value) == diagnostic
        assert factory.calls == calls and source.query_budget.queries == queries
        assert source._reconnect_attempts == 3 and source.stats.queries == 0
        assert not data[5].executed and all(item.closed for item in data[:5])
        assert 'PRIVATE' not in str(source.terminal_failure_diagnostic)
    finally: source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('begin_number', [1, 2])
def test_permission_failure_even_with_failed_rollback_never_rebuilds(engine, begin_number):
    source, data, _, factory = sessions(engine)
    fail_begin(data[1], engine, begin_number=begin_number, rollback_fails=True,
        failure=GaussFailure('42501') if engine == 'gauss' else mysql.DBError(1142, 'PRIVATE'))
    try:
        with pytest.raises(LightGraphError) as caught: read(source)
        assert safe_database_diagnostic(caught.value)['category'] == 'permission'
        assert source._reconnect_attempts == 0
        assert source.terminal_failure_code is not None
        assert not data[2].executed
        calls = factory.calls
        with pytest.raises(LightGraphError) as repeated: read(source)
        assert repeated.value.code == caught.value.code and factory.calls == calls
    finally: source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_runtime_probes_repeat_once_per_physical_session_under_shared_budget(engine, monkeypatch):
    probes = []
    cls = GaussSource if engine == 'gauss' else MySQLSource
    def probe(self, *, cancel):
        probes.append((self._connection, cancel))
        self._manifest.capabilities['query_cancel' if cancel else 'statement_timeout'] = 'verified'
    monkeypatch.setattr(cls, '_probe_runtime_control', probe)
    source, data, _, _ = sessions(engine, probe=True)
    try:
        assert source.capabilities().actual_database_verified and data[0].closed
        read(source); read(source)
        assert probes == [(connection, cancel) for connection in data[:3] for cancel in [False, True]]
        source.verify_runtime_controls()
        assert probes[-2:] == [(data[3], False), (data[3], True)] and len(probes) == 8
        assert data[3].closed
    finally: source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_idle_metadata_only_or_explicit_close_never_opens_fact_session(engine):
    source, data, _, factory = sessions(engine, metadata_only=True)
    calls = factory.calls
    try:
        assert data[0].closed and source.describe()
        with pytest.raises(LightGraphError, match='metadata_only_query_forbidden'): read(source)
        assert factory.calls == calls
    finally: source.close()
    with pytest.raises(LightGraphError, match='source_closed'): read(source)
    assert factory.calls == calls


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_cancelling_between_snapshots_stops_without_touching_closed_connection(engine):
    source, data, _, factory = sessions(engine)
    try:
        calls = factory.calls
        source.cancel()
        assert data[0].closed and not source.new_snapshot_recovery_allowed
        with pytest.raises(LightGraphError, match='query_cancelled'): read(source)
        assert factory.calls == calls and source._reconnect_attempts == 0
        assert source.terminal_failure_code is None
    finally: source.close()
