from __future__ import annotations

import asyncio
import json

import pytest
from agentscope import _config
from agentscope.tool import Toolkit, ToolResponse

from lightgraph.contracts import ColumnSpec, ExtractionGoal, TableSpec
from lightgraph.discovery.agentscope_tools import create_toolkit
from lightgraph.discovery.budget import BudgetAllocator
from lightgraph.discovery.tools import ToolExecutor
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.sources.memory import MemorySource


async def call(toolkit, name, arguments):
    chunks = [chunk async for chunk in await toolkit.call_tool_function({
        "type": "tool_use", "id": "call_test", "name": name, "input": arguments,
    })]
    assert len(chunks) == 1 and isinstance(chunks[0], ToolResponse)
    assert chunks[0].is_last and not chunks[0].stream
    return json.loads(chunks[0].content[0]["text"])


@pytest.fixture
def executor():
    table = TableSpec(source_id="s", name="items", columns=[
        ColumnSpec(name="code"), ColumnSpec(name="label"), ColumnSpec(name="password"),
    ])
    source = MemorySource([table], {"public.items": [
        ("A", "销售费用", "SOURCE_SECRET"), ("A", "销售费用", "SOURCE_SECRET"),
        ("B", "销售成本", "SOURCE_SECRET"),
    ]})
    tools = ToolExecutor(source, ExtractionGoal(), BudgetAllocator(), allow_source_samples=True)
    yield tools
    tools.close()
    source.close()


def test_real_agentscope_toolkit_preserves_all_executor_schemas(executor):
    expected = executor.tool_specs()
    toolkit = create_toolkit(executor)
    assert isinstance(toolkit, Toolkit)
    assert toolkit.get_json_schemas() == expected
    assert executor.tool_specs() == expected
    assert len(toolkit.tools) == 6


@pytest.mark.parametrize("name,arguments", [
    ("get_metadata", {}),
    ("search_knowledge", {"table": "public.items"}),
    ("profile_columns", {"table": "public.items", "columns": ["code"]}),
    ("check_fd", {"table": "public.items", "keys": ["code"], "values": ["label"]}),
    ("check_reference", {"source_table": "public.items", "source_columns": ["code"],
                         "target_table": "public.items", "target_columns": ["code"]}),
    ("execute_readonly_sql", {"sql": "SELECT DISTINCT code, label FROM public.items"}),
])
def test_all_six_tools_execute_via_agentscope(executor, name, arguments, caplog, capsys):
    result = asyncio.run(call(create_toolkit(executor), name, arguments))
    assert result["status"] == "ok"
    assert result["operator"] == name
    assert "SOURCE_SECRET" not in json.dumps(result)
    assert "销售费用" not in caplog.text
    assert "销售费用" not in str(capsys.readouterr())


@pytest.mark.parametrize("sql", [
    "DELETE FROM public.items WHERE code = 'SQL_CANARY'",
    "SELECT password FROM public.items",
    "EXPLAIN SELECT code FROM public.items WHERE code = 'SQL_CANARY'",
    "SELECT code FROM public.items; UPDATE public.items SET code = 'SQL_CANARY'",
])
def test_guarded_queries_still_reject_before_source_execution(executor, sql, caplog, capsys):
    result = asyncio.run(call(create_toolkit(executor), "execute_readonly_sql", {"sql": sql}))
    assert result["status"] == "error"
    assert executor.adapter.stats.queries == 0
    assert "SQL_CANARY" not in json.dumps(result) + caplog.text + str(capsys.readouterr())


def test_executor_metadata_only_policy_still_applies(executor):
    executor.allow_source_samples = False
    result = asyncio.run(call(create_toolkit(executor), "execute_readonly_sql", {
        "sql": "SELECT code FROM public.items",
    }))
    assert result["code"] == "metadata_only_query_forbidden"
    assert executor.adapter.stats.queries == 0


def test_unknown_tool_and_invalid_inputs_are_never_echoed(executor, caplog, capsys):
    toolkit = create_toolkit(executor)
    unknown = asyncio.run(call(toolkit, "SECRET_UNKNOWN_TOOL", {"secret": "VALUE_CANARY"}))
    invalid = asyncio.run(call(toolkit, "get_metadata", "VALUE_CANARY"))
    assert unknown == {"status": "error", "operator": "unknown", "code": "unknown_tool"}
    assert invalid["code"] == "invalid_tool_arguments"
    assert "CANARY" not in json.dumps([unknown, invalid]) + caplog.text + str(capsys.readouterr())


def test_dispatch_can_apply_loop_policy_without_executor_call(executor):
    observed = []

    async def dispatch(name, arguments):
        observed.append(name)
        return {"status": "error", "code": "repeated_tool_call"}

    result = asyncio.run(call(create_toolkit(executor, dispatch), "get_metadata", {}))
    assert result["code"] == "repeated_tool_call"
    assert observed == ["get_metadata"]
    assert executor._sequence == 0


@pytest.mark.parametrize("name,arguments", [
    ("get_metadata", {"table": 42}),
    ("get_metadata", {"table": ""}),
    ("get_metadata", {"extra": "ARGUMENT_CANARY"}),
    ("search_knowledge", {}),
    ("profile_columns", {"table": "public.items"}),
    ("profile_columns", {"table": "public.items", "columns": "code"}),
    ("profile_columns", {"table": "public.items", "columns": []}),
    ("check_fd", {"table": "public.items", "keys": ["code"], "values": [42]}),
    ("check_reference", {"source_table": "public.items", "source_columns": ["code"]}),
    ("execute_readonly_sql", {}),
    ("execute_readonly_sql", {"sql": "SELECT code FROM public.items", "max_rows": True}),
    ("execute_readonly_sql", {"sql": "SELECT code FROM public.items", "max_rows": 0}),
    ("execute_readonly_sql", {"sql": "SELECT code FROM public.items", "parameters": {"code": []}}),
    ("execute_readonly_sql", {"sql": "SELECT code FROM public.items", "parameters": {"code": float("nan")}}),
    ("execute_readonly_sql", {"sql": "ARGUMENT_CANARY" * 2000}),
])
def test_schema_rejections_precede_dispatch_and_never_echo_values(executor, name, arguments, caplog, capsys):
    observed = []

    def dispatch(tool, args):
        observed.append(tool)
        return executor.execute(tool, args)

    result = asyncio.run(call(create_toolkit(executor, dispatch), name, arguments))
    assert result["code"] == "invalid_tool_arguments"
    assert observed == []
    assert executor._sequence == 0
    assert "ARGUMENT_CANARY" not in json.dumps(result) + caplog.text + str(capsys.readouterr())


def test_registered_function_itself_enforces_schema_before_dispatch(executor):
    toolkit = create_toolkit(executor)
    result = asyncio.run(toolkit.tools["profile_columns"].original_func(
        table="public.items", columns="code",
    ))
    assert json.loads(result.content[0]["text"])["code"] == "invalid_tool_arguments"
    assert executor._sequence == 0


def test_dispatch_exception_never_becomes_framework_exception_text(executor, caplog, capsys):
    def dispatch(name, arguments):
        raise RuntimeError("SECRET_EXCEPTION_VALUE")

    result = asyncio.run(call(create_toolkit(executor, dispatch), "get_metadata", {}))
    assert result["code"] == "tool_execution_failed"
    assert "SECRET_EXCEPTION_VALUE" not in json.dumps(result) + caplog.text + str(capsys.readouterr())


def test_budget_exhaustion_terminates_without_yielding_a_tool_result(executor):
    def dispatch(name, arguments):
        raise BudgetExceeded("query_budget_exhausted")

    with pytest.raises(BudgetExceeded, match="query_budget_exhausted"):
        asyncio.run(call(create_toolkit(executor, dispatch), "get_metadata", {}))


def test_budget_carrier_is_isolated_per_concurrent_call(executor):
    async def dispatch(name, arguments):
        await asyncio.sleep(0)
        if arguments.get("table") == "budget":
            raise BudgetExceeded("query_budget_exhausted")
        return {"status": "ok"}

    async def exercise():
        toolkit = create_toolkit(executor, dispatch)
        return await asyncio.gather(
            call(toolkit, "get_metadata", {"table": "budget"}),
            call(toolkit, "get_metadata", {}), return_exceptions=True,
        )

    failed, succeeded = asyncio.run(exercise())
    assert isinstance(failed, BudgetExceeded)
    assert succeeded == {"status": "ok"}


def test_dynamic_executor_tool_is_registered_without_a_hardcoded_list():
    class CustomExecutor:
        def tool_specs(self):
            return [{"type": "function", "function": {"name": "custom_probe", "description": "Probe",
                     "parameters": {"type": "object", "properties": {"table": {"type": "string"}},
                                    "required": [], "additionalProperties": False}}}]

        def execute(self, name, arguments):
            return {"status": "ok", "operator": name}

    toolkit = create_toolkit(CustomExecutor())
    assert list(toolkit.tools) == ["custom_probe"]
    assert asyncio.run(call(toolkit, "custom_probe", {}))["status"] == "ok"


def test_tracing_enabled_elsewhere_fails_before_any_tool_observation(executor, monkeypatch):
    toolkit = create_toolkit(executor)
    monkeypatch.setattr(_config, "trace_enabled", True)
    with pytest.raises(LightGraphError, match="agent_tracing_forbidden"):
        asyncio.run(call(toolkit, "get_metadata", {}))
    assert executor._sequence == 0
