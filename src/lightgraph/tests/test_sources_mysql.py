"""MySQL DB-API substitutes: protocol/retention tests, not live DB acceptance."""
from __future__ import annotations

import ssl
import threading
import time

import pytest

from lightgraph.contracts import QueryLimits
from lightgraph.errors import LightGraphError
from lightgraph.sources.budget import QueryBudget
from lightgraph.sources.mysql import MySQLSource, _connect_options, product_version

DSN = 'mysql://reader:PRIVATE_PASSWORD@127.0.0.1:3306/analytics'
QUALIFIED = {'product': 'MySQL', 'version': '8.4.3', 'driver': 'pymysql_dbapi',
             'capabilities': {'statement_timeout': 'verified', 'query_cancel': 'verified'}}


class DBError(Exception):
    pass


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = []
        self.description = []
        self.closed = False
        self.data_query = False
        self._executed = None
        self._rows = None

    def execute(self, sql, parameters=None):
        db = self.connection
        self._executed = sql
        db.executed.append((sql, parameters))
        self.result = []
        if sql.startswith('KILL QUERY'):
            if not db.owner.cancel_works:
                raise DBError(1095, 'PRIVATE_CANCEL_DETAIL')
            db.owner.cancelled.set()
        elif sql.startswith('SET SESSION sql_mode'):
            if not db.ignore_sql_mode:
                db.sql_mode = sql.split("'")[1]
        elif sql.startswith('SET SESSION max_execution_time'):
            db.timeout = int(sql.rsplit('=', 1)[1])
        elif sql.startswith('SELECT @@session.transaction_read_only'):
            self.result = [(db.readonly, db.isolation, db.timeout, db.sql_mode)]
        elif sql.startswith('SELECT VERSION()'):
            self.result = [(db.version, db.version_comment, 42, db.server_uuid, db.current_user)]
        elif sql == 'SELECT @@server_uuid, CURRENT_USER()':
            self.result = [(db.server_uuid, db.current_user)]
        elif sql == 'SELECT %s':
            self.result = [(71,)]
        elif sql.startswith('SELECT DISTINCT 1') or sql == 'SELECT 1':
            self.result = [(1,)]
        elif sql.startswith('WITH p AS'):
            self.result = [(1,), (2,)]
        elif 'FROM information_schema.TABLES' in sql:
            self.result = [(db.table_type, db.engine, 'Business registry')]
        elif 'FROM information_schema.COLUMNS' in sql:
            self.result = [('tenant', 'varchar', 'NO', '租户'), ('code', 'varchar', 'NO', '业务编码'),
                           ('name', 'varchar', 'YES', '名称'), ('parent_code', 'varchar', 'YES', '')]
        elif 'FROM information_schema.TABLE_CONSTRAINTS' in sql:
            self.result = [
                ('PRIMARY', 'PRIMARY KEY', 'tenant', 1, None, None, None),
                ('PRIMARY', 'PRIMARY KEY', 'code', 2, None, None, None),
                ('uq_name', 'UNIQUE', 'tenant', 1, None, None, None),
                ('uq_name', 'UNIQUE', 'name', 2, None, None, None),
                ('fk_parent', 'FOREIGN KEY', 'tenant', 1, 'analytics', 'items', 'tenant'),
                ('fk_parent', 'FOREIGN KEY', 'parent_code', 2, 'analytics', 'items', 'code')]
        elif sql == 'SELECT SLEEP(0.25), 1':
            if db.timeout == 50:
                if db.timeout_works:
                    raise DBError(3024, 'PRIVATE_TIMEOUT_DETAIL')
            else:
                time.sleep(.08)
                if db.cancelled.is_set():
                    db.cancelled.clear()
                    raise DBError(1317, 'PRIVATE_CANCEL_DETAIL')
                if db.false_timeout_on_cancel:
                    raise DBError(3024, 'PRIVATE_TIMEOUT_NOT_CANCEL')
            self.result = [(0, 1)]
        elif 'FROM `analytics`.`items`' in sql:
            self.data_query = True
            if db.on_query:
                db.on_query()
            if db.fail_errno:
                raise DBError(db.fail_errno, 'PRIVATE_DRIVER_VALUE')
            self.result = list(db.rows)
            self.description = [('code',), ('name',)]
        return 0

    def fetchone(self):
        return self.result.pop(0) if self.result else None

    def close(self):
        if self.data_query and self.result and not self.connection.closed:
            self.connection.drained_unread = True
        self.closed = True


class Connection:
    def __init__(self, owner=None):
        self.owner = owner
        self.closed = False
        self.executed = []
        self.cursors = []
        self.rollbacks = 0
        self.sql_mode = 'NO_BACKSLASH_ESCAPES,ANSI_QUOTES'
        self.ignore_sql_mode = False
        self.timeout = 60000
        self.version = '8.4.3'
        self.version_comment = 'MySQL Community Server - GPL'
        self.server_uuid = 'f5dd37bc-840a-11ef-b2b0-0242ac120002'
        self.current_user = 'reader@%'
        self.engine = 'InnoDB'
        self.table_type = 'BASE TABLE'
        self.readonly = 1
        self.isolation = 'REPEATABLE-READ'
        self.timeout_works = self.cancel_works = True
        self.false_timeout_on_cancel = False
        self.cancelled = threading.Event()
        self.rows = [('001', 'Expenses')]
        self.fail_errno = None
        self.on_query = None
        self.drained_unread = False
        self.fail_close = self.fail_rollback = False

    def cursor(self):
        cursor = Cursor(self)
        self.cursors.append(cursor)
        return cursor

    def rollback(self):
        if self.fail_rollback:
            raise DBError(1, 'PRIVATE_ROLLBACK_VALUE')
        self.rollbacks += 1

    def close(self):
        if self.closed:
            raise DBError(1, "Already closed")
        self.closed = True
        if self.fail_close:
            raise DBError(1, 'PRIVATE_CLOSE_VALUE')


class Driver:
    def __init__(self):
        self.main = Connection()
        self.control = Connection(self.main)
        self.options = []

    def __call__(self, **kwargs):
        self.options.append(dict(kwargs))
        return self.main if len(self.options) == 1 else self.control


def source(driver, **kwargs):
    return MySQLSource(DSN, allowed_tables=['analytics.items'], connect_factory=driver, **kwargs)


def test_metadata_composite_keys_and_default_capability_gate():
    driver = Driver()
    with source(driver) as adapter:
        table = adapter.describe()[0]
        assert table.comment == 'Business registry'
        assert table.unique_keys == [['tenant', 'code'], ['tenant', 'name']]
        assert [c.name for c in table.columns if c.primary_key] == ['tenant', 'code']
        assert table.foreign_keys[0].columns == ['tenant', 'parent_code']
        assert table.foreign_keys[0].target_columns == ['tenant', 'code']
        assert table.columns[2].comment == '名称'
        manifest = adapter.capabilities()
        assert manifest.product == 'MySQL' and manifest.version == '8.4.3'
        assert manifest.capabilities['statement_timeout'] == manifest.capabilities['query_cancel'] == 'unknown'
        assert not manifest.actual_database_verified and not manifest.consistent_snapshot
        with pytest.raises(LightGraphError, match='runtime_capability_unverified'):
            list(adapter.project(table, ['code']))
        assert 'PRIVATE_PASSWORD' not in repr(adapter)
        assert len(driver.options) == 2
        assert all(not value['local_infile'] and value['client_flag'] == 0 for value in driver.options)
    assert driver.main.closed and driver.control.closed


def test_scoped_columns_do_not_project_partial_keys_or_foreign_keys():
    with source(Driver(), allowed_columns={'analytics.items': ['code', 'name']}) as adapter:
        table = adapter.describe()[0]
        assert table.column_names == ['code', 'name']
        assert not table.unique_keys and not table.foreign_keys
        assert not any(c.primary_key for c in table.columns)


def test_parameter_binding_snapshot_and_ephemeral_cleanup():
    driver = Driver()
    with source(driver, verified_capabilities=QUALIFIED) as adapter:
        table = adapter.describe()[0]
        before = len(driver.main.executed)
        with adapter.snapshot():
            assert adapter.capabilities().consistent_snapshot
            rows = []
            for batch in adapter.project(table, ['code', 'name'], predicate={'code': 'PRIVATE_QUERY_VALUE'}):
                rows.extend(batch.rows())
            assert rows == [('001', 'Expenses')]
            assert adapter.capabilities().consistent_snapshot
        assert not adapter.capabilities().consistent_snapshot
        generated = [(s, p) for s, p in driver.main.executed if 'FROM `analytics`.`items`' in s][-1]
        assert 'PRIVATE_QUERY_VALUE' not in generated[0]
        assert generated[1]['p0'] == 'PRIVATE_QUERY_VALUE'
        assert '%(p0)s' in generated[0] and '%(cap)s' in generated[0]
        assert sum(s.startswith('START TRANSACTION') for s, _ in driver.main.executed[before:]) == 1
        assert all(c.closed and c._executed is None for c in driver.main.cursors)
        assert adapter.stats.queries == 1
        assert adapter.capabilities().capabilities['cursor_paging'] == 'verified'
        assert not driver.main.drained_unread


@pytest.mark.parametrize('version,comment', [('5.7.44', 'MySQL Community Server'),
    ('8.1.0', 'MySQL Community Server'), ('8.4.3-MariaDB', 'MariaDB'),
    ('8.0.36-28', 'Percona Server'), ('8.0.11-TiDB-v7', 'MySQL compatible'),
    ('8.4.3', 'unknown server')])
def test_unqualified_products_and_versions_fail_closed(version, comment):
    driver = Driver()
    driver.main.version, driver.main.version_comment = version, comment
    with pytest.raises(LightGraphError, match='mysql_product_version_unsupported'):
        source(driver)
    assert driver.main.closed and driver.control.closed


@pytest.mark.parametrize('version', ['8.0.36', '8.4.3', '8.0.36-commercial'])
def test_exact_oracle_version_binding(version):
    assert product_version(version, 'MySQL Enterprise Server') == ('MySQL', version)


@pytest.mark.parametrize('field,value', [('version', '8.0.36'), ('driver', 'other'), ('product', 'MariaDB')])
def test_manifest_exact_binding(field, value):
    manifest = {**QUALIFIED, field: value}
    with pytest.raises(LightGraphError, match='capability_manifest_binding_mismatch'):
        source(Driver(), verified_capabilities=manifest)


def test_runtime_probes_verify_actual_interruptions_without_repeating_catalog():
    driver = Driver()
    with source(driver) as adapter:
        catalog_before = sum('information_schema' in sql for sql, _ in driver.main.executed)
        manifest = adapter.verify_runtime_controls()
        assert manifest.actual_database_verified
        assert manifest.capabilities['statement_timeout'] == manifest.capabilities['query_cancel'] == 'verified'
        assert not manifest.consistent_snapshot
        assert sum('information_schema' in sql for sql, _ in driver.main.executed) == catalog_before
        assert driver.control.executed == [('SELECT @@server_uuid, CURRENT_USER()', None), ('KILL QUERY 42', None)]
        assert 'SELECT SLEEP(0.25), 1' in [s for s, _ in driver.main.executed]


def test_cancel_probe_does_not_accept_server_timeout_as_cancellation():
    driver = Driver()
    driver.main.cancel_works = False
    driver.main.false_timeout_on_cancel = True
    with source(driver, limits=QueryLimits(timeout_ms=1)) as adapter:
        with pytest.raises(LightGraphError, match='query_cancel_unverified'):
            adapter.verify_runtime_controls()
        assert not adapter.capabilities().actual_database_verified
        assert adapter.capabilities().capabilities['query_cancel'] == 'unknown'
        assert ('SET SESSION max_execution_time = 1000', None) in driver.main.executed


def test_timeout_probe_cannot_be_assumed_from_set_acceptance():
    driver = Driver()
    driver.main.timeout_works = False
    with source(driver) as adapter:
        with pytest.raises(LightGraphError, match='statement_timeout_unverified'):
            adapter.verify_runtime_controls()
        assert not adapter.capabilities().actual_database_verified


@pytest.mark.parametrize('attribute,value,code', [('engine', 'MyISAM', 'mysql_storage_engine_unsupported'),
    ('table_type', 'VIEW', 'authorized_table_unavailable_or_view'),
    ('readonly', 0, 'read_only_transaction_unverified'),
    ('isolation', 'READ-COMMITTED', 'read_only_transaction_unverified')])
def test_view_engine_and_readonly_failures(attribute, value, code):
    driver = Driver()
    setattr(driver.main, attribute, value)
    with pytest.raises(LightGraphError, match=code):
        source(driver)


def test_query_failure_is_redacted_and_does_not_drain_remaining_rows():
    driver = Driver()
    with source(driver, verified_capabilities=QUALIFIED) as adapter:
        driver.main.fail_errno = 9999
        with pytest.raises(LightGraphError, match='query_execution_failed') as error:
            list(adapter.project(adapter.describe()[0], ['code']))
        assert 'PRIVATE_DRIVER_VALUE' not in str(error.value)
        assert driver.main.closed
        assert not adapter.capabilities().consistent_snapshot
        with pytest.raises(LightGraphError, match='source_closed'):
            with adapter.snapshot():
                pass


def test_early_generator_close_drops_unbuffered_connection_without_drain():
    driver = Driver()
    driver.main.rows = [('001', 'A'), ('002', 'B'), ('003', 'C')]
    with source(driver, verified_capabilities=QUALIFIED, limits=QueryLimits(max_batch_rows=1)) as adapter:
        iterator = adapter.project(adapter.describe()[0], ['code', 'name'])
        batch = next(iterator)
        assert list(batch.rows()) == [('001', 'A')]
        iterator.close()
        assert driver.main.closed and not driver.main.drained_unread
        assert not adapter.capabilities().consistent_snapshot


def test_cancel_is_bound_to_own_connection_and_error_is_safe():
    driver = Driver()
    with source(driver) as adapter:
        with pytest.raises(LightGraphError, match='query_cancel_target_forbidden'):
            adapter.cancel('43')
        assert not any(s.startswith('KILL') for s, _ in driver.control.executed)
        adapter.cancel()
        assert driver.control.executed[-1][0] == 'KILL QUERY 42'
        driver.main.cancel_works = False
        with pytest.raises(LightGraphError, match='query_cancel_failed') as error:
            adapter.cancel()
        assert 'PRIVATE_CANCEL_DETAIL' not in str(error.value)


def test_preflight_shared_budget_and_connect_errors_are_safe():
    driver = Driver()
    budget = QueryBudget(max_queries=2)
    with pytest.raises(LightGraphError, match='query_budget_exhausted'):
        source(driver, query_budget=budget)
    assert budget.queries == 2 and driver.main.closed and driver.control.closed
    def fail(**_kwargs):
        raise RuntimeError('PRIVATE_PASSWORD and source row')
    with pytest.raises(LightGraphError, match='database_connection_failed') as error:
        MySQLSource(DSN, allowed_tables=['analytics.items'], connect_factory=fail)
    assert 'PRIVATE_PASSWORD' not in str(error.value)


@pytest.mark.parametrize('sql', ['DELETE FROM analytics.items', 'SELECT name INTO OUTFILE "/tmp/private" FROM analytics.items',
                               'SELECT SLEEP(1)', 'KILL QUERY 42'])
def test_agent_cannot_use_adapter_control_sql(sql):
    from lightgraph.query.guard import ValidatedQuery
    driver = Driver()
    with source(driver, verified_capabilities=QUALIFIED) as adapter:
        before = len(driver.main.executed)
        with pytest.raises(LightGraphError):
            list(adapter.execute(ValidatedQuery(sql)))
        assert len(driver.main.executed) == before


def test_dsn_tls_and_option_whitelist(monkeypatch):
    limits = QueryLimits()
    context = ssl.create_default_context()
    monkeypatch.setattr('lightgraph.sources.mysql.ssl.create_default_context', lambda **_kw: context)
    secure = _connect_options('mysql://reader:p%40ss@db.example:3307/analytics', limits)
    assert secure['ssl'].check_hostname and secure['ssl'].verify_mode == ssl.CERT_REQUIRED
    assert secure['password'] == 'p@ss' and secure['port'] == 3307
    assert 'ssl' not in _connect_options(DSN, limits)
    assert _connect_options(DSN, limits)['ssl_disabled'] is True
    for bad in ['mysql://u:p@db.example/a?ssl_mode=DISABLED', DSN + '?local_infile=1',
                DSN + '?ssl_mode=DISABLED&ssl_mode=VERIFY_IDENTITY', 'postgres://u:p@localhost/a',
                DSN + '#secret', DSN + '?ssl_cert=only-one.pem']:
        with pytest.raises(LightGraphError, match='mysql_dsn_invalid'):
            _connect_options(bad, limits)


def test_default_driver_uses_unbuffered_cursor_and_real_distribution_version(monkeypatch):
    import pymysql
    from importlib.metadata import version
    driver = Driver()
    monkeypatch.setattr(pymysql, 'connect', driver)
    with MySQLSource(DSN, allowed_tables=['analytics.items']) as adapter:
        assert adapter.capabilities().driver == 'PyMySQL/' + version('PyMySQL')
        assert all(o['cursorclass'] is pymysql.cursors.SSCursor for o in driver.options)
        assert driver.options[1]['read_timeout'] <= 2


def test_failed_close_is_fixed_code_and_not_claimed_clean():
    driver = Driver()
    adapter = source(driver)
    driver.control.fail_close = True
    with pytest.raises(LightGraphError, match='source_close_failed') as error:
        adapter.close()
    assert 'PRIVATE_CLOSE_VALUE' not in str(error.value)
    assert driver.main.closed


def test_snapshot_rollback_failure_is_fixed_code():
    driver = Driver()
    with source(driver, verified_capabilities=QUALIFIED) as adapter:
        with pytest.raises(LightGraphError, match='snapshot_close_failed') as error:
            with adapter.snapshot():
                driver.main.fail_rollback = True
        driver.main.fail_rollback = False
        assert not adapter.capabilities().consistent_snapshot
        assert 'PRIVATE_ROLLBACK_VALUE' not in str(error.value)


def test_source_row_budget_aborts_without_draining_more_results():
    driver = Driver()
    driver.main.rows = [('1','A'),('2','B'),('3','C')]
    with source(driver, verified_capabilities=QUALIFIED, limits=QueryLimits(max_result_rows=1)) as adapter:
        with pytest.raises(LightGraphError, match='result_truncated'):
            list(adapter.execute(adapter.compiler.projection(adapter.describe()[0], ['code','name'])))
        assert driver.main.closed and not driver.main.drained_unread


def test_restricted_source_does_not_accept_other_catalog_query():
    from lightgraph.query.guard import ValidatedQuery
    driver = Driver()
    with source(driver, allowed_columns={'analytics.items':['code']}, verified_capabilities=QUALIFIED) as adapter:
        before = len(driver.main.executed)
        with pytest.raises(LightGraphError, match='unauthorized_column'):
            list(adapter.execute(ValidatedQuery('SELECT `name` FROM `analytics`.`items`')))
        assert len(driver.main.executed) == before


def test_mysql_sql_modes_are_set_and_verified_before_user_queries():
    driver = Driver()
    with source(driver) as adapter:
        assert 'NO_BACKSLASH_ESCAPES' not in driver.main.sql_mode
        assert 'ANSI_QUOTES' not in driver.main.sql_mode
        assert 'ONLY_FULL_GROUP_BY' in driver.main.sql_mode
        assert driver.main.executed[0][0].startswith('SET SESSION sql_mode')
    driver = Driver()
    driver.main.ignore_sql_mode = True
    with pytest.raises(LightGraphError, match='read_only_transaction_unverified'):
        source(driver)


@pytest.mark.parametrize('version', ['1.1.1', '1.2.2', '2.0.0', '1.2.3rc1', 'unknown'])
def test_old_or_unqualified_driver_rejected_before_connect(monkeypatch, version):
    import pymysql
    driver = Driver()
    monkeypatch.setattr(pymysql, 'connect', driver)
    monkeypatch.setattr('importlib.metadata.version', lambda _name: version)
    with pytest.raises(LightGraphError, match='mysql_driver_version_unsupported'):
        MySQLSource(DSN, allowed_tables=['analytics.items'])
    assert not driver.options


@pytest.mark.parametrize('attribute,value', [('server_uuid', 'other-server'), ('current_user', 'other-reader@%'), ('server_uuid', '')])
def test_control_connection_must_match_server_and_account(attribute, value):
    driver = Driver()
    setattr(driver.control, attribute, value)
    with pytest.raises(LightGraphError, match='query_cancel_connection_mismatch'):
        source(driver)
    assert driver.main.closed and driver.control.closed
    assert not any(s.startswith('KILL') for s, _ in driver.control.executed)


def test_late_timer_cannot_leave_connection_reusable(monkeypatch):
    # Model a timer still blocked inside control I/O after join's bound. It may
    # complete later, so the original connection must never serve another query.
    class LateTimer:
        ident = 123
        daemon = True
        def __init__(self, _delay, callback):
            self.callback = callback
        def start(self):
            pass
        def cancel(self):
            pass
        def join(self, timeout):
            pass
        def is_alive(self):
            return True
    driver = Driver()
    with source(driver, verified_capabilities=QUALIFIED) as adapter:
        monkeypatch.setattr('lightgraph.sources.mysql.threading.Timer', LateTimer)
        assert list(adapter.project(adapter.describe()[0], ['code']))
        assert driver.main.closed
        with pytest.raises(LightGraphError, match='source_closed'):
            list(adapter.project(adapter.describe()[0], ['code']))


def test_external_cancel_revokes_connection_before_control_io():
    driver = Driver()
    with source(driver, verified_capabilities=QUALIFIED) as adapter:
        original = adapter._interrupt
        def delayed_cancel(**kwargs):
            # The KILL has not yet been sent. A competing next query must fail.
            with pytest.raises(LightGraphError, match='source_closed'):
                with adapter.snapshot():
                    pass
            original(**kwargs)
        adapter._interrupt = delayed_cancel
        adapter.cancel()
        assert driver.control.executed[-1][0] == 'KILL QUERY 42'


def test_cancel_after_last_batch_does_not_report_query_success():
    driver = Driver()
    with source(driver, verified_capabilities=QUALIFIED) as adapter:
        iterator = adapter.project(adapter.describe()[0], ['code'])
        next(iterator)
        adapter.cancel()
        with pytest.raises(LightGraphError, match='query_cancelled'):
            next(iterator)
        assert driver.main.closed
