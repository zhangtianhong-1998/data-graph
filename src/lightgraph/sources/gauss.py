"""Gauss DB-API adapter with explicit capability evidence and read-only sessions.

No customer instance has been used to verify this adapter. Product/version and
runtime controls are probed on connection; unsupported features remain unknown.
"""
from __future__ import annotations
from contextlib import contextmanager
import re
import threading
import time
import uuid
from typing import Callable
import sqlglot
from lightgraph.contracts import CapabilityManifest, ColumnSpec, ForeignKeySpec, QueryLimits, TableSpec
from lightgraph.errors import LightGraphError
from lightgraph.query import QueryCompiler
from .base import SourceAdapter, QueryStats, cursor_batches
from .budget import QueryBudget, BudgetedCursor

REQUIRED_RUNTIME = {"read_only_transaction", "parameter_binding", "metadata_reading", "distinct_projection", "statement_timeout", "query_cancel"}


def product_version(banner: str) -> tuple[str, str]:
    """Bind a version adjacent to the actual product, never PostgreSQL/compiler.

    Unrecognized or conflicting banners remain unknown and cannot qualify live
    instance access. The original banner is neither retained nor exported.
    """
    numeric = r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9][A-Za-z0-9.+_-]*)?"
    enterprise = r"V[0-9]+R[0-9]+C[0-9]+(?:SPC[0-9]+)?"
    for product in ("GaussDB", "openGauss"):
        if not re.search(r"\b" + product + r"\b", banner, re.I):
            continue
        pattern = r"\b" + product + r"\s+(?:Kernel\s+)?(?:version\s+)?(" + enterprise + "|" + numeric + r")(?=[\s,)\]]|$)"
        versions = set(re.findall(pattern, banner, re.I))
        return product, next(iter(versions)) if len(versions) == 1 else "unknown"
    return "unknown", "unknown"

class GaussSource(SourceAdapter):
    def __init__(self, dsn: str, *, source_id: str = "gauss", allowed_tables: list[str], allowed_columns: dict[str, list[str]] | None = None, limits: QueryLimits | None = None, verified_capabilities: dict | CapabilityManifest | None = None, connect_factory: Callable | None = None, probe_runtime_limits: bool = False, query_budget: QueryBudget | None = None):
        if not allowed_tables or any(len(t.split(".")) != 2 or any(not p for p in t.split(".")) for t in allowed_tables):
            raise LightGraphError("authorized_catalog_required")
        if len(allowed_tables) != len(set(allowed_tables)):
            raise LightGraphError("duplicate_authorized_table")
        if allowed_columns is not None and set(allowed_columns) - set(allowed_tables):
            raise LightGraphError("unauthorized_column_scope")
        self.source_id = source_id; self.allowed_tables = tuple(allowed_tables)
        self.allowed_columns = allowed_columns or {}; self.limits = limits or QueryLimits()
        self.stats = QueryStats(); self._lock = threading.RLock(); self._depth = 0
        self.query_budget = query_budget or QueryBudget()
        self._closed = False; self._cancelled = False; self._tables = []
        self._manifest = CapabilityManifest(driver="psycopg_dbapi", capabilities={k: "unknown" for k in REQUIRED_RUNTIME | {"cursor_paging", "cte", "union_all"}})
        # Qualification records are bound to actual product/version/driver.
        self._qualification = verified_capabilities.model_dump() if isinstance(verified_capabilities, CapabilityManifest) else verified_capabilities
        if self._qualification is not None:
            allowed = {"product", "version", "driver", "capabilities", "qualification_ref", "consistent_snapshot", "actual_database_verified"}
            if not isinstance(self._qualification, dict) or set(self._qualification) - allowed or not {"product", "version", "driver", "capabilities"} <= set(self._qualification):
                raise LightGraphError("qualified_capability_manifest_required")
            if not isinstance(self._qualification["capabilities"], dict):
                raise LightGraphError("invalid_capability_manifest")
        try:
            if connect_factory is None:
                import psycopg
                self._manifest.driver = f"psycopg/{psycopg.__version__}"
                self._connection = psycopg.connect(dsn, connect_timeout=max(1, min(30, self.limits.timeout_ms // 1000)))
            else:
                self._connection = connect_factory(dsn)
            self.preflight(probe_runtime_limits=probe_runtime_limits)
        except LightGraphError:
            if hasattr(self, "_connection"): self._connection.close()
            raise
        except Exception:
            if hasattr(self, "_connection"): self._connection.close()
            raise LightGraphError("database_connection_failed") from None
        self.compiler = QueryCompiler(self._tables, dialect="postgres")

    def __repr__(self): return f"GaussSource(source_id={self.source_id!r}, product={self._manifest.product!r})"

    def _cursor(self, *, name=None):
        cursor = self._connection.cursor(name=name) if name else self._connection.cursor()
        return BudgetedCursor(cursor, self.query_budget, count_returns=name is None, cancel=self._connection.cancel)

    def _apply_qualification(self):
        if self._qualification is None: return
        for key in ("product", "version", "driver"):
            if self._qualification[key] == "unknown" or self._qualification[key] != getattr(self._manifest, key):
                raise LightGraphError("capability_manifest_binding_mismatch")
        for key, state in self._qualification["capabilities"].items():
            if key not in self._manifest.capabilities or state not in ("verified", "unknown", "unsupported"):
                raise LightGraphError("invalid_capability_manifest")
            if key in ("statement_timeout", "query_cancel"):
                self._manifest.capabilities[key] = state

    def _start_readonly(self):
        cursor = self._cursor()
        try:
            cursor.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            # Fixed adapter controls only. Model/rule SQL cannot access SET.
            cursor.execute("SET LOCAL search_path TO pg_catalog")
            cursor.execute(f"SET LOCAL statement_timeout = '{int(self.limits.timeout_ms)}ms'")
            cursor.execute("SET LOCAL lock_timeout = '5000ms'")
            cursor.execute("SELECT current_setting('transaction_read_only'), current_setting('transaction_isolation')")
            values = cursor.fetchone()
            if not values or str(values[0]).lower() not in ("on", "true") or str(values[1]).lower() != "repeatable read":
                raise LightGraphError("read_only_transaction_unverified")
        finally: cursor.close()

    def preflight(self, *, probe_runtime_limits: bool = False) -> CapabilityManifest:
        """Bounded controls; runtime cancellation tests require explicit opt-in.

        Opt-in must be run in a deployment-approved isolated environment. The
        only deliberate wait is 0.25 seconds and it reads no customer data.
        """
        with self._lock:
            try:
                self._connection.rollback(); self._start_readonly()
                cursor = self._cursor()
                try:
                    cursor.execute("SELECT version()")
                    version = str(cursor.fetchone()[0])
                    self._manifest.product, self._manifest.version = product_version(version)
                    self._manifest.capabilities["read_only_transaction"] = "verified"
                    cursor.execute("SELECT %s", (71,))
                    if cursor.fetchone()[0] != 71: raise LightGraphError("parameter_binding_unverified")
                    self._manifest.capabilities["parameter_binding"] = "verified"
                    cursor.execute("SELECT DISTINCT 1 AS n")
                    if cursor.fetchone()[0] != 1: raise LightGraphError("distinct_projection_unverified")
                    self._manifest.capabilities["distinct_projection"] = "verified"
                    self._tables = self._read_catalog(cursor)
                    self._manifest.capabilities["metadata_reading"] = "verified"
                    try:
                        cursor.execute("WITH p AS (SELECT 1 AS n) SELECT n FROM p UNION ALL SELECT 2")
                        if [r[0] for r in cursor.fetchall()] == [1, 2]:
                            self._manifest.capabilities["cte"] = "verified"
                            self._manifest.capabilities["union_all"] = "verified"
                    except Exception:
                        self._manifest.capabilities["cte"] = "unsupported"
                        self._manifest.capabilities["union_all"] = "unknown"
                finally: cursor.close()
                self._connection.rollback()
                self._apply_qualification()
                if probe_runtime_limits:
                    self.verify_runtime_controls()
                else:
                    self._manifest.actual_database_verified = False
            except LightGraphError: raise
            except Exception: raise LightGraphError("database_preflight_failed") from None
            finally:
                self._connection.rollback()
                self._manifest.consistent_snapshot = False
            return self.capabilities()

    def verify_runtime_controls(self) -> CapabilityManifest:
        """Verify timeout/cancel on this connection without re-reading metadata."""
        with self._lock:
            if self._closed: raise LightGraphError("source_closed")
            if self._depth: raise LightGraphError("capability_probe_during_snapshot")
            self._manifest.actual_database_verified = False
            if "unknown" in (self._manifest.product, self._manifest.version):
                raise LightGraphError("product_version_unverified")
            for key in ("statement_timeout", "query_cancel"):
                self._manifest.capabilities[key] = "unknown"
            try:
                self._probe_runtime_control(cancel=False)
                self._probe_runtime_control(cancel=True)
                self._manifest.actual_database_verified = all(
                    self._manifest.capabilities.get(k) == "verified" for k in REQUIRED_RUNTIME)
            except LightGraphError: raise
            except Exception: raise LightGraphError("database_runtime_preflight_failed") from None
            finally:
                self._manifest.consistent_snapshot = False
                try: self._connection.rollback()
                except Exception: raise LightGraphError("snapshot_close_failed") from None
            return self.capabilities()

    def _read_catalog(self, cursor) -> list[TableSpec]:
        tables = []
        attribute_maps = {}
        for name in self.allowed_tables:
            schema, table = name.split(".")
            # Base tables only: a view could invoke a privileged/volatile UDF.
            cursor.execute("SELECT c.column_name, c.data_type, c.is_nullable FROM information_schema.columns c JOIN information_schema.tables t ON c.table_schema=t.table_schema AND c.table_name=t.table_name WHERE c.table_schema=%s AND c.table_name=%s AND t.table_type='BASE TABLE' ORDER BY c.ordinal_position", (schema, table))
            columns = []
            for col, dtype, nullable in cursor.fetchall():
                if name in self.allowed_columns and col not in self.allowed_columns[name]: continue
                columns.append(ColumnSpec(name=col, data_type=dtype, nullable=nullable == "YES"))
            if not columns: raise LightGraphError("authorized_table_unavailable_or_view")
            if name in self.allowed_columns and not set(self.allowed_columns[name]) <= {c.name for c in columns}:
                raise LightGraphError("authorized_column_unavailable")
            cursor.execute("SELECT a.attname, pg_catalog.col_description(c.oid, a.attnum), a.attnum, EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conrelid=c.oid AND k.contype='p' AND a.attnum=ANY(k.conkey)) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped", (schema, table))
            annotation_rows = cursor.fetchall()
            annotations = {r[0]: (r[1] or "", bool(r[3])) for r in annotation_rows}
            attribute_maps[name] = {int(r[2]): r[0] for r in annotation_rows}
            for column in columns:
                column.comment, column.primary_key = annotations.get(column.name, ("", False))
                if len(column.comment.encode("utf-8")) > self.limits.max_batch_bytes:
                    raise LightGraphError("metadata_size_limit")
            cursor.execute("SELECT pg_catalog.obj_description(c.oid, 'pg_class') FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s", (schema, table))
            record = cursor.fetchone()
            tables.append(TableSpec(source_id=self.source_id, schema=schema, name=table, columns=columns, comment=(record[0] or "") if record else ""))
        self._read_constraints(cursor, tables, attribute_maps)
        return tables

    def _read_constraints(self, cursor, tables, attribute_maps):
        catalog = {t.qualified_name: t for t in tables}
        def numbers(value):
            if isinstance(value, (list, tuple)): result = list(value)
            elif isinstance(value, str) and re.fullmatch(r"\{[0-9,]*\}", value):
                result = value[1:-1].split(",") if value[1:-1] else []
            elif value is None: result = []
            else: raise LightGraphError("constraint_metadata_unsupported")
            if len(result) > 64: raise LightGraphError("constraint_metadata_limit")
            return [int(i) for i in result]
        for table in tables:
            cursor.execute("SELECT k.conname, k.contype, k.conkey, k.confkey, rn.nspname, rc.relname FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class c ON c.oid=k.conrelid JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_catalog.pg_class rc ON rc.oid=k.confrelid LEFT JOIN pg_catalog.pg_namespace rn ON rn.oid=rc.relnamespace WHERE n.nspname=%s AND c.relname=%s AND k.contype IN ('p','u','f')", (table.schema_name, table.name))
            for name, kind, local_keys, remote_keys, remote_schema, remote_table in cursor.fetchall():
                local = [attribute_maps[table.qualified_name].get(i) for i in numbers(local_keys)]
                if not local or any(c not in table.column_names for c in local): continue
                if kind in ("p", "u"):
                    table.unique_keys.append(local)
                elif kind == "f":
                    target_name = f"{remote_schema}.{remote_table}"
                    if target_name not in catalog: continue
                    target = catalog[target_name]
                    remote = [attribute_maps[target_name].get(i) for i in numbers(remote_keys)]
                    if len(local) != len(remote) or any(c not in target.column_names for c in remote): continue
                    table.foreign_keys.append(ForeignKeySpec(name=name, columns=local, target_schema=remote_schema, target_table=remote_table, target_columns=remote))

    def _probe_runtime_control(self, *, cancel: bool):
        self._connection.rollback(); self._start_readonly(); cursor = self._cursor()
        interrupted = False; timer = None; cancel_sent = threading.Event()
        try:
            if cancel:
                # A short configured statement timeout must not masquerade as
                # successful client cancellation (both report SQLSTATE 57014).
                cursor.execute("SET LOCAL statement_timeout = '1000ms'")
                def send_cancel():
                    try:
                        self._connection.cancel()
                        cancel_sent.set()
                    except Exception:
                        # Driver details may contain secrets; the probe below
                        # reports only query_cancel_unverified.
                        pass
                timer = threading.Timer(0.05, send_cancel); timer.start()
            else: cursor.execute("SET LOCAL statement_timeout = '50ms'")
            try: cursor.execute("SELECT pg_catalog.pg_sleep(0.25)")
            except Exception as exc:
                interrupted = getattr(exc, "sqlstate", None) == "57014" or getattr(exc, "pgcode", None) == "57014"
        finally:
            if timer: timer.cancel(); timer.join()
            cursor.close(); self._connection.rollback()
        if cancel and not cancel_sent.is_set(): interrupted = False
        if not interrupted: raise LightGraphError("query_cancel_unverified" if cancel else "statement_timeout_unverified")
        # Successful query after rollback confirms the connection has left the
        # cancelled query; driver cancel returning alone is insufficient.
        self._start_readonly(); check = self._cursor()
        try:
            check.execute("SELECT 1")
            if check.fetchone()[0] != 1: raise LightGraphError("runtime_control_unverified")
        finally: check.close(); self._connection.rollback()
        self._manifest.capabilities["query_cancel" if cancel else "statement_timeout"] = "verified"

    def describe(self): return [t.model_copy(deep=True) for t in self._tables]
    def capabilities(self): return self._manifest.model_copy(deep=True)
    @contextmanager
    def snapshot(self):
        with self._lock:
            if self._closed: raise LightGraphError("source_closed")
            if any(self._manifest.capabilities.get(k) != "verified" for k in REQUIRED_RUNTIME):
                raise LightGraphError("runtime_capability_unverified")
            outer = self._depth == 0
            if outer:
                try: self._start_readonly()
                except LightGraphError: raise
                except Exception: raise LightGraphError("read_only_transaction_failed") from None
                self._manifest.consistent_snapshot = True
            self._depth += 1
            try: yield self
            finally:
                self._depth -= 1
                if outer:
                    self._manifest.consistent_snapshot = False
                    try: self._connection.rollback()
                    except Exception: raise LightGraphError("snapshot_close_failed") from None
    def execute(self, query, *, row_limit=None):
        with self.snapshot():
            self.compiler.guard.validate(query.sql, query.parameters)
            sql = sqlglot.parse_one(query.sql, read="postgres").sql(dialect="postgres")
            # psycopg percent escaping applies inside identifiers too.
            sql = re.sub(r"%(?!\([a-zA-Z0-9_]+\)s)", "%%", sql)
            start = time.monotonic(); self.stats.queries += 1; self._cancelled = False
            cursor = None; timed_out = threading.Event()
            def timeout():
                timed_out.set()
                try: self._connection.cancel()
                except Exception: pass
            self.query_budget.check_time()
            timer = threading.Timer(min(self.limits.timeout_ms / 1000, max(0.001, self.query_budget.deadline - time.monotonic())), timeout)
            try:
                # A real server-side cursor prevents libpq from buffering the
                # whole result even when the Python code fetches one row.
                cursor = self._cursor(name="lightgraph_" + uuid.uuid4().hex)
                timer.start(); cursor.execute(sql, query.parameters)
                columns = tuple(d.name if hasattr(d, "name") else d[0] for d in cursor.description)
                yield from cursor_batches(cursor, limits=self.limits, columns=columns, row_limit=row_limit or self.limits.max_result_rows, stats=self.stats, deadline=start + self.limits.timeout_ms / 1000, cancelled=lambda: self._cancelled, budget=self.query_budget)
                self._manifest.capabilities["cursor_paging"] = "verified"
            except LightGraphError: raise
            except Exception as exc:
                self.query_budget.check_time()
                state = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
                code = "query_timeout" if timed_out.is_set() or state == "57014" and not self._cancelled else "query_cancelled" if self._cancelled else "query_execution_failed"
                raise LightGraphError(code) from None
            finally:
                timer.cancel(); timer.join() if timer.ident else None
                if cursor is not None:
                    try: cursor.close()
                    except Exception: pass
                self.stats.elapsed_seconds += time.monotonic() - start
    def cancel(self, query_id=None):
        self._cancelled = True
        try: self._connection.cancel()
        except Exception: raise LightGraphError("query_cancel_failed") from None
    def close(self):
        with self._lock:
            if not self._closed: self._connection.close(); self._closed = True
