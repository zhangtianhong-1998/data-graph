"""A table's discovery deadline must not poison the shared source budget."""
from contextlib import contextmanager
from types import SimpleNamespace
import time

import pytest

from lightgraph.contracts import ExtractionGoal
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.llm import ModelConfig
from lightgraph.discovery.models import ProposalBatch
from lightgraph.discovery.tools import ToolExecutor
from lightgraph.discovery.workflow import discover_with_agent
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.sources import MemorySource, QueryBudget
from lightgraph.tests.test_agent_pipeline import group, table


@pytest.fixture
def clock(monkeypatch):
    now = [time.monotonic()]
    timer = SimpleNamespace(monotonic=lambda: now[0])
    for module in ('lightgraph.sources.budget', 'lightgraph.discovery.budget',
                   'lightgraph.discovery.workflow', 'lightgraph.discovery.tools'):
        monkeypatch.setattr(module + '.time', timer)
    return now


def model_budget(seconds=100):
    return BudgetAllocator(Cost(db_cu=160, llm_calls=32, llm_tokens=None,
                                memory_bytes=8 * 1024 * 1024, seconds=seconds))


@pytest.mark.parametrize('during_query', [False, True])
def test_timed_out_first_table_does_not_prevent_second_table_discovery(clock, monkeypatch, during_query):
    start = clock[0]
    tables = [table(name, ['code', 'name']) for name in ('first', 'second')]
    queries = QueryBudget(max_run_seconds=120)
    budget = model_budget()
    visited, observed = [], []

    def explore(packet, tools, **kwargs):
        name = packet['scope']['table']
        visited.append(name)
        result = tools.execute('execute_readonly_sql', {'sql': f'SELECT code, name FROM public.{name}'})
        observed.append((name, result['status']))
        if name == 'first':
            # Reproduce a query followed by slow model reasoning. In the other
            # case, time expires inside the real source iterator instead.
            clock[0] = start + 60
            budget.check_deadline()
        return ProposalBatch.model_validate(group(packet, 'code', 'name'))

    model = SimpleNamespace(budget=budget, usage={'calls': 0}, explore=explore,
        config=ModelConfig(base_url='https://offline.invalid/v1', model='offline', allow_source_samples=True))
    with MemorySource(tables, {'public.first': [('1', 'First')],
                              'public.second': [('2', 'Second')]}, query_budget=queries) as source:
        original_execute = source.execute
        if during_query:
            def execute(query, **kwargs):
                for batch in original_execute(query, **kwargs):
                    if 'first' in query.sql:
                        clock[0] = start + 60
                    yield batch
            monkeypatch.setattr(source, 'execute', execute)
        with queries.exploration(deadline=start + 100, query_limit=100):
            result = discover_with_agent(source, model=model)
            assert queries.deadline == start + 100
        assert queries.deadline == start + 120
        # This includes real SQL discovery and the second rule's full FD check.
        assert source.stats.queries >= 3
    assert visited == ['first', 'second']
    assert ('second', 'ok') in observed
    assert [rule.source.table for rule in result.rules] == ['second']
    assert result.stats['table_time_deferrals'] == 1
    assert any(issue['table'] == 'public.first' and issue['code'] == 'wall_time_budget_exhausted'
               for issue in result.issues)
    assert not any(issue['code'] == 'run_time_budget_exhausted' for issue in result.issues)
    assert budget.deadline_monotonic == start + 100
    assert not budget.snapshot()['reserved']


def test_query_iterator_close_and_exception_restore_deadline(clock, monkeypatch):
    start = clock[0]
    current = table('items', ['code', 'name'])
    queries, budget = QueryBudget(max_run_seconds=120), model_budget(50)
    with MemorySource([current], {'public.items': [('1', 'First')]}, query_budget=queries) as source:
        tools = ToolExecutor(source, ExtractionGoal(), budget, allow_source_samples=True)
        query = tools.compiler.guard.validate('SELECT code FROM public.items')
        stream = tools._query(query, row_limit=1)
        batch = next(stream)
        assert queries.deadline == start + 50
        batch.release()
        stream.close()
        assert queries.deadline == start + 120
        assert budget.used.db_cu == 1

        def broken(*args, **kwargs):
            assert queries.deadline == start + 50
            raise LightGraphError('query_execution_failed')
        monkeypatch.setattr(source, 'execute', broken)
        assert tools.execute('execute_readonly_sql', {'sql': 'SELECT code FROM public.items'})['code'] == 'query_execution_failed'
        assert queries.deadline == start + 120


@pytest.mark.parametrize('elapsed,expected', [(60, 'wall_time_budget_exhausted'),
                                              (110, 'discovery_time_budget_exhausted'),
                                              (130, 'run_time_budget_exhausted')])
def test_query_timeouts_keep_table_phase_and_run_distinct(clock, elapsed, expected):
    start = clock[0]
    queries = QueryBudget(max_run_seconds=120)
    with pytest.raises(BudgetExceeded, match='^' + expected + '$'):
        with queries.exploration(deadline=start + 100, query_limit=100):
            with queries.deadline_scope(start + 50):
                clock[0] = start + elapsed
                # Model accounting in a finally block may be the first timeout.
                raise BudgetExceeded('wall_time_budget_exhausted')
    assert queries.deadline == start + 120


@pytest.mark.parametrize('expire', [False, True])
def test_cross_source_shared_budget_is_scoped_once_and_restored(clock, monkeypatch, expire):
    start = clock[0]
    queries, budget = QueryBudget(max_run_seconds=120), model_budget(50)
    entered = []
    original_scope = queries.deadline_scope

    @contextmanager
    def counted_scope(deadline):
        entered.append(deadline)
        with original_scope(deadline):
            yield
    monkeypatch.setattr(queries, 'deadline_scope', counted_scope)
    left_table, right_table = [table(name, ['code', 'name']) for name in ('left', 'right')]
    left_table.source_id, right_table.source_id = 'left', 'right'
    with MemorySource([left_table], {'public.left': [('1', 'First')]}, query_budget=queries) as left, \
         MemorySource([right_table], {'public.right': [('1', 'First')]}, query_budget=queries) as right:
        if expire:
            execute = left.execute
            def slow(query, **kwargs):
                clock[0] = start + 60
                yield from execute(query, **kwargs)
            monkeypatch.setattr(left, 'execute', slow)
        tools = ToolExecutor(left, ExtractionGoal(), budget, allow_source_samples=True,
                             source_adapters={'right': right})
        args = {'source_table': 'left:public.left', 'source_columns': ['code'],
                'target_table': 'right:public.right', 'target_columns': ['code']}
        if expire:
            with pytest.raises(BudgetExceeded, match='wall_time_budget_exhausted'):
                tools.execute('check_reference', args)
        else:
            result = tools.execute('check_reference', args)
            assert result['status'] == 'ok'
            assert result['coverage']['confirmed']
            assert queries.queries > 0
    assert entered == [start + 50]
    assert queries.deadline == start + 120


@pytest.mark.parametrize('reverse', [False, True])
def test_distinct_source_budgets_preserve_earliest_run_timeout(clock, reverse):
    start = clock[0]
    short, long = QueryBudget(max_run_seconds=120), QueryBudget(max_run_seconds=200)
    current = table('items', ['code', 'name'])
    with MemorySource([current], {'public.items': [('1', 'First')]}, query_budget=long) as source:
        tools = ToolExecutor(source, ExtractionGoal(), model_budget(50), allow_source_samples=True)
        adapters = [SimpleNamespace(query_budget=short), SimpleNamespace(query_budget=long)]
        with pytest.raises(BudgetExceeded, match='^run_time_budget_exhausted$'):
            with tools._query_deadlines(adapters[::-1] if reverse else adapters):
                clock[0] = start + 130
                raise BudgetExceeded('wall_time_budget_exhausted')
    assert short.deadline == start + 120
    assert long.deadline == start + 200
