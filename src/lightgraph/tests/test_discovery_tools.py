from __future__ import annotations

import json
import pickle

import pytest

from lightgraph.contracts import ColumnSpec, ExtractionGoal, ForeignKeySpec, QueryLimits, TableSpec
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.tools import ToolExecutor
from lightgraph.documents import load_document
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.sources.memory import MemorySource


def fixture(*, allowed=True, goal=None, rows=None, limits=None, budget=None, tables=None):
    tables = tables or [TableSpec(source_id="s", name="items", columns=[ColumnSpec(name=name) for name in
                       ("code", "label", "password", "amount")])]
    adapter = MemorySource(tables, rows or {"public.items": [("A", "销售费用", "CREDENTIAL_CANARY", 10),
                                                          ("A", "销售费用", "CREDENTIAL_CANARY", 20),
                                                          ("B", "销售成本", "CREDENTIAL_CANARY", 30)]}, limits=limits)
    executor = ToolExecutor(adapter, goal or ExtractionGoal(), budget or BudgetAllocator(), allow_source_samples=allowed)
    return adapter, executor


def test_safe_sql_reads_actual_authorized_values_with_bound_parameter():
    adapter, tools = fixture()
    response = tools.execute("execute_readonly_sql", {"sql": "SELECT DISTINCT code, label FROM public.items WHERE code = :code", "parameters": {"code": "A"}})
    assert response["status"] == "ok"
    assert response["data"] == {"columns": ["code", "label"], "rows": [["A", "销售费用"]]}
    assert response["coverage"]["result_complete"]
    assert not response["coverage"]["full_source_validation"]
    assert adapter.stats.queries == 1
    assert tools.budget.used.db_cu == 1
    tools.close(); adapter.close()


def test_reopened_executor_does_not_reuse_packet_evidence_ids():
    adapter,tools=fixture()
    tools.reserve_probe_ids(['probe_0001','probe_0002','full_fd_check'])
    result=tools.execute('execute_readonly_sql',{'sql':'SELECT code FROM public.items'})
    assert result['status']=='ok' and result['probe_id']=='probe_0003'
    tools.close();adapter.close()


@pytest.mark.parametrize("sql", [
    "DELETE FROM public.items", "UPDATE public.items SET label = 'x'",
    "INSERT INTO public.items(code) VALUES ('x')", "SELECT code FROM public.items; SELECT code FROM public.items",
    "WITH deleted AS (DELETE FROM public.items RETURNING code) SELECT code FROM deleted",
    "SELECT code INTO public.copy FROM public.items", "SELECT pg_sleep(10)",
    "SELECT code FROM public.items FOR UPDATE", "SELECT * FROM public.items",
    "SELECT a.code FROM public.items a CROSS JOIN public.items b",
    "SELECT a.code FROM public.items a JOIN public.items b ON a.code = a.code",
    "SELECT code FROM public.items OFFSET 10", "SELECT password FROM public.items",
    "SELECT code FROM public.missing", "SELECT label FROM public.items WHERE password = 'x'",
    "WITH x AS (SELECT password FROM public.items) SELECT password FROM x",
    "SELECT code FROM public.items GROUP BY code HAVING MAX(password) > 'x'",
    "SELECT code AS password FROM public.items ORDER BY LENGTH(password)",
])
def test_unsafe_or_forbidden_sql_never_reaches_adapter(sql):
    adapter, tools = fixture()
    response = tools.execute("execute_readonly_sql", {"sql": sql})
    assert response["status"] == "error"
    assert adapter.stats.queries == 0
    assert "CREDENTIAL_CANARY" not in json.dumps(response)
    assert "sql" not in response and "arguments" not in response
    adapter.close()


def test_flexible_join_and_cte_are_allowed_and_still_scoped():
    adapter, tools = fixture()
    response = tools.execute("execute_readonly_sql", {"sql": "WITH x AS (SELECT DISTINCT code, label FROM public.items) SELECT a.code, b.label FROM x a JOIN x b ON a.code = b.code"})
    assert response["status"] == "ok"
    assert len(response["data"]["rows"]) == 2
    adapter.close()


@pytest.mark.parametrize("tool,args", [
    ("profile_columns", {"table": "public.items", "columns": ["code"]}),
    ("check_fd", {"table": "public.items", "keys": ["code"], "values": ["label"]}),
    ("check_reference", {"source_table": "public.items", "source_columns": ["code"], "target_table": "public.items", "target_columns": ["code"]}),
    ("execute_readonly_sql", {"sql": "SELECT COUNT(*) AS count FROM public.items"}),
])
def test_metadata_only_blocks_every_query_tool(tool, args):
    adapter, tools = fixture(allowed=False)
    response = tools.execute(tool, args)
    assert response["code"] == "metadata_only_query_forbidden"
    assert adapter.stats.queries == 0
    assert tools.execute("get_metadata", {})["status"] == "ok"
    adapter.close()


def test_goal_columns_and_table_allowlist_apply_to_common_tools_and_sql():
    adapter, tools = fixture(goal=ExtractionGoal(excluded_columns=["public.items.amount"]))
    metadata = tools.execute("get_metadata", {})
    assert {c["name"] for c in metadata["data"]["tables"][0]["columns"]} == {"code", "label"}
    for name, args in [("profile_columns", {"table": "public.items", "columns": ["amount"]}),
                       ("check_fd", {"table": "public.items", "keys": ["code"], "values": ["amount"]}),
                       ("execute_readonly_sql", {"sql": "SELECT amount FROM public.items"})]:
        assert tools.execute(name, args)["code"] == "unauthorized_column"
    restricted = ToolExecutor(adapter, ExtractionGoal(), BudgetAllocator(), allow_source_samples=True, allowed_tables=[])
    assert restricted.execute("execute_readonly_sql", {"sql": "SELECT code FROM public.items"})["code"] == "unauthorized_object"
    assert adapter.stats.queries == 0
    adapter.close()


def test_source_qualified_goal_and_evidence_only_columns():
    adapter, tools = fixture(goal=ExtractionGoal(source_scope=["s:public.items"],
                             evidence_only_columns=["amount"], excluded_columns=["s:public.items.label"]))
    result = tools.execute("execute_readonly_sql", {"sql": "SELECT amount FROM public.items"})
    assert result["status"] == "ok"
    assert tools.execute("execute_readonly_sql", {"sql": "SELECT label FROM public.items"})["code"] == "unauthorized_column"
    adapter.close()


def test_rows_are_bounded_and_truncation_is_not_full_validation():
    adapter, tools = fixture()
    response = tools.execute("execute_readonly_sql", {"sql": "SELECT code FROM public.items", "max_rows": 2})
    assert response["status"] == "ok"
    assert len(response["data"]["rows"]) == 2
    assert response["coverage"]["truncated"]
    assert not response["coverage"]["result_complete"]
    assert not response["coverage"]["full_source_validation"]
    adapter.close()


def test_explicit_smaller_limit_is_preserved_without_claiming_full_source():
    adapter, tools = fixture()
    response = tools.execute("execute_readonly_sql", {"sql": "SELECT code FROM public.items LIMIT :n", "parameters": {"n": 1}})
    assert response["status"] == "ok"
    assert response["data"]["rows"] == [["A"]]
    assert response["coverage"]["requested_limit"] == 1
    assert response["coverage"]["result_complete"]
    assert not response["coverage"]["full_source_validation"]
    adapter.close()


def test_result_bytes_bounded_without_silently_cutting_source_value():
    adapter, tools = fixture(rows={"public.items": [("A", "x" * 40000, "secret", 0)]})
    response = tools.execute("execute_readonly_sql", {"sql": "SELECT label FROM public.items"})
    assert response["status"] == "ok"
    assert response["data"]["rows"] == []
    assert response["coverage"]["truncated"]
    adapter.close()


def test_common_probes_report_observed_conflicts_not_semantic_truth():
    adapter, tools = fixture(rows={"public.items": [("A", "First", "secret", 0), ("A", "Second", "secret", 0)]})
    result = tools.execute("check_fd", {"table": "public.items", "keys": ["code"], "values": ["label"]})
    assert result["status"] == "ok"
    assert result["aggregates"]["label"]["violating_groups"] == 1
    assert not result["coverage"]["semantic_relationship_confirmed"]
    assert tools.budget.used.db_cu == 2
    assert "First" not in json.dumps(result)
    adapter.close()


def test_reference_probe_uses_real_key_coverage():
    tables = [TableSpec(source_id="s", name=name, columns=[ColumnSpec(name="code")]) for name in ("a", "b")]
    adapter, tools = fixture(tables=tables, rows={"public.a": [("A",), ("B",)], "public.b": [("A",)]})
    result = tools.execute("check_reference", {"source_table": "public.a", "source_columns": ["code"], "target_table": "public.b", "target_columns": ["code"]})
    assert result["status"] == "ok"
    assert result["aggregates"]["unique_matches"] == 1
    assert result["aggregates"]["unmatched"] == 1
    adapter.close()


def test_missing_parameters_and_bad_argument_shapes_are_safe_errors():
    adapter, tools = fixture()
    assert tools.execute("execute_readonly_sql", {"sql": "SELECT code FROM public.items WHERE code = :value"})["status"] == "error"
    assert tools.execute("execute_readonly_sql", {"sql": "SELECT code FROM public.items", "parameters": {"x": {"nested": 1}}})["code"] == "invalid_tool_arguments"
    assert tools.execute("execute_readonly_sql", {"sql": "SELECT code FROM public.items", "max_rows": True})["code"] == "invalid_result_limit"
    assert tools.execute("get_metadata", {"unknown": "SOURCE_CANARY"})["code"] == "invalid_tool_arguments"
    assert tools.execute("SOURCE_CANARY", {}) == {"status": "error", "operator": "unknown", "probe_id": "probe_0005", "code": "unknown_tool"}
    assert adapter.stats.queries == 0
    adapter.close()


def test_source_exception_body_is_never_returned_or_retained(monkeypatch):
    adapter, tools = fixture()
    def broken(*args, **kwargs):
        raise LightGraphError("SOURCE_CANARY")
    monkeypatch.setattr(adapter, "execute", broken)
    response = tools.execute("execute_readonly_sql", {"sql": "SELECT code FROM public.items"})
    assert response["code"] == "tool_execution_failed"
    assert "SOURCE_CANARY" not in repr(response) + repr(tools)
    with pytest.raises(LightGraphError, match="ephemeral_serialization_forbidden"):
        pickle.dumps(tools)
    adapter.close()


def test_closed_executor_does_not_close_borrowed_adapter():
    adapter, tools = fixture()
    tools.close()
    assert tools.execute("get_metadata", {})["code"] == "executor_closed"
    assert adapter.describe()
    adapter.close()


def test_budget_admission_prevents_query():
    budget = BudgetAllocator(Cost(db_cu=0, llm_calls=1, llm_tokens=100, memory_bytes=65536, seconds=60))
    adapter, tools = fixture(budget=budget)
    with pytest.raises(BudgetExceeded, match="db_cu_budget_exhausted"):
        tools.execute("execute_readonly_sql", {"sql": "SELECT code FROM public.items"})
    assert adapter.stats.queries == 0
    adapter.close()


def test_hard_query_budget_exhaustion_propagates_without_observations():
    adapter, tools = fixture()
    adapter.query_budget.max_queries = 1
    assert tools.execute("execute_readonly_sql", {"sql": "SELECT code FROM public.items"})["status"] == "ok"
    with pytest.raises(BudgetExceeded, match="query_budget_exhausted"):
        tools.execute("execute_readonly_sql", {"sql": "SELECT label FROM public.items"})
    assert adapter.stats.queries == 1
    adapter.close()


def test_gauss_decimal_counts_are_numbers_only_in_compiler_owned_aggregates(monkeypatch):
    from lightgraph.sources.base import EphemeralBatch
    adapter, tools = fixture()
    def decimal_result(*args, **kwargs):
        yield EphemeralBatch(["rows", "nulls"], [("3", "0")])
    monkeypatch.setattr(adapter, "execute", decimal_result)
    result = tools.execute("profile_columns", {"table": "public.items", "columns": ["code"]})
    assert result["aggregates"]["code"] == {"rows": 3, "nulls": 0}
    raw = tools.execute("execute_readonly_sql", {"sql": "SELECT code FROM public.items"})
    assert raw["data"]["rows"] == [["3", "0"]]
    adapter.close()


@pytest.mark.parametrize("value", ["SOURCE_CANARY", "NaN", "Infinity", "1E999999999", "1.5", "-1"])
def test_unexpected_aggregate_values_fail_closed_without_source_echo(monkeypatch, value):
    from lightgraph.sources.base import EphemeralBatch
    adapter, tools = fixture()
    def bad_result(*args, **kwargs):
        yield EphemeralBatch(["rows"], [(value,)])
    monkeypatch.setattr(adapter, "execute", bad_result)
    result = tools.execute("profile_columns", {"table": "public.items", "columns": ["code"]})
    assert result["code"] == "invalid_aggregate_shape"
    assert value not in json.dumps(result)
    adapter.close()


def test_metadata_hides_foreign_keys_and_unique_constraints_on_hidden_objects():
    table = TableSpec(source_id="s", name="items", columns=[ColumnSpec(name="code"), ColumnSpec(name="password")],
                      unique_keys=[["code"], ["password"]], foreign_keys=[ForeignKeySpec(name="private_fk", columns=["code"],
                      target_schema="public", target_table="hidden", target_columns=["code"])])
    adapter, tools = fixture(tables=[table], rows={"public.items": [("A", "secret")]})
    item = tools.execute("get_metadata", {})["data"]["tables"][0]
    assert item["unique_keys"] == [["code"]]
    assert item["foreign_keys"] == []
    assert item["columns"][0]["id"].startswith("c_")
    adapter.close()


def test_document_tool_uses_authorized_scoped_description_sections():
    adapter, tools = fixture(allowed=False)
    from lightgraph.documents import DocumentCorpus
    tools.documents = DocumentCorpus([load_document("context.md", text="---\npurpose: data_description\napplies_to: [public.items]\n---\n# 描述\nitems 的 code 是项目编码。")])
    result = tools.execute("search_knowledge", {"table": "public.items"})
    assert result["status"] == "ok" and result["data"]["sections"]
    assert adapter.stats.queries == 0
    assert tools.execute("search_knowledge", {"table": "public.hidden"})["code"] == "unauthorized_object"
    adapter.close()
