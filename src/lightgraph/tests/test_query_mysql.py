"""MySQL compilation/security checks; no live database or model is used."""
from __future__ import annotations

import sqlite3

import pytest
import sqlglot

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.errors import LightGraphError
from lightgraph.query import QueryCompiler, SQLGuard
from lightgraph.query.compiler import ident, mysql_driver_sql


@pytest.fixture
def tables():
    return [TableSpec(source_id="mysql", schema="business", name="items", columns=[
        ColumnSpec(name="code", data_type="int unsigned"),
        ColumnSpec(name="label", data_type="varchar(100)"),
        ColumnSpec(name="tenant", data_type="char(10)"),
        ColumnSpec(name="parent", data_type="int"),
        ColumnSpec(name="amount", data_type="double"),
        ColumnSpec(name="occurred", data_type="datetime"),
        ColumnSpec(name="payload", data_type="json"),
    ]), TableSpec(source_id="mysql", schema="business", name="dictionary", columns=[
        ColumnSpec(name="code", data_type="bigint"),
        ColumnSpec(name="label", data_type="longtext"),
        ColumnSpec(name="tenant", data_type="varchar(10)"),
    ])]


def test_mysql_identifiers_binds_aliases_and_length_semantics(tables):
    compiler = QueryCompiler(tables, dialect="mysql")
    query = compiler.projection(tables[0], ["code", "label"], predicate={"label": "' OR 1=1; --"}, limit=7)
    assert "`business`.`items`" in query.sql
    assert "LIMIT :cap" in query.sql
    assert query.parameters == {"p0": "' OR 1=1; --", "cap": 8}
    assert "OR 1=1" not in query.sql
    rendered = mysql_driver_sql(query)
    assert "%(p0)s" in rendered and "%(cap)s" in rendered
    assert query.parameters["p0"] not in rendered
    profile = compiler.profile_column(tables[0], "label")
    assert "CAST(`label` AS CHAR)" in profile.sql
    assert "CHAR_LENGTH(" in profile.sql
    assert "AS `rows`" in profile.sql
    assert compiler.ident("a`b") == "`a``b`"
    assert ident('a"b') == '"a""b"'


def test_percent_and_colon_inside_identifiers_are_not_driver_binds():
    table = TableSpec(source_id="mysql", schema="business", name="percent%table", columns=[
        ColumnSpec(name="weird%(p0)s:cap`name", data_type="varchar(64)"),
    ])
    compiler = QueryCompiler([table], dialect="mysql")
    query = compiler.projection(table, [table.columns[0].name], predicate={table.columns[0].name: "100%"})
    sql = mysql_driver_sql(query)
    assert "`percent%%table`" in sql
    assert "`weird%%(p0)s:cap``name`" in sql
    # Equivalent to the driver's final pyformat stage, with representative
    # driver-escaped values. Identifiers survive without becoming parameters.
    expanded = sql % {"p0": "'100%'", "cap": "20001"}
    assert "`weird%(p0)s:cap``name`" in expanded
    assert "`percent%table`" in expanded
    assert "= '100%'" in expanded


def test_mysql_all_registered_operator_sql_roundtrips(tables):
    c = QueryCompiler(tables, dialect="mysql")
    a, b = tables
    queries = [
        c.projection(a, ["code", "label"]),
        c.profile_column(a, "label"), c.profile_column_group(a, ["tenant", "code"]),
        c.check_fd(a, ["tenant", "code"], "label"),
        c.check_value_domain(a, "code", [1, 2, None]),
        c.check_reference(a, ["code"], b, ["code"], discriminator_columns=["tenant"]),
        c.path_probe(a, ["code", "parent"]),
        c.unpivot_probe(a, [{"code": "code", "label": "label"}, {"code": "parent"}], limit=10),
        c.compare_attribute_role(a, "label", b, "label"),
    ]
    for query in queries:
        checked = c.guard.validate(query.sql, query.parameters)
        assert mysql_driver_sql(checked)
    overlap = queries[-1]
    assert "INTERSECT" not in overlap.sql
    assert "INNER JOIN" in overlap.sql


def test_mysql_set_overlap_and_fd_keep_existing_operator_semantics(tables):
    c = QueryCompiler(tables, dialect="mysql")
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("ATTACH DATABASE ':memory:' AS business")
    connection.execute("CREATE TABLE business.items(code INTEGER,label TEXT,tenant TEXT,parent INTEGER,amount REAL,occurred TEXT,payload TEXT)")
    connection.execute("CREATE TABLE business.dictionary(code INTEGER,label TEXT,tenant TEXT)")
    connection.executemany("INSERT INTO business.items(code,label,tenant) VALUES(?,?,?)", [
        (1, "销售", "A"), (1, "销售", "A"), (2, "退货", "A"), (2, "退款", "A"), (None, None, "A")])
    connection.executemany("INSERT INTO business.dictionary VALUES(?,?,?)", [(1, "销售", "A"), (1, "销售", "A"), (2, "退货", "A"), (3, "采购", "A")])
    def evaluate(query):
        # SQLite execution is a semantic unit test of relational operators, not
        # a claim that the MySQL driver or a real MySQL deployment is verified.
        sql = sqlglot.parse_one(query.sql, read="mysql").sql(dialect="sqlite")
        return dict(connection.execute(sql, query.parameters).fetchone())
    try:
        assert evaluate(c.compare_attribute_role(tables[0], "label", tables[1], "label"))["overlap_values"] == 2
        fd = evaluate(c.check_fd(tables[0], ["code"], "label"))
        assert fd["x_groups"] == 2 and fd["violating_groups"] == 1
        assert evaluate(c.profile_column(tables[0], "label"))["max_len"] == 2
        ref = evaluate(c.check_reference(tables[0], ["code"], tables[1], ["code"]))
        assert ref["unique_matches"] == 2 and ref["null_refs"] == 1
    finally:
        connection.close()


@pytest.mark.parametrize("kind,value", [
    ("tinyint", 1), ("mediumint unsigned", 1), ("double", 1.5),
    ("datetime", "2026-01-01T01:02:03"), ("year", 2026),
    ("longtext", "名称"), ("mediumtext", "说明"), ("enum('A','B')", "A"),
])
def test_mysql_native_builtin_types_have_typed_predicates(kind, value):
    table = TableSpec(source_id="mysql", schema="business", name="native_types", columns=[ColumnSpec(name="value", data_type=kind)])
    query = QueryCompiler([table], dialect="mysql").projection(table, ["value"], predicate={"value": value})
    assert query.parameters["p0"] == value


def test_mysql_json_projection_does_not_implicitly_enable_json_predicates(tables):
    c = QueryCompiler(tables, dialect="mysql")
    assert c.projection(tables[0], ["payload"])
    with pytest.raises(LightGraphError, match="predicate_type_unsupported"):
        c.projection(tables[0], ["payload"], predicate={"payload": '{"x":1}'})


def test_mysql_native_json_metadata_projection_and_char_profile(tables):
    table = tables[0]
    compiler = QueryCompiler(tables, dialect="mysql")
    projection = compiler.projection(table, ["code", "payload"], limit=32)
    assert projection.result_columns == ("code", "payload")
    assert projection.parameters == {"cap": 33}
    assert "`payload`" in mysql_driver_sql(projection)
    profile = compiler.profile_column(table, "payload")
    rendered = mysql_driver_sql(profile)
    assert "COUNT(`payload`)" in rendered
    assert "COUNT(DISTINCT `payload`)" in rendered
    assert "CHAR_LENGTH(CAST(`payload` AS CHAR))" in rendered
    assert "AS `distinct_exact`" in rendered
    # The source declaration remains native JSON, not a rewritten VARCHAR
    # assertion; no JSON path extraction is needed to read serialized values.
    assert table.columns[-1].data_type == "json"


@pytest.mark.parametrize("sql", [
    "SELECT JSON_EXTRACT(payload, '$.code') FROM business.items",
    "SELECT payload->'$.code' FROM business.items",
    "SELECT payload->>'$.code' FROM business.items",
    "SELECT JSON_SET(payload, '$.code', 2) FROM business.items",
])
def test_mysql_json_projection_does_not_allow_json_path_operators(tables, sql):
    with pytest.raises(LightGraphError, match="unsafe_sql"):
        SQLGuard(tables, dialect="mysql").validate(sql)


@pytest.mark.parametrize("sql", [
    "SELECT code FROM business.items INTO OUTFILE '/tmp/export'",
    "SELECT code INTO OUTFILE '/tmp/export' FROM business.items",
    "SELECT code FROM business.items INTO DUMPFILE '/tmp/export'",
    "SELECT code INTO @value FROM business.items",
    "SELECT @value", "SELECT @'value'", "SELECT @@version", "SELECT @value := 1",
    "SELECT SLEEP(10)", "SELECT GET_LOCK('name', 1)", "SELECT RELEASE_LOCK('name')",
    "SELECT BENCHMARK(100000, 1)", "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT LAST_INSERT_ID(1)", "SELECT dangerous_udf(code) FROM business.items",
    "SELECT code FROM business.items FOR UPDATE",
    "SELECT code FROM business.items FOR SHARE",
    "SELECT code FROM business.items LOCK IN SHARE MODE",
    "SELECT code FROM business.items; DELETE FROM business.items",
    "SELECT code FROM business.items; SELECT code FROM business.items",
    "WITH deleted AS (DELETE FROM business.items RETURNING code) SELECT code FROM deleted",
    "WITH selected AS (SELECT code FROM business.items) UPDATE business.items SET code=2",
    "INSERT INTO business.items(code) SELECT code FROM business.items",
    "REPLACE INTO business.items(code) VALUES(2)",
    "CALL dangerous_proc()", "LOAD DATA LOCAL INFILE '/tmp/input' INTO TABLE business.items",
    "EXPLAIN SELECT code FROM business.items WHERE label='PRIVATE_QUERY_SENTINEL'",
    "SELECT code FROM business.items /*!50000 INTO OUTFILE '/tmp/export' */",
    "SELECT code FROM business.items /*M!100100 INTO OUTFILE '/tmp/export' */",
    "SELECT /*+ SET_VAR(sort_buffer_size=1048576) */ code FROM business.items",
    "SELECT code FROM business.items LIMIT 1, 2",
    "WITH RECURSIVE nested AS (SELECT code FROM business.items) SELECT code FROM nested",
])
def test_mysql_side_effect_and_unbounded_sql_rejected(tables, sql, caplog):
    with pytest.raises(LightGraphError):
        SQLGuard(tables, dialect="mysql").validate(sql)
    assert sql not in caplog.text


@pytest.mark.parametrize("schema", ["mysql", "information_schema", "performance_schema", "sys", "MySQL"])
def test_mysql_system_schemas_are_forbidden_even_if_misregistered(schema):
    table = TableSpec(source_id="mysql", schema=schema, name="users", columns=[ColumnSpec(name="user")])
    with pytest.raises(LightGraphError, match="unauthorized_object"):
        SQLGuard([table], dialect="mysql").validate(f"SELECT `user` FROM `{schema}`.`users`")


def test_mysql_guard_keeps_safe_cte_join_and_commentlike_strings(tables):
    guard = SQLGuard(tables, dialect="mysql")
    assert guard.validate("WITH selected AS (SELECT code FROM business.items) SELECT code FROM selected")
    assert guard.validate("SELECT a.code FROM business.items a JOIN business.dictionary b ON a.code=b.code")
    assert guard.validate("SELECT '/*!50000 ordinary data */' AS note FROM business.items")
    assert guard.validate("SELECT code FROM business.items /* ordinary comment */")
    with pytest.raises(LightGraphError, match="unbounded_join"):
        guard.validate("SELECT a.code FROM business.items a JOIN business.dictionary b ON a.code=a.code")


def test_postgres_sqlite_type_and_quote_contract_not_broadened():
    table = TableSpec(source_id="src", schema="public", name="native", columns=[ColumnSpec(name="value", data_type="tinyint")])
    for dialect in ("postgres", "sqlite"):
        with pytest.raises(LightGraphError, match="untrusted_column_type"):
            QueryCompiler([table], dialect=dialect).projection(table, ["value"])
    table.columns[0].data_type = "integer"
    for dialect in ("postgres", "sqlite"):
        query = QueryCompiler([table], dialect=dialect).projection(table, ["value"])
        assert '"public"."native"' in query.sql
        assert query.sql.endswith("LIMIT :cap")
