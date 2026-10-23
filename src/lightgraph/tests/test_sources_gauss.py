"""DB-API contract tests, deliberately not live Gauss acceptance."""
from __future__ import annotations
import pytest
from lightgraph.sources import GaussSource
from lightgraph.errors import LightGraphError

class Cursor:
    def __init__(self, connection, name=None): self.connection=connection; self.name=name; self.result=[]; self.description=[]
    def execute(self, sql, parameters=None):
        self.connection.executed.append((sql, parameters))
        if "current_setting" in sql: self.result=[("on","repeatable read")]
        elif "SELECT version" in sql: self.result=[("openGauss 6.0.0 test fixture",)]
        elif sql=="SELECT %s": self.result=[(71,)]
        elif "information_schema.columns" in sql: self.result=[("code","text","NO"),("name","text","YES")]
        elif "pg_catalog.col_description" in sql: self.result=[("code","Business code",1,True),("name","Display name",2,False)]
        elif "SELECT k.conname" in sql: self.result=[("items_pkey","p",[1],None,None,None),("items_fk","f",[1],[1],"public","items")]
        elif "pg_catalog.obj_description" in sql: self.result=[("Business registry",)]
        elif "WITH p AS" in sql: self.result=[(1,),(2,)]
        elif "SELECT DISTINCT 1" in sql or sql=="SELECT 1": self.result=[(1,)]
        elif self.name:
            self.result=[("001","Expenses")]; self.description=[("code",), ("name",)]
        else: self.result=[]
    def fetchone(self): return self.result.pop(0) if self.result else None
    def fetchall(self): rows=self.result; self.result=[]; return rows
    def close(self): pass
class Connection:
    def __init__(self): self.executed=[];self.closed=False;self.cancelled=False
    def cursor(self,name=None): return Cursor(self,name)
    def rollback(self): pass
    def close(self): self.closed=True
    def cancel(self): self.cancelled=True


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"])
def test_runtime_driver_preserves_explicit_ssl_mode(monkeypatch, mode):
    from psycopg.conninfo import conninfo_to_dict
    from lightgraph.web.database_access import ConnectionSpec, build_dsn

    dsn = build_dsn(ConnectionSpec(kind="gauss", host="db.example.com", database="business", username="reader", sslmode=mode))
    db = Connection()
    calls = []
    def connect(value, **kwargs):
        calls.append((conninfo_to_dict(value), kwargs))
        return db
    monkeypatch.setattr("psycopg.connect", connect)
    with GaussSource(dsn, allowed_tables=["public.items"]) as source:
        assert source.describe()[0].name == "items"
    assert len(calls) == 1 and calls[0][0]["sslmode"] == mode
    assert any("READ ONLY" in sql for sql, _ in db.executed)
    assert db.closed


def test_unknown_runtime_capabilities_block_instance_rows():
    db=Connection()
    with GaussSource("SECRET_DSN",source_id="s",allowed_tables=["public.items"],connect_factory=lambda _: db) as source:
        assert source.capabilities().product=="openGauss"
        assert not source.capabilities().actual_database_verified
        assert source.capabilities().capabilities["statement_timeout"]=="unknown"
        assert "SECRET_DSN" not in repr(source)
        with pytest.raises(LightGraphError,match="runtime_capability_unverified"):
            list(source.project(source.describe()[0],["code"]))

def test_driver_parameters_server_cursor_and_snapshot_lifetime():
    db=Connection()
    with GaussSource("SECRET_DSN",source_id="s",allowed_tables=["public.items"],connect_factory=lambda _:db, verified_capabilities={"product":"openGauss", "version":"6.0.0", "driver":"psycopg_dbapi", "capabilities":{"statement_timeout":"verified","query_cancel":"verified"}}) as source:
        table=source.describe()[0]
        assert table.columns[0].primary_key
        assert table.comment=="Business registry"
        assert table.unique_keys == [["code"]]
        assert table.foreign_keys[0].target_columns == ["code"]
        assert not source.capabilities().consistent_snapshot
        with source.snapshot():
            assert source.capabilities().consistent_snapshot
            rows=[]
            for batch in source.project(table,["code","name"],predicate={"code":"001"}): rows.extend(batch.rows())
            assert rows==[("001","Expenses")]
            assert source.capabilities().consistent_snapshot
        assert not source.capabilities().consistent_snapshot
        generated=[(sql,p) for sql,p in db.executed if 'FROM "public"."items"' in sql][-1]
        assert "001" not in generated[0] and "001" in generated[1].values()
        assert source.stats.queries==1
        source.cancel();assert db.cancelled
    assert db.closed


def test_gauss_decimal_aggregate_counts_preserve_zero_and_generic_rows_stay_text():
    from decimal import Decimal

    class DecimalCursor(Cursor):
        def execute(self, sql, parameters=None):
            super().execute(sql, parameters)
            if not self.name:
                return
            if "distinct_tuples" in sql:
                self.description = [(name,) for name in ("distinct_tuples", "rows", "duplicate_groups", "max_duplicate", "null_key_rows")]
                self.result = [(2, Decimal("3"), 1, 2, Decimal("0"))]
            elif "violating_groups" in sql:
                self.description = [(name,) for name in ("x_groups", "violating_groups", "excess_values", "max_values_per_x", "all_null_groups", "mixed_null_groups", "null_attribute_rows")]
                self.result = [(2, 0, Decimal("0"), 1, 0, 0, Decimal("0"))]
            elif "raw_count" in sql:
                self.description = [("raw_count",)]
                self.result = [(Decimal("2"),)]

    class DecimalConnection(Connection):
        def cursor(self, name=None):
            return DecimalCursor(self, name)

    with GaussSource("secret", allowed_tables=["public.items"], connect_factory=lambda _: DecimalConnection(),
        verified_capabilities={"product":"openGauss", "version":"6.0.0", "driver":"psycopg_dbapi",
                               "capabilities":{"statement_timeout":"verified","query_cancel":"verified"}}) as source:
        table = source.describe()[0]
        check = source.check_fd(table, ["code"], ["name"])
        assert check["null_key_rows"] == 0 and type(check["null_key_rows"]) is int
        assert check["excess_values"] == 0 and not check["null_attribute_rows"]
        group = source.probe("profile_column_group", table, {"columns": ["code"]})
        assert group["rows"] == 3 and type(group["rows"]) is int
        query = source.compiler.finish("SELECT COUNT(*) AS raw_count FROM public.items", {}, "select")
        rows = []
        for batch in source.execute(query):
            with batch:
                rows.extend(batch.rows())
        assert rows == [("2",)]
        before = source.stats.queries
        with pytest.raises(LightGraphError, match="unregistered_aggregate_operator"):
            source._aggregate(query)
        assert source.stats.queries == before

def test_connect_error_is_redacted():
    def fail(_): raise RuntimeError("password and BUSINESS_VALUE")
    with pytest.raises(LightGraphError) as caught:
        GaussSource("SECRET_DSN",source_id="s",allowed_tables=["public.items"],connect_factory=fail)
    assert str(caught.value)=="database_connection_failed"
    assert caught.value.__suppress_context__

def test_qualification_requires_exact_product_version_driver():
    manifest={"product":"openGauss","version":"5.0.0","driver":"psycopg_dbapi","capabilities":{"query_cancel":"verified","statement_timeout":"verified"}}
    with pytest.raises(LightGraphError,match="binding_mismatch"):
        GaussSource("secret",allowed_tables=["public.items"],verified_capabilities=manifest,connect_factory=lambda _:Connection())
    with pytest.raises(LightGraphError,match="qualified_capability_manifest_required"):
        GaussSource("secret",allowed_tables=["public.items"],verified_capabilities={"query_cancel":"verified"},connect_factory=lambda _:Connection())

def test_catalog_and_preflight_consume_shared_budget():
    from lightgraph.sources import QueryBudget
    budget=QueryBudget(max_queries=2)
    with pytest.raises(LightGraphError,match="query_budget_exhausted"):
        GaussSource("secret",allowed_tables=["public.items"],query_budget=budget,connect_factory=lambda _:Connection())
    assert budget.snapshot()["queries"]==2


def test_nested_snapshot_uses_one_transaction_and_outer_failure_rolls_back():
    class TrackedConnection(Connection):
        rollbacks=0
        def rollback(self): self.rollbacks+=1
    db=TrackedConnection()
    with GaussSource("secret",allowed_tables=["public.items"],connect_factory=lambda _:db,
        verified_capabilities={"product":"openGauss","version":"6.0.0","driver":"psycopg_dbapi","capabilities":{"statement_timeout":"verified","query_cancel":"verified"}}) as source:
        initial_rollbacks=db.rollbacks;initial_commands=len(db.executed)
        with pytest.raises(RuntimeError,match="outer failed"):
            with source.snapshot():
                with source.snapshot():
                    assert source.capabilities().consistent_snapshot
                assert source.capabilities().consistent_snapshot
                assert db.rollbacks==initial_rollbacks
                raise RuntimeError("outer failed")
        assert not source.capabilities().consistent_snapshot
        assert db.rollbacks==initial_rollbacks+1
        assert sum(sql.startswith("BEGIN ") for sql,_ in db.executed[initial_commands:])==1


def test_prefix_uses_bound_like_operator_in_catalog_search_path():
    db=Connection()
    with GaussSource("secret",allowed_tables=["public.items"],connect_factory=lambda _:db,
        verified_capabilities={"product":"openGauss","version":"6.0.0","driver":"psycopg_dbapi","capabilities":{"statement_timeout":"verified","query_cancel":"verified"}}) as source:
        table=source.describe()[0]
        list(source.project(table,["code"],predicate={"column":"code","op":"prefix","value":"A_%!"}))
        sql,parameters=[(sql,p) for sql,p in db.executed if 'FROM "public"."items"' in sql][-1]
        assert "LIKE %(p0)s ESCAPE '!'" in sql
        assert parameters["p0"]=="A!_!%!!%"
        assert any("search_path" in sql and "pg_catalog" in sql for sql,_ in db.executed)


def test_snapshot_close_failure_is_safe_and_never_confirms_consistency():
    class FailingCloseConnection(Connection):
        fail_rollback=False
        def rollback(self):
            if self.fail_rollback:raise RuntimeError("PRIVATE_DRIVER_PAYLOAD")
    db=FailingCloseConnection()
    with GaussSource("secret",allowed_tables=["public.items"],connect_factory=lambda _:db,
        verified_capabilities={"product":"openGauss","version":"6.0.0","driver":"psycopg_dbapi","capabilities":{"statement_timeout":"verified","query_cancel":"verified"}}) as source:
        with pytest.raises(LightGraphError,match="snapshot_close_failed") as caught:
            with source.snapshot():db.fail_rollback=True
        db.fail_rollback=False
        assert not source.capabilities().consistent_snapshot
        assert "PRIVATE_DRIVER_PAYLOAD" not in str(caught.value)


@pytest.mark.parametrize("banner,expected", [
    ("PostgreSQL 9.2.4 (openGauss 6.0.0 build abc) compiled by GCC 12.2.0", ("openGauss","6.0.0")),
    ("PostgreSQL 9.2.4 (GaussDB Kernel 503.1.0 build abc)", ("GaussDB","503.1.0")),
    ("GaussDB V500R002C00SPC010", ("GaussDB","V500R002C00SPC010")),
    ("openGauss 6.0.0-RC1", ("openGauss","6.0.0-RC1")),
    ("GaussDB Kernel 503.0.0 based on openGauss 5.0.0", ("GaussDB","503.0.0")),
    ("PostgreSQL 9.2.4 (openGauss unknown) compiled by GCC 12.2.0", ("openGauss","unknown")),
    ("openGauss 5.0.0 and openGauss 6.0.0", ("openGauss","unknown")),
    ("PostgreSQL 15.2", ("unknown","unknown")),
])
def test_product_version_is_bound_to_product_marker(banner,expected):
    from lightgraph.sources.gauss import product_version
    assert product_version(banner)==expected


class VersionCursor(Cursor):
    def execute(self,sql,parameters=None):
        super().execute(sql,parameters)
        if "SELECT version" in sql:self.result=[(self.connection.banner,)]


class VersionConnection(Connection):
    def __init__(self,banner):super().__init__();self.banner=banner
    def cursor(self,name=None):return VersionCursor(self,name)


def test_product_upgrade_invalidates_manifest_even_with_same_postgres_prefix():
    qualification={"product":"openGauss","version":"5.0.0","driver":"psycopg_dbapi",
                   "capabilities":{"statement_timeout":"verified","query_cancel":"verified"}}
    with GaussSource("secret",allowed_tables=["public.items"],verified_capabilities=qualification,
        connect_factory=lambda _:VersionConnection("PostgreSQL 9.2.4 (openGauss 5.0.0 build abc)")) as source:
        assert source.capabilities().version=="5.0.0"
    with pytest.raises(LightGraphError,match="capability_manifest_binding_mismatch"):
        GaussSource("secret",allowed_tables=["public.items"],verified_capabilities=qualification,
            connect_factory=lambda _:VersionConnection("PostgreSQL 9.2.4 (openGauss 6.0.0 build def)"))
    qualification["version"]="9.2.4"
    with pytest.raises(LightGraphError,match="capability_manifest_binding_mismatch"):
        GaussSource("secret",allowed_tables=["public.items"],verified_capabilities=qualification,
            connect_factory=lambda _:VersionConnection("PostgreSQL 9.2.4 (openGauss 6.0.0 build def)"))


def test_unknown_product_version_cannot_claim_runtime_verification():
    with GaussSource("secret",allowed_tables=["public.items"],
        connect_factory=lambda _:VersionConnection("PostgreSQL 9.2.4 (openGauss custom release)")) as source:
        assert source.capabilities().version=="unknown"
        with pytest.raises(LightGraphError,match="product_version_unverified"):source.verify_runtime_controls()
        assert not source.capabilities().actual_database_verified


def test_runtime_controls_do_not_repeat_catalog_and_partial_failure_is_unverified(monkeypatch):
    db=Connection();probes=[]
    with GaussSource("secret",allowed_tables=["public.items"],connect_factory=lambda _:db) as source:
        before=list(db.executed)
        def probe(*,cancel):
            probes.append(cancel)
            if cancel:raise LightGraphError("query_cancel_unverified")
            source._manifest.capabilities["statement_timeout"]="verified"
        monkeypatch.setattr(source,"_probe_runtime_control",probe)
        with pytest.raises(LightGraphError,match="query_cancel_unverified"):source.verify_runtime_controls()
        assert probes==[False,True]
        assert db.executed==before
        assert not source.capabilities().actual_database_verified
        assert source.capabilities().capabilities["query_cancel"]=="unknown"


@pytest.mark.parametrize("verify_runtime",[False,True])
def test_cli_connect_check_uses_single_catalog_preflight(tmp_path,monkeypatch,capsys,verify_runtime):
    import json
    import lightgraph.cli as cli
    import lightgraph.sources as sources
    from lightgraph.sources import QueryBudget
    config=tmp_path/"gauss.yaml"
    config.write_text("sources:\n  - id: gauss\n    kind: gauss_database\n    dsn_env: AUDIT_TEST_DSN\n    allowed_tables: [public.items]\nmax_db_queries: 20\n")
    monkeypatch.setenv("AUDIT_TEST_DSN","TEST_ONLY_DSN")
    db=Connection();probes=[]
    def factory(dsn,**kwargs):return GaussSource(dsn,connect_factory=lambda _:db,**kwargs)
    def probe(self,*,cancel):
        probes.append(cancel)
        self._manifest.capabilities["query_cancel" if cancel else "statement_timeout"]="verified"
    monkeypatch.setattr(sources,"GaussSource",factory)
    monkeypatch.setattr(GaussSource,"_probe_runtime_control",probe)
    args=["connect-check",str(config)]+(["--verify-runtime"] if verify_runtime else [])
    assert cli.main(args)==0
    output=capsys.readouterr().out
    manifest=json.loads(output)["gauss"]
    assert "TEST_ONLY_DSN" not in output
    assert manifest["actual_database_verified"] is verify_runtime
    assert sum("SELECT version" in sql for sql,_ in db.executed)==1
    assert sum("information_schema.columns" in sql for sql,_ in db.executed)==1
    assert probes==([False,True] if verify_runtime else [])
    assert db.closed


@pytest.mark.parametrize("cancel_outcome", ["cancelled", "driver_error", "ignored", "late", "never"])
def test_cancel_probe_cannot_be_satisfied_by_short_statement_timeout(monkeypatch, cancel_outcome):
    import re
    import threading
    from types import SimpleNamespace
    from lightgraph.contracts import QueryLimits

    timers = []

    class ProbeTimer:
        """Control query/cancel ordering without relying on OS timer scheduling."""

        def __init__(self, interval, callback):
            self.callback = callback
            self.started = self.stopped = self.joined = False
            timers.append(self)

        def start(self):
            self.started = True

        def fire(self):
            assert self.started
            self.callback()

        def cancel(self):
            self.stopped = True

        def join(self):
            # A callback already running cannot be stopped by Timer.cancel().
            # Model it returning only after the query has finished normally.
            if cancel_outcome == "late":
                self.fire()
            self.joined = True

    # Replace only this adapter's timer; QueryBudget has independent deadlines.
    monkeypatch.setattr("lightgraph.sources.gauss.threading", SimpleNamespace(
        Timer=ProbeTimer, Event=threading.Event, RLock=threading.RLock,
    ))

    class Cancelled(Exception):sqlstate="57014"
    class RuntimeCursor(Cursor):
        def execute(self,sql,parameters=None):
            if sql.startswith("SET LOCAL statement_timeout"):
                self.connection.timeout=int(re.search(r"'([0-9]+)ms'",sql).group(1))
            if "pg_sleep" in sql:
                if self.connection.timeout<250:raise Cancelled("PRIVATE_TIMEOUT_DETAIL")
                if cancel_outcome not in {"late", "never"}:
                    timers[0].fire()
                if self.connection.cancelled:raise Cancelled("PRIVATE_CANCEL_DETAIL")
            return super().execute(sql,parameters)
    class RuntimeConnection(Connection):
        timeout=0
        def cursor(self,name=None):return RuntimeCursor(self,name)
        def cancel(self):
            if cancel_outcome in {"cancelled", "late"}:self.cancelled=True
            elif cancel_outcome == "driver_error":raise RuntimeError("PRIVATE_DRIVER_DETAIL")
    db=RuntimeConnection()
    with GaussSource("secret",allowed_tables=["public.items"],limits=QueryLimits(timeout_ms=1),connect_factory=lambda _:db) as source:
        if cancel_outcome == "cancelled":
            source._probe_runtime_control(cancel=True)
            assert source.capabilities().capabilities["query_cancel"]=="verified"
        else:
            with pytest.raises(LightGraphError,match="^query_cancel_unverified$"):
                source._probe_runtime_control(cancel=True)
            assert source.capabilities().capabilities["query_cancel"]=="unknown"
    assert len(timers) == 1 and timers[0].stopped and timers[0].joined
    assert db.closed
