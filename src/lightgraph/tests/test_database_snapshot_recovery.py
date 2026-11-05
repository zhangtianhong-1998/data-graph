"""DB-API failure injection, not qualification of an actual Gauss/MySQL server."""
import threading
import traceback
from types import SimpleNamespace

import pytest

from lightgraph.errors import LightGraphError
from lightgraph.sources.gauss import GaussSource
from lightgraph.sources.database_errors import database_diagnostic, safe_database_diagnostic
from lightgraph.tests import test_sources_gauss as gauss
from lightgraph.tests import test_sources_mysql as mysql


GAUSS_QUALIFIED = {'product': 'openGauss', 'version': '6.0.0', 'driver': 'psycopg_dbapi',
                   'capabilities': {'statement_timeout': 'verified', 'query_cancel': 'verified'}}


def gauss_source(db, **kwargs):
    return GaussSource('fixture-only', allowed_tables=['public.items'],
        connect_factory=lambda _: db, verified_capabilities=GAUSS_QUALIFIED, _keep_connection=True, **kwargs)


class GaussFailure(Exception):
    def __init__(self, state):
        self.sqlstate = state
        super().__init__('PRIVATE_DRIVER_SQL_AND_PASSWORD')


class GaussConnection(gauss.Connection):
    failure_states = None
    fail_rollback = False
    rollbacks = 0
    fail_query = None

    def cursor(self, name=None):
        class Cursor(gauss.Cursor):
            def execute(cursor, sql, parameters=None):
                if sql.startswith('BEGIN') and self.failure_states:
                    self.executed.append((sql, parameters))
                    raise GaussFailure(self.failure_states.pop(0))
                if cursor.name and self.fail_query:
                    raise GaussFailure(self.fail_query)
                return super().execute(sql, parameters)
        return Cursor(self, name)

    def rollback(self):
        self.rollbacks += 1
        if self.fail_rollback:
            raise GaussFailure('08006')


def test_gauss_aborted_transaction_restarts_setup_once_before_rows():
    db = GaussConnection()
    with gauss_source(db) as source:
        before = len(db.executed)
        rollback_before = db.rollbacks
        db.failure_states = ['25P02']
        batches = list(source.project(source.describe()[0], ['code', 'name']))
        commands = [sql for sql, _ in db.executed[before:]]
        assert sum(sql.startswith('BEGIN') for sql in commands) == 2
        assert sum('current_setting' in sql for sql in commands) == 1
        assert sum('statement_timeout' in sql for sql in commands) == 1
        assert source.stats.queries == 1
        assert batches and db.rollbacks == rollback_before + 2
        assert not source.capabilities().consistent_snapshot


@pytest.mark.parametrize('state,category', [
    (None, 'unknown'), ('42501', 'permission'), ('28P01', 'authentication'),
    ('0A000', 'unsupported')])
def test_gauss_start_failure_is_classified_cleaned_and_not_blindly_retried(state, category):
    db = GaussConnection()
    with gauss_source(db) as source:
        before, rollbacks = len(db.executed), db.rollbacks
        db.failure_states = [state]
        with pytest.raises(LightGraphError, match='^read_only_transaction_failed$') as caught:
            with source.snapshot():
                pytest.fail('invalid transaction must not yield')
        diagnostic = safe_database_diagnostic(caught.value)
        assert diagnostic['category'] == category
        assert diagnostic['sqlstate'] == (state or 'unknown')
        assert diagnostic['rollback_succeeded'] is True
        assert caught.value.retryable is False
        assert sum(sql.startswith('BEGIN') for sql, _ in db.executed[before:]) == 1
        assert db.rollbacks == rollbacks + 1 and source.stats.queries == 0
        assert not source.capabilities().consistent_snapshot
        assert 'PRIVATE_DRIVER' not in ''.join(traceback.format_exception(caught.value))
        if category in {'connection', 'idle_transaction_timeout'}:
            assert db.closed and diagnostic['recovery'] == 'reconnect_required'


def test_gauss_repeated_aborted_transaction_stops_after_one_recovery():
    db = GaussConnection()
    with gauss_source(db) as source:
        before = len(db.executed)
        db.failure_states = ['25P02', '25P02', '25P02']
        with pytest.raises(LightGraphError) as caught:
            with source.snapshot():
                pytest.fail('repeated failure must not yield')
        assert safe_database_diagnostic(caught.value)['recovery'] == 'rollback_rebegin_failed'
        assert not caught.value.retryable
        assert sum(sql.startswith('BEGIN') for sql, _ in db.executed[before:]) == 2
        assert source.stats.queries == 0


def test_gauss_setup_budget_failure_keeps_budget_code_and_cleans_transaction():
    db = GaussConnection()
    with gauss_source(db) as source:
        source.query_budget.max_queries = source.query_budget.queries + 1
        before = db.rollbacks
        with pytest.raises(LightGraphError, match='^query_budget_exhausted$'):
            with source.snapshot():
                pytest.fail('must not yield after failed control setup')
        assert db.rollbacks == before + 1
        assert source._depth == 0 and not source.capabilities().consistent_snapshot
        assert source.stats.queries == 0


def test_gauss_failed_start_rollback_does_not_replace_original_driver_cause():
    db = GaussConnection()
    with gauss_source(db) as source:
        db.failure_states, db.fail_rollback = ['42501'], True
        with pytest.raises(LightGraphError, match='^read_only_transaction_failed$') as caught:
            with source.snapshot():
                pytest.fail('must not yield')
        diagnostic = safe_database_diagnostic(caught.value)
        assert diagnostic['sqlstate'] == '42501'
        assert diagnostic['rollback_succeeded'] is False
        assert db.closed


def test_gauss_query_timeout_survives_cleanup_failure_without_replay():
    db = GaussConnection()
    with gauss_source(db) as source:
        db.fail_query, db.fail_rollback = '57014', True
        with pytest.raises(LightGraphError, match='^query_timeout$') as caught:
            list(source.project(source.describe()[0], ['code']))
        diagnostic = safe_database_diagnostic(caught.value)
        assert diagnostic['sqlstate'] == '57014'
        assert diagnostic['rollback_succeeded'] is False
        assert diagnostic['recovery'] == 'reconnect_required'
        assert source.stats.queries == 1 and db.closed


def test_gauss_failure_inside_existing_snapshot_never_restarts_it():
    db = GaussConnection()
    with gauss_source(db) as source:
        before = len(db.executed)
        with source.snapshot():
            for batch in source.project(source.describe()[0], ['code']):
                with batch:
                    assert batch.rows()
            db.fail_query = '25P02'
            with pytest.raises(LightGraphError, match='query_execution_failed'):
                list(source.project(source.describe()[0], ['code']))
        assert sum(sql.startswith('BEGIN') for sql, _ in db.executed[before:]) == 1
        assert source.stats.queries == 2


@pytest.mark.parametrize('number,category,retryable', [
    (9999, 'unknown', False), (1045, 'authentication', False), (1142, 'permission', False),
    (1205, 'lock_timeout', True), (1213, 'deadlock', True)])
def test_mysql_start_failure_cleanup_and_classification(number, category, retryable):
    driver = mysql.Driver()
    with mysql.source(driver, verified_capabilities=mysql.QUALIFIED, _keep_connection=True) as source:
        original = driver.main.cursor
        failed = []
        def cursor():
            current = original()
            execute = current.execute
            def wrapped(sql, parameters=None):
                if sql.startswith('START TRANSACTION'):
                    failed.append(sql)
                    raise mysql.DBError(number, 'PRIVATE_DRIVER_PASSWORD')
                return execute(sql, parameters)
            current.execute = wrapped
            return current
        driver.main.cursor = cursor
        rollbacks = driver.main.rollbacks
        with pytest.raises(LightGraphError, match='^read_only_transaction_failed$') as caught:
            with source.snapshot():
                pytest.fail('must not yield')
        diagnostic = safe_database_diagnostic(caught.value)
        assert diagnostic['category'] == category and diagnostic['errno'] == number
        assert diagnostic['rollback_succeeded'] is True
        assert caught.value.retryable is retryable
        assert len(failed) == 1 and driver.main.rollbacks == rollbacks + 1
        assert source.stats.queries == 0 and not source.capabilities().consistent_snapshot
        assert 'PRIVATE_DRIVER' not in ''.join(traceback.format_exception(caught.value))
        if category == 'connection':
            assert driver.main.closed


def test_mysql_failed_start_rollback_preserves_permission_failure():
    driver = mysql.Driver()
    with mysql.source(driver, verified_capabilities=mysql.QUALIFIED, _keep_connection=True) as source:
        def fail():
            raise mysql.DBError(1142, 'PRIVATE_DRIVER_PASSWORD')
        source._start_readonly = fail
        driver.main.fail_rollback = True
        with pytest.raises(LightGraphError, match='^read_only_transaction_failed$') as caught:
            with source.snapshot():
                pytest.fail('must not yield')
        diagnostic = safe_database_diagnostic(caught.value)
        assert diagnostic['errno'] == 1142 and diagnostic['rollback_succeeded'] is False
        assert driver.main.closed


def test_unknown_driver_text_cannot_become_a_classification_or_leak():
    error = LightGraphError('read_only_transaction_failed')
    error.database_diagnostic = {'engine': 'PRIVATE', 'category': 'PRIVATE',
        'sqlstate': 'PRIVATE', 'errno': 'PRIVATE', 'driver_message': 'PRIVATE',
        'recovery': 'PRIVATE', 'stage': 'PRIVATE', 'rollback_succeeded': 'PRIVATE'}
    assert 'PRIVATE' not in str(safe_database_diagnostic(error))
    assert database_diagnostic(RuntimeError('timeout password'), engine='gauss', stage='query')['category'] == 'unknown'


def test_gauss_query_cancellation_join_is_bounded_and_connection_discarded(monkeypatch):
    db = GaussConnection()
    with gauss_source(db) as source:
        timers = []
        class StuckTimer:
            ident = 1
            def __init__(self, interval, callback):
                self.daemon = False
                timers.append(self)
            def start(self):
                assert self.daemon
            def cancel(self): pass
            def join(self, timeout=None):
                assert timeout == 2
            def is_alive(self): return True
        monkeypatch.setattr('lightgraph.sources.gauss.threading', SimpleNamespace(
            Timer=StuckTimer, Event=threading.Event, RLock=threading.RLock))
        with pytest.raises(LightGraphError, match='^query_cancel_failed$'):
            list(source.project(source.describe()[0], ['code']))
        assert len(timers) == 1 and db.closed
        with pytest.raises(LightGraphError, match='query_cancel_failed'):
            list(source.project(source.describe()[0], ['code']))
