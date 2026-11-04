"""Real CTE/JOIN failures from extraction, reduced to neutral authorized data."""
import sqlite3

import pytest
from lightgraph.contracts import TableSpec, ColumnSpec
from lightgraph.errors import LightGraphError
from lightgraph.query import SQLGuard
from lightgraph.query.guard import ValidatedQuery
from lightgraph.sources import MemorySource


def catalog():
    return TableSpec(source_id='s', name='items', columns=[ColumnSpec(name=c) for c in
        ['tenant', 'code', 'parent', 'name']])


def rows(source, sql):
    return [row for batch in source.execute(ValidatedQuery(sql)) for row in batch.rows()]


def test_materialized_cte_count_read_has_query_scoped_authority():
    table = catalog()
    with MemorySource([table], {'public.items': [('T', 'A', 'R', 'first'), ('T', 'B', 'A', 'second')]}) as source:
        sql = '''WITH p AS (
            SELECT tenant, code, name FROM public.items UNION ALL SELECT tenant, code, name FROM public.items)
            SELECT (SELECT COUNT(*) FROM p) AS cells,
                   (SELECT COUNT(DISTINCT code) FROM p) AS objects,
                   (SELECT MAX(c) FROM (SELECT COUNT(DISTINCT tenant) AS c FROM p GROUP BY code)) AS scopes'''
        assert rows(source, sql) == [(4, 2, 1)]
        assert source._derived_count_sources == frozenset()
        assert source._authorize(sqlite3.SQLITE_READ, 'p', '', None, None) == sqlite3.SQLITE_DENY
        # Fabricating a ValidatedQuery cannot grant CTE authority or table access.
        with pytest.raises(LightGraphError, match='unauthorized_object'):
            list(source.execute(ValidatedQuery('SELECT COUNT(*) FROM p', derived_count_sources=('p',))))
        for sql in ['WITH p AS (SELECT code FROM private.items) SELECT COUNT(*) FROM p',
                    'WITH p AS (SELECT excluded FROM public.items) SELECT COUNT(*) FROM p',
                    'WITH p AS (DELETE FROM public.items RETURNING code) SELECT COUNT(*) FROM p']:
            with pytest.raises(LightGraphError): rows(source, sql)
        assert rows(source, 'SELECT COUNT(*) FROM public.items') == [(2,)]


@pytest.mark.parametrize('dialect', ['sqlite', 'postgres', 'mysql'])
def test_equality_join_with_additional_filter_is_authorized(dialect):
    table = catalog()
    query = ('SELECT a.code FROM public.items a JOIN public.items b '
             'ON (a.tenant = b.tenant AND a.parent = b.code) AND a.parent <> a.code')
    assert SQLGuard([table], dialect=dialect).validate(query).sql == query


def test_left_join_filter_stays_in_on_clause():
    table = catalog()
    with MemorySource([table], {'public.items': [('T', 'A', 'R', 'first'), ('T', 'B', 'A', 'second')]}) as source:
        sql = ('SELECT a.code, b.code FROM public.items a LEFT JOIN public.items b '
               "ON a.parent = b.code AND b.name <> 'first' ORDER BY a.code")
        assert rows(source, sql) == [('A', None), ('B', None)]
        bounded = ('SELECT a.code FROM public.items a JOIN public.items b '
                   'ON a.code=b.code AND (a.code=b.code OR 1=1) ORDER BY a.code')
        assert rows(source, bounded) == [('A',), ('B',)]


@pytest.mark.parametrize('on', [
    'a.code=b.code OR 1=1', '(a.code=b.code OR a.name=b.name)', 'NOT (a.code=b.code)',
    'a.parent <> b.code', 'a.code=a.code AND a.name=b.name OR 1=1',
    'a.code=b.code AND c.name=a.name',
])
def test_additional_predicates_do_not_authorize_unbounded_or_unbound_join(on):
    with pytest.raises(LightGraphError):
        SQLGuard([catalog()]).validate('SELECT a.code FROM public.items a JOIN public.items b ON ' + on)


def test_alias_name_does_not_authorize_physical_table_or_hidden_column():
    table = catalog()
    for query in ['WITH p AS (SELECT code FROM public.items) SELECT code FROM private.p',
                  'SELECT a.code FROM public.items a JOIN public.items b ON a.code=b.code AND b.secret=1']:
        with pytest.raises(LightGraphError): SQLGuard([table]).validate(query)
