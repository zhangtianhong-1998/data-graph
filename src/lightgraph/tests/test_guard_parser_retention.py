import logging

import pytest
import sqlglot

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.errors import LightGraphError
from lightgraph.query.guard import SQLGuard


@pytest.mark.parametrize("dialect", ["postgres", "sqlite"])
@pytest.mark.parametrize("sql", [
    "EXPLAIN SELECT code FROM public.items WHERE code = 'SQL_LOG_CANARY'",
    "SHOW SQL_LOG_CANARY",
    "SELECT code FROM public.items WHERE code = 'SQL_LOG_CANARY'; EXPLAIN SELECT code FROM public.items",
    "SELECT code FROM public.items WHERE code = 'SQL_LOG_CANARY' AND (",
])
def test_rejected_sql_never_enters_parser_logs(dialect, sql, caplog, capsys):
    guard = SQLGuard([TableSpec(source_id="s", name="items", columns=[ColumnSpec(name="code")])], dialect=dialect)
    with caplog.at_level(logging.DEBUG), pytest.raises(LightGraphError) as caught:
        guard.validate(sql)
    assert caught.value.code == "unsafe_sql"
    assert "SQL_LOG_CANARY" not in str(caught.value) + caplog.text + str(capsys.readouterr())


def test_guard_keeps_sqlglot_logging_and_parser_behavior_outside_guard(caplog):
    guard = SQLGuard([TableSpec(source_id="s", name="items", columns=[ColumnSpec(name="code")])])
    with pytest.raises(LightGraphError):
        guard.validate("EXPLAIN SELECT 'INSIDE_CANARY'")
    with caplog.at_level(logging.WARNING):
        sqlglot.parse("EXPLAIN SELECT 'OUTSIDE_CANARY'", read="postgres")
    assert "INSIDE_CANARY" not in caplog.text
    assert "OUTSIDE_CANARY" in caplog.text
    assert guard.validate("SELECT code FROM public.items").operator == "select"
