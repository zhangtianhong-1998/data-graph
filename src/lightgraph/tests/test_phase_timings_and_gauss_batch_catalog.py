"""Tests for phase timing breakdown, batched multi-table Gauss catalog, layered timeouts, and co-projection fallback."""
from contextlib import contextmanager

import pytest
from openpyxl import Workbook

from lightgraph.contracts import ColumnSpec, QueryLimits, TableSpec
from lightgraph.construction import executor
from lightgraph.construction.executor import execute_draft
from lightgraph.construction.runner import run_construction
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.sources.csv import CSVSource
from lightgraph.sources.gauss import GaussSource
from lightgraph.tests import test_sources_gauss as gauss_fx
from lightgraph.tests.test_construction_runner import configuration, scripted_bridge, save_object, completed
from lightgraph.tests.test_database_reconnect import Factory
from lightgraph.tests.test_database_snapshot_recovery import GAUSS_QUALIFIED


class MultiTableCatalogCursor(gauss_fx.Cursor):
    def execute(self, sql, parameters=None):
        self.connection.executed.append((sql, parameters))
        if 'current_setting' in sql:
            self.result = [('on', 'repeatable read')]
        elif 'SELECT pg_catalog.version' in sql:
            self.result = [('openGauss 6.0.0 test fixture',)]
        elif sql == 'SELECT %s':
            self.result = [(71,)]
        elif 'SELECT DISTINCT 1' in sql or sql == 'SELECT 1':
            self.result = [(1,)]
        elif 'WITH p AS' in sql:
            self.result = [(1,), (2,)]
        elif 'c.table_schema, c.table_name, c.column_name' in sql:
            self.result = [
                ('dm', 't1', 'code', 'varchar', 'NO'),
                ('dm', 't1', 'label', 'varchar', 'YES'),
                ('dm', 't2', 'code', 'varchar', 'NO'),
                ('dm', 't2', 'amount', 'numeric', 'YES'),
                ('dm', 't3', 'ref_code', 'varchar', 'NO'),
            ]
        elif 'n.nspname, c.relname, a.attname' in sql:
            self.result = [
                ('dm', 't1', 'code', '编码1', 1, True),
                ('dm', 't1', 'label', '名称1', 2, False),
                ('dm', 't2', 'code', '编码2', 1, True),
                ('dm', 't2', 'amount', '金额', 2, False),
                ('dm', 't3', 'ref_code', '引用编码', 1, True),
            ]
        elif 'n.nspname, c.relname, pg_catalog.obj_description' in sql:
            self.result = [
                ('dm', 't1', '表1注释'),
                ('dm', 't2', '表2注释'),
                ('dm', 't3', '表3注释'),
            ]
        elif 'k.conname, k.contype' in sql:
            self.result = [
                ('dm', 't1', 't1_pk', 'p', [1], None, None, None),
                ('dm', 't2', 't2_pk', 'p', [1], None, None, None),
                ('dm', 't3', 't3_fk', 'f', [1], [1], 'dm', 't1'),
            ]
        else:
            self.result = []


class MultiTableConnection(gauss_fx.Connection):
    def cursor(self, name=None):
        return MultiTableCatalogCursor(self, name)


def test_batched_multi_table_gauss_catalog_reduces_queries_and_preserves_exact_specs():
    conn = MultiTableConnection()
    source = GaussSource(
        'fixture-dsn',
        allowed_tables=['dm.t1', 'dm.t2', 'dm.t3'],
        connect_factory=lambda _: conn,
        verified_capabilities=GAUSS_QUALIFIED,
    )
    try:
        tables = {t.qualified_name: t for t in source.describe()}
        assert set(tables) == {'dm.t1', 'dm.t2', 'dm.t3'}
        assert tables['dm.t1'].comment == '表1注释'
        assert tables['dm.t2'].columns[1].data_type == 'numeric'
        assert tables['dm.t3'].foreign_keys[0].target_table == 't1'
        # Batched: exactly 4 catalog queries total for 3 tables instead of 12!
        assert source.stats.catalog_queries == 4
        assert source.stats.preflight_catalog_seconds >= 0.0
        assert source.stats.connect_tcp_seconds >= 0.0
    finally:
        source.close()


@pytest.mark.parametrize('changed', ['column_name', 'column_type', 'foreign_target'])
def test_batched_catalog_change_detection_rejects_same_size_changes(changed):
    class RenamedCursor(MultiTableCatalogCursor):
        def execute(self, sql, parameters=None):
            super().execute(sql, parameters)
            if 'c.table_schema, c.table_name, c.column_name' in sql and changed != 'foreign_target':
                self.result[1] = ('dm', 't1', 'label_v2' if changed == 'column_name' else 'label',
                                  'text' if changed == 'column_type' else 'varchar', 'YES')
            elif 'k.conname, k.contype' in sql and changed == 'foreign_target':
                self.result[-1] = ('dm', 't3', 't3_fk', 'f', [1], [1], 'dm', 't2')

    class RenamedConn(MultiTableConnection):
        def cursor(self, name=None):
            return RenamedCursor(self, name)

    c1 = MultiTableConnection()
    c2 = RenamedConn()
    factory = Factory([c1, c2])
    source = GaussSource(
        'fixture-dsn',
        allowed_tables=['dm.t1', 'dm.t2', 'dm.t3'],
        connect_factory=factory,
        verified_capabilities=GAUSS_QUALIFIED,
        _keep_connection=True,
    )
    c1.closed = True
    try:
        with pytest.raises(LightGraphError, match='source_catalog_changed'):
            with source.snapshot():
                pass
    finally:
        source.close()


def test_co_projection_merges_same_table_narrow_mappings_and_falls_back_on_high_cardinality():
    table = TableSpec(source_id='s', name='wide', columns=[ColumnSpec(name=c) for c in ['a', 'b', 'c', 'd']])
    # Case 1: small cardinality -> 1 union projection + 1 hit instead of 2 separate queries
    rows_small = [('A1', 'NameA1', 'B1', 'NameB1'), ('A2', 'NameA2', 'B2', 'NameB2')]
    draft = {
        'draft_id': 'co',
        'entities': [
            {'type': 'EntA', 'key_names': ['code'], 'mappings': [
                {'source_id': 's', 'table': 'wide', 'key_columns': ['a'], 'label_column': 'b'}]},
            {'type': 'EntB', 'key_names': ['code'], 'mappings': [
                {'source_id': 's', 'table': 'wide', 'key_columns': ['c'], 'label_column': 'd'}]},
        ],
    }
    with MemorySource([table], {table.qualified_name: rows_small}) as src:
        res_co = execute_draft(draft, [src], [table], enable_co_projection=True)
        q_co = src.stats.queries
    with MemorySource([table], {table.qualified_name: rows_small}) as src_sep:
        res_sep = execute_draft(draft, [src_sep], [table], enable_co_projection=False)
        q_sep = src_sep.stats.queries
    assert res_co['passed'] and res_sep['passed']
    assert q_co < q_sep  # 2 queries (1 count + 1 project) vs 4 queries!
    assert res_co['metrics']['co_projection_groups'] == 1
    assert res_co['metrics']['co_projection_hits'] == 1
    assert {n['id'] for n in res_co['graph']['nodes']} == {n['id'] for n in res_sep['graph']['nodes']}

    # Case 2: orthogonal values (4 * 4 = 16 combinations > cap=5), while each narrow mapping has only 4 distinct values <= 5!
    rows_ortho = [(f'A{i}', f'NA{i}', f'B{j}', f'NB{j}') for i in range(4) for j in range(4)]
    with MemorySource([table], {table.qualified_name: rows_ortho}) as src_fb:
        res_fb = execute_draft(draft, [src_fb], [table], max_rows=5, enable_co_projection=True)
    assert res_fb['passed'], res_fb['diagnostics']
    assert res_fb['metrics']['co_projection_fallbacks'] == 1
    assert len(res_fb['graph']['nodes']) == 8


def test_layered_timeouts_and_phase_timings_in_run_report(tmp_path):
    table = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name='code'), ColumnSpec(name='name')])
    limits = QueryLimits(timeout_ms=60000, probe_timeout_ms=15000, projection_timeout_ms=120000)
    assert limits.effective_timeout_ms('probe') == 15000
    assert limits.effective_timeout_ms('projection') == 120000
    assert limits.effective_timeout_ms('default') == 60000

    config = RunConfig(
        sources=[],
        limits=limits,
        model={'mode': 'in_environment', 'model': 'offline', 'base_url': 'https://model.invalid/v1'},
        output_dir=str(tmp_path / 'out'),
    )
    with MemorySource([table], {table.qualified_name: [('C1', '收入')]}, limits=limits) as src:
        def runtime(goal, **kwargs):
            return {'finish_reason': 'completed', 'budget': {'calls': 1}}

        report = run_construction(config, adapters=[src], runtime=runtime)
    assert 'phase_timings' in report.construction.execution
    for key in ('connect_tcp_seconds', 'preflight_catalog_seconds', 'preflight_runtime_probe_seconds',
                'sql_count_seconds', 'sql_projection_seconds', 'materialization_seconds', 'workspace_write_seconds'):
        assert key in report.counts
    assert report.counts['query_timeout_count'] == 0
    assert report.counts['query_cancel_unverified_count'] == 0


def test_immutable_excel_validation_reused_without_requery(tmp_path):
    path = tmp_path / 'data.xlsx'
    book = Workbook()
    ws = book.active
    ws.title = 'items'
    ws.append(['code', 'name'])
    ws.append(['001', '指标A'])
    book.save(path)
    book.close()

    config = RunConfig.model_validate({
        'sources': [{'id': 'excel', 'kind': 'excel_workbook', 'path': str(path)}],
        'model': {'mode': 'in_environment'},
    })
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        draft_obj = {
            'draft_id': 'items',
            'entities': [{'type': 'Item', 'key_names': ['code'], 'mappings': [
                {'source_id': 'excel', 'table': 'items', 'key_columns': ['code'], 'label_column': 'name'}]}],
        }
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft_obj))['status'] == 'ok'
        first = tools.call('validate_graph_draft', draft_id='items')
        assert first['passed'] and not first.get('reused_immutable_validation')
        queries_after_first = tools.adapters[0].stats.queries
        second = tools.call('validate_graph_draft', draft_id='items', reuse_validated=True)
        assert second['passed'] and second.get('reused_immutable_validation') is True
        assert tools.adapters[0].stats.queries == queries_after_first
    finally:
        tools.close()


class PhaseClock:
    seconds = 0.0

    def monotonic(self):
        return self.seconds


@pytest.mark.parametrize('failure,expected_count,expected_project', [
    ('count_open', 2, 0), ('count_close', 7, 0),
    ('project_open', 2, 3), ('project_close', 2, 8), ('snapshot_close', 2, 3),
])
def test_failed_projection_accounts_for_open_stream_and_teardown(monkeypatch, failure, expected_count, expected_project):
    clock = PhaseClock()
    monkeypatch.setattr(executor, 'time', clock)
    table = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name='code')])
    draft = {'draft_id': 'timed', 'entities': [{'type': 'Item', 'key_names': ['code'],
        'mappings': [{'source_id': 's', 'table': 'items', 'key_columns': ['code']}]}]}

    class BrokenClose:
        def __init__(self, stream):
            self.stream = stream

        def __iter__(self):
            return iter(self.stream)

        def close(self):
            self.stream.close()
            clock.seconds += 5
            raise LightGraphError('query_execution_failed')

    class TimedSource(MemorySource):
        @contextmanager
        def snapshot(self):
            clock.seconds += 7
            try:
                with super().snapshot():
                    yield self
            finally:
                clock.seconds += 11
                if failure == 'snapshot_close':
                    raise LightGraphError('snapshot_close_failed')

        def execute(self, query, *, row_limit=None):
            stream = super().execute(query, row_limit=row_limit)
            if query.operator == 'profile_column_group':
                clock.seconds += 2
                if failure == 'count_open':
                    raise LightGraphError('query_execution_failed')
                if failure == 'count_close':
                    return BrokenClose(stream)
            return stream

        def project(self, *args, **kwargs):
            clock.seconds += 3
            if failure == 'project_open':
                raise LightGraphError('query_execution_failed')
            stream = super().project(*args, **kwargs)
            return BrokenClose(stream) if failure == 'project_close' else stream

    with TimedSource([table], {table.qualified_name: [('x',)]}) as src:
        result = execute_draft(draft, [src], [table])
    assert not result['passed']
    code = 'snapshot_close_failed' if failure == 'snapshot_close' else 'query_execution_failed'
    assert code in [d['code'] for d in result['diagnostics']]
    phases = result['phase_timings']
    assert phases['sql_count_seconds'] == expected_count
    assert phases['sql_projection_seconds'] == expected_project
    assert phases['projection_operation_seconds'] == 7 + expected_count + expected_project + 11
    assert phases['materialization_seconds'] == 0


def test_run_report_counts_repeated_validations_and_immutable_reuse(tmp_path, monkeypatch, scripted_bridge):
    clock = PhaseClock()
    monkeypatch.setattr(executor, 'time', clock)
    original_execute = CSVSource.execute
    original_clear = executor.GraphAccumulator.clear
    original_display = executor.derive_display_graph

    def timed_execute(self, query, **kwargs):
        # Two sub-rounding-precision count samples must survive accumulation.
        clock.seconds += .00004 if query.operator == 'profile_column_group' else 2
        return original_execute(self, query, **kwargs)

    def timed_clear(self):
        clock.seconds += 3
        return original_clear(self)

    def timed_display(*args, **kwargs):
        clock.seconds += 5
        return original_display(*args, **kwargs)

    monkeypatch.setattr(CSVSource, 'execute', timed_execute)
    monkeypatch.setattr(executor.GraphAccumulator, 'clear', timed_clear)
    monkeypatch.setattr(executor, 'derive_display_graph', timed_display)
    config = configuration(tmp_path)
    results = []

    def validate_twice(intent, **kwargs):
        tools = scripted_bridge['tools']
        draft = save_object(tools)
        for reuse in (False, False, True):
            results.append(tools.call('validate_graph_draft', draft_id=draft['draft_id'], reuse_validated=reuse))
        return completed()

    report = run_construction(config, runtime=validate_twice)
    assert len(results) == 3 and all(result['passed'] for result in results)
    first, second, cached = results
    assert first['validation_id'] != second['validation_id']
    assert cached['reused_immutable_validation'] and cached['validation_id'] == second['validation_id']
    phases = report.construction.execution['phase_timings']
    assert phases['sql_count_seconds'] == report.counts['sql_count_seconds'] == .0001
    assert phases['sql_projection_seconds'] == report.counts['sql_projection_seconds'] == 4
    assert phases['materialization_seconds'] == report.counts['materialization_seconds'] == 6
    assert phases['display_seconds'] == 10

    def reuse_from_prior_run(intent, **kwargs):
        results.append(scripted_bridge['tools'].call('validate_graph_draft', draft_id='资料对象', reuse_validated=True))
        return completed()

    resumed = run_construction(config, runtime=reuse_from_prior_run)
    assert len(results) == 4 and results[-1]['reused_immutable_validation']
    assert results[-1]['validation_id'] == second['validation_id']
    assert resumed.counts['db_queries'] == 0
    phases = resumed.construction.execution['phase_timings']
    assert all(phases[key] == 0 for key in ('sql_count_seconds', 'sql_projection_seconds',
               'projection_operation_seconds', 'materialization_seconds', 'display_seconds'))
