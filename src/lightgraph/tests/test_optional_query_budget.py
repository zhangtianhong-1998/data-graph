"""Count opt-out keeps resource guards and survives the workbench config boundary."""
import json
import time

import pytest
from pydantic import ValidationError

from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.runtime.config import RunConfig, open_sources
from lightgraph.sources.budget import QueryBudget
from lightgraph.tests.web_client import RevisionClient
from lightgraph.web.app import create_app


def test_default_and_explicit_unlimited_config_roundtrip():
    for value in [{}, {'max_db_queries': None}, {'max_db_queries': 7}]:
        config = RunConfig(sources=[], **value)
        restored = RunConfig.model_validate_json(config.model_dump_json())
        assert restored.max_db_queries == value.get('max_db_queries')
    assert RunConfig(sources=[]).construction_target == 'instances'
    assert RunConfig(sources=[], construction_target='structure').construction_target == 'structure'


@pytest.mark.parametrize('value', [0, -1, 1.5, True, '240'])
def test_query_limit_requires_positive_integer_or_null(value):
    with pytest.raises(ValidationError):
        RunConfig(sources=[], max_db_queries=value)
    with pytest.raises(LightGraphError, match='invalid_query_budget'):
        QueryBudget(max_queries=value)


def test_unlimited_accounting_and_finite_limit_do_not_hide_consumption():
    unlimited = QueryBudget(max_queries=None)
    for _ in range(1001):
        unlimited.charge_query()
    snapshot = unlimited.snapshot()
    assert snapshot['queries'] == 1001
    assert snapshot['max_queries'] is None and snapshot['remaining_queries'] is None
    json.dumps(snapshot, allow_nan=False)
    finite = QueryBudget(max_queries=2)
    finite.charge_query(); finite.charge_query()
    with pytest.raises(BudgetExceeded) as error:
        finite.charge_query()
    assert error.value.code == 'query_budget_exhausted' and not error.value.retryable
    assert finite.snapshot()['queries'] == 2
    assert finite.snapshot()['remaining_queries'] == 0


def test_unlimited_keeps_time_row_and_byte_guards():
    for kwargs, action, code in [
        ({'deadline': time.monotonic() - 1}, lambda budget: budget.charge_query(), 'run_time_budget_exhausted'),
        ({'max_returned_rows': 1}, lambda budget: budget.charge_return(2, 0), 'returned_rows_budget_exhausted'),
        ({'max_returned_bytes': 1}, lambda budget: budget.charge_return(0, 2), 'returned_bytes_budget_exhausted'),
    ]:
        budget = QueryBudget(max_queries=None, **kwargs)
        with pytest.raises(BudgetExceeded, match=code):
            action(budget)


def test_absorbing_and_temporarily_limiting_unlimited_budget():
    previous = QueryBudget(max_queries=None)
    for _ in range(5): previous.charge_query()
    current = QueryBudget(max_queries=None)
    current.absorb(previous); current.absorb(previous)
    assert current.queries == 5
    with pytest.raises(BudgetExceeded, match='discovery_query_budget_exhausted'):
        with current.exploration(deadline=current.deadline, query_limit=6):
            current.charge_query(); current.charge_query()
    assert current.max_queries is None
    current.charge_query()
    with current.exploration(deadline=current.deadline, query_limit=None):
        current.charge_query()
    assert current.queries == 8
    finite = QueryBudget(max_queries=2)
    with finite.exploration(deadline=finite.deadline, query_limit=None):
        assert finite.max_queries == 2


def test_cost_allocator_accepts_unlimited_queries_but_rejects_unknown_measured_cost():
    budget = BudgetAllocator(Cost(db_cu=None, memory_bytes=100, seconds=60))
    budget.reserve('check', Cost(db_cu=1000))
    budget.admit(Cost(db_cu=1000), 'check')
    budget.charge(Cost(db_cu=1000), 'check')
    assert budget.snapshot()['used']['db_cu'] == 1000
    with pytest.raises(ValueError): budget.charge(Cost(db_cu=None))
    with pytest.raises(BudgetExceeded, match='memory_budget_exhausted'):
        budget.admit(Cost(memory_bytes=101))


def test_source_opening_passes_optional_shared_budget(tmp_path):
    source = tmp_path / 'objects.csv'
    source.write_text('code,name\nA,Alpha\n', encoding='utf-8')
    config = RunConfig.model_validate({'sources': [{'id': 'local', 'kind': 'csv_file', 'path': str(source),
        'encoding': 'utf-8', 'delimiter': ',', 'header': True, 'null_values': [''], 'type_inference': 'text'}],
        'max_db_queries': None})
    adapters = open_sources(config)
    try:
        assert adapters[0].query_budget.max_queries is None
        table = adapters[0].describe()[0]
        for batch in adapters[0].project(table, ['code'], limit=10):
            with batch: assert list(batch) == [('A',)]
        assert adapters[0].query_budget.queries > 0
    finally:
        for adapter in adapters: adapter.close()


def test_workbench_save_reload_and_run_payload_preserve_target_and_unlimited(tmp_path):
    from lightgraph.web.execution import _resolved_payload
    with RevisionClient(create_app(tmp_path / 'work', frontend_dir=tmp_path / 'absent')) as client:
        assert client.post('/api/projects', json={'name': 'budget', 'project_id': 'budget'}).status_code == 201
        saved = client.get('/api/projects/budget/config').json()
        assert saved['max_db_queries'] is None
        for target, limit in [('structure', None), ('instances', 500), ('instances', None)]:
            saved.update(construction_target=target, max_db_queries=limit)
            response = client.put('/api/projects/budget/config', json=saved)
            assert response.status_code == 200, response.text
            restored = client.get('/api/projects/budget/config').json()
            assert restored['max_db_queries'] == limit and restored['construction_target'] == target
            parsed = RunConfig.model_validate(restored)
            assert parsed.max_db_queries == limit and parsed.construction_target == target
            payload = _resolved_payload(parsed, tmp_path / 'work' / 'projects' / 'budget')['config']
            assert payload['max_db_queries'] == limit and payload['construction_target'] == target
