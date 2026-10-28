"""Model-facing identifiers and derived aliases stay inside the source catalog."""
import json

import pytest

from lightgraph.contracts import ColumnSpec, ExtractionGoal, TableSpec
from lightgraph.discovery.budget import BudgetAllocator
from lightgraph.discovery.tools import ToolExecutor
from lightgraph.errors import LightGraphError
from lightgraph.query import SQLGuard
from lightgraph.query.compiler import ident
from lightgraph.sources.memory import MemorySource


@pytest.fixture
def source_tools():
    table = TableSpec(source_id="fruit", name="水果报表项层级", columns=[
        ColumnSpec(name=name) for name in ("经营主体", "L1_CODE", "L2_CODE", "password")])
    source = MemorySource([table], {table.qualified_name: [
        ("经营主体甲", "P", "C", "PRIVATE_CANARY"),
        ("经营主体甲", "C", "D", "PRIVATE_CANARY"),
    ]})
    tools = ToolExecutor(source, ExtractionGoal(), BudgetAllocator(), allow_source_samples=True)
    yield source, tools
    tools.close()
    source.close()


def test_metadata_sql_identifiers_execute_without_using_opaque_ids(source_tools):
    source, tools = source_tools
    metadata = tools.execute("get_metadata", {})["data"]
    table = metadata["tables"][0]
    assert table["tool_table_id"] == "fruit:public.水果报表项层级"
    assert table["sql_table"] == '"public"."水果报表项层级"'
    assert table["sql_readable_on_current_source"] is True
    assert "identifier_usage" in metadata
    columns = {column["name"]: column for column in table["columns"]}
    assert "password" not in columns
    assert columns["经营主体"]["sql_identifier"] == '"经营主体"'
    assert columns["经营主体"]["id"].startswith("c_")
    result = tools.execute("execute_readonly_sql", {
        "sql": f'SELECT {columns["经营主体"]["sql_identifier"]} FROM {table["sql_table"]}'})
    assert result["status"] == "ok"
    assert result["data"]["rows"] == [["经营主体甲"], ["经营主体甲"]]


def test_cte_column_list_renames_union_projection_without_losing_authorization(source_tools):
    source, tools = source_tools
    result = tools.execute("execute_readonly_sql", {"sql": '''
        WITH items(code) AS (
          SELECT "L1_CODE" FROM "public"."水果报表项层级"
          UNION ALL
          SELECT "L2_CODE" FROM "public"."水果报表项层级"
        )
        SELECT code, COUNT(*) AS n FROM items GROUP BY code ORDER BY n DESC, code
    '''})
    assert result["status"] == "ok"
    assert result["data"]["rows"] == [["C", 2], ["D", 1], ["P", 1]]
    assert source.stats.queries == 1


@pytest.mark.parametrize("sql", [
    'WITH x(code) AS (SELECT password FROM public."水果报表项层级") SELECT code FROM x',
    'WITH x(code) AS (SELECT "L1_CODE" FROM public."水果报表项层级") SELECT password FROM x',
    'WITH x(code) AS (SELECT "L1_CODE" FROM public."水果报表项层级") SELECT "L1_CODE" FROM x',
])
def test_cte_renaming_cannot_expose_forbidden_or_hidden_columns(source_tools, sql):
    source, tools = source_tools
    result = tools.execute("execute_readonly_sql", {"sql": sql})
    assert result["code"] == "unauthorized_column"
    assert source.stats.queries == 0
    assert "PRIVATE_CANARY" not in json.dumps(result)


def test_rejected_identifier_help_does_not_echo_unknown_names_or_parameters(source_tools):
    source, tools = source_tools
    result = tools.execute("execute_readonly_sql", {
        "sql": 'SELECT bad_column FROM public."UNKNOWN_OBJECT_CANARY" WHERE bad_column=:value',
        "parameters": {"value": "PARAMETER_CANARY"},
    })
    assert result["code"] == "unauthorized_object"
    assert "get_metadata" in result["guidance"]
    assert "sql_table" in result["guidance"]
    assert "UNKNOWN_OBJECT_CANARY" not in json.dumps(result)
    assert "PARAMETER_CANARY" not in json.dumps(result)
    assert source.stats.queries == 0


def test_having_alias_has_safe_rewrite_guidance_and_expression_form_works(source_tools):
    source, tools = source_tools
    base = 'SELECT "经营主体", COUNT(*) AS n FROM public."水果报表项层级" GROUP BY "经营主体"'
    rejected = tools.execute("execute_readonly_sql", {"sql": base + ' HAVING n > 1'})
    assert rejected["code"] == "unauthorized_column"
    assert "HAVING" in rejected["guidance"]
    assert source.stats.queries == 0
    accepted = tools.execute("execute_readonly_sql", {"sql": base + ' HAVING COUNT(*) > 1'})
    assert accepted["status"] == "ok"
    assert accepted["data"]["rows"] == [["经营主体甲", 2]]


@pytest.mark.parametrize("dialect", ["sqlite", "postgres", "mysql"])
def test_cte_explicit_alias_authorization_across_dialects(dialect):
    table = TableSpec(source_id="s", name="items", columns=[ColumnSpec(name="code")])
    guard = SQLGuard([table], dialect=dialect)
    source = f'{ident("public", dialect)}.{ident("items", dialect)}'
    query = f'WITH x(renamed) AS (SELECT code FROM {source}) SELECT renamed FROM x'
    assert guard.validate(query).sql == query
    with pytest.raises(LightGraphError, match="unauthorized_column"):
        guard.validate(f'WITH x(renamed) AS (SELECT secret FROM {source}) SELECT renamed FROM x')


@pytest.mark.parametrize("sql,rows", [
    ('SELECT "L1_CODE" AS code FROM public."水果报表项层级" '
     'UNION ALL SELECT "L2_CODE" AS code FROM public."水果报表项层级" ORDER BY code',
     [["C"], ["C"], ["D"], ["P"]]),
    ('SELECT "经营主体" AS subject, COUNT(*) AS n FROM public."水果报表项层级" '
     'GROUP BY subject ORDER BY n', [["经营主体甲", 2]]),
])
def test_compound_order_and_group_projection_aliases_execute(source_tools, sql, rows):
    source, tools = source_tools
    result = tools.execute("execute_readonly_sql", {"sql": sql})
    assert result["status"] == "ok"
    assert result["data"]["rows"] == rows
    assert source.stats.queries == 1


@pytest.mark.parametrize("sql", [
    'SELECT "L1_CODE" AS password, COUNT(*) FROM public."水果报表项层级" GROUP BY password',
    'SELECT "L1_CODE" AS PASSWORD, COUNT(*) FROM public."水果报表项层级" GROUP BY PASSWORD',
    'SELECT "L1_CODE" AS code FROM public."水果报表项层级" '
    'UNION ALL SELECT password AS code FROM public."水果报表项层级" ORDER BY code',
    'SELECT "L1_CODE" AS code FROM public."水果报表项层级" '
    'UNION ALL SELECT "L2_CODE" AS code FROM public."水果报表项层级" ORDER BY password',
    'SELECT "L1_CODE" AS code, COUNT(*) FROM public."水果报表项层级" AS t GROUP BY t.code',
    'SELECT "L1_CODE" AS code, "L2_CODE" AS code, COUNT(*) '
    'FROM public."水果报表项层级" GROUP BY code',
])
def test_output_aliases_do_not_authorize_hidden_inputs_or_unexposed_union_columns(source_tools, sql):
    source, tools = source_tools
    result = tools.execute("execute_readonly_sql", {"sql": sql})
    assert result["code"] == "unauthorized_column"
    assert source.stats.queries == 0
    assert "PRIVATE_CANARY" not in json.dumps(result)


def test_group_uses_allowed_input_before_conflicting_output_alias():
    table = TableSpec(source_id="s", name="items", columns=[ColumnSpec(name="code"), ColumnSpec(name="name")])
    with MemorySource([table], {table.qualified_name: [("A", "X"), ("A", "Y")]}) as source:
        tools = ToolExecutor(source, ExtractionGoal(), BudgetAllocator(), allow_source_samples=True)
        result = tools.execute("execute_readonly_sql", {
            "sql": 'SELECT code AS name, COUNT(*) AS n FROM public.items GROUP BY name'})
        assert result["status"] == "ok"
        assert result["data"]["rows"] == [["A", 1], ["A", 1]]
        tools.close()


@pytest.mark.parametrize("dialect", ["sqlite", "postgres", "mysql"])
def test_group_alias_requires_complete_internal_catalog_and_rejects_hidden_collision(dialect):
    table = TableSpec(source_id="s", name="items", columns=[ColumnSpec(name="code")])
    source = f'{ident("public", dialect)}.{ident("items", dialect)}'
    valid = f'SELECT code AS code_alias, COUNT(*) FROM {source} GROUP BY code_alias'
    unknown_inventory = SQLGuard([table], dialect=dialect)
    with pytest.raises(LightGraphError, match="unauthorized_column"):
        unknown_inventory.validate(valid)
    guard = SQLGuard([table], dialect=dialect, physical_columns={table.qualified_name: ["code", "hidden"]})
    assert guard.validate(valid).sql == valid
    with pytest.raises(LightGraphError, match="unauthorized_column"):
        guard.validate(f'SELECT code AS hidden, COUNT(*) FROM {source} GROUP BY hidden')
    with pytest.raises(LightGraphError, match="unauthorized_column"):
        guard.validate(f'SELECT code AS HIDDEN, COUNT(*) FROM {source} GROUP BY HIDDEN')


@pytest.mark.parametrize("pending_formula", [False, True])
def test_excel_group_alias_preserves_inventory_across_type_and_formula_guards(tmp_path, pending_formula):
    from openpyxl import Workbook
    from lightgraph.sources import ExcelSource

    path = tmp_path / "items.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "items"
    sheet.append(["code", "password", "calculated"])
    sheet.append(["A", "PRIVATE_CANARY", "=1+1" if pending_formula else "2"])
    book.save(path)
    book.close()
    with ExcelSource(path) as source:
        tools = ToolExecutor(source, ExtractionGoal(), BudgetAllocator(), allow_source_samples=True)
        metadata = tools.execute("get_metadata", {})["data"]
        assert "password" not in {c["name"] for c in metadata["tables"][0]["columns"]}
        assert "physical_columns" not in metadata
        result = tools.execute("execute_readonly_sql", {
            "sql": "SELECT code AS grouped_code, COUNT(*) AS n FROM public.items GROUP BY grouped_code"})
        assert result["status"] == "ok"
        assert result["data"]["rows"] == [["A", 1]]
        before = source.stats.queries
        result = tools.execute("execute_readonly_sql", {
            "sql": "SELECT code AS PASSWORD, COUNT(*) AS n FROM public.items GROUP BY PASSWORD"})
        assert result["code"] == "unauthorized_column"
        assert source.stats.queries == before
        if pending_formula:
            query = source.compiler.finish(
                "SELECT code AS calculated, COUNT(*) AS n FROM public.items GROUP BY calculated", {}, "select")
            with pytest.raises(LightGraphError, match="formula_cache_missing_in_projection"):
                list(source.execute(query))
            assert source.stats.queries == before
        tools.close()


@pytest.mark.parametrize("qualifier", ["", "u."])
def test_scalar_count_over_nested_union_keeps_derived_alias_binding(source_tools, qualifier):
    source, tools = source_tools
    selected = ", ".join(qualifier + name for name in ("subject", "code", "name"))
    result = tools.execute("execute_readonly_sql", {"sql": f'''
        SELECT (SELECT COUNT(*) FROM (
          SELECT DISTINCT {selected} FROM (
            SELECT "经营主体" AS subject, "L1_CODE" AS code, "L1_CODE" AS name
            FROM public."水果报表项层级"
            UNION ALL
            SELECT "经营主体", "L2_CODE", "L2_CODE" FROM public."水果报表项层级"
          ) u
        ) x) AS total
    '''})
    assert result["status"] == "ok"
    assert result["data"]["rows"] == [[3]]
    assert source.stats.queries == 1


@pytest.mark.parametrize("sql", [
    'SELECT (SELECT COUNT(*) FROM (SELECT "L1_CODE" AS code FROM public."水果报表项层级" '
    'UNION ALL SELECT password FROM public."水果报表项层级") u) AS total',
    'SELECT (SELECT unknown FROM (SELECT "L1_CODE" AS code FROM public."水果报表项层级") u) AS total',
    'SELECT (SELECT unknown FROM public."水果报表项层级") AS total',
    'SELECT (SELECT code FROM (SELECT "L1_CODE" AS code FROM public."水果报表项层级") u) AS total, code',
    'SELECT t."L1_CODE", (SELECT t.password) AS hidden FROM public."水果报表项层级" t',
    'SELECT t."L1_CODE", (SELECT t."L1_CODE") AS correlated FROM public."水果报表项层级" t',
    'SELECT "L1_CODE", (SELECT password) AS hidden FROM public."水果报表项层级"',
])
def test_scalar_subqueries_keep_hidden_unbound_and_correlated_references_blocked(source_tools, sql):
    source, tools = source_tools
    result = tools.execute("execute_readonly_sql", {"sql": sql})
    assert result["code"] == "unauthorized_column"
    assert source.stats.queries == 0
    assert "PRIVATE_CANARY" not in json.dumps(result)


def test_scalar_nested_union_runs_through_goal_scoped_excel(tmp_path):
    from openpyxl import Workbook
    from lightgraph.sources import ExcelSource
    from lightgraph.sources.scoped import GoalScopedSource

    path = tmp_path / "items.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "items"
    sheet.append(["tenant", "a", "b", "c", "d", "password"])
    sheet.append(["T", "A", "N1", "B", "N2", "PRIVATE_CANARY"])
    sheet.append(["T", "A", "N1", "C", "N3", "PRIVATE_CANARY"])
    book.save(path)
    book.close()
    with ExcelSource(path) as source:
        goal = ExtractionGoal(excluded_columns=["password"])
        tools = ToolExecutor(GoalScopedSource(source, goal), goal, BudgetAllocator(), allow_source_samples=True)
        result = tools.execute("execute_readonly_sql", {"sql": '''
            SELECT (SELECT COUNT(*) FROM (
              SELECT DISTINCT subject, code, name FROM (
                SELECT tenant AS subject, a AS code, b AS name FROM public.items
                UNION ALL SELECT tenant, c, d FROM public.items
              ) u
            ) x) AS total
        '''})
        assert result["status"] == "ok"
        assert result["data"]["rows"] == [[3]]
        tools.close()
