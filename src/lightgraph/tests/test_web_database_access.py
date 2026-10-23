"""Onboarding uses fixed catalog SQL; fake drivers never contact a database."""
from io import BytesIO
import json
from zipfile import ZipFile

import pytest
from pydantic import ValidationError
from openpyxl import Workbook

from lightgraph.web.database_access import (
    ConnectionSpec, DatabaseAccessError, build_dsn, discover_tables,
    normalize_selected_tables, parse_table_list_excel, safe_connection_details,
    test_connection as check_connection,
)

SECRET = "NEVER_PERSIST_THIS_PASSWORD"


def spec(kind="mysql", **kwargs):
    return ConnectionSpec(kind=kind, host="127.0.0.1", database="analytics", username="reader", password=SECRET, **kwargs)


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.sql = ""
        self.closed = False
        self._executed = self._rows = "DRIVER_PRIVATE_DETAIL"

    def execute(self, sql, params=None):
        self.sql = sql
        self.connection.executed.append((sql, params))
        if sql in {"SET SESSION TRANSACTION READ ONLY", "BEGIN TRANSACTION READ ONLY"} and not self.connection.ignore_read_only:
            self.connection.read_only = 1
        if self.connection.fail_at and self.connection.fail_at in sql:
            raise RuntimeError(SECRET)

    def fetchone(self):
        if "transaction_read_only" in self.sql:
            return (self.connection.read_only,)
        return (1,)

    def fetchmany(self, size):
        self.connection.fetch_limits.append(size)
        return self.connection.rows[:size]

    def close(self):
        self.closed = True
        if self.connection.fail_cursor_close:
            raise RuntimeError(SECRET)


class Connection:
    def __init__(self, *, schema="analytics", fail_at=None):
        self.rows = [(schema, "items", "BASE TABLE"), (schema, "items_view", "VIEW")]
        self.executed = []
        self.fetch_limits = []
        self.cursors = []
        self.read_only = 0
        self.ignore_read_only = False
        self.fail_at = fail_at
        self.fail_cursor_close = self.fail_rollback = self.fail_close = False
        self.closed = False
        self.rollback_calls = 0

    def cursor(self):
        cursor = Cursor(self)
        self.cursors.append(cursor)
        return cursor

    def rollback(self):
        self.rollback_calls += 1
        if self.fail_rollback:
            raise RuntimeError(SECRET)

    def close(self):
        self.closed = True
        if self.fail_close:
            raise RuntimeError(SECRET)


def install_driver(monkeypatch, kind, db):
    calls = []
    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return db
    monkeypatch.setattr("pymysql.connect" if kind == "mysql" else "psycopg.connect", connect)
    return calls


@pytest.mark.parametrize("kind", ["mysql", "gauss"])
def test_credentials_encoded_and_not_in_repr_or_public_details(kind):
    password = "secret:/@? #中文'&"
    value = ConnectionSpec(kind=kind, host="::1", database="analytics", username="reader", password=password)
    dsn = build_dsn(value)
    if kind == "mysql":
        from lightgraph.sources.mysql import _connect_options
        from lightgraph.contracts import QueryLimits
        assert _connect_options(dsn, QueryLimits())["password"] == password
    else:
        from psycopg.conninfo import conninfo_to_dict
        assert conninfo_to_dict(dsn)["password"] == password
    details = safe_connection_details(value)
    assert details["host"] == "::1"
    assert details["schema"] == ("analytics" if kind == "mysql" else "public")
    assert "dsn" not in details and "password" not in details
    assert password not in repr(value) and password not in value.model_dump_json()
    dsn_value = ConnectionSpec(kind=kind, dsn=dsn)
    assert password not in repr(dsn_value) and password not in dsn_value.model_dump_json()


@pytest.mark.parametrize("kwargs", [
    {"dsn": "mysql://r:p@localhost/db", "password": SECRET},
    {"dsn": "mysql://r:p@localhost/db", "host": "localhost"},
    {"host": "localhost", "database": "db"},
    {"dsn": ""},
])
def test_connection_forms_are_exclusive_and_validation_hides_values(kwargs):
    with pytest.raises(ValidationError) as error:
        ConnectionSpec(kind="mysql", **kwargs)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("dsn", [
    "host=localhost dbname=db user=r options='-c transaction_read_only=off'",
    "host=localhost dbname=db user=r service=private_service",
    "host=localhost dbname=db user=r passfile=/private/file",
    "host=/tmp dbname=db user=r",
    "host=db.example.com dbname=db user=r sslmode=disable",
    "host=localhost,other dbname=db user=r",
    "host=localhost dbname=db user=r port=99999",
    "not valid PRIVATE_PASSWORD",
])
def test_gauss_rejects_unbounded_startup_options_and_local_file_resolution(dsn):
    with pytest.raises(DatabaseAccessError) as error:
        build_dsn(ConnectionSpec(kind="gauss", dsn=dsn))
    assert "PRIVATE_PASSWORD" not in str(error.value)


def test_remote_connections_require_tls_verification():
    from psycopg.conninfo import conninfo_to_dict
    from lightgraph.sources.mysql import _connect_options
    from lightgraph.contracts import QueryLimits
    gauss = ConnectionSpec(kind="gauss", host="db.example.com", database="analytics", username="r", password=SECRET)
    assert conninfo_to_dict(build_dsn(gauss))["sslmode"] == "verify-full"
    mysql = ConnectionSpec(kind="mysql", host="db.example.com", database="analytics", username="r", password=SECRET)
    options = _connect_options(build_dsn(mysql), QueryLimits())
    assert options["ssl"].check_hostname is True
    assert options["local_infile"] is False and options["client_flag"] == 0
    with pytest.raises(DatabaseAccessError):
        build_dsn(ConnectionSpec(kind="mysql", dsn="mysql://r:p@db.example.com/db?ssl_mode=DISABLED"))


@pytest.mark.parametrize("prefix", ["jdbc:gaussdb", "jdbc:postgresql"])
@pytest.mark.parametrize("host", ["db.example.com", "[::1]"])
def test_jdbc_url_with_separate_credentials_preserves_endpoint_and_password(prefix, host):
    from psycopg.conninfo import conninfo_to_dict
    password = "  secret:/@? #中文'&+%  "
    value = ConnectionSpec(kind="gauss", dsn=f"{prefix}://{host}:8000/business", username="reader", password=password)
    options = conninfo_to_dict(build_dsn(value))
    assert options == {"host": host.strip("[]"), "port": "8000", "dbname": "business", "user": "reader",
                       "password": password, "sslmode": "disable" if host == "[::1]" else "verify-full", "connect_timeout": "5"}
    assert safe_connection_details(value)["database"] == "business"
    assert password not in repr(value) and password not in value.model_dump_json()


def test_jdbc_query_credentials_decode_once_and_matching_separate_credentials_work():
    from urllib.parse import urlencode
    from psycopg.conninfo import conninfo_to_dict
    password = "SECRET_JDBC:/@? #中文'&+%25"
    query = urlencode({"user": "reader", "password": password, "connectTimeout": "9999", "sslmode": "verify-full"})
    dsn = "jdbc:gaussdb://db.example.com:8000/%E4%B8%9A%E5%8A%A1?" + query
    for fields in ({}, {"username": "reader", "password": password}):
        value = ConnectionSpec(kind="gauss", dsn=dsn, **fields)
        options = conninfo_to_dict(build_dsn(value))
        assert options["dbname"] == "业务" and options["password"] == password
        assert options["connect_timeout"] == "5"
        assert password not in repr(value) and query not in repr(value)


@pytest.mark.parametrize("query,fields", [
    ("user=other", {"username": "reader"}),
    ("user=reader&password=OTHER_SECRET", {"password": SECRET}),
])
def test_jdbc_credentials_conflict_is_rejected_without_echoing_secrets(query, fields):
    with pytest.raises(DatabaseAccessError, match="^database_connection_credentials_conflict$") as error:
        build_dsn(ConnectionSpec(kind="gauss", dsn="jdbc:gaussdb://localhost:8000/business?" + query, **fields))
    assert SECRET not in str(error.value) and "OTHER_SECRET" not in str(error.value)


@pytest.mark.parametrize("suffix", [
    "localhost:8000/", "localhost:8000/db/other", "localhost:0/db", "localhost:65536/db", "localhost:/db",
    "localhost,other:8000/db", "reader:PRIVATE_PASSWORD@localhost:8000/db", "localhost:8000/db#PRIVATE_PASSWORD",
    "localhost:8000/db?user=a&user=b", "localhost:8000/db?options=-c%20transaction_read_only=off",
    "localhost:8000/db?service=private_service", "localhost:8000/db?passfile=/private/file",
    "localhost:8000/db?host=other", "localhost:8000/db?password=%ZZ", "localhost:8000/db?password=%FF",
    "localhost:8000/db?connectTimeout=3&connect_timeout=5", "local\nhost:8000/db",
    "db.example.com:8000/db?sslmode=disable",
])
def test_jdbc_invalid_or_unsupported_options_fail_before_connecting(monkeypatch, suffix):
    calls = install_driver(monkeypatch, "gauss", Connection())
    with pytest.raises(DatabaseAccessError) as error:
        check_connection(ConnectionSpec(kind="gauss", dsn="jdbc:gaussdb://" + suffix, username="reader"))
    assert not calls and "PRIVATE_PASSWORD" not in str(error.value)


def test_jdbc_requires_user_and_still_rejects_mixed_endpoints():
    dsn = "jdbc:gaussdb://localhost:8000/business"
    with pytest.raises(DatabaseAccessError, match="^database_connection_fields_required$"):
        build_dsn(ConnectionSpec(kind="gauss", dsn=dsn))
    with pytest.raises(ValidationError, match="database_connection_forms_conflict"):
        ConnectionSpec(kind="gauss", dsn=dsn, username="reader", host="other")
    with pytest.raises(ValidationError, match="database_connection_forms_conflict"):
        ConnectionSpec(kind="gauss", dsn="postgresql://reader@localhost/business", password=SECRET)


def test_jdbc_test_and_discovery_use_normalized_readonly_connection(monkeypatch):
    from psycopg.conninfo import conninfo_to_dict
    db = Connection(schema="public")
    calls = install_driver(monkeypatch, "gauss", db)
    value = ConnectionSpec(kind="gauss", dsn="jdbc:gaussdb://localhost:8000/business", username="reader", password=SECRET)
    assert check_connection(value) == {"status": "ok"}
    assert discover_tables(value)["tables"][0]["qualified_name"] == "public.items"
    assert len(calls) == 2
    for args, kwargs in calls:
        assert not args[0].startswith("jdbc:")
        assert conninfo_to_dict(args[0])["port"] == "8000"
        assert conninfo_to_dict(args[0])["password"] == SECRET
        assert kwargs["connect_timeout"] == 5
    assert any("READ ONLY" in sql for sql, _ in db.executed)
    assert db.closed and db.rollback_calls == 2


@pytest.mark.parametrize("kind", ["mysql", "gauss"])
def test_connection_probe_is_fixed_readonly_and_closes(monkeypatch, kind):
    db = Connection()
    calls = install_driver(monkeypatch, kind, db)
    assert check_connection(spec(kind)) == {"status": "ok"}
    assert db.executed[-1] == ("SELECT 1", None)
    assert all("information_schema" not in sql for sql, _ in db.executed)
    assert any("READ ONLY" in sql for sql, _ in db.executed)
    assert any("5000" in sql for sql, _ in db.executed)
    assert db.closed and db.rollback_calls == 1 and all(c.closed for c in db.cursors)
    assert all(c._executed is None and c._rows is None for c in db.cursors)
    assert calls[0][1]["connect_timeout"] == 5
    assert SECRET not in json.dumps({"status": "ok"})


@pytest.mark.parametrize("kind,schema", [("mysql", "analytics"), ("gauss", "business")])
def test_discovery_only_reads_bounded_schema_metadata(monkeypatch, kind, schema):
    db = Connection(schema=schema)
    install_driver(monkeypatch, kind, db)
    result = discover_tables(spec(kind, schema=schema))
    assert result == {"tables": [
        {"schema": schema, "name": "items", "qualified_name": schema + ".items", "table_type": "BASE TABLE"},
        {"schema": schema, "name": "items_view", "qualified_name": schema + ".items_view", "table_type": "VIEW"},
    ], "truncated": False}
    sql, params = db.executed[-1]
    assert "information_schema" in sql and "LIMIT %s" in sql
    assert params == (schema, 1001)
    assert schema not in sql
    assert db.fetch_limits == [1001]
    assert db.closed
    assert SECRET not in json.dumps(result)


def test_discovery_marks_truncation_without_returning_lookahead(monkeypatch):
    db = Connection()
    db.rows = [("analytics", f"table_{i}", "BASE TABLE") for i in range(1001)]
    install_driver(monkeypatch, "mysql", db)
    result = discover_tables(spec())
    assert len(result["tables"]) == 1000 and result["truncated"] is True
    assert db.fetch_limits == [1001] and db.closed


@pytest.mark.parametrize("schema", ["mysql", "pg_catalog", "pg_temp_12", "information_schema", "bad\x00name", "bad.name"])
def test_schema_rejected_before_driver_is_opened(monkeypatch, schema):
    db = Connection()
    calls = install_driver(monkeypatch, "mysql", db)
    with pytest.raises(DatabaseAccessError):
        discover_tables(spec(schema=schema))
    assert not calls


@pytest.mark.parametrize("kind", ["mysql", "gauss"])
@pytest.mark.parametrize("failure", ["query", "readonly", "cursor_close", "rollback", "close"])
def test_every_failure_closes_without_returning_driver_diagnostics(monkeypatch, kind, failure):
    db = Connection(schema="analytics" if kind == "mysql" else "public")
    if failure == "query": db.fail_at = "information_schema"
    if failure == "readonly": db.ignore_read_only = True
    if failure == "cursor_close": db.fail_cursor_close = True
    if failure == "rollback": db.fail_rollback = True
    if failure == "close": db.fail_close = True
    install_driver(monkeypatch, kind, db)
    with pytest.raises(DatabaseAccessError) as error:
        discover_tables(spec(kind))
    assert SECRET not in str(error.value) and SECRET not in repr(error.value)
    assert db.closed and all(c.closed for c in db.cursors)


@pytest.mark.parametrize("kind", ["mysql", "gauss"])
def test_driver_connection_error_is_sanitized(monkeypatch, kind):
    def fail(*args, **kwargs):
        raise RuntimeError(SECRET)
    monkeypatch.setattr("pymysql.connect" if kind == "mysql" else "psycopg.connect", fail)
    with pytest.raises(DatabaseAccessError, match="^database_connection_failed$"):
        discover_tables(spec(kind))


@pytest.mark.parametrize("row", [
    ("other", "items", "BASE TABLE"), ("analytics", "items\x00drop", "BASE TABLE"),
    ("analytics", "items", "PRIVATE_RAW_VALUE"), ("analytics", "items"),
])
def test_discovery_validates_driver_output_shape(monkeypatch, row):
    db = Connection()
    db.rows = [row]
    install_driver(monkeypatch, "mysql", db)
    with pytest.raises(DatabaseAccessError):
        discover_tables(spec())
    assert db.closed


def test_manual_selection_scoped_deduplicated_and_bounded():
    assert normalize_selected_tables(spec(), ["items", "analytics.items", "items_view"]) == ["analytics.items", "analytics.items_view"]
    assert normalize_selected_tables(spec("gauss"), ["客户清单"]) == ["public.客户清单"]
    for names in (["other.items"], ["items\x00DROP"], [], ["item"] * 1001):
        with pytest.raises(DatabaseAccessError):
            normalize_selected_tables(spec(), names)


def xlsx(rows):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    buf = BytesIO()
    book.save(buf)
    book.close()
    return buf.getvalue()


@pytest.mark.parametrize("rows,expected", [
    ([["items"], ["analytics.other"]], ["items", "analytics.other"]),
    ([["备注", "表名", "schema"], ["忽略", "items", "analytics"], ["重复", "items", "analytics"]], ["analytics.items"]),
    ([["table_name"], ["客户清单"], [None]], ["客户清单"]),
])
def test_excel_table_list_reads_headers_or_first_column(rows, expected):
    assert parse_table_list_excel(xlsx(rows)) == expected


@pytest.mark.parametrize("rows,code", [
    ([["table"], ["=CONCAT(\"pri\",\"vate\")"]], "table_list_formula_forbidden"),
    ([["table", "schema"], ["x", "=A1"]], "table_list_formula_forbidden"),
    ([["table"], ["bad.name.parts"]], "database_identifier_invalid"),
    ([["table"], ["pg_catalog.items"]], "system_catalog_forbidden"),
    ([["table", "schema"], ["other.items", "analytics"]], "database_table_schema_mismatch"),
    ([["table"], [123]], "database_identifier_invalid"),
    ([["table"]], "table_list_empty"),
])
def test_excel_rejects_formulas_invalid_and_system_identifiers(rows, code):
    with pytest.raises(DatabaseAccessError, match="^" + code + "$"):
        parse_table_list_excel(xlsx(rows))


def test_excel_size_and_expansion_limits():
    with pytest.raises(DatabaseAccessError, match="table_list_size_limit"):
        parse_table_list_excel(b"x" * (5 * 1024 * 1024 + 1))
    with pytest.raises(DatabaseAccessError, match="table_list_size_limit"):
        parse_table_list_excel(xlsx([["table"]] + [[f"table_{i}"] for i in range(1001)]))
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for i in range(129): archive.writestr(str(i), "a")
    with pytest.raises(DatabaseAccessError, match="table_list_size_limit"):
        parse_table_list_excel(output.getvalue())
    with pytest.raises(DatabaseAccessError, match="table_list_invalid"):
        parse_table_list_excel(b"not an xlsx")


def test_literal_identifiers_with_spaces_quotes_and_digits_stay_parameters(monkeypatch):
    schema = "2026-business data"
    db = Connection(schema=schema)
    db.rows = [(schema, 'cost "by" region; report', "BASE TABLE")]
    install_driver(monkeypatch, "mysql", db)
    result = discover_tables(spec(schema=schema))
    expected = schema + '.cost "by" region; report'
    assert result["tables"][0]["qualified_name"] == expected
    assert normalize_selected_tables(spec(schema=schema), [expected]) == [expected]
    sql, params = db.executed[-1]
    assert params == (schema, 1001)
    assert schema not in sql and 'region' not in sql


def inject_zip_member(content, filename, value):
    output = BytesIO()
    with ZipFile(BytesIO(content)) as source, ZipFile(output, "w") as target:
        for entry in source.infolist():
            if entry.filename != filename:
                target.writestr(entry, source.read(entry.filename))
        target.writestr(filename, value)
    return output.getvalue()


@pytest.mark.parametrize("filename,value,code", [
    ("xl/vbaProject.bin", b"macro", "table_list_active_content_forbidden"),
    ("xl/externalLinks/link.xml", b"link", "table_list_active_content_forbidden"),
    ("xl/extra.xml", b'<!DOCTYPE x [<!ENTITY e "expand">]><x>&e;</x>', "table_list_xml_entities_forbidden"),
    ("xl/_rels/test.rels", b'<!ENTITY e "expand">', "table_list_xml_entities_forbidden"),
    ("xl/extra.xml", '<!DOCTYPE x><x />'.encode("utf-16"), "table_list_xml_entities_forbidden"),
])
def test_excel_rejects_active_content_and_xml_entities(filename, value, code):
    content = inject_zip_member(xlsx([["items"]]), filename, value)
    with pytest.raises(DatabaseAccessError, match="^" + code + "$"):
        parse_table_list_excel(content)


@pytest.mark.parametrize("dialect", ["postgres", "mysql"])
def test_selected_literal_names_remain_quoted_through_extraction_compiler(dialect):
    from lightgraph.contracts import ColumnSpec, TableSpec
    from lightgraph.query import QueryCompiler
    table = TableSpec(source_id="db", schema="2026-business data", name='cost "by" region; report',
                      columns=[ColumnSpec(name="code", data_type="text")])
    compiler = QueryCompiler([table], dialect=dialect)
    query = compiler.projection(table, ["code"], limit=1)
    assert query.operator == "distinct_projection"
    assert len(compiler.guard.validate(query.sql, query.parameters).parameters) == 1
