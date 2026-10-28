from __future__ import annotations

import pytest

from lightgraph.contracts import ColumnSpec, ExtractionGoal, TableSpec
from lightgraph.errors import LightGraphError
from lightgraph.sources import ExcelSource, MemorySource, QueryBudget
from lightgraph.sources.scoped import GoalScopedSource


@pytest.fixture
def source():
    tables = [TableSpec(source_id="s", name=name, columns=[ColumnSpec(name=column) for column in
              ("code", "name", "amount", "password")]) for name in ("items", "other")]
    with MemorySource(tables, {"public.items": [("A", "Item", 10, "hidden")], "public.other": [("A", "Other", 20, "hidden")]}) as adapter:
        yield adapter


def collect(batches):
    rows = []
    for batch in batches:
        with batch:
            rows.extend(batch.rows())
    return rows


@pytest.mark.parametrize("sql", [
    "SELECT amount FROM public.items",
    "SELECT code FROM public.items WHERE amount > 0",
    "SELECT code FROM public.items GROUP BY code HAVING MAX(amount) > 0",
    "SELECT code FROM public.items ORDER BY amount",
    "SELECT code AS amount FROM public.items ORDER BY ABS(amount)",
    "SELECT code AS amount FROM public.items GROUP BY code HAVING MAX(amount) > 0",
    "SELECT DISTINCT ON (ABS(amount)) code AS amount FROM public.items",
    "WITH x AS (SELECT code, amount FROM public.items) SELECT code FROM x",
    "SELECT a.code FROM public.items a JOIN public.other b ON a.amount = b.amount",
])
def test_goal_scope_rechecks_sql_compiled_against_original_catalog(source, sql):
    scoped = GoalScopedSource(source, ExtractionGoal(excluded_columns=["amount"]))
    query = source.compiler.finish(sql, {}, "select")
    with pytest.raises(LightGraphError, match="unauthorized_column"):
        collect(scoped.execute(query))
    assert source.stats.queries == 0


@pytest.mark.parametrize("method", ["project", "profile", "check_fd", "check_reference", "probe"])
def test_inherited_common_operations_cannot_bypass_excluded_columns(source, method):
    scoped = GoalScopedSource(source, ExtractionGoal(excluded_columns=["amount"]))
    table = scoped.describe()[0]
    with pytest.raises(LightGraphError, match="unauthorized_column"):
        if method == "project":
            collect(scoped.project(table, ["amount"]))
        elif method == "profile":
            scoped.profile(table, ["amount"])
        elif method == "check_fd":
            scoped.check_fd(table, ["amount"], ["name"])
        elif method == "check_reference":
            scoped.check_reference(table, ["amount"], table, ["amount"])
        else:
            scoped.probe("distinct_projection", table, {"columns": ["amount"]}).__next__()
    assert source.stats.queries == 0


def test_describe_and_schema_hash_input_stay_original_but_source_scope_is_enforced(source):
    scoped = GoalScopedSource(source, ExtractionGoal(source_scope=["s:public.items"], excluded_columns=["amount"]))
    assert [t.model_dump() for t in scoped.describe()] == [t.model_dump() for t in source.describe()]
    assert collect(scoped.project(scoped.describe()[0], ["code", "name"])) == [("A", "Item")]
    with pytest.raises(LightGraphError, match="unauthorized_object"):
        collect(scoped.project(scoped.describe()[1], ["code"]))
    assert scoped.stats is source.stats
    assert source.stats.queries == 1


def test_goal_view_only_blocks_explicit_exclusions_and_keeps_evidence_columns_readable(source):
    scoped = GoalScopedSource(source, ExtractionGoal(evidence_only_columns=["amount"]))
    # Sensitive-column screening belongs to the Agent tool policy. This common
    # boundary must not silently add regex business/permission rules.
    assert collect(scoped.project(scoped.describe()[0], ["amount", "password"])) == [(10, "hidden")]


def test_safe_projection_alias_and_cte_columns_still_work(source):
    scoped = GoalScopedSource(source, ExtractionGoal(excluded_columns=["amount"]))
    for sql in ("SELECT code AS item FROM public.items ORDER BY item",
                "WITH x AS (SELECT code AS item FROM public.items) SELECT item FROM x ORDER BY item",
                "SELECT code FROM public.items GROUP BY code HAVING COUNT(code) > 0"):
        query = source.compiler.finish(sql, {}, "select")
        assert collect(scoped.execute(query)) == [("A",)]


def test_group_alias_uses_full_inventory_through_goal_scope(source):
    scoped = GoalScopedSource(source, ExtractionGoal(excluded_columns=["amount"]))
    query = source.compiler.finish(
        "SELECT code AS item, COUNT(*) AS n FROM public.items GROUP BY item", {}, "select")
    assert collect(scoped.execute(query)) == [("A", 1)]
    # The backing adapter permits amount, but this borrowed view excludes it.
    # Its complete inventory must still identify the alias/input name clash.
    query = source.compiler.finish(
        "SELECT code AS amount, COUNT(*) AS n FROM public.items GROUP BY amount", {}, "select")
    before = source.stats.queries
    with pytest.raises(LightGraphError, match="unauthorized_column"):
        collect(scoped.execute(query))
    assert source.stats.queries == before


def test_borrowed_adapter_lifecycle_and_budget_delegation(source):
    scoped = GoalScopedSource(source, ExtractionGoal())
    budget = QueryBudget(max_queries=100)
    scoped.install_budget(budget)
    assert scoped.query_budget is source.query_budget is budget
    with scoped.snapshot() as entered:
        assert entered is scoped
        assert scoped.capabilities().consistent_snapshot
        assert collect(scoped.project(scoped.describe()[0], ["code"])) == [("A",)]
    scoped.close()
    with pytest.raises(LightGraphError, match="source_closed"):
        collect(scoped.execute(source.compiler.projection(source.describe()[0], ["code"])))
    assert collect(source.project(source.describe()[0], ["code"])) == [("A",)]


def test_projection_override_failure_is_preserved_after_goal_authorization():
    class FailingSource(MemorySource):
        invoked = 0
        def project(self, *args, **kwargs):
            self.invoked += 1
            raise LightGraphError("projection_hook_failed")
            yield

    table = TableSpec(source_id="s", name="t", columns=[ColumnSpec(name="code"), ColumnSpec(name="amount")])
    with FailingSource([table], {"public.t": [("A", 1)]}) as adapter:
        scoped = GoalScopedSource(adapter, ExtractionGoal(excluded_columns=["amount"]))
        with pytest.raises(LightGraphError, match="unauthorized_column"):
            collect(scoped.project(table, ["amount"]))
        assert adapter.invoked == 0
        with pytest.raises(LightGraphError, match="projection_hook_failed"):
            collect(scoped.project(table, ["code"]))
        assert adapter.invoked == 1


def test_projection_override_cancellation_after_batch_is_not_skipped():
    from lightgraph.contracts import QueryLimits

    class CancellingSource(MemorySource):
        def project(self, *args, **kwargs):
            for batch in super().project(*args, **kwargs):
                yield batch
                self.cancel()

    table = TableSpec(source_id="s", name="t", columns=[ColumnSpec(name="code")])
    with CancellingSource([table], {"public.t": [("A",), ("B",)]}, limits=QueryLimits(max_batch_rows=1)) as adapter:
        scoped = GoalScopedSource(adapter, ExtractionGoal())
        with pytest.raises(LightGraphError, match="query_cancelled"):
            collect(scoped.project(table, ["code"]))


@pytest.mark.parametrize("sql", [
    "SELECT formula FROM public.items",
    "SELECT code FROM public.items WHERE formula IS NULL",
    "SELECT code FROM public.items ORDER BY formula",
    "WITH x AS (SELECT formula AS value FROM public.items) SELECT value FROM x",
    "SELECT a.code FROM public.items a JOIN public.items b ON a.formula = b.formula",
])
def test_missing_excel_formula_cache_is_rejected_for_raw_sql_and_scoped_views(tmp_path, sql):
    import openpyxl

    path = tmp_path / "input.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "items"
    sheet.append(["code", "formula"])
    sheet.append(["A", "=1+1"])
    workbook.save(path)
    with ExcelSource(path) as adapter:
        scoped = GoalScopedSource(adapter, ExtractionGoal())
        query = adapter.compiler.finish(sql, {}, "select")
        before = adapter.stats.queries
        for view in (adapter, scoped):
            with pytest.raises(LightGraphError, match="formula_cache_missing_in_projection"):
                collect(view.execute(query))
        assert adapter.stats.queries == before
        assert collect(scoped.project(scoped.describe()[0], ["code"])) == [("A",)]
        count = adapter.compiler.finish("SELECT COUNT(*) AS n FROM public.items", {}, "select")
        assert collect(scoped.execute(count)) == [(1,)]
