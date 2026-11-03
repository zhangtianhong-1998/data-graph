"""Harness composition/accounting tests; no external model endpoint is contacted."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
import yaml

from lightgraph.construction.harness import HarnessOptions, profile_patch, prepare_runtime, run_goal


def options(**changes):
    return HarnessOptions(model="local-model", base_url="http://127.0.0.1:9/v1", **changes)


def test_profile_is_domain_only_and_uses_working_target(tmp_path):
    patch = profile_patch(tmp_path / "config.yaml", tmp_path, options(),
                          ("sdk-app-startup", "persistent-bash", "surprise-web-tool", "system-prompt"))
    assert {p["id"] for p in patch if p.get("disabled")} == {"persistent-bash", "surprise-web-tool"}
    plugins = {p["id"]: p for p in patch[-1]["insert"]}
    skill = plugins["graph-skill-filesystem"]["config"]
    assert skill["includeDefaultRoots"] is False
    assert skill["customSkillDirs"][0].endswith("construction/skills")
    compaction = plugins["graph-compaction"]["config"]
    assert compaction["thresholdRatio"] * (1048576 + 65536) == 32768
    assert compaction["retainTokens"] < 32768
    provider = plugins["graph-provider"]["config"]["providers"]["graph-model"]
    assert provider["api"] == "openai-completions"
    assert provider["timeoutMs"] == 600000
    assert provider["retryPolicy"]["maxRetries"] == 1


def test_secrets_are_not_written_into_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DSN", "postgres://private_password")
    monkeypatch.setenv("OPENAI_API_KEY", "private_api_key")
    config = tmp_path / "config.yaml"
    config.write_text("goal: demo\n")
    patch = prepare_runtime(config, tmp_path, options(), inspect_base=False)
    text = patch.read_text()
    assert "private_password" not in text and "private_api_key" not in text
    assert "SOURCE_DSN" not in text and "OPENAI_API_KEY" in text


def test_run_uses_sdk_sessions_without_legacy_loop(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("goal: demo\n")
    seen = {}
    @dataclass
    class Notification:
        method: str = "session.event"
    class SDK:
        def __init__(self, **kwargs): seen.update(kwargs)
        def __enter__(self): return self
        def __exit__(self, *args): seen["closed"] = True
        def run(self, prompt, session_id, on_notification):
            seen["prompt"] = prompt
            on_notification(Notification())
            return SimpleNamespace(session_id=session_id, final_response="candidate saved", finish_reason="completed")
    events = []
    result = run_goal("发现目标涉及的实体和关系", config, tmp_path, options(),
                      session_id="resume-1", sdk_factory=SDK, on_event=events.append)
    assert seen["profile"] == "sdk-minimal"
    assert seen["provider"] == "graph-model"
    assert seen["dsh_home"] == str(tmp_path / "harness")
    assert seen["prompt"].startswith("/build-graph\n")
    assert result["session_id"] == "resume-1"
    assert result["graph_acceptance"] == "read_published_artifact"
    assert seen["closed"] and events == [{"method": "session.event"}]


@pytest.mark.parametrize("cancel", [True, False])
def test_cancel_and_deadline_close_blocked_runtime_and_preserve_history(tmp_path, cancel):
    config = tmp_path / "config.yaml"
    config.write_text("goal: demo\n")
    history = tmp_path / "history.yaml"
    history.write_text("saved: true\n")
    closed = threading.Event()
    class SDK:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
        def close(self): closed.set()
        def run(self, *args, **kwargs):
            assert closed.wait(3)
            raise RuntimeError("transport closed")
    started = time.monotonic()
    result = run_goal("test", config, tmp_path, options(max_run_seconds=0.15),
                      sdk_factory=SDK, cancel_requested=lambda: cancel and time.monotonic() - started > 0.05)
    assert result["finish_reason"] == ("cancelled" if cancel else "time_budget_exhausted")
    assert closed.is_set() and history.read_text() == "saved: true\n"


def test_http_mcp_keeps_authorization_out_of_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPH_AUTH", "private-bridge-token")
    config = tmp_path / "config.yaml"
    config.write_text("goal: demo\n")
    patch = prepare_runtime(config, tmp_path, options(), inspect_base=False,
                            mcp_url="http://127.0.0.1:8123/mcp", mcp_token_env="GRAPH_AUTH")
    assert "private-bridge-token" not in patch.read_text()
    profile = profile_patch(config, tmp_path, options(),
                            mcp_url="http://127.0.0.1:8123/mcp", mcp_token_env="GRAPH_AUTH")
    bridge = next(row for row in profile[-1]["insert"] if row["id"] == "graph-domain-tools")["config"]
    assert bridge["transport"] == "streamable-http" and "command" not in bridge
    with pytest.raises(ValueError):
        profile_patch(config, tmp_path, options(), mcp_url="http://example.org/mcp", mcp_token_env="GRAPH_AUTH")


def test_budget_plugin_counts_all_dispatches_and_denies_foreign_tools(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the Harness plugin")
    module = Path(__file__).parents[1] / "construction/runtime/run-budget.mjs"
    script = tmp_path / "test.mjs"
    script.write_text("""
import { apply } from MODULE
const hooks = new Map()
let guard
const ctx = { on: (name, fn) => hooks.set(name, fn), tokenMeter: {estimateMessage:()=>0}, tools: {
  guard: fn => { guard = fn }, schemas: () => [
    { name: 'mcp__graph__describe_sources' }, { name: 'bash' }, { name: 'skill' }]
} }
apply(ctx, { maxCalls: 2, maxRunSeconds: 60, output: OUTPUT })
let allowed
await hooks.get('agent/pre-step')({agent:{ctx:{tools:{restrict: x => {allowed=x.allow}}}}}, async()=>{})
let network = 0
async function* next() { network++; yield {type:'text', text:'ok'} }
for (const purpose of ['agent', 'summary']) {
  for await (const chunk of hooks.get('llm/stream')({purpose}, next)) {}
}
let code
try { for await (const chunk of hooks.get('llm/stream')({purpose:'retry'}, next)) {} }
catch (error) { code=error.code }
console.log(JSON.stringify({network, code, allowed, denied:guard({name:'bash'})}))
""".replace("MODULE", json.dumps(module.as_uri())).replace("OUTPUT", json.dumps(str(tmp_path / "budget.yaml"))))
    run = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=20, check=True)
    result = json.loads(run.stdout)
    assert result == {"network": 2, "code": "construction_call_budget_exhausted",
                      "allowed": ["mcp__graph__describe_sources", "skill"],
                      "denied": "construction_tool_not_allowed"}
    assert yaml.safe_load((tmp_path / "budget.yaml").read_text())["calls"] == 2


@pytest.mark.parametrize("change", [dict(max_calls=0), dict(max_calls=1.5), dict(retries=True),
                                    dict(base_url="http://user:password@localhost/v1"),
                                    dict(api_key_env="bad;command")])
def test_invalid_runtime_settings_rejected(change):
    with pytest.raises(ValueError):
        HarnessOptions(**{"model": "m", "base_url": "http://localhost/v1", **change})


def test_preparation_time_reduces_runtime_budget(tmp_path, monkeypatch):
    import lightgraph.construction.harness as harness
    from lightgraph.errors import LightGraphError
    config = tmp_path / "config.yaml"
    config.write_text("goal: demo\n")
    original = harness.prepare_runtime
    def prepare(*args, **kwargs):
        time.sleep(0.03)
        return original(*args, **kwargs)
    monkeypatch.setattr(harness, "prepare_runtime", prepare)
    with pytest.raises(LightGraphError, match="construction_time_budget_exhausted"):
        run_goal("test", config, tmp_path, options(max_run_seconds=0.01),
                 sdk_factory=lambda **kwargs: pytest.fail("expired preparation must not start runtime"))


def test_real_runtime_requires_parent_owned_mcp_instead_of_reopening_summary(tmp_path):
    from lightgraph.errors import LightGraphError
    config = tmp_path / "config.yaml"
    config.write_text("goal: sanitized launch summary\n")
    with pytest.raises(LightGraphError, match="construction_mcp_required"):
        run_goal("goal", config, tmp_path, options())


def test_windows_bootstrap_does_not_execute_batch_shim(tmp_path):
    from importlib.util import module_from_spec, spec_from_file_location
    path = Path(__file__).parents[3] / "scripts/bootstrap_harness.py"
    spec = spec_from_file_location("bootstrap_harness", path)
    bootstrap = module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    shim = tmp_path / "space in path/pnpm.cmd"
    script = shim.parent / "node_modules/corepack/dist/pnpm.js"
    script.parent.mkdir(parents=True)
    script.write_text("// test fixture")
    assert bootstrap.pnpm_command("node.exe", str(shim), windows=True) == ["node.exe", str(script)]


@pytest.mark.parametrize("libc", ["glibc", "musl"])
def test_native_addon_matches_linux_libc_manifest(tmp_path, libc):
    from lightgraph.construction.launcher import required_addon
    folder = tmp_path / "native/system/packages/linux-x64"
    folder.mkdir(parents=True)
    (folder / "prebuilds.json").write_text(json.dumps({"binaries": [
        {"tool": "flock", "libc": kind, "path": f"bin/{kind}/system.node"} for kind in ("glibc", "musl")]}))
    assert required_addon(tmp_path, "linux", "x64", libc) == folder / f"bin/{libc}/system.node"
    assert required_addon(tmp_path, "win32", "x64") is None


def test_input_guard_preserves_native_usage_calibration_and_prices_summary_separately(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the Harness plugin")
    module = Path(__file__).parents[1] / "construction/runtime/run-budget.mjs"
    script = tmp_path / "guard.mjs"
    script.write_text("""
import { apply } from MODULE
const hooks = new Map()
const ctx = {on:(name,fn)=>hooks.set(name,fn),tools:{guard:()=>{}},agents:{get:()=>({session:{}})},
  tokenMeter:{estimateMessage:()=>10,measure:()=>({totalTokens:5000,baseline:{kind:'usage'}})}}
apply(ctx,{maxCalls:3,maxRunSeconds:60,maxInputTokens:1000,output:OUTPUT})
let count=0,code
async function* next(){count++; yield {type:'text',text:'ok'}}
const request={sessionId:'session',messages:[{role:'user',content:[{type:'text',text:'test'}]}]}
try { for await (const chunk of hooks.get('llm/stream')(request,next)){} }
catch(error){code=error.code}
for await (const chunk of hooks.get('llm/stream')({...request,purpose:'compaction'},next)){}
console.log(JSON.stringify({count,code}))
""".replace("MODULE", json.dumps(module.as_uri())).replace("OUTPUT", json.dumps(str(tmp_path / "budget.yaml"))))
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=20, check=True)
    assert json.loads(result.stdout) == {"count": 1, "code": "construction_input_limit_exceeded"}
    budget = yaml.safe_load((tmp_path / "budget.yaml").read_text())
    assert budget["calls"] == 1
    assert budget["input_diagnostic"]["measurement"] == "native_usage_calibrated_estimate"
    assert budget["input_diagnostic"]["estimated_input_tokens"] == 5000
