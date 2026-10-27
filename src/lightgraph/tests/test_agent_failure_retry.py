"""Incomplete Agent transport/runtime work must remain pending across runs."""
import pytest

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.errors import LightGraphError
from lightgraph.pipeline import _context_versions, run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response


@pytest.mark.parametrize("failure_code", [
    "model_stream_invalid",
    "model_stream_incomplete",
    "agent_source_tracing_forbidden",
    "agent_source_persistence_forbidden",
    "agent_tracing_forbidden",
    "agent_nontext_tool_response",
    "agent_sync_entrypoint_inside_event_loop",
    "invalid_tool_schema",
])
def test_agent_failure_keeps_only_affected_table_pending_until_next_run(tmp_path, monkeypatch, failure_code):
    tables = [TableSpec(source_id="s", name=name, columns=[ColumnSpec(name="key"), ColumnSpec(name="label")])
              for name in ("a", "b")]
    attempts = []

    def model(fail_b):
        def handler(request):
            _, packet = input_packet(request)
            if packet["task"] == "T2_cross_table":
                return response({"task": packet["task"], "abstain": True})
            return response(group(packet, "key", "label"))
        result = client(monkeypatch, handler)
        original_explore = result.explore
        def explore(packet, executor, **kwargs):
            attempts.append((packet["task"], packet["scope"]["table"]))
            if fail_b and packet["task"] == "T0_discovery" and packet["scope"]["table"] == "b":
                raise LightGraphError(failure_code)
            return original_explore(packet, executor, **kwargs)
        result.explore = explore
        return result

    config = RunConfig(sources=[], output_dir=str(tmp_path / "out"))
    rows = {"public.a": [("A", "Alpha")], "public.b": [("B", "Beta")]}
    with MemorySource(tables, rows) as source:
        first = run_pipeline(config, [source], model=model(True))
        assert first.status == "PARTIAL", first.issues
        assert first.rules_published
        assert any(issue["code"] == failure_code for issue in first.issues)
        assert [name for stage, name in attempts if stage == "T0_discovery"] == ["a", "b"]
        with GraphStore(tmp_path / "out/graph.sqlite") as store:
            assert set(_context_versions(store.current_versions("discovery:s"))) == {"public.a"}

        attempts.clear()
        second = run_pipeline(config, [source], model=model(False))
        assert second.status in {"COMPLETED", "COMPLETED_WITH_GAPS"}, second.issues
        assert [name for stage, name in attempts if stage == "T0_discovery"] == ["b"]
        assert not any(issue["code"] == failure_code for issue in second.issues)

        attempts.clear()
        third = run_pipeline(config, [source], model=model(False))
        assert attempts==[('T2_cross_table','a'),('T2_cross_table','b')]  # only unresolved relations retry
        assert third.counts["reused_rules"] == 2

    with GraphStore(tmp_path / "out/graph.sqlite") as store:
        assert set(_context_versions(store.current_versions("discovery:s"))) == {"public.a", "public.b"}
        business = [node for node in store.view({"public"})["nodes"] if node["layer"] == "business"]
        assert {node["label"] for node in business} == {"Alpha", "Beta"}
