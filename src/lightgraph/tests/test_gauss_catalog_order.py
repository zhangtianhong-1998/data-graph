"""Stable catalog identity across snapshot connections; no live database."""
from copy import deepcopy

import pytest

from lightgraph.errors import LightGraphError
from lightgraph.sources.gauss import GaussSource
from lightgraph.tests import test_sources_gauss as fixtures
from lightgraph.tests.test_database_reconnect import Factory, read
from lightgraph.tests.test_database_snapshot_recovery import GAUSS_QUALIFIED


CONSTRAINTS = [
    ('pk', 'p', [1], None, None, None),
    ('uq_name', 'u', [2], None, None, None),
    ('uq_pair', 'u', [1, 2], None, None, None),
    ('fk_pair', 'f', [1, 2], [2, 1], 'public', 'items'),
    ('fk_name', 'f', [2], [1], 'public', 'items'),
]


class CatalogCursor(fixtures.Cursor):
    def execute(self, sql, parameters=None):
        result = super().execute(sql, parameters)
        if 'SELECT k.conname' in sql:
            self.result = deepcopy(self.connection.constraints)
        return result


class CatalogConnection(fixtures.Connection):
    def __init__(self, constraints):
        super().__init__()
        self.constraints = deepcopy(constraints)

    def cursor(self, name=None):
        return CatalogCursor(self, name)


def source_with_replacement(constraints):
    connections = [CatalogConnection(CONSTRAINTS), CatalogConnection(constraints)]
    factory = Factory(connections)
    source = GaussSource('fixture-only', allowed_tables=['public.items'],
        connect_factory=factory, verified_capabilities=GAUSS_QUALIFIED)
    return source, connections, factory


def test_constraint_row_order_does_not_invalidate_a_new_snapshot():
    source, connections, factory = source_with_replacement(list(reversed(CONSTRAINTS)))
    try:
        initial = source.describe()
        assert read(source) == [('001', 'Expenses')]
        assert source.describe() == initial
        assert factory.calls == 2 and source.connection_generation == 1
        assert source._reconnect_attempts == 0 and source.terminal_failure_code is None
        assert source.stats.queries == 1 and all(item.closed for item in connections)
        table = initial[0]
        assert ['code', 'name'] in table.unique_keys
        pair = next(key for key in table.foreign_keys if key.name == 'fk_pair')
        assert pair.columns == ['code', 'name'] and pair.target_columns == ['name', 'code']
    finally:
        source.close()


@pytest.mark.parametrize('change', ['added', 'removed', 'key_order', 'foreign_key_columns', 'target_columns'])
def test_real_constraint_change_still_stops_before_fact_queries(change):
    altered = deepcopy(CONSTRAINTS)
    if change == 'added':
        altered.append(('uq_reverse', 'u', [2, 1], None, None, None))
    elif change == 'removed':
        altered.pop()
    elif change == 'key_order':
        altered[2] = ('uq_pair', 'u', [2, 1], None, None, None)
    elif change == 'foreign_key_columns':
        altered[3] = ('fk_pair', 'f', [2, 1], [2, 1], 'public', 'items')
    else:
        altered[3] = ('fk_pair', 'f', [1, 2], [1, 2], 'public', 'items')
    source, connections, factory = source_with_replacement(altered)
    try:
        with pytest.raises(LightGraphError, match='^source_catalog_changed$'):
            read(source)
        assert source._reconnect_attempts == 0 and source.stats.queries == 0
        assert factory.calls == 2 and all(item.closed for item in connections)
        assert not any('FROM "public"."items"' in sql for sql, _ in connections[1].executed)
    finally:
        source.close()
