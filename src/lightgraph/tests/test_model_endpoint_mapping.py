"""UI/CLI settings are normalized once and translated to native provider options."""
import pytest
from pydantic import ValidationError

from lightgraph.construction.harness import profile_patch
from lightgraph.construction.runner import runtime_options
from lightgraph.runtime.config import ModelConfig, RunConfig
from lightgraph.runtime.model_endpoint import normalize_model_base_url


@pytest.mark.parametrize("entered,expected", [
    (" https://EXAMPLE.com/v1/chat/completions/ ", "https://example.com/v1"),
    ("https://example.com/proxy/v2/chat/completions", "https://example.com/proxy/v2"),
    ("http://localhost:8080/chat/completions", "http://localhost:8080"),
    ("http://localhost:8080", "http://localhost:8080"),
    ("https://example.com/v1///", "https://example.com/v1"),
])
def test_endpoint_normalization_is_idempotent_and_keeps_proxy_prefix(entered, expected):
    model = ModelConfig(base_url=entered, model=" local-model ", api_key_env="  ")
    assert model.base_url == expected
    assert normalize_model_base_url(expected) == expected
    assert model.model == "local-model" and model.api_key_env == "LIGHTGRAPH_API_KEY"
    assert ModelConfig.model_validate_json(model.model_dump_json()).base_url == expected


@pytest.mark.parametrize("url", ["file:///tmp/key", "https://user:key@example.com/v1",
    "https://example.com/v1?api_key=SECRET", "http://localhost:BAD/v1", "not a URL"])
def test_invalid_or_credential_bearing_model_url_is_rejected_without_echo(url):
    with pytest.raises(ValidationError) as failure:
        ModelConfig(base_url=url)
    assert "SECRET" not in str(failure.value)


@pytest.mark.parametrize("name,expected", [("deepseek-chat", "max_tokens"), ("local-model", "max_tokens"),
    ("gpt-4o", "max_tokens"), ("gpt-5.1", "max_completion_tokens"), ("openai/o3-mini", "max_completion_tokens")])
def test_native_profile_receives_real_supported_provider_controls(tmp_path, name, expected):
    settings = ModelConfig(mode="in_environment", base_url="http://localhost:8080/v1/chat/completions",
        model=name, api_key_env=" MODEL_KEY ", max_prompt_tokens=32000, max_output_tokens=4000,
        timeout_seconds=12.5, retries=0, stream_options_include_usage=False)
    selected = runtime_options(RunConfig(sources=[], model=settings))
    profile = profile_patch(tmp_path / "config.yaml", tmp_path, selected)
    provider = next(p for p in profile[-1]["insert"] if p["id"] == "graph-provider")["config"]["providers"]["graph-model"]
    assert provider["baseURL"] == "http://localhost:8080/v1"
    assert provider["apiKeyEnv"] == "MODEL_KEY"
    assert provider["models"][0]["id"] == name
    assert provider["timeoutMs"] == provider["streamIdleTimeoutMs"] == 12500
    assert provider["retryPolicy"]["maxRetries"] == 0
    assert provider["compat"] == {"maxTokensField": expected, "supportsStore": False,
                                  "supportsUsageInStreaming": False}
