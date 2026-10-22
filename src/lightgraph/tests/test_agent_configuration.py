"""Separate AgentScope configuration, precedence and secret handling (offline)."""
from pathlib import Path
from types import SimpleNamespace
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


def test_model_client_receives_all_agent_settings_and_budgets(tmp_path, monkeypatch):
    import lightgraph.discovery.llm as llm
    settings = {
        "mode": "in_environment", "base_url": "http://gateway.internal/v1", "model": "offline",
        "api_key": CANARY, "api_key_env": "OFFLINE_AGENT_KEY", "stream": True,
        "stream_options_include_usage": False, "allow_insecure_http": True,
        "timeout_seconds": 32, "max_output_tokens": 800, "max_prompt_bytes": 9000,
        "max_response_bytes": 10000, "retries": 0, "max_calls": 9,
        "max_tokens": 22000, "max_discovery_steps": 5, "max_tool_calls": 7,
    }
    config = load_config(write_config(tmp_path, agent={"name": "custom-explorer", "llm": settings}))
    monkeypatch.setattr(llm, "ModelConfig", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(llm, "StructuredModelClient", lambda config, budget: SimpleNamespace(config=config, budget=budget))
    client = make_model(config)
    for key, value in settings.items():
        if key in ("mode", "max_calls", "max_tokens", "api_key"):
            continue
        assert getattr(client.config, key) == value
    assert client.config.api_key.get_secret_value() == CANARY
    assert client.config.agent_name == "custom-explorer"
    assert client.config.allow_source_samples is True
    assert client.budget.limits.llm_calls == 9
    assert client.budget.limits.llm_tokens == 22000


def test_legacy_inline_defaults_and_disabled_mode_remain_compatible():
    config = RunConfig(sources=[])
    assert config.resolved_model is config.model
    assert config.resolved_model.stream is False
    assert config.resolved_model.allow_insecure_http is False
    assert make_model(config) is None


def test_example_agent_and_schema_are_valid():
    root = Path(__file__).resolve().parents[3]
    config = load_agent_config(root / "examples/agent.example.yaml")
    assert config.framework == "agentscope"
    assert config.llm.stream is True
    assert not config.llm.api_key.get_secret_value()
    schema = json.loads((root / "src/lightgraph/schemas/RunConfig.schema.json").read_text(encoding="utf-8"))
    assert schema == RunConfig.model_json_schema()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("direct_key", ["", CANARY])
def test_loaded_configuration_drives_transport_and_key_precedence(tmp_path, monkeypatch, stream, direct_key):
    import httpx
    monkeypatch.setenv("CONFIG_TEST_ENV_KEY", "offline-environment-key")
    config = load_config(write_config(tmp_path, agent={"llm": {
        "mode": "metadata_only", "model": "offline", "base_url": "https://model.invalid/v1",
        "api_key": direct_key, "api_key_env": "CONFIG_TEST_ENV_KEY", "stream": stream,
        "stream_options_include_usage": False,
    }}))
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert request.headers["Authorization"] == "Bearer " + (direct_key or "offline-environment-key")
        assert payload["stream"] is stream
        assert "stream_options" not in payload
        if stream:
            events = [
                {"choices": [{"index": 0, "delta": {"content": '{"ok":true}'}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            content = "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
            return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}]})
    client = make_model(config)
    client.transport = httpx.MockTransport(handler)
    result = client._request([{"role": "user", "content": "synthetic"}], lambda message: json.loads(message["content"]))
    assert result == {"ok": True}
    assert len(calls) == 1
    assert client.config.agent_name == "lightgraph-discovery"
    assert CANARY not in client.config.model_dump_json()


def test_internal_http_requires_explicit_configuration(tmp_path):
    settings = {"mode": "metadata_only", "model": "offline", "base_url": "http://gateway.internal/v1"}
    config = load_config(write_config(tmp_path, agent={"llm": settings}))
    with pytest.raises(LightGraphError, match="insecure_model_endpoint"):
        make_model(config)
    settings["allow_insecure_http"] = True
    config = load_config(write_config(tmp_path, agent={"llm": settings}))
    assert make_model(config).config.allow_insecure_http is True


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
    monkeypatch.setattr(cli, "open_sources", lambda config, metadata_only=False: [])
    seen = []
    def run_pipeline(config, sources, *, rules, documents, model, discover_only):
        assert config.model.mode == "disabled"
        assert config.resolved_model.mode == mode
        assert model is not None
        assert model.allow_source_samples is samples
        assert model.config.agent_name == "cli-agent"
        assert model.config.max_discovery_steps == 4
        assert model.config.max_tool_calls == 5
        assert model.budget.limits.llm_calls == 3
        assert model.budget.limits.llm_tokens == 9000
        assert discover_only
        seen.append(mode)
        return RunReport(run_id="offline-config-check")
    monkeypatch.setattr(pipeline, "run_pipeline", run_pipeline)
    assert cli.run(cli.parser().parse_args(["discover", str(path)])) == 0
    assert seen == [mode]


def test_transport_config_validation_hides_secret_input():
    from lightgraph.discovery.llm import ModelConfig as TransportConfig
    with pytest.raises(ValidationError) as exc:
        TransportConfig.model_validate({"api_key": CANARY})
    assert CANARY not in str(exc.value)
