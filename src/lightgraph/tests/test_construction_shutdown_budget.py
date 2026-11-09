"""MCP teardown cancels source work; it must not manufacture a run timeout."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from threading import Event
import time

import pytest

from lightgraph.construction import runner
from lightgraph.construction.tools import ConstructionTools, TERMINAL_QUERY_CODES
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.sources.budget import QueryBudget
from lightgraph.tests.test_construction_runner import configuration, save_object


def test_explicit_cancellation_preserves_deadlines_and_actual_consumption():
    source = QueryBudget(max_run_seconds=120)
    source.charge_query()
    source.charge_return(2, 20)
    allocator = BudgetAllocator(Cost(db_cu=10, seconds=120))
    allocator.reserve('active', Cost(db_cu=3))
    source_deadline, allocator_deadline = source.deadline, allocator.deadline_monotonic

    source.cancel()
    allocator.cancel()
    assert (source.deadline, allocator.deadline_monotonic) == (source_deadline, allocator_deadline)
    for action in (source.check_time, source.charge_query, allocator.remaining_seconds,
                   lambda: allocator.admit(Cost(db_cu=1)),
                   lambda: allocator.reserve('another', Cost(db_cu=1))):
        with pytest.raises(LightGraphError, match='^query_cancelled$') as error:
            action()
        assert not isinstance(error.value, BudgetExceeded)
    with pytest.raises(LightGraphError, match='^query_cancelled$'):
        allocator.charge(Cost(db_cu=1), 'active')
    assert source.snapshot()['queries'] == 1
    assert source.snapshot()['returned_rows'] == 2
    assert allocator.snapshot()['used']['db_cu'] == 1
    assert allocator.snapshot()['reserved'] == {}


def test_cancellation_does_not_hide_a_real_expired_deadline():
    source = QueryBudget(deadline=time.monotonic() - 1)
    allocator = BudgetAllocator(Cost(seconds=120))
    allocator.tighten_deadline(time.monotonic() - 1)
    source.cancel()
    allocator.cancel()
    with pytest.raises(BudgetExceeded, match='^run_time_budget_exhausted$'):
        source.check_time()
    with pytest.raises(BudgetExceeded, match='^wall_time_budget_exhausted$'):
        allocator.remaining_seconds()


def test_cancelled_query_scope_restores_deadline_and_preserves_real_phase_timeout():
    source = QueryBudget(max_run_seconds=120)
    enclosing = source.deadline
    with pytest.raises(LightGraphError, match='^query_cancelled$'):
        with source.deadline_scope(enclosing - 10):
            source.cancel()
            source.charge_return(1, 1)
    assert source.deadline == enclosing
    assert source.snapshot()['returned_rows'] == 0
    # A genuinely expired temporary task window is still a phase timeout,
    # rather than cancellation or exhaustion of the enclosing run.
    with pytest.raises(BudgetExceeded, match='^wall_time_budget_exhausted$'):
        with source.deadline_scope(time.monotonic() - 1):
            pytest.fail('expired scope must reject admission')
    assert source.deadline == enclosing


def block_statement(monkeypatch, adapter, *, statement=1):
    """Hold a real validation before one statement; driver cancel wakes it."""
    entered, cancelled = Event(), Event()
    execute, cancel = adapter.execute, adapter.cancel
    calls = 0

    def blocked(query, **kwargs):
        nonlocal calls
        calls += 1
        if calls == statement:
            entered.set()
            assert cancelled.wait(5), 'test operation was not cancelled'
            # A driver may clear its own cancellation flag before its next SQL.
            # Run cancellation must remain observable in the shared budget.
            adapter._cancelled = False
            adapter.query_budget.check_time()
        yield from execute(query, **kwargs)

    def interrupt():
        cancel()
        cancelled.set()

    monkeypatch.setattr(adapter, 'execute', blocked)
    monkeypatch.setattr(adapter, 'cancel', interrupt)
    return entered, cancelled


@pytest.mark.parametrize('statement', [1, 2], ids=['count', 'projection'])
def test_active_validation_shutdown_is_cancelled_without_exhausting_budget(tmp_path, monkeypatch, statement):
    tools = ConstructionTools(configuration(tmp_path), tmp_path / 'work')
    try:
        save_object(tools)
        adapter = tools.adapters[0]
        entered, interrupted = block_statement(monkeypatch, adapter, statement=statement)
        deadlines = tools.budget.deadline_monotonic, adapter.query_budget.deadline
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(tools.call, 'validate_graph_draft', draft_id='资料对象')
            assert entered.wait(5)
            tools.request_shutdown()
            checked = pending.result(timeout=5)
        assert interrupted.is_set()
        assert deadlines == (tools.budget.deadline_monotonic, adapter.query_budget.deadline)
        assert not checked['passed']
        assert 'construction_cancelled' in checked['diagnostic_codes']
        assert not TERMINAL_QUERY_CODES.intersection(checked['diagnostic_codes'])
        assert checked['metrics']['nodes'] == checked['metrics']['edges'] == 0
        assert tools.terminal_reason is None
        assert json.loads((tools.workspace.root / 'query-stop.json').read_text()) == {'code': None}
        assert tools.source_circuits.summary()['sources'] == []
        assert not tools.workspace.read_summary()['publications']
        assert tools.call('read_workspace')['code'] == 'construction_cancelled'
        with pytest.raises(LightGraphError, match='^query_cancelled$'):
            adapter.query_budget.charge_query()
    finally:
        tools.close()


def test_transport_failure_teardown_keeps_primary_report_cause(tmp_path, monkeypatch):
    """Scripted Harness failure plus real pending validation and shutdown."""
    bound = {}
    with ThreadPoolExecutor(max_workers=1) as executor:
        @contextmanager
        def serve(tools):
            bound['tools'] = tools
            try:
                yield 'http://127.0.0.1:9998/mcp', 'test-token'
            finally:
                tools.request_shutdown()
                bound['validation'] = bound['pending'].result(timeout=5)

        def runtime(intent, **options):
            tools = bound['tools']
            save_object(tools)
            entered, _ = block_statement(monkeypatch, tools.adapters[0])
            bound['pending'] = executor.submit(tools.call, 'validate_graph_draft', draft_id='资料对象')
            assert entered.wait(5)
            return {'finish_reason': 'error', 'budget': {
                'calls': 1, 'stop_reason': 'construction_tool_transport_failed'}}

        monkeypatch.setattr(runner, 'serve_local_tools', serve)
        report = runner.run_construction(configuration(tmp_path), runtime=runtime)

    assert report.status == 'PARTIAL'
    codes = {issue['code'] for issue in report.issues}
    assert 'construction_tool_transport_failed' in codes
    assert not TERMINAL_QUERY_CODES.intersection(codes)
    assert 'harness_run_interrupted' not in codes
    assert not bound['validation']['passed']
    assert bound['tools'].terminal_reason is None
    assert report.counts['business_nodes'] == report.counts['business_edges'] == 0
    assert report.construction.drafts[0].draft_id == '资料对象'


def test_shutdown_stops_a_multi_statement_probe_without_stopping_source_circuit(tmp_path, monkeypatch):
    tools = ConstructionTools(configuration(tmp_path), tmp_path / 'work')
    try:
        table = tools.describe_sources()['tables'][0]
        adapter = tools.adapters[0]
        entered, _ = block_statement(monkeypatch, adapter)
        deadline = adapter.query_budget.deadline
        query_count = adapter.query_budget.queries
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(tools.call, 'inspect_source', table=table['table'],
                operation='profile_columns', arguments={'columns': ['代号', '中文名']})
            assert entered.wait(5)
            tools.request_shutdown()
            checked = pending.result(timeout=5)
        assert checked['status'] == 'error' and checked['code'] == 'query_cancelled'
        assert adapter.query_budget.deadline == deadline
        assert adapter.query_budget.queries == query_count
        assert tools.source_circuits.summary()['sources'] == []
        assert tools.terminal_reason is None
    finally:
        tools.close()
