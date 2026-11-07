"""Driver scalar types survive probes, instance identities and durable graphs."""
from copy import copy, deepcopy
from datetime import date, datetime, time
from decimal import Decimal, localcontext
import json

import pytest

from lightgraph.contracts import ColumnSpec, TableSpec, QueryLimits, canonical, digest
from lightgraph.sources import MemorySource
from lightgraph.sources.base import cursor_batches, QueryStats
from lightgraph.source_values import (source_scalar, scalar_kind, typed_key, identity_values,
    persisted_identity_key, SOURCE_KEY_SEMANTICS_VERSION)
from lightgraph.cross_source import check_cross_source_reference
from lightgraph.construction.executor import execute_draft
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml, safe_load_yaml, _merge_graphs
from lightgraph.construction.statistics import graph_statistics
from lightgraph.construction.display import derive_display_graph
from lightgraph.runtime.config import RunConfig
from lightgraph.errors import LightGraphError


class Cursor:
    def __init__(self, rows):
        self.rows = iter(rows)

    def fetchone(self):
        return next(self.rows, None)


class DriverSource(MemorySource):
    """SQL execution fixture whose projected scalars use the actual DB decoder.

    SQLite handles guarded queries, DISTINCT and aggregates. The converter is
    the fake driver's return type, independent of its declared catalog type.
    """
    def __init__(self, table, rows, converters=None):
        self.converters = converters or {}
        super().__init__([table], {table.qualified_name: rows})

    def execute(self, query, *, row_limit=None):
        for batch in super().execute(query, row_limit=row_limit):
            with batch:
                rows = [tuple(self.converters.get(name, lambda v: v)(value) if value is not None else None
                    for name, value in zip(batch.columns, row)) for row in batch]
                yield from cursor_batches(Cursor(rows), limits=self.limits, columns=batch.columns,
                    row_limit=row_limit or self.limits.max_result_rows, stats=QueryStats())


def table(sid):
    # Intentionally text declarations: the driver value, not a catalog guess,
    # must establish the scalar type (also covers CSV's intentional text mode).
    return TableSpec(source_id=sid, name='items', columns=[ColumnSpec(name='id'), ColumnSpec(name='code')])


def draft(*, lookup=True):
    return {'draft_id': 'typed', 'entities': [
        {'type': 'Order', 'key_names': ['code'], 'mappings': [
            {'source_id': 'a', 'table': 'items', 'key_columns': ['code'], 'context_columns': ['code']}]},
        {'type': 'Target', 'key_names': ['code'], 'mappings': [
            {'source_id': 'b', 'table': 'items', 'key_columns': ['code']}]}],
        'relations': [{'type': 'links', 'source_type': 'Order', 'target_type': 'Target', 'mappings': [
            {'source_id': 'a', 'table': 'items', 'source_key_columns': ['code'], 'target_key_columns': ['code'],
             **({'target_lookup': {'source_id': 'b', 'table': 'items', 'match_columns': ['code'],
                                  'key_columns': ['code']}} if lookup else {})}]}]}


CASES = [
    ('1.00', Decimal, '1.00', str, False),
    ('1.00', Decimal, '1.0', Decimal, True),
    ('-0.00', Decimal, '0', Decimal, True),
    ('1', Decimal, 1, int, False),
    ('1', Decimal, 1.0, float, False),
    ('001', str, '1', str, False),
    ('001', str, '001', str, True),
    (1, int, '1', str, False),
    (1, int, 1, int, True),
    (1, int, 1.0, float, False),
    (1.0, float, 1.0, float, True),
    (1, bool, 1, int, False),
    (1, bool, 1, bool, True),
    ('2026-01-02', date.fromisoformat, '2026-01-02', str, False),
    ('2026-01-02', date.fromisoformat, '2026-01-02', date.fromisoformat, True),
    ('2026-01-02T03:04:05', datetime.fromisoformat, '2026-01-02T03:04:05', str, False),
    ('03:04:05', time.fromisoformat, '03:04:05', str, False),
]


@pytest.mark.parametrize('left,lc,right,rc,matched', CASES)
@pytest.mark.parametrize('lookup', [True, False])
def test_probe_and_actual_or_direct_endpoints_share_type_contract(left, lc, right, rc, matched, lookup):
    a, b = table('a'), table('b')
    with DriverSource(a, [('O', left)], {'code': lc}) as aa, DriverSource(b, [('T', right)], {'code': rc}) as bb:
        probe = check_cross_source_reference(aa, a, ['code'], bb, b, ['code'])
        assert probe['complete'], probe
        assert probe['confirmed'] is matched
        result = execute_draft(draft(lookup=lookup), [aa, bb], [a, b])
        assert result['passed'] is matched, result['diagnostics']
        assert len(result['graph']['edges']) == int(matched)
        assert result['graph']['source_key_semantics'] == SOURCE_KEY_SEMANTICS_VERSION
        assert all('identity_types' in node for node in result['graph']['nodes'])


def test_real_tool_validation_rejects_mixed_decimal_text_and_publishes_same_decimal(tmp_path):
    a, b = table('a'), table('b')
    config = RunConfig(sources=[], model={'mode': 'in_environment'})
    for name, converter in [('mismatch', str), ('same', Decimal)]:
        with DriverSource(a, [('O', '1.00')], {'code': Decimal}) as aa, DriverSource(b, [('T', '1.0' if name == 'same' else '1.00')], {'code': converter}) as bb:
            tools = ConstructionTools(config, tmp_path / name, adapters=[aa, bb])
            try:
                assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft()))['status'] == 'ok'
                checked = tools.call('validate_graph_draft', draft_id='typed')
                assert checked['passed'] is (name == 'same'), checked
                published = tools.call('publish_graph_draft', draft_id='typed')
                if name == 'mismatch':
                    assert published['code'] == 'graph_validation_required'
                    assert any(d['code'] == 'lookup_reference_unmatched' for d in checked['diagnostics'])
                else:
                    assert published['status'] == 'ok' and published['edges'] == 1
                    graph = tools.workspace.read_published_graph()
                    support = graph['edges'][0]['supports'][0]['lookup_matches'][0]
                    assert support['input_value_types'] == support['resolved_key_types'] == ['decimal']
                    assert graph == safe_load_yaml(dump_yaml(graph))
                    assert graph == json.loads(json.dumps(graph))
            finally:
                tools.close()


@pytest.mark.parametrize('value', [Decimal('1.00000000000000000000000000000000001'), date(2026, 1, 2),
    datetime(2026, 1, 2, 3, 4, 5), time(3, 4, 5)])
def test_runtime_tags_copy_and_display_without_losing_key_precision(value):
    typed = source_scalar(value)
    assert str(typed) == (str(value) if isinstance(value, Decimal) else value.isoformat())
    assert typed_key([typed]) == typed_key([copy(typed)]) == typed_key([deepcopy(typed)])
    assert scalar_kind(typed) != 'text'
    assert json.loads(json.dumps(typed)) == str(typed)
    with localcontext() as context:
        context.prec = 2
        assert typed_key([typed]) == typed_key([value])
    if isinstance(value, Decimal):
        assert typed_key([typed]) != typed_key([Decimal('1')])


def test_decimal_scale_identity_merges_across_drafts_and_survives_yaml():
    t = table('a')
    graphs = []
    for value in ['1.0', '1.00']:
        raw = {'draft_id': 'scale', 'entities': [{'type': 'Number', 'key_names': ['code'], 'mappings': [
            {'source_id': 'a', 'table': 'items', 'key_columns': ['code'], 'context_columns': ['code']}]}]}
        with DriverSource(t, [('n', value)], {'code': Decimal}) as source:
            graph = execute_draft(raw, [source], [t])['graph']
        graphs.append(safe_load_yaml(dump_yaml(graph)))
    assert graphs[0]['nodes'][0]['id'] == graphs[1]['nodes'][0]['id']
    assert persisted_identity_key(graphs[0]['nodes'][0]) == persisted_identity_key(graphs[1]['nodes'][0])
    assert len(_merge_graphs(graphs)['nodes']) == 1
    old = deepcopy(graphs[0]); old.pop('source_key_semantics')
    with pytest.raises(LightGraphError, match='published_graph_key_semantics_conflict'):
        _merge_graphs([old, graphs[1]])


def test_mixed_type_identity_stats_context_and_display_keep_distinction():
    t = table('a')
    graphs = []
    for converter in [str, Decimal]:
        raw = {'draft_id': 'mixed', 'entities': [{'type': 'Item', 'key_names': ['code'], 'mappings': [
            {'source_id': 'a', 'table': 'items', 'key_columns': ['code'], 'context_columns': ['code']}]}]}
        with DriverSource(t, [('n', '1')], {'code': converter}) as source:
            graphs.append(execute_draft(raw, [source], [t])['graph'])
    graph = _merge_graphs(graphs)
    assert len(graph['nodes']) == 2
    assert graph_statistics(graph)['integrity']['duplicate_node_identities'] == 0
    assert len({n['supports'][0]['row_context']['id'] for n in graph['nodes']}) == 2
    text, number = graphs[0]['nodes'][0], graphs[1]['nodes'][0]
    graph['edges'] = [{'id': 'edge', 'type': 'ref', 'source': number['id'], 'target': text['id']}]
    displayed = derive_display_graph(graph)
    numeric = next(n for n in displayed['nodes'] if n['id'] == number['id'])
    assert numeric['label'].endswith('；code：1）')  # Numeric key was not covered by the text endpoint.


def test_primitive_ids_remain_compatible_and_typed_replay_cannot_silently_erase_type():
    for value in ['001', '1', 1, 1.0, True]:
        assert digest('Item', identity_values([value])) == digest('Item', [value])
    t = table('s')
    with pytest.raises(LightGraphError, match='source_value_type_unsupported'):
        MemorySource([t], {t.qualified_name: [('n', source_scalar(Decimal('1')))]})


@pytest.mark.parametrize('target_value,expected', [('1.00', False), (Decimal('1.0'), True)])
def test_gauss_dbapi_cursor_through_probe_and_full_construction_tools(tmp_path, target_value, expected):
    """Real GaussSource adapter, fake DB-API connection; no customer DB claims."""
    from lightgraph.sources import GaussSource
    from lightgraph.tests.test_sources_gauss import Cursor as GaussCursor, Connection, Driver

    qualification = {'product': 'openGauss', 'version': '6.0.0', 'driver': 'psycopg_dbapi',
        'capabilities': {'statement_timeout': 'verified', 'query_cancel': 'verified'}}

    def driver_for(value):
        class TypedCursor(GaussCursor):
            def execute(self, sql, parameters=None):
                super().execute(sql, parameters)
                if not self.name:
                    return
                if 'distinct_tuples' in sql:
                    self.description = [(name,) for name in ('distinct_tuples', 'rows',
                        'duplicate_groups', 'max_duplicate', 'null_key_rows')]
                    self.result = [(1, 1, 0, 1, 0)]
                else:
                    self.description = [('code',)]
                    self.result = [(value,)]

        class TypedConnection(Connection):
            def cursor(self, name=None):
                return TypedCursor(self, name)

        return Driver(TypedConnection)

    left_driver, right_driver = driver_for(Decimal('1.00')), driver_for(target_value)
    with GaussSource('synthetic', source_id='a', allowed_tables=['public.items'],
            connect_factory=left_driver, verified_capabilities=qualification) as left, GaussSource(
            'synthetic', source_id='b', allowed_tables=['public.items'], connect_factory=right_driver,
            verified_capabilities=qualification) as right:
        probe = check_cross_source_reference(left, left.describe()[0], ['code'],
            right, right.describe()[0], ['code'])
        assert probe['complete'] and probe['confirmed'] is expected, probe
        tools = ConstructionTools(RunConfig(sources=[], model={'mode': 'in_environment'}, max_db_queries=500),
            tmp_path / 'graph', adapters=[left, right])
        try:
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft()))['status'] == 'ok'
            result = tools.call('validate_graph_draft', draft_id='typed')
            assert result['passed'] is expected, result
            publication = tools.call('publish_graph_draft', draft_id='typed')
            if expected:
                assert publication['status'] == 'ok' and publication['edges'] == 1
                node = next(n for n in tools.workspace.read_published_graph()['nodes'] if n['type'] == 'Order')
                assert node['identity_types'] == {'code': 'decimal'}
            else:
                assert publication['code'] == 'graph_validation_required'
                assert any(d['code'] == 'lookup_reference_unmatched' for d in result['diagnostics'])
        finally:
            tools.close()
    assert all(c.closed for c in left_driver.connections + right_driver.connections)
