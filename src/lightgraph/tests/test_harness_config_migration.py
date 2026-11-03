"""Persisted configurations select Harness without preserving an old engine route."""
import copy

import pytest
from pydantic import ValidationError

from lightgraph.contracts import ExtractionGoal
from lightgraph.runtime.config import AgentConfig, ModelConfig, RunConfig


def test_old_agent_framework_is_migrated_without_mutating_input():
    original = {"framework": "agentscope", "llm": {"model": "local-model", "max_calls": 80}}
    before = copy.deepcopy(original)
    config = AgentConfig.model_validate(original)
    assert config.framework == "deepseek_harness"
    assert config.model_dump()["framework"] == "deepseek_harness"
    assert config.llm.max_calls == 80
    assert original == before


def test_framework_schema_only_advertises_harness():
    field = AgentConfig.model_json_schema()["properties"]["framework"]
    assert field["const"] == "deepseek_harness"
    assert AgentConfig(llm={}).framework == "deepseek_harness"
    with pytest.raises(ValidationError):
        AgentConfig(framework="other-engine", llm={})


def test_default_goal_is_generic_and_does_not_supply_business_vocabulary():
    goal = ExtractionGoal()
    assert goal.target == "graph"
    assert goal.goal_id == "user_graph"
    assert goal.entity_kinds == goal.relation_kinds == goal.exclude == []
    assert "经营分析" not in goal.purpose
    assert "指标" not in goal.purpose
    config = RunConfig(sources=[], goal={"purpose": "发现设备、部件与安装关系", "exclude": ["个人信息"]})
    assert config.goal.purpose == "发现设备、部件与安装关系"
    assert config.goal.exclude == ["个人信息"]
    with pytest.raises(ValidationError):
        ExtractionGoal(target="ontology")


def test_legacy_limits_round_trip_without_changing_active_call_budget():
    model = ModelConfig(max_calls=None, max_discovery_steps=8, max_tool_calls=12, max_prompt_bytes=1024)
    assert model.max_calls is None
    assert model.max_tokens is None
    assert model.model_dump()["max_discovery_steps"] == 8
    assert ModelConfig(max_calls=0).max_calls == 0


def test_migrated_config_does_not_serialize_secrets():
    config = AgentConfig(framework="agentscope", llm={"api_key": "must-not-appear", "max_calls": 20})
    assert "must-not-appear" not in config.model_dump_json()
