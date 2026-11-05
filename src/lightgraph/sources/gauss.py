"""Gauss DB-API adapter with explicit capability evidence and read-only sessions.

No customer instance has been used to verify this adapter. Product/version and
runtime controls are probed on connection; unsupported features remain unknown.
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
import re
import logging
import math
import sys
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
from .gauss_compat import catalog_value, catalog_bool, safe_database_cause, set_optional_lock_timeout
from .database_errors import connection_is_closed, database_diagnostic, database_error
from .reconnect import configure_reconnection, new_snapshot_recovery_allowed, reconnect_at_boundary, terminal_failure, latch_terminal_failure, prepare_snapshot

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
    def __init__(self, dsn: str, *, source_id: str = "gauss", allowed_tables: list[str], allowed_columns: dict[str, list[str]] | None = None, limits: QueryLimits | None = None, verified_capabilities: dict | CapabilityManifest | None = None, connect_factory: Callable | None = None, probe_runtime_limits: bool = False, query_budget: QueryBudget | None = None, metadata_only: bool = False, _keep_connection: bool = False):
        if not allowed_tables or any(len(t.split(".")) != 2 or any(not p for p in t.split(".")) for t in allowed_tables):
            raise LightGraphError("authorized_catalog_required")
        if len(allowed_tables) != len(set(allowed_tables)):
            raise LightGraphError("duplicate_authorized_table")
        if allowed_columns is not None and set(allowed_columns) - set(allowed_tables):
            raise LightGraphError("unauthorized_column_scope")
        self.source_id = source_id; self.allowed_tables = tuple(allowed_tables)
        self.metadata_only = metadata_only
        self.allowed_columns = allowed_columns or {}; self.limits = limits or QueryLimits()
        self.stats = QueryStats(); self._lock = threading.RLock(); self._depth = 0
        self.query_budget = query_budget or QueryBudget()
        self._closed = False; self._cancelled = False; self._tables = []
        self._physical_column_names = {}
        self._lock_timeout_supported = None
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
            self.query_budget.check_time()
            if connect_factory is None:
                import psycopg
                self._manifest.driver = f"psycopg/{psycopg.__version__}"
                remaining = max(1, math.ceil(self.query_budget.deadline - time.monotonic()))
                self._connection = psycopg.connect(dsn, connect_timeout=max(1,
                    min(30, self.limits.timeout_ms // 1000, remaining)))
            else:
                self._connection = connect_factory(dsn)
            if connection_is_closed(self._connection):
                raise LightGraphError('database_connection_failed')
            self.query_budget.check_time()
            self.preflight(probe_runtime_limits=probe_runtime_limits)
            if probe_runtime_limits and (not self._manifest.actual_database_verified or
                    any(self._manifest.capabilities.get(k) != 'verified' for k in REQUIRED_RUNTIME)):
                raise LightGraphError('runtime_capability_unverified')
        except BaseException as exc:
            if hasattr(self, '_connection'):
                try: self._connection.close()
                except Exception: pass
            if isinstance(exc, LightGraphError) or not isinstance(exc, Exception):
                raise
            raise database_error('database_connection_failed', exc, engine='gauss',
                stage='connection_preflight') from None
        self.compiler = QueryCompiler(self._tables, dialect="postgres",
            physical_columns=self._physical_column_names)
        reopen_options = {'source_id': source_id, 'allowed_tables': list(self.allowed_tables),
            'allowed_columns': deepcopy(self.allowed_columns), 'limits': self.limits.model_copy(deep=True),
            'verified_capabilities': deepcopy(self._qualification), 'connect_factory': connect_factory,
            'metadata_only': metadata_only}
        configure_reconnection(self, lambda probe, budget: GaussSource(dsn, **reopen_options,
            probe_runtime_limits=probe, query_budget=budget, _keep_connection=True))
        if not _keep_connection:
            self._release_snapshot_connection()

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
            cursor.execute(f"SET LOCAL statement_timeout = '{int(self.limits.timeout_ms)}ms'")
            if self._lock_timeout_supported is not False:
                self._lock_timeout_supported = set_optional_lock_timeout(cursor, 5000)
            cursor.execute("SELECT pg_catalog.current_setting('transaction_read_only'), pg_catalog.current_setting('transaction_isolation')")
            values = cursor.fetchone()
            if not values or str(catalog_value(values[0])).lower() not in ("on", "true") or str(catalog_value(values[1])).lower() != "repeatable read":
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
                    cursor.execute("SELECT pg_catalog.version()")
                    version = str(catalog_value(cursor.fetchone()[0]))
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
            except Exception as exc:
                raise database_error('database_preflight_failed', exc, engine='gauss',
                    stage='connection_preflight', connection_closed=connection_is_closed(self._connection)) from None
            finally:
                active_error = sys.exc_info()[1]
                try:
                    if not self._closed: self._connection.rollback()
                except Exception as exc:
                    if active_error is None:
                        raise database_error('snapshot_close_failed', exc, engine='gauss',
                            stage='connection_preflight', rollback_succeeded=False) from None
                    if isinstance(active_error, LightGraphError):
                        active_error.database_diagnostic = {**getattr(active_error, 'database_diagnostic', {}),
                            'rollback_succeeded': False}
                finally:
                    self._manifest.consistent_snapshot = False
            return self.capabilities()

    def verify_runtime_controls(self):
        """Qualify a physical connection, releasing an explicitly acquired lease."""
        with self._lock:
            acquired = getattr(self, '_idle_released', False)
            if acquired:
                stopped = terminal_failure(self)
                if stopped is not None: raise stopped
                if self._disposed: raise LightGraphError('source_closed')
                if self._cancelled: raise LightGraphError('query_cancelled')
                self._boundary_recovery_used = False
                self._reconnect()
            try:
                # A previously runtime-qualified source repeats these probes
                # inside replacement construction, once per physical session.
                if acquired and self._manifest.actual_database_verified:
                    return self.capabilities()
                return self._verify_runtime_controls_current()
            finally:
                if acquired: self._release_snapshot_connection()

    def _verify_runtime_controls_current(self) -> CapabilityManifest:
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
            except Exception as exc:
                raise LightGraphError("database_runtime_preflight_failed") from safe_database_cause(exc, logging.getLogger(__name__))
            finally:
                self._manifest.consistent_snapshot = False
                if not self._closed:
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
            self._physical_column_names[name] = set()
            for row in cursor.fetchall():
                col, dtype, nullable = (catalog_value(value) for value in row)
                self._physical_column_names[name].add(col)
                if name in self.allowed_columns and col not in self.allowed_columns[name]: continue
                columns.append(ColumnSpec(name=col, data_type=dtype, nullable=nullable == "YES"))
            if not columns: raise LightGraphError("authorized_table_unavailable_or_view")
            if name in self.allowed_columns and not set(self.allowed_columns[name]) <= {c.name for c in columns}:
                raise LightGraphError("authorized_column_unavailable")
            cursor.execute("SELECT a.attname, pg_catalog.col_description(c.oid, a.attnum), a.attnum, EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conrelid=c.oid AND k.contype='p' AND a.attnum=ANY(k.conkey)) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped", (schema, table))
            annotation_rows = [tuple(catalog_value(value) for value in row) for row in cursor.fetchall()]
            annotations = {r[0]: (r[1] or "", catalog_bool(r[3])) for r in annotation_rows}
            attribute_maps[name] = {int(r[2]): r[0] for r in annotation_rows}
            for column in columns:
                column.comment, column.primary_key = annotations.get(column.name, ("", False))
                if len(column.comment.encode("utf-8")) > self.limits.max_batch_bytes:
                    raise LightGraphError("metadata_size_limit")
            cursor.execute("SELECT pg_catalog.obj_description(c.oid, 'pg_class') FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s", (schema, table))
            record = cursor.fetchone()
            tables.append(TableSpec(source_id=self.source_id, schema=schema, name=table, columns=columns, comment=(catalog_value(record[0]) or "") if record else ""))
        self._read_constraints(cursor, tables, attribute_maps)
        return tables

    def _read_constraints(self, cursor, tables, attribute_maps):
        catalog = {t.qualified_name: t for t in tables}
        def numbers(value):
            value = catalog_value(value)
            if isinstance(value, (list, tuple)): result = list(value)
            elif isinstance(value, str) and re.fullmatch(r"\{[0-9,]*\}", value):
                result = value[1:-1].split(",") if value[1:-1] else []
            elif value is None: result = []
            else: raise LightGraphError("constraint_metadata_unsupported")
            if len(result) > 64: raise LightGraphError("constraint_metadata_limit")
            return [int(i) for i in result]
        for table in tables:
            cursor.execute("SELECT k.conname, k.contype, k.conkey, k.confkey, rn.nspname, rc.relname FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class c ON c.oid=k.conrelid JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_catalog.pg_class rc ON rc.oid=k.confrelid LEFT JOIN pg_catalog.pg_namespace rn ON rn.oid=rc.relnamespace WHERE n.nspname=%s AND c.relname=%s AND k.contype IN ('p','u','f')", (table.schema_name, table.name))
            for row in cursor.fetchall():
                name, kind, local_keys, remote_keys, remote_schema, remote_table = (catalog_value(value) for value in row)
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
                query_connection = self._connection
                def send_cancel():
                    try:
                        query_connection.cancel()
                        cancel_sent.set()
                    except Exception:
                        # Driver details may contain secrets; the probe below
                        # reports only query_cancel_unverified.
                        pass
                timer = threading.Timer(0.05, send_cancel); timer.daemon = True; timer.start()
            else: cursor.execute("SET LOCAL statement_timeout = '50ms'")
            try: cursor.execute("SELECT pg_catalog.pg_sleep(0.25)")
            except Exception as exc:
                interrupted = getattr(exc, "sqlstate", None) == "57014" or getattr(exc, "pgcode", None) == "57014"
        finally:
            if timer:
                timer.cancel()
                timer.join(timeout=min(2, max(.001, self.query_budget.deadline - time.monotonic())))
                if timer.is_alive():
                    # Driver cancellation can block on a separate network
                    # connection. Never reuse this session after a late cancel,
                    # or let its background thread hold the worker open.
                    self._closed = True
                    try: cursor.close()
                    except Exception: pass
                    try: self._connection.close()
                    except Exception: pass
                    raise LightGraphError('query_cancel_unverified')
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

    @property
    def terminal_failure_code(self):
        return self._terminal_failure_code

    @property
    def terminal_failure_diagnostic(self):
        return deepcopy(self._terminal_failure_diagnostic)

    def _release_snapshot_connection(self):
        """Release only the physical lease; authorized metadata stays available."""
        eligible = self._reconnect_allowed
        self._discard_connection()
        self._idle_released = True
        self._reconnect_allowed = eligible
        self._manifest.consistent_snapshot = False

    @property
    def new_snapshot_recovery_allowed(self):
        return new_snapshot_recovery_allowed(self, required_capabilities=REQUIRED_RUNTIME,
            disconnected=not self._closed and connection_is_closed(self._connection))

    def _discard_connection(self, *, reconnect_allowed=False):
        """A failed rollback or late cancellation makes this session unusable."""
        self._manifest.consistent_snapshot = False
        self._reconnect_allowed = reconnect_allowed
        if not self._closed:
            self._closed = True
            try: self._connection.close()
            except Exception: pass

    def _reconnect(self):
        def adopt(replacement):
            for field in ('_connection', '_manifest', '_tables', '_physical_column_names',
                          '_lock_timeout_supported', 'compiler'):
                setattr(self, field, getattr(replacement, field))
            self._closed = self._cancelled = False
            replacement._closed = replacement._disposed = True
            replacement._reopen = None
        reconnect_at_boundary(self, engine='gauss', discard=self._discard_connection, adopt=adopt)

    def _begin_snapshot(self):
        """Recover only an aborted transaction before any snapshot rows exist."""
        for attempt in range(2):
            try:
                self._start_readonly()
                return
            except BaseException as exc:
                # BEGIN can succeed before a SET/check fails. This cleanup must
                # also run when the context manager never reaches its yield.
                self._manifest.consistent_snapshot = False
                was_closed = connection_is_closed(self._connection)
                try:
                    self._connection.rollback()
                    rolled_back = True
                except Exception:
                    rolled_back = False
                    self._discard_connection(reconnect_allowed=was_closed)
                if isinstance(exc, LightGraphError) or not isinstance(exc, Exception):
                    self._reconnect_allowed = False
                    if isinstance(exc, LightGraphError) and exc.code == 'read_only_transaction_unverified':
                        self._manifest.capabilities['read_only_transaction'] = 'unknown'
                    raise
                diagnostic = database_diagnostic(exc, engine='gauss', stage='snapshot_start')
                if diagnostic['sqlstate'] == '25P02' and rolled_back and attempt == 0:
                    # Reapply all readonly/isolation/time controls. This is not
                    # a retry of an interrupted or partially consumed query.
                    continue
                reconnect = (diagnostic['category'] in {'connection', 'idle_transaction_timeout', 'shutdown', 'unavailable'}
                             or diagnostic['category'] == 'unknown' and (was_closed or not rolled_back))
                self._reconnect_allowed = reconnect
                if reconnect:
                    self._discard_connection(reconnect_allowed=True)
                elif was_closed:
                    self._discard_connection()
                recovery = ('reconnect_required' if reconnect or not rolled_back else
                            'rollback_rebegin_failed' if attempt else 'rolled_back')
                raise database_error('read_only_transaction_failed', exc, engine='gauss',
                    stage='snapshot_start', rollback_succeeded=rolled_back, recovery=recovery,
                    connection_closed=was_closed
                ) from safe_database_cause(exc, logging.getLogger(__name__))

    @contextmanager
    def snapshot(self):
        with self._lock:
            stopped = terminal_failure(self)
            if stopped is not None: raise stopped
            if self._disposed: raise LightGraphError('source_closed')
            if self._cancelled: raise LightGraphError('query_cancelled')
            if self.metadata_only: raise LightGraphError('metadata_only_query_forbidden')
            if any(self._manifest.capabilities.get(k) != 'verified' for k in REQUIRED_RUNTIME):
                raise LightGraphError('runtime_capability_unverified')
            outer = self._depth == 0
            if outer:
                prepare_snapshot(self, engine='gauss', begin=self._begin_snapshot,
                    disconnected=connection_is_closed(self._connection))
                self._manifest.consistent_snapshot = True
            elif self._closed:
                raise LightGraphError('source_closed')
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
                if outer:
                    self._manifest.consistent_snapshot = False
                    active_error = sys.exc_info()[1]
                    try:
                        if not self._closed:
                            try: self._connection.rollback()
                            except Exception as exc:
                                self._discard_connection(reconnect_allowed=not self._cancelled)
                                if active_error is None:
                                    raise database_error('snapshot_close_failed', exc, engine='gauss',
                                        stage='snapshot_close', rollback_succeeded=False,
                                        recovery='reconnect_required') from None
                                if isinstance(active_error, LightGraphError):
                                    active_error.database_diagnostic = {**getattr(active_error, 'database_diagnostic', {}),
                                        'rollback_succeeded': False, 'recovery': 'reconnect_required'}
                    finally:
                        # No transaction or network connection survives Agent
                        # think-time. Nested queries share this lease only.
                        self._release_snapshot_connection()

    def execute(self, query, *, row_limit=None):
        # Reject unauthorized SQL before reconnecting or touching a transaction.
        self.compiler.guard.validate(query.sql, query.parameters)
        with self.snapshot():
            sql = sqlglot.parse_one(query.sql, read="postgres").sql(dialect="postgres")
            # psycopg percent escaping applies inside identifiers too.
            sql = re.sub(r"%(?!\([a-zA-Z0-9_]+\)s)", "%%", sql)
            start = time.monotonic(); self.stats.queries += 1; self._cancelled = False
            cursor = None; timed_out = threading.Event()
            query_connection = self._connection
            def timeout():
                timed_out.set()
                try: query_connection.cancel()
                except Exception: pass
            self.query_budget.check_time()
            timer = threading.Timer(min(self.limits.timeout_ms / 1000, max(0.001, self.query_budget.deadline - time.monotonic())), timeout)
            timer.daemon = True
            try:
                # A real server-side cursor prevents libpq from buffering the
                # whole result even when the Python code fetches one row.
                cursor = self._cursor(name="lightgraph_" + uuid.uuid4().hex)
                timer.start(); cursor.execute(sql, query.parameters)
                columns = tuple(catalog_value(d.name if hasattr(d, "name") else d[0]) for d in cursor.description)
                yield from cursor_batches(cursor, limits=self.limits, columns=columns, row_limit=row_limit or self.limits.max_result_rows, stats=self.stats, deadline=start + self.limits.timeout_ms / 1000, cancelled=lambda: self._cancelled, budget=self.query_budget)
                if self._cancelled: raise LightGraphError('query_cancelled')
                if timed_out.is_set(): raise LightGraphError('query_timeout')
                self._manifest.capabilities["cursor_paging"] = "verified"
            except LightGraphError: raise
            except Exception as exc:
                self.query_budget.check_time()
                state = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
                code = "query_timeout" if timed_out.is_set() or state == "57014" and not self._cancelled else "query_cancelled" if self._cancelled else "query_execution_failed"
                diagnostic = database_diagnostic(exc, engine='gauss', stage='query')
                reconnect = (diagnostic['category'] in {'connection', 'idle_transaction_timeout', 'shutdown', 'unavailable'}
                             or diagnostic['category'] == 'unknown' and connection_is_closed(self._connection))
                if reconnect:
                    self._discard_connection(reconnect_allowed=True)
                raise database_error(code, exc, engine='gauss', stage='query',
                    recovery='reconnect_required' if reconnect else 'none',
                    connection_closed=connection_is_closed(query_connection)
                ) from safe_database_cause(exc, logging.getLogger(__name__))
            finally:
                timer.cancel()
                if timer.ident: timer.join(timeout=2)
                stuck_cancel = timer.is_alive()
                if stuck_cancel:
                    self._discard_connection()
                    latch_terminal_failure(self, LightGraphError('query_cancel_failed'), engine='gauss')
                if cursor is not None:
                    try: cursor.close()
                    except Exception: pass
                self.stats.elapsed_seconds += time.monotonic() - start
                if stuck_cancel and sys.exc_info()[1] is None:
                    raise LightGraphError('query_cancel_failed')
    def cancel(self, query_id=None):
        self._cancelled = True
        self._reconnect_allowed = False
        self._reconnect_blocked = True
        if self._closed:
            return
        try: self._connection.cancel()
        except Exception: raise LightGraphError("query_cancel_failed") from None
    def close(self):
        with self._lock:
            self._disposed = True
            self._reopen = None
            if not self._closed: self._connection.close(); self._closed = True
