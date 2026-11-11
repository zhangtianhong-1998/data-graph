"""Probe readbacks can requalify a new session; unqualified facts never escape."""
from copy import deepcopy

import pytest

from lightgraph.errors import LightGraphError
from lightgraph.sources.database_errors import safe_database_diagnostic
from lightgraph.sources import reconnect
from lightgraph.sources.gauss import GaussSource
from lightgraph.sources.mysql import MySQLSource
from lightgraph.tests import test_sources_gauss as gauss, test_sources_mysql as mysql
from lightgraph.tests.test_database_reconnect import read
from lightgraph.tests.test_database_snapshot_lifecycle import sessions, fail_begin
from lightgraph.tests.test_database_multi_reconnect import FaultFactory


PROBES = {
    'parameter_binding_unverified': 'SELECT %s',
    'distinct_projection_unverified': 'SELECT DISTINCT 1 AS n',
}


def readback(connection, probe, rows, *, driver_error=None):
    cursor_factory = connection.cursor
    def cursor(*args, **kwargs):
        value = cursor_factory(*args, **kwargs)
        execute = value.execute
        def execute_with_readback(sql, parameters=None):
            result = execute(sql, parameters)
            selected = (sql == PROBES.get(probe) if probe in PROBES else
                        'current_setting' in sql or sql.startswith('SELECT @@session.transaction_read_only'))
            if selected:
                if driver_error is not None:
                    raise driver_error
                value.result = deepcopy(rows)
            return result
        value.execute = execute_with_readback
        return value
    connection.cursor = cursor


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('probe', list(PROBES))
@pytest.mark.parametrize('rows,reason', [([(999,)], 'readback_mismatch'), ([], 'no_row'), ([()], 'malformed_row')])
def test_probe_readback_requalifies_fresh_connection_before_data(engine, probe, rows, reason, monkeypatch, caplog):
    pauses = []
    monkeypatch.setattr(reconnect, '_recovery_pause', lambda source: pauses.append(source._boundary_recoveries))
    source, data, controls, factory = sessions(engine, count=3)
    readback(data[1], probe, rows)
    try:
        assert read(source) == [('001', 'Expenses')]
        assert source._reconnect_attempts == source._boundary_recoveries == 1
        assert source.connection_generation == 1 and source.terminal_failure_code is None
        assert pauses == [1] and source.stats.queries == 1
        assert all(item.closed for item in data + controls)
        assert not any('FROM "public"."items"' in sql or 'FROM `analytics`.`items`' in sql
                       for sql, _ in data[1].executed)
        recorded = [record.database_diagnostic for record in caplog.records
                    if hasattr(record, 'database_diagnostic') and record.database_diagnostic.get('probe_code') == probe]
        assert recorded
        assert recorded[0]['category'] == 'qualification'
        assert recorded[0]['probe_failure'] == reason
        assert recorded[0]['stage'] == 'connection_preflight'
        assert '999' not in str(recorded[0])
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('probe', list(PROBES))
def test_persistent_probe_mismatch_stops_after_three_recoveries(engine, probe, monkeypatch):
    monkeypatch.setattr(reconnect, '_recovery_pause', lambda source: None)
    source, data, controls, factory = sessions(engine, count=6)
    for connection in data[1:5]:
        readback(connection, probe, [('PRIVATE_DRIVER_VALUE',)])
    try:
        with pytest.raises(LightGraphError, match='^database_reconnect_failed$') as caught:
            read(source)
        diagnostic = safe_database_diagnostic(caught.value)
        assert diagnostic['category'] == 'qualification'
        assert diagnostic['probe_code'] == probe and diagnostic['probe_failure'] == 'readback_mismatch'
        assert diagnostic['boundary_recovery_attempts'] == diagnostic['recovery_attempt_limit'] == 3
        assert diagnostic['terminal'] and diagnostic['connection_closed'] is False
        assert source.stats.queries == 0 and source.connection_generation == 0
        assert all(item.closed for item in data[:5] + controls[:5])
        assert not data[-1].executed
        assert 'PRIVATE' not in str(diagnostic)
        before = factory.calls
        with pytest.raises(LightGraphError, match='^database_reconnect_failed$'):
            read(source)
        assert factory.calls == before
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('rows,reason', [([], 'no_row'), ([()], 'malformed_row'),
    ([('off', 'read committed', 'PRIVATE', 'PRIVATE')], 'readback_mismatch')])
def test_readonly_probe_failure_is_diagnostic_and_never_retried(engine, rows, reason, monkeypatch):
    monkeypatch.setattr(reconnect, '_recovery_pause', lambda source: pytest.fail('readonly failure was retried'))
    source, data, controls, factory = sessions(engine, count=3)
    if rows and reason == 'readback_mismatch' and engine == 'gauss':
        rows = [rows[0][:2]]
    readback(data[1], 'read_only_transaction_unverified', rows)
    try:
        with pytest.raises(LightGraphError, match='^read_only_transaction_unverified$') as caught:
            read(source)
        detail = safe_database_diagnostic(caught.value)
        assert detail['stage'] == 'connection_preflight'
        assert detail['category'] == 'qualification'
        assert detail['probe_code'] == 'read_only_transaction_unverified' and detail['probe_failure'] == reason
        assert detail['terminal'] and detail['boundary_recovery_attempts'] == 0
        assert source.stats.queries == 0 and not data[2].executed
        assert data[1].closed and all(item.closed for item in controls[:2])
        assert 'PRIVATE' not in str(detail)
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('probe', list(PROBES))
def test_initial_probe_failure_is_fail_closed_without_recovery(engine, probe):
    main = gauss.Connection() if engine == 'gauss' else mysql.Connection()
    readback(main, probe, [(999,)])
    controls = [] if engine == 'gauss' else [mysql.Connection(main)]
    factory = FaultFactory([main, *controls])
    cls = GaussSource if engine == 'gauss' else MySQLSource
    with pytest.raises(LightGraphError, match='^database_preflight_failed$') as caught:
        cls('private fixture' if engine == 'gauss' else mysql.DSN,
            allowed_tables=['public.items' if engine == 'gauss' else 'analytics.items'], connect_factory=factory)
    detail = safe_database_diagnostic(caught.value)
    assert detail['probe_code'] == probe and detail['category'] == 'qualification'
    assert not detail['terminal'] and factory.calls == 1 + len(controls)
    assert all(item.closed for item in [main, *controls])


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_probe_mismatch_and_qualified_begin_share_recovery_budget(engine, monkeypatch):
    monkeypatch.setattr(reconnect, '_recovery_pause', lambda source: None)
    source, data, _, factory = sessions(engine, count=5)
    readback(data[1], 'parameter_binding_unverified', [(999,)])
    readback(data[2], 'distinct_projection_unverified', [(999,)])
    fail_begin(data[3], engine, begin_number=2, rollback_fails=False)
    try:
        assert read(source) == [('001', 'Expenses')]
        assert source._boundary_recoveries == source._reconnect_attempts == 3
        assert source.stats.queries == 1 and source.terminal_failure_code is None
    finally:
        source.close()


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
@pytest.mark.parametrize('stop', ['cancel', 'time', 'queries'])
def test_probe_recovery_honors_cancellation_and_budget(engine, stop, monkeypatch):
    source, data, _, _ = sessions(engine, count=3)
    readback(data[1], 'parameter_binding_unverified', [(999,)])
    original_pause = reconnect._recovery_pause
    def pause(current):
        if stop == 'cancel': current.cancel()
        elif stop == 'time': current.query_budget.deadline = 0
        else: current.query_budget.max_queries = current.query_budget.queries
        original_pause(current)
    monkeypatch.setattr(reconnect, '_recovery_pause', pause)
    try:
        expected = {'cancel': 'query_cancelled', 'time': 'run_time_budget_exhausted', 'queries': 'query_budget_exhausted'}[stop]
        with pytest.raises(LightGraphError, match=expected):
            read(source)
        assert source.terminal_failure_code is None and source.stats.queries == 0
        assert not data[2].executed
    finally:
        source.close()
