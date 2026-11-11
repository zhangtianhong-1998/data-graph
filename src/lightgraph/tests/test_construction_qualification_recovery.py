"""Real Gauss adapter, guarded SQLite transport: recover before relation reads.

Only DB-API and server data are synthetic. No real Gauss/model acceptance is
claimed by these checks of qualification, pairing, evidence and publication.
"""
import re
import sqlite3

import pytest
import sqlglot

from lightgraph.construction.models import COLUMN_IDENTITY_NAMES
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.runtime.config import RunConfig
from lightgraph.sources.gauss import GaussSource
from lightgraph.sources import reconnect
from lightgraph.tests.test_sources_gauss import Connection, Cursor
from lightgraph.tests.test_database_snapshot_recovery import GAUSS_QUALIFIED


def star_source(failures, *, readonly_failure=False):
    columns = [('indicator', 'text'), ('indicator_name', 'text'), ('region', 'text'),
               ('region_name', 'text'), ('ytd', 'numeric'), ('mtd', 'numeric')]
    data = sqlite3.connect(':memory:')
    data.execute("ATTACH DATABASE ':memory:' AS public")
    data.execute('CREATE TABLE public.items (indicator TEXT, indicator_name TEXT, region TEXT, region_name TEXT, ytd NUMERIC, mtd NUMERIC)')
    data.executemany('INSERT INTO public.items VALUES (?, ?, ?, ?, ?, ?)', [
        ('I1', '收入', 'N', '北区', 123456789, None),
        ('I1', '收入', 'S', '南区', 987654321, 7654321)])
    data.commit()
    data.execute('PRAGMA query_only=ON')
    connections = []

    class StarCursor(Cursor):
        def execute(self, sql, parameters=None):
            super().execute(sql, parameters)
            if sql == 'SELECT %s' and self.connection.bad_probe:
                self.result = [(70,)]  # No business projection has started.
            elif 'current_setting' in sql and readonly_failure and self.connection.bad_probe:
                self.result = [('off', 'repeatable read')]
            elif 'information_schema.columns' in sql:
                self.result = [(name, kind, 'YES') for name, kind in columns]
            elif 'pg_catalog.col_description' in sql:
                self.result = [(name, {'ytd': '年累计', 'mtd': '当月完成'}.get(name, name), n + 1, False)
                               for n, (name, _) in enumerate(columns)]
            elif 'SELECT k.conname' in sql:
                self.result = []
            elif 'FROM "public"."items"' in sql:
                statement = re.sub(r'%\((\w+)\)s', r':\1', sql)
                cursor = data.execute(sqlglot.transpile(statement, read='postgres', write='sqlite')[0], parameters or {})
                self.description, self.result = cursor.description, cursor.fetchall()
                cursor.close()

    class StarConnection(Connection):
        def cursor(self, name=None):
            return StarCursor(self, name)

    def factory(_dsn):
        connection = StarConnection()
        connection.bad_probe = 1 <= len(connections) <= failures
        connections.append(connection)
        return connection

    source = GaussSource('test-only', source_id='facts', allowed_tables=['public.items'],
                         connect_factory=factory, verified_capabilities=GAUSS_QUALIFIED)
    return source, connections, data


def star_draft(ref):
    origin = {'source_id': 'facts', 'schema': 'public', 'table': 'items'}
    return {'draft_id': 'star', 'entities': [
        {'type': 'Indicator', 'key_names': ['code'], 'mappings': [
            {**origin, 'key_columns': ['indicator'], 'label_column': 'indicator_name'}]},
        {'type': 'Region', 'key_names': ['code'], 'mappings': [
            {**origin, 'key_columns': ['region'], 'label_column': 'region_name'}]},
        {'type': 'Measure', 'key_names': COLUMN_IDENTITY_NAMES, 'mappings': [
            {**origin, 'kind': 'metadata_column', 'column': column, 'evidence_refs': [ref]}
            for column in ('ytd', 'mtd')]}],
        'relations': [
            {'type': 'measured_in', 'source_type': 'Indicator', 'target_type': 'Region', 'mappings': [
                {**origin, 'source_key_columns': ['indicator'], 'target_key_columns': ['region']}]},
            {'type': 'uses_measure', 'source_type': 'Indicator', 'target_type': 'Measure', 'mappings': [
                {**origin, 'source_key_columns': ['indicator'], 'target_metadata': {
                    **origin, 'column': column, 'evidence_refs': [ref]}, 'metadata_basis': 'schema_applies_to_rows'}
                for column in ('ytd', 'mtd')]}]}


@pytest.mark.parametrize('failures', [1, 4], ids=['recovers', 'exhausts'])
def test_preflight_recovery_preserves_paired_relations_or_keeps_failed_draft(tmp_path, monkeypatch, failures):
    monkeypatch.setattr(reconnect, '_recovery_pause', lambda source: None)
    source, connections, data = star_source(failures)
    tools = ConstructionTools(RunConfig(sources=[], model={'mode': 'in_environment'}), tmp_path / 'work', adapters=[source])
    try:
        ref = tools.call('inspect_source', table='facts:public.items', operation='get_metadata')['evidence_ref']
        draft = star_draft(ref)
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
        check = tools.call('validate_graph_draft', draft_id='star')
        assert source._reconnect_attempts == min(failures, 3)
        assert all(c.closed for c in connections)
        assert all(not any('FROM "public"."items"' in sql for sql, _ in c.executed)
                   for c in connections[1:failures + 1])
        if failures == 1:
            assert check['passed'], check
            assert tools.call('publish_graph_draft', draft_id='star')['status'] == 'ok'
            graph = tools.workspace.read_published_graph()
            by_id = {node['id']: node for node in graph['nodes']}
            assert len(by_id) == 5 and len(graph['edges']) == 4
            assert {(by_id[e['source']]['identity']['code'], by_id[e['target']]['identity']['code'])
                    for e in graph['edges'] if e['type'] == 'measured_in'} == {('I1', 'N'), ('I1', 'S')}
            assert {(by_id[e['source']]['identity']['code'], by_id[e['target']]['identity']['column'])
                    for e in graph['edges'] if e['type'] == 'uses_measure'} == {('I1', 'ytd'), ('I1', 'mtd')}
            assert not any(value in dump_yaml(graph) for value in ('123456789', '987654321', '7654321'))
            assert source.terminal_failure_code is None
        else:
            assert not check['passed']
            assert tools.call('publish_graph_draft', draft_id='star')['code'] == 'graph_validation_required'
            parent = tools.workspace.get_draft('star')
            child = tools.call('save_graph_draft', metadata_from_draft='star')['draft_id']
            assert tools.call('validate_graph_draft', draft_id=child)['passed']
            assert tools.call('publish_graph_draft', draft_id=child)['nodes'] == 2
            assert tools.workspace.read_published_graph()['edges'] == []
            assert tools.workspace.get_draft('star') == parent
    finally:
        tools.close()
        source.close()
        data.close()


def test_unverified_readonly_session_stops_source_with_actionable_probe_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setattr(reconnect, '_recovery_pause', lambda source: pytest.fail('read-only failure retried'))
    source, connections, data = star_source(1, readonly_failure=True)
    tools = ConstructionTools(RunConfig(sources=[], model={'mode': 'in_environment'}), tmp_path / 'work', adapters=[source])
    try:
        ref = tools.call('inspect_source', table='facts:public.items', operation='get_metadata')['evidence_ref']
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(star_draft(ref)))['status'] == 'ok'
        check = tools.call('validate_graph_draft', draft_id='star')
        assert not check['passed']
        assert check['failure_class'] == 'source_execution' and check['retryable'] is False
        assert 'probe_code' in check['next_action'] and 'not proof' in check['next_action']
        assert source._reconnect_attempts == 0 and len(connections) == 2
        diagnostic, = [d for d in check['diagnostics'] if d['code'] == 'read_only_transaction_unverified']
        assert diagnostic['database']['probe_code'] == 'read_only_transaction_unverified'
        assert diagnostic['source_blocked'] is True
        assert all(c.closed for c in connections)
        assert not any('FROM "public"."items"' in sql for c in connections for sql, _ in c.executed)
        assert tools.call('publish_graph_draft', draft_id='star')['code'] == 'metadata_catalog_requalification_required'
    finally:
        tools.close()
        source.close()
        data.close()
