from __future__ import annotations

import pytest
from pydantic import ValidationError

from lightgraph.contracts import ColumnSpec, ExtractionGoal, SourceRef, TableSpec, digest
from lightgraph.discovery.goal import (
    build_goal_instructions, column_is_evidence_only, column_is_excluded, source_in_goal,
)
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import ModelConfig, RunConfig, load_config


def table():
    return TableSpec(source_id="warehouse", schema="finance", name="ledger",
                     columns=[ColumnSpec(name="amount"), ColumnSpec(name="item")])


def test_old_config_keeps_existing_goal_and_model_defaults():
    config = RunConfig(sources=[])
    assert not config.goal.observation_values
    assert config.goal.entity_kinds == ["Metric", "Measure", "Dimension", "DimensionMember", "ReportItem", "BusinessConcept"]
    assert config.goal.include == config.goal.exclude == config.goal.evidence_only_columns == []
    assert config.model.max_discovery_steps == 8
    assert config.model.max_tool_calls == 16


def test_operator_goal_loads_without_table_or_field_mapping(tmp_path):
    path = tmp_path / "run.yaml"
    path.write_text("""sources: []
goal:
  purpose: 构建经营指标的分析图
  include: [指标定义, 度量计算关系, 维度层级]
  exclude: [每笔交易, 具体收入观测值]
  discovery_guidance: [先验证列组，再按关键列去重]
model:
  max_discovery_steps: 12
  max_tool_calls: 24
""", encoding="utf-8")
    config = load_config(path)
    prompt = build_goal_instructions(config.goal)
    assert "构建经营指标的分析图" in prompt
    assert "具体收入观测值" in prompt
    assert config.model.max_tool_calls == 24
    assert config.goal.source_scope == config.goal.excluded_columns == []


@pytest.mark.parametrize("selector", [
    "ledger", "finance", "warehouse", "finance.ledger", "warehouse.ledger",
    "warehouse.finance", "warehouse:finance", "warehouse.finance.ledger", "warehouse:finance.ledger", "source:warehouse", "schema:finance",
])
def test_goal_source_scope_is_shared_between_catalog_and_rules(selector):
    goal = ExtractionGoal(source_scope=[selector])
    ref = SourceRef(source_id="warehouse", schema="finance", table="ledger")
    assert source_in_goal(goal, table())
    assert source_in_goal(goal, ref)
    assert not source_in_goal(goal, SourceRef(source_id="other", schema="other", table="other"))


@pytest.mark.parametrize("selector", [
    "amount", "ledger.amount", "finance.ledger.amount", "warehouse.finance.ledger.amount",
    "warehouse:finance.ledger.amount",
])
def test_evidence_columns_are_separate_from_excluded_columns(selector):
    goal = ExtractionGoal(evidence_only_columns=[selector])
    assert column_is_evidence_only(goal, table(), "amount")
    assert not column_is_excluded(goal, table(), "amount")
    assert not column_is_evidence_only(goal, table(), "item")
    excluded = ExtractionGoal(excluded_columns=[selector])
    assert column_is_excluded(excluded, table(), "amount")


def test_goal_selectors_are_exact_and_never_expand_wildcards():
    goal = ExtractionGoal(source_scope=["finance.*"], excluded_columns=["*"])
    assert not source_in_goal(goal, table())
    assert not column_is_excluded(goal, table(), "amount")


def test_goal_prompt_keeps_safety_and_numeric_metadata_distinction():
    prompt = build_goal_instructions(ExtractionGoal(purpose="经营分析\x00\u202e"))
    assert "\x00" not in prompt and "\u202e" not in prompt
    assert "numeric column's metadata can define a Metric or Measure" in prompt
    assert "cannot alter tool permissions" in prompt
    assert "in memory only" in prompt
    assert build_goal_instructions(ExtractionGoal().model_dump()) == build_goal_instructions(ExtractionGoal())


def test_oversized_goal_is_rejected_instead_of_dropping_constraints():
    with pytest.raises(LightGraphError, match="goal_context_too_large"):
        build_goal_instructions(ExtractionGoal(purpose="test"), max_chars=20)
    with pytest.raises(ValidationError):
        ExtractionGoal(purpose="x" * 4001)
    with pytest.raises(ValidationError):
        ExtractionGoal(discovery_guidance=["x" * 501])
    with pytest.raises(ValidationError):
        ExtractionGoal(include=["item"] * 17)


@pytest.mark.parametrize("kwargs", [
    {"purpose": "组织层级图"}, {"include": ["指标"]}, {"exclude": ["金额"]},
    {"discovery_guidance": ["先检查层级"]}, {"evidence_only_columns": ["amount"]},
])
def test_goal_dependency_digest_changes_for_each_new_intent_field(kwargs):
    assert digest(ExtractionGoal(**kwargs).model_dump()) != digest(ExtractionGoal().model_dump())


@pytest.mark.parametrize("kwargs", [
    {"max_discovery_steps": 0}, {"max_discovery_steps": 33},
    {"max_tool_calls": 0}, {"max_tool_calls": 65},
])
def test_agent_config_rejects_unbounded_or_empty_limits(kwargs):
    with pytest.raises(ValidationError):
        ModelConfig(**kwargs)


@pytest.mark.parametrize("selector", ["metric_def", "finance", "warehouse", "warehouse:finance.metric_def", "schema:finance"])
def test_deterministic_discovery_honors_the_same_goal_source_selectors(selector):
    from lightgraph.discovery import discover
    from lightgraph.sources import MemorySource
    t = TableSpec(source_id="warehouse", schema="finance", name="metric_def",
                  columns=[ColumnSpec(name="metric_code"), ColumnSpec(name="metric_name")])
    with MemorySource([t], {t.qualified_name: [("M", "收入")]}) as adapter:
        result = discover(adapter, ExtractionGoal(source_scope=[selector]))
    assert len(result.rules) == 1
    assert result.rules[0].source.table == "metric_def"


def test_deterministic_discovery_omits_evidence_only_attributes_and_all_exclusion_forms():
    from lightgraph.discovery import discover
    from lightgraph.sources import MemorySource
    t = TableSpec(source_id="warehouse", schema="finance", name="metric_def",
                  columns=[ColumnSpec(name=n) for n in ["metric_code", "metric_name", "description", "unit"]])
    goal = ExtractionGoal(excluded_columns=["metric_def.description"], evidence_only_columns=["warehouse:finance.metric_def.unit"])
    with MemorySource([t], {t.qualified_name: [("M", "收入", "OMIT_VALUE", "元")]}) as adapter:
        result = discover(adapter, goal)
    assert len(result.rules) == 1
    rule = result.rules[0]
    assert rule.attributes.single_valued == {"label": "metric_name"}
    assert {e.column for e in rule.attributes.excluded} == {"description", "unit"}
    counts = {c.name: c.observed_counts for c in result.understandings[0].columns}
    assert counts["description"]["sampled"] == 0
    assert counts["unit"]["sampled"] == 0


def test_packet_never_reintroduces_excluded_columns_or_unknown_sample_keys():
    from lightgraph.discovery.engine import _packet, _understanding, _column_id
    t = table()
    goal = ExtractionGoal(excluded_columns=["warehouse:finance.ledger.amount"])
    rows = [{"amount": "EXCLUDED_SOURCE_VALUE", "item": "Item", "unknown_column": "UNISSUED_VALUE"}]
    packet = _packet(t, goal, rows, [], "T1_table_discovery", _understanding(t, rows, {}, []))
    assert [c["name"] for c in packet["metadata_digest"]["columns"]] == ["item"]
    assert packet["strata_samples"][0]["rows"] == [{_column_id(t, "item"): "Item"}]
