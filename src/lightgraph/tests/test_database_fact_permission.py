"""Per-source data permission with production adapters and local DB-API fixtures.

These verify query routing and controls, not a live customer database connection.
"""
import threading

import pytest
from pydantic import ValidationError

from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig, SourceConfig, open_sources
from lightgraph.sources.gauss import GaussSource
from lightgraph.sources.mysql import MySQLSource
from lightgraph.tests import test_sources_gauss as gauss_fixture
from lightgraph.tests import test_sources_mysql as mysql_fixture


class Cancelled(Exception):
    sqlstate = '57014'


class GaussCursor(gauss_fixture.Cursor):
    def execute(self, sql, parameters=None):
        if sql.startswith('SET LOCAL statement_timeout'):
            self.connection.timeout_probe = "'50ms'" in sql
        if sql == 'SELECT pg_catalog.pg_sleep(0.25)':
            self.connection.executed.append((sql, parameters))
            if self.connection.timeout_probe:
                if self.connection.timeout_works:
                    raise Cancelled()
            elif self.connection.cancelled_event.wait(.5):
                self.connection.cancelled_event.clear()
                raise Cancelled()
            return
        return super().execute(sql, parameters)


class GaussConnection(gauss_fixture.Connection):
    def __init__(self):
        super().__init__()
        self.cancelled_event = threading.Event()
        self.timeout_probe = False
        self.timeout_works = True

    def cursor(self, name=None):
        return GaussCursor(self, name)

    def cancel(self):
        self.cancelled_event.set()


def setup_database(monkeypatch, kind, allow):
    import lightgraph.sources as sources
    if kind == 'gauss':
        db = GaussConnection()
        factory = lambda dsn, **kw: GaussSource(dsn, connect_factory=lambda _: db, **kw)
        monkeypatch.setattr(sources, 'GaussSource', factory)
        dsn, table = 'fixture-only-dsn', 'public.items'
        connections = [db]
    else:
        driver = mysql_fixture.Driver()
        db = driver.main
        factory = lambda dsn, **kw: MySQLSource(dsn, connect_factory=driver, **kw)
        monkeypatch.setattr(sources, 'MySQLSource', factory)
        dsn, table = mysql_fixture.DSN, 'analytics.items'
        connections = [driver.main, driver.control]
    monkeypatch.setenv('FACT_PERMISSION_TEST_DSN', dsn)
    config = RunConfig(sources=[SourceConfig(id='db', kind=kind + '_database',
        dsn_env='FACT_PERMISSION_TEST_DSN', allowed_tables=[table], allow_fact_data=allow)])
    return config, db, connections


@pytest.mark.parametrize('kind', ['gauss', 'mysql'])
def test_checkbox_permission_auto_verifies_current_connection_without_yaml(monkeypatch, kind):
    config, db, connections = setup_database(monkeypatch, kind, True)
    # A legacy file cannot override or prevent live verification in explicit mode.
    config.sources[0].capability_manifest = 'missing-legacy-qualification.yaml'
    assert config.deployment_retention_verified is False
    [adapter] = open_sources(config)
    try:
        assert adapter.metadata_only is False
        manifest = adapter.capabilities()
        assert manifest.actual_database_verified
        assert manifest.capabilities['statement_timeout'] == 'verified'
        assert manifest.capabilities['query_cancel'] == 'verified'
        assert not manifest.consistent_snapshot
        assert sum('SLEEP' in sql.upper() for sql, _ in db.executed) == 2
        rows = []
        for batch in adapter.project(adapter.describe()[0], ['code', 'name']):
            with batch:
                rows.extend(batch)
        assert rows == [('001', 'Expenses')]
        assert config.deployment_retention_verified is False
        # A successful probe grants no mutation permission.
        with pytest.raises(LightGraphError, match='unsafe_sql'):
            adapter.compiler.guard.validate(f'DELETE FROM {adapter.describe()[0].qualified_name}')
    finally:
        adapter.close()
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize('kind', ['gauss', 'mysql'])
@pytest.mark.parametrize('global_metadata', [False, True])
def test_unchecked_permission_or_global_metadata_mode_blocks_all_instance_reads(monkeypatch, kind, global_metadata):
    config, db, _ = setup_database(monkeypatch, kind, True if global_metadata else False)
    # Even an uploaded manifest and old project attestation cannot re-enable data.
    config.sources[0].capability_manifest = 'unused-legacy-manifest.yaml'
    config.deployment_retention_verified = True
    [adapter] = open_sources(config, metadata_only=global_metadata)
    try:
        assert adapter.metadata_only is True
        table = adapter.describe()[0]
        assert 'code' in table.column_names
        assert not any('SLEEP' in sql.upper() for sql, _ in db.executed)
        # Metadata-only CLI connect-check may test constant control queries.
        adapter.verify_runtime_controls()
        assert adapter.capabilities().actual_database_verified
        before = list(db.executed)
        query = adapter.compiler.projection(table, ['code'])
        reads = [lambda: list(adapter.execute(query)), lambda: list(adapter.project(table, ['code'])),
                 lambda: adapter.profile(table, ['code']),
                 lambda: adapter.check_fd(table, ['code'], ['name']),
                 lambda: list(adapter.probe('sample_strata', table, {'columns': ['code']}))]
        for read in reads:
            with pytest.raises(LightGraphError, match='metadata_only_query_forbidden'):
                read()
        with pytest.raises(LightGraphError, match='metadata_only_query_forbidden'):
            with adapter.snapshot():
                pytest.fail('data snapshot opened')
        assert db.executed == before
    finally:
        adapter.close()


@pytest.mark.parametrize('kind', ['gauss', 'mysql'])
def test_failed_automatic_probe_closes_connections_and_never_reads_rows(monkeypatch, kind):
    config, db, connections = setup_database(monkeypatch, kind, True)
    db.timeout_works = False
    with pytest.raises(LightGraphError, match='statement_timeout_unverified'):
        open_sources(config)
    assert all(connection.closed for connection in connections)
    assert not any('FROM "public"."items"' in sql or 'FROM `analytics`.`items`' in sql for sql, _ in db.executed)


@pytest.mark.parametrize('kind', ['gauss', 'mysql'])
def test_probe_must_attest_complete_live_capabilities(monkeypatch, kind):
    config, _, connections = setup_database(monkeypatch, kind, True)
    adapter_type = GaussSource if kind == 'gauss' else MySQLSource
    monkeypatch.setattr(adapter_type, 'verify_runtime_controls', lambda self: self.capabilities())
    with pytest.raises(LightGraphError, match='runtime_capability_unverified'):
        open_sources(config)
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize('kind', ['gauss', 'mysql'])
def test_metadata_database_does_not_disable_facts_from_other_file(monkeypatch, kind, tmp_path):
    config, _, _ = setup_database(monkeypatch, kind, False)
    file = tmp_path / 'items.csv'
    file.write_text('code,name\nA,Local item\n')
    config.sources.append(SourceConfig(id='local', kind='csv_file', path=str(file),
        encoding='utf-8', delimiter=',', header=True, null_values=[''], type_inference='text'))
    adapters = open_sources(config)
    try:
        assert adapters[0].metadata_only and not adapters[1].metadata_only
        rows = []
        for batch in adapters[1].project(adapters[1].describe()[0], ['code', 'name']):
            with batch:
                rows.extend(batch)
        assert rows == [('A', 'Local item')]
    finally:
        for adapter in adapters:
            adapter.close()


@pytest.mark.parametrize('value', ['true', 'false', 'yes', 1, 0, [], {}])
def test_permission_requires_a_boolean_not_truthy_text(value):
    with pytest.raises(ValidationError):
        SourceConfig(id='db', kind='gauss_database', allow_fact_data=value)
