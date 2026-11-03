"""Old response-format settings migrate without reviving the retired JSON loop."""
import pytest
from pydantic import ValidationError
from lightgraph.construction.harness import profile_patch
from lightgraph.construction.runner import runtime_options
from lightgraph.runtime.config import AgentConfig, ModelConfig, RunConfig

@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
def test_legacy_response_setting_is_readable_but_harness_uses_native_tools(mode, tmp_path):
    settings = ModelConfig(mode="metadata_only", base_url="https://model.invalid/v1", model="offline",
                           response_format=mode, api_key="UNRETAINED_CONFIG_KEY")
    assert AgentConfig(llm=settings).llm.response_format == mode
    options = runtime_options(RunConfig(sources=[], model=settings))
    profile = profile_patch(tmp_path / "config.yaml", tmp_path, options)
    provider = next(p for p in profile[-1]["insert"] if p["id"] == "graph-provider")["config"]["providers"]["graph-model"]
    assert provider["api"] == "openai-completions"
    assert "response_format" not in provider
    assert not hasattr(options, "response_format")
    assert "UNRETAINED_CONFIG_KEY" not in settings.model_dump_json() + repr(settings) + repr(profile)
    with pytest.raises(ValidationError):
        ModelConfig(response_format="auto")
