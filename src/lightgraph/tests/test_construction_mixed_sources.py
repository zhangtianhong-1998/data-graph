"""Mixed file/DB construction using real adapters and local DB-API substitutes.

The database transport runs guarded SELECTs against an in-memory SQLite fixture;
these tests do not certify a live MySQL/Gauss server or model discovery quality.
"""
import sqlite3
import re

import pytest
import sqlglot
from openpyxl import Workbook

from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.runtime.config import RunConfig
from lightgraph.sources.gauss import GaussSource
from lightgraph.sources.mysql import MySQLSource
from lightgraph.tests import test_sources_gauss as gauss_fixture
from lightgraph.tests import test_sources_mysql as mysql_fixture


def database_transport(kind):
    """Keep the production adapters, replacing only their DB-API connections."""
    fixture = mysql_fixture if kind == 'mysql' else gauss_fixture
    schema = 'analytics' if kind == 'mysql' else 'public'
    dialect = 'mysql' if kind == 'mysql' else 'postgres'
    data = sqlite3.connect(':memory:', check_same_thread=False)
    data.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
    data.execute(f'CREATE TABLE {schema}.items (tenant TEXT, code TEXT, name TEXT, parent_code TEXT)')
    data.executemany(f'INSERT INTO {schema}.items VALUES (?, ?, ?, ?)',
                     [('A', 'D1', '部门甲', None), ('A', 'D2', '部门乙', None)])
    data.commit()
    data.execute('PRAGMA query_only=ON')
    quoted_table = '`analytics`.`items`' if kind == 'mysql' else '"public"."items"'

    class Cursor(fixture.Cursor):
        def execute(self, sql, parameters=None):
            if self.connection.closed:
                raise RuntimeError('fixture connection is closed')
            if quoted_table in sql:
                # Execute the adapter's generated query, not a canned row set;
                # this exercises count reconciliation, DISTINCT and projections.
                self.connection.executed.append((sql, parameters))
                statement = re.sub(r'%\((\w+)\)s', r':\1', sql)
                cursor = data.execute(sqlglot.transpile(statement, read=dialect, write='sqlite')[0], parameters or {})
                self.description = cursor.description
                self.result = cursor.fetchall()
                cursor.close()
                return
            return super().execute(sql, parameters)

    class Connection(fixture.Connection):
        def cursor(self, name=None):
            if self.closed:
                raise RuntimeError('fixture connection is closed')
            cursor = Cursor(self) if kind == 'mysql' else Cursor(self, name)
            if kind == 'mysql':
                self.cursors.append(cursor)
            return cursor

    main = Connection()
    physical_connections = []
    main_commands = main.executed
    current_main = main
    def factory(*args, **kwargs):
        nonlocal current_main
        is_main = kind != 'mysql' or len(physical_connections) % 2 == 0
        if is_main:
            current_main = main if not physical_connections else Connection()
            current_main.executed = main_commands
            connection = current_main
        else:
            connection = Connection(current_main)
        physical_connections.append(connection)
        return connection
    # Existing SQL assertions inspect this shared backend history; physical
    # connection state remains distinct and every closed connection rejects I/O.
    main.physical_connections = physical_connections
    qualification = (mysql_fixture.QUALIFIED if kind == 'mysql' else {
        'product': 'openGauss', 'version': '6.0.0', 'driver': 'psycopg_dbapi',
        'capabilities': {'statement_timeout': 'verified', 'query_cancel': 'verified'}})
    return schema, data, main, factory, qualification


def mixed_config(tmp_path, monkeypatch, db_kind, file_kind, *, qualified=True):
    schema, data, connection, factory, qualification = database_transport(db_kind)
    source_class = MySQLSource if db_kind == 'mysql' else GaussSource
    opened = []

    def source(dsn, **kwargs):
        result = source_class(dsn, connect_factory=factory, **kwargs)
        opened.append(result)
        return result

    monkeypatch.setattr('lightgraph.sources.' + ('MySQLSource' if db_kind == 'mysql' else 'GaussSource'), source)
    monkeypatch.setenv('MIXED_SOURCE_TEST_DSN', mysql_fixture.DSN if db_kind == 'mysql' else 'test-dsn')
    columns = ['code', 'name', 'parent_code', 'note']
    rows = [['I1', '项目甲', 'D1', '补充甲'], ['I2', '项目乙', 'D2', '补充乙']]
    if file_kind == 'excel':
        path = tmp_path / 'items.xlsx'
        book = Workbook()
        sheet = book.active
        sheet.title = 'items'
        sheet.append(columns)
        for row in rows:
            sheet.append(row)
        book.save(path)
        book.close()
        local = {'id': 'file', 'kind': 'excel_workbook', 'path': str(path)}
    else:
        path = tmp_path / 'items.csv'
        path.write_text('\n'.join(','.join(row) for row in [columns, *rows]) + '\n', encoding='utf-8')
        local = {'id': 'file', 'kind': 'csv_file', 'path': str(path), 'schema_name': schema,
                 'encoding': 'utf-8', 'delimiter': ',', 'header': True,
                 'null_values': [''], 'type_inference': 'text'}
    database = {'id': 'database', 'kind': db_kind + '_database',
                'dsn_env': 'MIXED_SOURCE_TEST_DSN', 'allowed_tables': [schema + '.items']}
    if qualified:
        manifest = tmp_path / 'capability.yaml'
        manifest.write_text(dump_yaml({'database': qualification}), encoding='utf-8')
        database['capability_manifest'] = str(manifest)
    config = RunConfig.model_validate({'sources': [local, database],
        'deployment_retention_verified': True, 'model': {'mode': 'in_environment'},
        'goal': {'purpose': '合并本地项目与数据库部门资料，补充属性和所属关系'}})
    return config, data, connection, opened


def mixed_draft(catalog):
    tables = {t['source']: {'source_id': t['source'], 'schema': t['schema'], 'table': t['name']}
              for t in catalog['tables']}
    file, database = tables['file'], tables['database']
    return {'draft_id': '联合构建', 'entities': [
        {'type': '项目', 'key_names': ['编码'], 'mappings': [
            {**file, 'key_columns': ['code'], 'label_column': 'name'}]},
        {'type': '部门', 'key_names': ['编码'], 'mappings': [
            {**database, 'key_columns': ['code'], 'label_column': 'name'},
            {**file, 'key_columns': ['parent_code'], 'properties': {'补充': 'note'}}]}],
        'relations': [{'type': '归属', 'source_type': '项目', 'target_type': '部门',
            'mappings': [{**file, 'source_key_columns': ['code'], 'target_key_columns': ['parent_code']}]}]}


@pytest.mark.parametrize('db_kind', ['mysql', 'gauss'])
@pytest.mark.parametrize('file_kind', ['csv', 'excel'])
def test_mixed_adapters_inspect_join_validate_and_publish(tmp_path, monkeypatch, db_kind, file_kind):
    config, data, connection, opened = mixed_config(tmp_path, monkeypatch, db_kind, file_kind)
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        catalog = tools.call('describe_sources')
        tables = {t['source']: t['table'] for t in catalog['tables']}
        assert set(tables) == {'file', 'database'}
        assert {t['source']: t['sql_dialect'] for t in catalog['tables']} == {
            'file': 'sqlite', 'database': 'mysql' if db_kind == 'mysql' else 'postgres'}
        specs = tools.call('read_material', name='source_operations')['operations']
        sql_spec = next(s['function'] for s in specs if s['function']['name'] == 'execute_readonly_sql')
        assert 'sql_dialect returned by get_metadata' in sql_spec['description']
        assert 'in the sqlite dialect' not in sql_spec['description']
        assert tools.call('inspect_source', table='items', operation='get_metadata')['code'] == 'ambiguous_table'
        metadata = {}
        for source, table in tables.items():
            result = tools.call('inspect_source', table=table, operation='get_metadata')
            assert result['status'] == 'ok', result
            metadata[source] = result['data']['tables'][0]
            assert metadata[source]['source_id'] == source
            sql = f"SELECT code, name FROM {metadata[source]['sql_table']} ORDER BY code"
            sample = tools.call('inspect_source', table=table, operation='execute_readonly_sql', arguments={'sql': sql})
            assert sample['status'] == 'ok', sample
            assert [r[0] for r in sample['data']['rows']] == (['I1', 'I2'] if source == 'file' else ['D1', 'D2'])
        assert metadata['database']['sql_dialect'] == ('mysql' if db_kind == 'mysql' else 'postgres')
        refs = tools.call('inspect_source', table=tables['file'], operation='check_reference', arguments={
            'source_table': tables['file'], 'source_columns': ['parent_code'],
            'target_table': tables['database'], 'target_columns': ['code']})
        assert refs['status'] == 'ok' and refs['coverage']['confirmed'], refs
        assert refs['aggregates']['unique_matches'] == 2
        assert refs['coverage']['consistency_scope'] == 'independent_source_snapshots'
        assert not opened[0].capabilities().consistent_snapshot
        draft = mixed_draft(catalog)
        draft['evidence_refs'] = [refs['evidence_ref']]
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
        check = tools.call('validate_graph_draft', draft_id=draft['draft_id'])
        assert check['passed'], check
        published = tools.call('publish_graph_draft', draft_id=draft['draft_id'])
        assert published['status'] == 'ok' and published['nodes'] == 4 and published['edges'] == 2, published
        graph = tools.workspace.read_published_graph()
        nodes = {n['id']: n for n in graph['nodes']}
        assert {(nodes[e['source']]['label'], nodes[e['target']]['label']) for e in graph['edges']} == {
            ('项目甲', '部门甲'), ('项目乙', '部门乙')}
        department = next(n for n in nodes.values() if n['label'] == '部门甲')
        assert department['properties']['补充'] == ['补充甲']
        assert {p['source']['source_id'] for p in department['provenance']} == {'file', 'database'}
        assert not opened[0].capabilities().consistent_snapshot
        # Neither raw writes nor a forged source qualifier reach the database.
        before = len(connection.executed)
        for sql in (f"DELETE FROM {metadata['database']['sql_table']}",
                    f"UPDATE {metadata['database']['sql_table']} SET name='wrong'",
                    f"INSERT INTO {metadata['database']['sql_table']} (code) VALUES ('wrong')"):
            denied = tools.call('inspect_source', table=tables['database'], operation='execute_readonly_sql', arguments={'sql': sql})
            assert denied['code'] == 'unsafe_sql'
        assert len(connection.executed) == before
        assert tools.call('inspect_source', table='unknown:public.items', operation='get_metadata')['code'] == 'unauthorized_object'
    finally:
        tools.close()
        data.close()
    assert opened[0]._closed
    assert all(connection.closed for connection in connection.physical_connections)
    assert len({id(item) for item in connection.physical_connections}) == len(connection.physical_connections)


def test_snapshot_entry_without_consistency_still_blocks_projection(tmp_path):
    from lightgraph.contracts import ColumnSpec, TableSpec
    from lightgraph.construction.executor import execute_draft
    from lightgraph.construction.models import GraphDraft
    from lightgraph.sources import MemorySource

    class UnverifiedSnapshot(MemorySource):
        def capabilities(self):
            return super().capabilities().model_copy(update={'consistent_snapshot': False})

    table = TableSpec(source_id='db', name='items', columns=[ColumnSpec(name='code')])
    draft = GraphDraft.model_validate({'draft_id': 'blocked', 'entities': [
        {'type': '对象', 'key_names': ['code'], 'mappings': [
            {'source_id': 'db', 'table': 'items', 'key_columns': ['code']}]}]})
    with UnverifiedSnapshot([table], {'public.items': [('001',)]}) as adapter:
        result = execute_draft(draft, [adapter], [table])
        assert not result['passed'] and result['graph']['nodes'] == []
        failure = next(d for d in result['diagnostics'] if d['code'] == 'snapshot_or_projection_unavailable')
        assert failure['source_id'] == 'db' and failure['phase'] == 'snapshot_open'
        assert any(d['code'] == 'mapping_source_unavailable' for d in result['diagnostics'])
        assert adapter.stats.queries == 0 and adapter._depth == 0


@pytest.mark.parametrize('db_kind', ['mysql', 'gauss'])
def test_unverified_database_still_cannot_publish_mixed_graph(tmp_path, monkeypatch, db_kind):
    config, data, connection, opened = mixed_config(tmp_path, monkeypatch, db_kind, 'csv', qualified=False)
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        draft = mixed_draft(tools.call('describe_sources'))
        tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
        result = tools.call('validate_graph_draft', draft_id=draft['draft_id'])
        assert not result['passed']
        assert 'runtime_capability_unverified' in {d['code'] for d in result['diagnostics']}
        assert tools.call('publish_graph_draft', draft_id=draft['draft_id'])['status'] == 'error'
        assert not tools.workspace.read_summary()['publications']
    finally:
        tools.close()
        data.close()
