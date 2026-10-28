"""Explicit API budget opt-out still accounts usage and enforces source limits."""
import json

import pytest

from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.errors import BudgetExceeded
from lightgraph.pipeline import make_model
from lightgraph.runtime.config import RunConfig


def test_null_api_totals_survive_config_and_do_not_disable_query_limits():
    config = RunConfig(sources=[], max_db_queries=2, model={
        "mode": "approved_external", "model": "offline", "base_url": "https://example.invalid/v1",
        "max_calls": None, "max_tokens": None,
    })
    roundtrip = RunConfig.model_validate_json(config.model_dump_json())
    budget = make_model(roundtrip).budget
    charge = Cost(llm_calls=1000, llm_tokens=100000000)
    budget.reserve("work", charge)
    budget.admit(charge, "work")
    budget.charge(charge, "work")
    budget.release("work")
    snapshot = budget.snapshot()
    assert snapshot["used"]["llm_calls"] == 1000
    assert snapshot["used"]["llm_tokens"] == 100000000
    assert snapshot["limits"]["llm_calls"] is None
    assert snapshot["limits"]["llm_tokens"] is None
    json.dumps(snapshot, allow_nan=False)
    with pytest.raises(BudgetExceeded, match="db_cu_budget_exhausted"):
        budget.admit(Cost(db_cu=3))


def test_zero_is_still_a_limit_and_each_api_total_is_independent():
    budget = BudgetAllocator(Cost(llm_calls=0, llm_tokens=None, seconds=60))
    with pytest.raises(BudgetExceeded, match="llm_calls_budget_exhausted"):
        budget.admit(Cost(llm_calls=1))
    budget = BudgetAllocator(Cost(llm_calls=None, llm_tokens=10, seconds=60))
    with pytest.raises(BudgetExceeded, match="llm_tokens_budget_exhausted"):
        budget.charge(Cost(llm_calls=2, llm_tokens=11))
    assert budget.used.llm_calls == 2
    assert budget.used.llm_tokens == 11


def test_uncapped_api_still_enforces_deadline_and_memory():
    budget = BudgetAllocator(Cost(llm_calls=None, llm_tokens=None, memory_bytes=10, seconds=60))
    with pytest.raises(BudgetExceeded, match="memory_budget_exhausted"):
        budget.admit(Cost(memory_bytes=11))
    budget.tighten_deadline(0)
    with pytest.raises(BudgetExceeded, match="wall_time_budget_exhausted"):
        budget.admit(Cost(llm_calls=1))
