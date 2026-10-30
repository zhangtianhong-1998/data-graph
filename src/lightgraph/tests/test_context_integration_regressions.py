"""Regression outcomes for selective discovery and automatic reference checks."""
from __future__ import annotations

import json

import httpx

from lightgraph.contracts import ColumnSpec, IdentitySpec, RuleSpec, SourceRef, TableSpec, canonical
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.workflow import discover_references_with_agent, discover_with_agent
from lightgraph.documents import load_document
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response


def _table(name, source="s", columns=("key", "label")):
    return TableSpec(source_id=source, name=name,
                     columns=[ColumnSpec(name=column) for column in columns])


def _reference_packet(packet):
    ids = {column["name"]: column["id"] for column in packet["metadata_digest"]["columns"]}
    return {"task": packet["task"], "proposals": [{
        "kind": "reference",
        "claim": {
            "source_entity": "left", "target_entity": "right",
            "source_columns": [ids["key"]], "target_columns": [ids["ref"]],
        },
        "evidence_cited": [ids["ref"]],
    }]}


def _endpoints(left, right):
    # These are already validated endpoint contracts supplied to T2. The tests
    # exercise new relationship discovery, not endpoint discovery or publication.
    return [
        RuleSpec(rule_id=rid, kind="column_group_entity",
                 source=SourceRef(source_id=table.source_id, schema=table.schema_name,
                                  table=table.name),
                 identity=IdentitySpec(key_columns=[column]), status="published",
                 validation_ref="validation_" + rid)
        for rid, table, column in (("left", left, "key"), ("right", right, "code"))
    ]


def _global_reference_document():
    return load_document("reference.md", text=(
        "---\nid: reference_definition\npurpose: data_description\n"
        "---\n# 关系\n引用。"))


def test_transient_model_failure_is_retried_without_redoing_completed_table(tmp_path, monkeypatch):
    tables = [_table("a"), _table("b")]
    calls = []

    def model(fail_b):
        def handle(request):
            _, packet = input_packet(request)
            calls.append((packet["task"], packet["scope"]["table"]))
            if packet["task"] == "T2_cross_table":
                return response({"task": packet["task"], "abstain": True})
            if fail_b and packet["scope"]["table"] == "b":
                return httpx.Response(503, text="synthetic temporary provider failure")
            return response(group(packet, "key", "label"))
        model=client(monkeypatch, handle)
        # Two provider failures are charged conservatively for the complete
        # schema-bearing prompt. This case tests retry state, not token limits.
        from dataclasses import replace
        model.budget.limits=replace(model.budget.limits,llm_tokens=200000)
        return model

    config = RunConfig(sources=[], output_dir=str(tmp_path / "out"))
    with MemorySource(tables, {"public.a": [("A", "Alpha")],
                               "public.b": [("B", "Beta")]}) as source:
        first = run_pipeline(config, [source], model=model(True))
        assert first.rules_published
        assert first.status == "PARTIAL", first.issues
        assert any(issue["code"] == "model_http_error" for issue in first.issues)
        calls.clear()
        second = run_pipeline(config, [source], model=model(False))
        assert [name for stage, name in calls if stage == "T0_discovery"] == ["b"]
        assert not any(issue["code"] == "no_confirmable_entity_group" for issue in second.issues)
        calls.clear()
        third = run_pipeline(config, [source], model=model(False))
        assert calls==[('T2_cross_table','a'),('T2_cross_table','b')]
        assert third.counts["reused_rules"] == 2
    with GraphStore(tmp_path / "out" / "graph.sqlite") as store:
        business = [node for node in store.view({"public"})["nodes"] if node["layer"] == "business"]
        assert {node["label"] for node in business} == {"Alpha", "Beta"}


def test_automatic_cross_source_verification_honors_512_byte_memory_budget(monkeypatch):
    left = _table("mapping", "A", ("key", "ref"))
    right = _table("codes", "B", ("code",))
    def propose(request):
        packet = _reference_packet(input_packet(request)[1])
        # A valid business relation must reach the memory check; the metadata
        # default verified_reference is now correctly rejected before querying.
        packet['proposals'][0]['claim']['relation_kind'] = 'catalog_link'
        packet['semantic_schema'] = {'relations': [{'name':'catalog_link',
            'description':'A business reference between catalog objects.',
            'source_kinds':['ReportItem'], 'target_kinds':['ReportItem']}]}
        return response(packet)
    model = client(monkeypatch, propose)
    budget = BudgetAllocator(Cost(db_cu=20, llm_calls=4, llm_tokens=100000,
                                  memory_bytes=512, seconds=60))
    model.budget = budget
    # Individually valid values fit normal source limits, but the distinct-key
    # working set cannot fit the task's 512-byte comparison budget.
    with MemorySource([left], {"public.mapping": [
        (str(index), str(index) + "x" * 100) for index in range(20)
    ]}) as a, MemorySource([right], {"public.codes": [
        (str(index) + "x" * 100,) for index in range(20)
    ]}) as b:
        result = discover_references_with_agent(
            a, _endpoints(left, right), documents=[_global_reference_document()],
            model=model, budget=budget, source_adapters={"A": a, "B": b})
        assert not result.rules
        assert any(issue["code"] in {"cross_source_reference_incomplete", "memory_budget_exhausted"}
                   for issue in result.issues)
        assert a.stats.queries + b.stats.queries > 0


def test_automatic_target_reads_cannot_skip_target_document_conditions(monkeypatch):
    left = _table("mapping", "A", ("key", "ref"))
    right = _table("codes", "B", ("code",))
    condition = load_document("target.md", text=(
        "---\nid: target_conditions\npurpose: data_description\n"
        "applies_to: [B:public.codes]\n---\n"
        "# 限制\n本任务暂不允许将这些编码作为引用端点。"))
    seen_target_conditions = []

    def handle(request):
        _, packet = input_packet(request)
        visible = any(section["doc_id"] == condition.document_id
                      for section in packet["document_snippets"])
        seen_target_conditions.append(visible)
        # The mock's final decision changes only after the target's actual
        # condition is supplied. Adding an unseen dependency alone is insufficient.
        return response({"task": packet["task"], "abstain": True}
                        if visible else _reference_packet(packet))

    model = client(monkeypatch, handle)
    with MemorySource([left], {"public.mapping": [("M", "X")]}) as a, \
         MemorySource([right], {"public.codes": [("X",)]}) as b:
        result = discover_references_with_agent(
            a, _endpoints(left, right),
            documents=[_global_reference_document(), condition], model=model,
            source_adapters={"A": a, "B": b})
        assert not result.rules
        if not any(seen_target_conditions):
            # Explicit deferral is also safe when the scheduler cannot fit or
            # revisit newly required context; it must precede remote reads.
            assert any("context" in issue["code"] for issue in result.issues)
            assert b.stats.queries == 0


def test_forced_rediscovery_includes_t2_for_unchanged_tables(tmp_path, monkeypatch):
    tables = [_table("a"), _table("b")]
    calls = []

    def handle(request):
        _, packet = input_packet(request)
        calls.append((packet["task"], packet["scope"]["table"]))
        return response({"task": packet["task"], "abstain": True}
                        if packet["task"] == "T2_cross_table"
                        else group(packet, "key", "label"))

    model = client(monkeypatch, handle)
    config = RunConfig(sources=[], output_dir=str(tmp_path / "out"))
    with MemorySource(tables, {"public.a": [("A", "Alpha")],
                               "public.b": [("B", "Beta")]}) as source:
        assert len(run_pipeline(config, [source], model=model).rules_published) == 2
        added = load_document("a.md", text=(
            "---\nid: a_only\npurpose: data_description\n"
            "applies_to: [s:public.a]\n---\n# 定义\n编码标识业务项目。"))
        calls.clear()
        config.rediscover = True
        report = run_pipeline(config, [source], model=model, documents=[added])
        assert report.rules_published
        assert {name for stage, name in calls if stage == "T0_discovery"} == {"a", "b"}
        assert {name for stage, name in calls if stage == "T2_cross_table"} == {"a", "b"}


def test_requested_last_table_is_not_starved_by_64_unrelated_catalog_entries(monkeypatch):
    tables = [_table(f"t{index:03}") for index in range(65)]
    for table in tables:
        # Detailed declarations force prompt-based shrinking below the 64-table
        # schema limit. They are metadata, not observations or model instructions.
        table.comment = "Opaque identifier and human-readable label. " * 40
    calls = []
    visible_counts = []

    def handle(request):
        payload, packet = input_packet(request)
        calls.append(packet["scope"]["table"])
        visible_counts.append(len(packet["catalog"]))
        assert packet["catalog"][0]["name"] == "t064"
        assert 1 <= len(packet["catalog"]) < 64
        assert len(canonical({"messages": payload["messages"], "tools": payload["tools"]}).encode()) <= model.config.max_prompt_bytes
        context = json.loads(payload["messages"][1]["content"])["call_context"]
        assert context["authorization_scope"][0] == {
            "source_id": "s", "schema": "public", "table": "t064", "columns": ["key", "label"]}
        assert f"{len(packet['catalog'])} of 65" in context["subtask"]
        return response(group(packet, "key", "label"))

    model = client(monkeypatch, handle)
    original_limit = model.config.max_prompt_bytes
    with MemorySource(tables, {table.qualified_name: [("K", table.name)] for table in tables}) as source:
        result = discover_with_agent(source, model=model, table_refs={"public.t064"})
        assert calls == ["t064"], result.issues
        assert len(result.rules) == 1 and result.rules[0].source.table == "t064"
        assert model.config.max_prompt_bytes == original_limit
        assert result.stats["catalog_tables"] == 65
        assert result.stats["catalog_window_min"] == result.stats["catalog_window_max"] == visible_counts[0]
        assert result.stats["catalog_windows_partial"] == 1
