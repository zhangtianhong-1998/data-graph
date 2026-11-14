from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
import threading

import pytest

from lightgraph.contracts import ExtractionGoal, QueryLimits
from lightgraph.discovery.budget import BudgetAllocator
from lightgraph.discovery.tools import ToolExecutor
from lightgraph.errors import LightGraphError
from lightgraph.sources import base, gauss
from lightgraph.sources.base import QueryStats, SourceAdapter, cursor_batches
from lightgraph.sources.mysql import _connect_options
from lightgraph.sources.scoped import GoalScopedSource
from lightgraph.tests.test_cross_source import pair
from lightgraph.tests.test_database_reconnect import read
from lightgraph.tests.test_database_snapshot_lifecycle import sessions
from lightgraph.tests.test_sources_csv import FORMAT
from lightgraph.tests.test_sources_mysql import DSN
from lightgraph.sources import CSVSource


def test_timeout_override_is_local_to_the_operation():
    source = SourceAdapter()
    source.limits = QueryLimits(timeout_ms=60000)
    with ThreadPoolExecutor(max_workers=1) as pool, source.timeout_scope(240000):
        assert pool.submit(source._effective_timeout_ms).result() == 60000
        with source.timeout_scope(15000):
            assert source._effective_timeout_ms() == 15000
        assert source._effective_timeout_ms() == 240000
    assert source._effective_timeout_ms() == 60000


def test_mysql_socket_timeout_permits_longer_projection():
    options = _connect_options(DSN, QueryLimits(timeout_ms=60000, projection_timeout_ms=240000))
    assert options['connect_timeout'] == 30
    assert options['read_timeout'] == options['write_timeout'] == 242


def test_csv_sqlite_query_interrupts_at_probe_timeout_and_completes_under_projection_limit(tmp_path):
    path = tmp_path / 'items.csv'
    path.write_text('code\n' + 'A\n' * 1024, encoding='utf-8')
    limits = QueryLimits(probe_timeout_ms=1, projection_timeout_ms=10000)
    with CSVSource(path, **FORMAT, limits=limits) as source:
        query = source.compiler.guard.validate(
            'SELECT COUNT(*) FROM public.items a JOIN public.items b ON a.code=b.code')
        with source.timeout_scope(limits.effective_timeout_ms('probe')):
            with pytest.raises(LightGraphError, match='^query_timeout$'):
                list(source.execute(query))
        with source.timeout_scope(limits.effective_timeout_ms('projection')):
            assert [row for batch in source.execute(query) for row in batch] == [(1024 * 1024,)]
        assert source.stats.query_timeout_count == 1


@pytest.mark.parametrize('engine', ['gauss', 'mysql'])
def test_fresh_database_snapshot_sets_phase_statement_timeout_after_preflight(engine):
    source, data, _, _ = sessions(engine)
    with source:
        for i, timeout in enumerate([6000, 85000], 1):
            with source.timeout_scope(timeout):
                assert read(source) == [('001', 'Expenses')]
            prefix = ('SET LOCAL statement_timeout' if engine == 'gauss'
                      else 'SET SESSION max_execution_time')
            settings = [sql for sql, _ in data[i].executed if sql.startswith(prefix)]
            expected_value = f"'{timeout}ms'" if engine == 'gauss' else str(timeout)
            assert settings[-1] == f'{prefix} = {expected_value}'


@pytest.mark.parametrize('tool,arguments', [
    ('profile_columns', {'table': 'left:public.same_name', 'columns': ['code']}),
    ('check_fd', {'table': 'left:public.same_name', 'keys': ['code'], 'values': ['label']}),
    ('execute_readonly_sql', {'sql': 'SELECT code FROM public.same_name'}),
    ('check_reference', {'source_table': 'left:public.same_name', 'source_columns': ['code'],
        'target_table': 'right:public.same_name', 'target_columns': ['code']}),
])
def test_discovery_timeout_reaches_borrowed_adapters_before_snapshot_or_execute(monkeypatch, tool, arguments):
    from lightgraph.sources.memory import MemorySource
    observed = []
    original = MemorySource.snapshot
    execute = MemorySource.execute
    @contextmanager
    def record_snapshot(self):
        observed.append((self.describe()[0].source_id, self._effective_timeout_ms()))
        with original(self):
            yield self
    def record_execute(self, *args, **kwargs):
        observed.append((self.describe()[0].source_id, self._effective_timeout_ms()))
        yield from execute(self, *args, **kwargs)
    monkeypatch.setattr(MemorySource, 'snapshot', record_snapshot)
    monkeypatch.setattr(MemorySource, 'execute', record_execute)
    rows = [('A', '1', 'a')]
    goal = ExtractionGoal()
    with pair(rows, rows) as (left, a, right, b):
        left.limits = QueryLimits(probe_timeout_ms=120000)
        right.limits = QueryLimits(probe_timeout_ms=15000)
        tools = ToolExecutor(GoalScopedSource(left, goal), goal, BudgetAllocator(), allow_source_samples=True,
            source_adapters={'right': GoalScopedSource(right, goal)})
        result = tools.execute(tool, arguments)
        assert result['status'] == 'ok', result
        expected = {('left', 120000)}
        if tool == 'check_reference':
            expected.add(('right', 15000))
        assert set(observed) == expected
        assert left._effective_timeout_ms() == right._effective_timeout_ms() == 60000


def test_stream_timer_excludes_consumer_time_and_honors_phase_batch_lifetime(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(base, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    rows = iter([('a',), ('b',), None])
    def fetchone():
        clock[0] += 0.01
        return next(rows)
    stats = QueryStats()
    batches = cursor_batches(SimpleNamespace(fetchone=fetchone),
        limits=QueryLimits(max_batch_rows=1, timeout_ms=1000), columns=('x',),
        row_limit=2, stats=stats, timeout_ms=20000)
    first = next(batches)
    clock[0] += 5
    assert list(first) == [('a',)]
    assert list(next(batches)) == [('b',)]
    assert next(batches, None) is None
    assert stats.sql_execute_seconds == pytest.approx(.03)


def test_gauss_client_timer_counts_a_single_timeout(monkeypatch):
    class ExpiredTimer:
        ident = None
        def __init__(self, interval, callback):
            self.callback = callback
        def start(self):
            self.callback()
        def cancel(self):
            pass
        def is_alive(self):
            return False
    source, _, _, _ = sessions('gauss')
    with source, source.snapshot():
        monkeypatch.setattr(gauss, 'threading', SimpleNamespace(Timer=ExpiredTimer, Event=threading.Event))
        with pytest.raises(LightGraphError, match='^query_timeout$'):
            read(source)
        assert source.stats.query_timeout_count == 1
