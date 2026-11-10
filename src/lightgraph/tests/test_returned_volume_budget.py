"""Whole-run source volume is configurable and distinct from working memory."""
import json
import time

import pytest
from pydantic import ValidationError

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.runtime.config import RunConfig, install_run_query_budget, open_sources
from lightgraph.sources import MemorySource
from lightgraph.sources.budget import BudgetedCursor, QueryBudget
from lightgraph.tests.web_client import RevisionClient
from lightgraph.web.app import create_app


def test_volume_config_defaults_roundtrip_and_independent_memory_limit():
    old = RunConfig(sources=[], max_working_bytes=1024**3)
    assert old.max_returned_rows == 400000
    assert old.max_returned_bytes == 64*1024**2
    for rows, size in [(None, None), (900000, 1024**3), (3, None), (None, 17)]:
        config = RunConfig(sources=[], max_returned_rows=rows, max_returned_bytes=size)
        restored = RunConfig.model_validate_json(config.model_dump_json())
        assert (restored.max_returned_rows, restored.max_returned_bytes) == (rows, size)
        assert restored.max_working_bytes == 32*1024**2


@pytest.mark.parametrize('field', ['max_returned_rows', 'max_returned_bytes'])
@pytest.mark.parametrize('value', [0, -1, 1.5, True, '100'])
def test_volume_limits_are_positive_integers_or_null(field, value):
    with pytest.raises(ValidationError):
        RunConfig(sources=[], **{field: value})
    with pytest.raises(LightGraphError, match='invalid_query_budget'):
        QueryBudget(**{field: value})


@pytest.mark.parametrize('field, cost, code', [
    ('max_returned_rows', (3, 9), 'returned_rows_budget_exhausted'),
    ('max_returned_bytes', (1, 11), 'returned_bytes_budget_exhausted'),
])
def test_consumed_volume_is_retained_and_exhaustion_blocks_new_queries(field, cost, code):
    budget = QueryBudget(**{field: 10})
    budget.charge_query()
    budget.charge_return(8, 8)
    with pytest.raises(BudgetExceeded, match=code) as error:
        budget.charge_return(*cost)
    expected = (8+cost[0], 8+cost[1])
    assert (budget.returned_rows, budget.returned_bytes) == expected
    detail = error.value.query_budget_diagnostic
    assert detail['config_field'] == field
    assert detail['scope'] == 'whole_run_all_sources'
    assert detail['remaining_'+field.removeprefix('max_')] == 0
    assert not error.value.retryable
    with pytest.raises(BudgetExceeded, match=code):
        budget.charge_query()
    assert budget.queries == 1  # No hidden COUNT or next mapping is admitted.


def test_unlimited_volume_keeps_counters_and_other_resource_guards():
    budget = QueryBudget(max_returned_rows=None, max_returned_bytes=None, max_queries=1)
    budget.charge_query()
    budget.charge_return(500000, 2*1024**3)
    snapshot = budget.snapshot()
    assert snapshot['returned_rows'] == 500000 and snapshot['returned_bytes'] == 2*1024**3
    assert snapshot['remaining_returned_rows'] is snapshot['remaining_returned_bytes'] is None
    json.dumps(snapshot, allow_nan=False)
    with pytest.raises(BudgetExceeded, match='query_budget_exhausted'):
        budget.charge_query()
    budget.deadline = time.monotonic()-1
    with pytest.raises(BudgetExceeded, match='run_time_budget_exhausted'):
        budget.charge_return(1, 1)


def test_phase_windows_and_batch_release_do_not_refund_volume():
    table = TableSpec(source_id='test', name='items', columns=[ColumnSpec(name='code')])
    budget = QueryBudget(max_returned_rows=3, max_returned_bytes=None)
    with MemorySource([table], {'public.items': [('A',), ('B',)]}, query_budget=budget) as source:
        with budget.exploration(deadline=budget.deadline, query_limit=None):
            for batch in source.project(table, ['code']):
                with batch: assert list(batch) == [('A',), ('B',)]
        assert budget.returned_rows == 2
        with pytest.raises(BudgetExceeded, match='returned_rows_budget_exhausted'):
            for batch in source.project(table, ['code']):
                with batch: list(batch)
        assert budget.returned_rows == 4


def test_install_run_budget_shares_limits_and_imports_prior_consumption_once():
    table_a = TableSpec(source_id='a', name='items', columns=[ColumnSpec(name='code')])
    table_b = table_a.model_copy(update={'source_id':'b'})
    previous = QueryBudget(max_run_seconds=60)
    previous.charge_query(); previous.charge_return(11, 100)
    config = RunConfig(sources=[], max_returned_rows=None, max_returned_bytes=1024**3)
    with MemorySource([table_a], {'public.items': [('A',)]}, query_budget=previous) as a, \
         MemorySource([table_b], {'public.items': [('B',)]}, query_budget=previous) as b:
        budget = install_run_query_budget(config, [a, b])
        assert a.query_budget is b.query_budget is budget
        assert (budget.queries, budget.returned_rows, budget.returned_bytes) == (1, 11, 100)
        assert budget.deadline == previous.deadline
        assert budget.max_returned_rows is None and budget.max_returned_bytes == 1024**3
        for source, table in [(a, table_a), (b, table_b)]:
            for batch in source.project(table, ['code']):
                with batch: list(batch)
        assert budget.returned_rows == 13
        assert budget.returned_bytes > 100


def test_imported_volume_and_cancellation_cannot_be_reset():
    old = QueryBudget(max_returned_rows=None, max_returned_bytes=None)
    old.charge_return(5, 200)
    smaller = QueryBudget(max_returned_rows=4)
    with pytest.raises(BudgetExceeded, match='returned_rows_budget_exhausted'):
        smaller.absorb(old)
    assert smaller.returned_rows == 5
    old.cancel()
    replacement = QueryBudget(max_returned_rows=None, max_returned_bytes=None)
    with pytest.raises(LightGraphError, match='^query_cancelled$'):
        replacement.absorb(old)
    assert replacement.returned_bytes == 200


@pytest.mark.parametrize('limit', [1024**3, None])
def test_source_opening_uses_saved_volume_budget_for_all_sources(tmp_path, limit):
    source = tmp_path/'objects.csv'
    source.write_text('code,name\nA,Alpha\n', encoding='utf-8')
    sources = [{'id':name,'kind':'csv_file','path':str(source),'encoding':'utf-8',
        'delimiter':',','header':True,'null_values':[''],'type_inference':'text'} for name in ['a','b']]
    config = RunConfig.model_validate({'sources':sources,'max_returned_bytes':limit,'max_returned_rows':None})
    adapters = open_sources(config)
    try:
        assert adapters[0].query_budget is adapters[1].query_budget
        assert adapters[0].query_budget.max_returned_bytes == limit
        assert adapters[0].query_budget.max_returned_rows is None
    finally:
        for adapter in adapters: adapter.close()


def test_budgeted_metadata_cursor_counts_and_reports_volume_without_values():
    class Cursor:
        def fetchone(self): return ('PRIVATE_SOURCE_VALUE',)
    budget = QueryBudget(max_returned_bytes=1)
    cursor = BudgetedCursor(Cursor(), budget)
    with pytest.raises(BudgetExceeded) as error:
        cursor.fetchone()
    assert error.value.code == 'returned_bytes_budget_exhausted'
    assert budget.returned_rows == 1 and budget.returned_bytes > 1
    assert 'PRIVATE_SOURCE_VALUE' not in json.dumps(error.value.query_budget_diagnostic)


def test_workbench_volume_save_reload_and_worker_payload(tmp_path):
    from lightgraph.web.execution import _resolved_payload
    with RevisionClient(create_app(tmp_path/'work', frontend_dir=tmp_path/'absent')) as client:
        assert client.post('/api/projects', json={'name':'volume','project_id':'volume'}).status_code == 201
        saved = client.get('/api/projects/volume/config').json()
        assert saved['max_returned_bytes'] == 64*1024**2
        for rows, size in [(1000000, 1024**3), (None, None)]:
            saved.update(max_returned_rows=rows, max_returned_bytes=size)
            response = client.put('/api/projects/volume/config', json=saved)
            assert response.status_code == 200, response.text
            restored = client.get('/api/projects/volume/config').json()
            assert (restored['max_returned_rows'], restored['max_returned_bytes']) == (rows, size)
            payload = _resolved_payload(RunConfig.model_validate(restored), tmp_path/'work'/'projects'/'volume')['config']
            assert (payload['max_returned_rows'], payload['max_returned_bytes']) == (rows, size)
