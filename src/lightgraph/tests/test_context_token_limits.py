"""Harness route capacities, input estimates and output reservations use tokens."""
import pytest
from pydantic import ValidationError
from lightgraph.construction.harness import profile_patch
from lightgraph.construction.runner import runtime_options
from lightgraph.runtime.config import RunConfig


def config(**settings):
    return RunConfig(sources=[], model={"mode": "approved_external", "model": "offline",
        "base_url": "https://example.invalid/v1", **settings})


@pytest.mark.parametrize("prompt,window,output,expected", [
    (8000,10000,3000,7000), (5000,10000,3000,5000), (None,10000,3000,7000),
    (8000,None,3000,8000), (None,None,3000,None)])
def test_effective_input_reserves_output_without_a_hidden_null_limit(prompt, window, output, expected):
    selected = runtime_options(config(max_prompt_tokens=prompt, context_window_tokens=window,
                                      max_output_tokens=output))
    assert selected.max_input_tokens == expected
    assert selected.max_output_tokens == output


def test_roundtrip_preserves_legacy_settings_without_using_them_as_harness_guards(tmp_path):
    run = config(max_calls=240, max_tokens=1, max_prompt_bytes=1024, max_response_bytes=2048,
        max_prompt_tokens=1048576, context_window_tokens=1114112, max_output_tokens=65536,
        working_context_tokens=24576)
    selected = runtime_options(RunConfig.model_validate_json(run.model_dump_json()))
    assert selected.max_input_tokens == 1048576 and selected.max_output_tokens == 65536
    assert selected.working_context_tokens == 24576 and selected.max_calls == 240
    assert not hasattr(selected, "max_tokens") and not hasattr(selected, "max_prompt_bytes")
    profile = profile_patch(tmp_path / "config.yaml", tmp_path, selected)
    budget = next(p for p in profile[-1]["insert"] if p["id"] == "graph-run-budget")["config"]
    assert budget["maxInputTokens"] == 1048576
    with pytest.raises(ValidationError, match="context_window_must_exceed_output_reservation"):
        config(context_window_tokens=4096, max_output_tokens=4096)


def test_disabling_input_guard_and_proactive_compaction_are_independent(tmp_path):
    selected = runtime_options(config(max_prompt_tokens=None, context_window_tokens=None,
                                      working_context_tokens=None))
    profile = profile_patch(tmp_path / "config.yaml", tmp_path, selected)
    plugins = {p["id"]: p["config"] for p in profile[-1]["insert"] if "config" in p}
    assert plugins["graph-run-budget"]["maxInputTokens"] is None
    assert plugins["graph-compaction"]["auto"] is False
