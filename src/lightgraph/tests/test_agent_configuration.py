"""Harness configuration migration, precedence and secret handling (offline)."""
from pathlib import Path
import json

import pytest
import yaml
from pydantic import ValidationError

from lightgraph.errors import LightGraphError
from lightgraph.pipeline import make_model
from lightgraph.runtime.config import AgentConfig, ModelConfig, RunConfig, load_agent_config, load_config


CANARY = "offline-config-secret-91837"


def write_config(tmp_path, agent=None, inline=None):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    settings = agent if agent is not None else {
        "framework": "agentscope", "name": "metadata-explorer",
        "llm": {"mode": "metadata_only", "model": "offline", "base_url": "https://model.invalid/v1",
                "api_key": CANARY, "stream": True},
    }
    (config_dir / "agent.local.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    run = {"sources": [], "agent_config": "config/agent.local.yaml", "output_dir": "out"}
    if inline is not None:
        run["model"] = inline
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump(run), encoding="utf-8")
    return path


def test_agent_file_resolves_relative_to_run_and_preserves_single_source(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    config = load_config(path)
    assert config.agent_config == str(tmp_path / "config/agent.local.yaml")
    assert config.agent_name == "metadata-explorer"
    assert config.resolved_model.model == "offline"
    assert config.resolved_model.mode == "metadata_only"
    assert config.resolved_model.stream is True
    assert config.resolved_model.api_key.get_secret_value() == CANARY
    # Resolved settings are private; no second competing model section is emitted.
    exported = config.model_dump(mode="json")
    assert "model" not in exported
    assert CANARY not in json.dumps(exported)
    assert CANARY not in repr(config)
    assert CANARY not in config.model_dump_json()
    roundtrip = RunConfig.model_validate(exported).resolve_paths(tmp_path)
    assert roundtrip.resolved_model.model == "offline"


@pytest.mark.parametrize("inline", [{"mode": "disabled"}, {"model": "offline"}, {"api_key": CANARY}])
def test_nonempty_inline_model_and_agent_file_are_rejected(tmp_path, inline):
    with pytest.raises(LightGraphError, match="config_invalid") as exc:
        load_config(write_config(tmp_path, inline=inline))
    assert CANARY not in str(exc.value)


def test_empty_inline_model_is_compatible_with_agent_file(tmp_path):
    config = load_config(write_config(tmp_path, inline={}))
    assert config.resolved_model.model == "offline"


@pytest.mark.parametrize("settings", [
    {"framework": "other", "llm": {"api_key": CANARY}},
    {"llm": {"api_key": CANARY, "timeout_seconds": 0}},
    {"llm": {"api_key": CANARY, "max_output_tokens": 0}},
    {"llm": {"api_key": CANARY, "stream": "invalid"}},
    {"llm": {"api_key": CANARY, "retries": 2}},
])
def test_invalid_configuration_never_echoes_credentials(tmp_path, settings):
    path = write_config(tmp_path, agent=settings)
    with pytest.raises(LightGraphError, match="config_invalid") as exc:
        load_config(path)
    assert CANARY not in str(exc.value)
    with pytest.raises(ValidationError) as exc:
        AgentConfig.model_validate(settings)
    assert CANARY not in str(exc.value)


def test_keys_excluded_from_every_configuration_dump():
    settings = ModelConfig(api_key=CANARY)
    agent = AgentConfig(llm=settings)
    run = RunConfig(sources=[], model=settings)
    for config in (settings, agent, run):
        assert CANARY not in repr(config)
        assert CANARY not in str(config)
        assert CANARY not in repr(config.model_dump())
        assert CANARY not in config.model_dump_json()
    assert "api_key" not in settings.model_dump(include={"api_key"})


def test_missing_or_unloaded_agent_file_is_explicit(tmp_path):
    config = RunConfig(sources=[], agent_config="missing.yaml")
    with pytest.raises(LightGraphError, match="agent_config_unresolved"):
        make_model(config)
    with pytest.raises(LightGraphError, match="agent_config_invalid"):
        load_agent_config(tmp_path / "missing.yaml")


def test_harness_receives_active_settings_and_ignores_retired_loop_controls(tmp_path):
    settings = {"mode": "in_environment", "base_url": "http://gateway.internal/v1", "model": "offline",
        "api_key": CANARY, "api_key_env": "OFFLINE_AGENT_KEY", "allow_insecure_http": True,
        "timeout_seconds": 32, "max_output_tokens": 800, "max_prompt_tokens": 9000,
        "working_context_tokens": 5000, "retries": 0, "max_calls": 9,
        "max_tokens": 22000, "max_discovery_steps": 5, "max_tool_calls": 7}
    config = load_config(write_config(tmp_path, agent={"name": "custom-explorer", "llm": settings}))
    selected = make_model(config)
    for key in ("model", "base_url", "api_key_env", "timeout_seconds", "max_output_tokens",
                "working_context_tokens", "retries", "max_calls"):
        assert getattr(selected, key) == settings[key]
    assert selected.max_input_tokens == 9000
    assert not hasattr(selected, "max_tokens") and not hasattr(selected, "max_discovery_steps")
    assert CANARY not in repr(selected)


def test_legacy_inline_defaults_and_disabled_mode_remain_compatible():
    config = RunConfig(sources=[])
    assert config.resolved_model is config.model
    assert config.resolved_model.stream is False
    assert config.resolved_model.response_format == 'json_schema'
    assert config.resolved_model.allow_insecure_http is False
    assert make_model(config) is None


@pytest.mark.parametrize("separate_agent", [False, True])
@pytest.mark.parametrize("configured_steps, expected_steps", [(None, 16), (8, 8)])
def test_discovery_default_and_explicit_limit_reach_client_without_expanding_shared_budget(
        tmp_path, separate_agent, configured_steps, expected_steps):
    settings = {"mode": "metadata_only", "model": "offline",
                "base_url": "https://model.invalid/v1", "api_key": CANARY,
                "max_calls": 5, "max_tokens": 9000}
    if configured_steps is not None:
        settings["max_discovery_steps"] = configured_steps
    if separate_agent:
        config = load_config(write_config(tmp_path, agent={"llm": settings}))
    else:
        path = tmp_path / "inline.yaml"
        path.write_text(yaml.safe_dump({"sources": [], "model": settings}), encoding="utf-8")
        config = load_config(path)
    selected = make_model(config)
    assert config.resolved_model.max_discovery_steps == expected_steps  # readable migration field
    assert selected.max_calls == 5
    assert not hasattr(selected, "max_discovery_steps") and not hasattr(selected, "max_tokens")


def test_example_agent_and_schema_are_valid():
    root = Path(__file__).resolve().parents[3]
    config = load_agent_config(root / "examples/agent.example.yaml")
    assert config.framework == "deepseek_harness"
    assert config.llm.max_calls == 240
    assert config.llm.max_prompt_tokens == 1048576
    assert not config.llm.api_key.get_secret_value()
    schema = json.loads((root / "src/lightgraph/schemas/RunConfig.schema.json").read_text(encoding="utf-8"))
    assert schema == RunConfig.model_json_schema()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("direct_key", ["", CANARY])
def test_loaded_configuration_drives_transport_and_key_precedence(tmp_path, monkeypatch, stream, direct_key):
    from lightgraph.construction.runner import run_construction
    monkeypatch.setenv("CONFIG_TEST_ENV_KEY", "offline-environment-key")
    config = load_config(write_config(tmp_path, agent={"llm": {
        "mode": "metadata_only", "model": "offline", "base_url": "https://model.invalid/v1",
        "api_key": direct_key, "api_key_env": "CONFIG_TEST_ENV_KEY", "stream": stream}}))
    config.output_dir = str(tmp_path / "output")
    captured = []
    def runtime(goal, **kwargs):
        captured.append(kwargs)
        return {"finish_reason": "completed", "budget": {"calls": 1}}
    report = run_construction(config, runtime=runtime)
    assert report.status == "COMPLETED_WITH_GAPS"
    assert captured[0]["environment"]["CONFIG_TEST_ENV_KEY"] == (direct_key or "offline-environment-key")
    assert not hasattr(captured[0]["options"], "stream")  # protocol belongs to native SDK
    assert CANARY not in (tmp_path / "output/construction/config.yaml").read_text()


def test_internal_http_requires_explicit_configuration(tmp_path):
    settings = {"mode": "metadata_only", "model": "offline", "base_url": "http://gateway.internal/v1"}
    config = load_config(write_config(tmp_path, agent={"llm": settings}))
    with pytest.raises(LightGraphError, match="insecure_model_endpoint"):
        make_model(config)
    settings["allow_insecure_http"] = True
    config = load_config(write_config(tmp_path, agent={"llm": settings}))
    assert make_model(config).base_url == settings["base_url"]


def test_agent_schema_matches_contract_and_cli_exports_it(tmp_path, capsys):
    from lightgraph.cli import parser, run
    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / "src/lightgraph/schemas/AgentConfig.schema.json").read_text(encoding="utf-8"))
    assert schema == AgentConfig.model_json_schema()
    output = tmp_path / "schemas"
    assert run(parser().parse_args(["schema", str(output)])) == 0
    assert json.loads((output / "AgentConfig.schema.json").read_text(encoding="utf-8")) == schema
    assert CANARY not in capsys.readouterr().out


@pytest.mark.parametrize("mode, samples", [
    ("metadata_only", False), ("in_environment", True), ("approved_external", True),
])
def test_separate_agent_mode_is_used_by_cli_even_when_inline_default_is_disabled(tmp_path, monkeypatch, mode, samples):
    from lightgraph import cli, pipeline
    from lightgraph.contracts import RunReport
    settings = {"mode": mode, "model": "offline", "base_url": "https://model.invalid/v1",
                "max_calls": 3, "max_tokens": 9000, "max_discovery_steps": 4, "max_tool_calls": 5}
    path = write_config(tmp_path, agent={"name": "cli-agent", "llm": settings})
    opened = []
    monkeypatch.setattr(cli, "open_sources", lambda config, metadata_only=False: opened.append(metadata_only) or [])
    seen = []
    def run_pipeline(config, sources, *, discover_only):
        assert config.model.mode == "disabled"
        assert config.resolved_model.mode == mode
        assert make_model(config).max_calls == 3
        assert discover_only
        seen.append(mode)
        return RunReport(run_id="offline-config-check")
    monkeypatch.setattr(pipeline, "run_pipeline", run_pipeline)
    assert cli.run(cli.parser().parse_args(["discover", str(path)])) == 0
    assert seen == [mode]
    assert opened == [not samples]


def test_runtime_config_validation_hides_secret_input():
    with pytest.raises(ValidationError) as exc:
        ModelConfig.model_validate({"api_key": CANARY, "timeout_seconds": 0})
    assert CANARY not in str(exc.value)
