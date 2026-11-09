"""Persisted configurations select Harness without preserving an old engine route."""
import copy

import pytest
from pydantic import ValidationError

from lightgraph.contracts import ExtractionGoal
from lightgraph.runtime.config import AgentConfig, ModelConfig, RunConfig


def test_new_project_defaults_match_native_runtime_and_preserve_compression_target(tmp_path):
    from lightgraph.construction.harness import HarnessOptions
    from lightgraph.construction.runner import runtime_options
    from lightgraph.web.workspace import Workspace
    workspace = Workspace(tmp_path / 'projects')
    workspace.create('默认额度', 'defaults')
    config = workspace.get_config('defaults')
    model = config['model']
    expected = {'max_calls': 2000, 'max_prompt_tokens': 1048576,
                'max_output_tokens': 65536, 'timeout_seconds': 800}
    assert {key: model[key] for key in expected} == expected
    assert config['max_run_seconds'] == 60000
    assert model['working_context_tokens'] == 32768
    # Defaults reach the native adapter as well as the form/API contract.
    model.update(mode='in_environment', model='fixture', base_url='http://127.0.0.1:9999/v1')
    selected = runtime_options(RunConfig.model_validate(config))
    direct = HarnessOptions(model='fixture', base_url='http://127.0.0.1:9999/v1')
    assert selected.max_input_tokens == direct.max_input_tokens == 1048576
    assert selected.max_output_tokens == direct.max_output_tokens == 65536
    assert selected.max_calls == direct.max_calls == 2000
    assert selected.max_run_seconds == direct.max_run_seconds == 60000
    assert selected.timeout_seconds == direct.timeout_seconds == 800
    assert selected.working_context_tokens == direct.working_context_tokens == 32768


def test_explicit_saved_limits_and_null_opt_out_survive_new_defaults(tmp_path):
    from lightgraph.web.workspace import Workspace
    workspace = Workspace(tmp_path / 'projects')
    workspace.create('保留旧配置', 'kept')
    config = workspace.get_config('kept')
    explicit = {'max_calls': 40, 'max_prompt_tokens': None, 'max_output_tokens': 4096,
                'timeout_seconds': 60, 'working_context_tokens': 65536}
    config['model'].update(explicit)
    config['max_run_seconds'] = 1800
    workspace.save_config('kept', config)
    saved = Workspace(workspace.root).get_config('kept')
    assert {key: saved['model'][key] for key in explicit} == explicit
    assert saved['max_run_seconds'] == 1800
    assert ModelConfig(max_calls=None, max_prompt_tokens=None).max_calls is None
    assert ModelConfig(max_prompt_tokens=None).max_prompt_tokens is None


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
