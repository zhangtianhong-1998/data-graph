"""MySQL 8.0/8.4 InnoDB adapter with bounded, read-only instance access.

Runtime timeout/cancellation remain unverified until explicit probes succeed or
an exactly bound deployment qualification is supplied. No live MySQL instance
was used to qualify this implementation. Agent SQL never reaches control SQL.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import ipaddress
import math
import re
import ssl
import threading
import sys
import time
from typing import Callable
from urllib.parse import parse_qsl, unquote, urlsplit

from lightgraph.contracts import CapabilityManifest, ColumnSpec, ForeignKeySpec, QueryLimits, TableSpec
from lightgraph.errors import LightGraphError
from lightgraph.query import QueryCompiler
from .base import SourceAdapter, QueryStats, cursor_batches
from .budget import BudgetedCursor, QueryBudget
from .database_errors import connection_is_closed, database_diagnostic, database_error, qualification_error, readback_shape_failure, annotate_database_error, cleanup_preserving_error
from .reconnect import configure_reconnection, new_snapshot_recovery_allowed, reconnect_at_boundary, terminal_failure, latch_terminal_failure, prepare_snapshot

MYSQL_SQL_MODES = frozenset({"ONLY_FULL_GROUP_BY", "STRICT_TRANS_TABLES", "ERROR_FOR_DIVISION_BY_ZERO", "NO_ENGINE_SUBSTITUTION"})

REQUIRED_RUNTIME = {"read_only_transaction", "parameter_binding", "metadata_reading", "distinct_projection", "statement_timeout", "query_cancel"}


def product_version(version, comment):
    """Do not mislabel MySQL-compatible products as a qualified MySQL server."""
    text = str(version) + " " + str(comment)
    if re.search(r"mariadb|percona|tidb|aurora|polardb|oceanbase", text, re.I):
        return "unknown", "unknown"
    match = re.fullmatch(r"(8\.(?:0|4)\.[0-9]+)(?:[-+][A-Za-z0-9._+~-]+)?", str(version))
    if not match or not re.search(r"\bMySQL\b", str(comment), re.I):
        return "unknown", "unknown"
    # Bind the exact server build string, not just its compatibility prefix.
    return "MySQL", str(version)


def _connect_options(dsn, limits):
    try:
        uri = urlsplit(dsn)
        pairs = parse_qsl(uri.query, keep_blank_values=True, strict_parsing=True)
        options = dict(pairs)
        if (uri.scheme != "mysql" or not uri.hostname or not uri.username or uri.fragment
                or len(options) != len(pairs) or set(options) - {"ssl_mode", "ssl_ca", "ssl_cert", "ssl_key"}):
            raise ValueError
        database = unquote(uri.path.removeprefix("/"))
        if not database or "/" in database or "\x00" in database:
            raise ValueError
        if bool(options.get("ssl_cert")) != bool(options.get("ssl_key")):
            raise ValueError
        try:
            loopback = ipaddress.ip_address(uri.hostname).is_loopback
        except ValueError:
            loopback = uri.hostname.lower() == "localhost"
        mode = options.get("ssl_mode", "DISABLED" if loopback else "VERIFY_IDENTITY").upper()
        if mode not in {"DISABLED", "VERIFY_IDENTITY"} or mode == "DISABLED" and not loopback:
            raise ValueError
        if mode == "DISABLED" and any(options.get(k) for k in ("ssl_ca", "ssl_cert", "ssl_key")):
            raise ValueError
        timeout = max(1, math.ceil(limits.timeout_ms / 1000))
        result = dict(host=uri.hostname, port=uri.port or 3306, user=unquote(uri.username),
                      password=unquote(uri.password or ""), database=database, charset="utf8mb4",
                      connect_timeout=min(30, timeout), read_timeout=timeout + 2,
                      write_timeout=timeout + 2, autocommit=True, local_infile=False, client_flag=0)
        if mode == "DISABLED":
            result["ssl_disabled"] = True
        if mode == "VERIFY_IDENTITY":
            context = ssl.create_default_context(cafile=options.get("ssl_ca") or None)
            if options.get("ssl_cert"):
                context.load_cert_chain(options["ssl_cert"], options["ssl_key"])
            result["ssl"] = context
        return result
    except Exception:
        raise LightGraphError("mysql_dsn_invalid") from None


def _errno(exc):
    value = exc.args[0] if getattr(exc, "args", ()) else None
    return value if isinstance(value, int) else None


class MySQLSource(SourceAdapter):
    def __init__(self, dsn: str, *, source_id="mysql", allowed_tables: list[str],
                 allowed_columns: dict[str, list[str]] | None = None, limits: QueryLimits | None = None,
                 verified_capabilities: dict | CapabilityManifest | None = None,
                 connect_factory: Callable | None = None, probe_runtime_limits=False,
                 query_budget: QueryBudget | None = None, metadata_only: bool = False, _keep_connection: bool = False):
        original_connect_factory = connect_factory
        if not allowed_tables or any(len(t.split(".")) != 2 or any(not p for p in t.split(".")) for t in allowed_tables):
            raise LightGraphError("authorized_catalog_required")
        if len(set(allowed_tables)) != len(allowed_tables):
            raise LightGraphError("duplicate_authorized_table")
        if allowed_columns is not None and set(allowed_columns) - set(allowed_tables):
            raise LightGraphError("unauthorized_column_scope")
        if any(t.split(".")[0].casefold() in {"mysql", "sys", "information_schema", "performance_schema"} for t in allowed_tables):
            raise LightGraphError("system_catalog_forbidden")
        self.source_id = source_id
        self.metadata_only = metadata_only
        self.allowed_tables = tuple(allowed_tables)
        self.allowed_columns = allowed_columns or {}
        self.limits = limits or QueryLimits()
        self.query_budget = query_budget or QueryBudget()
        self.stats = QueryStats()
        self._lock, self._control_lock = threading.RLock(), threading.Lock()
        self._depth = 0
        self._closed = self._broken = self._cancelled = False
        self._data_closed = False
        self._tables = []
        self._physical_column_names = {}
        self._connection = self._control = None
        self._connection_id = None
        self._manifest = CapabilityManifest(driver="pymysql_dbapi", capabilities={
            k: "unknown" for k in REQUIRED_RUNTIME | {"cursor_paging", "cte", "union_all"}})
        self._qualification = verified_capabilities.model_dump() if isinstance(verified_capabilities, CapabilityManifest) else verified_capabilities
        if self._qualification is not None:
            if (not isinstance(self._qualification, dict)
                    or set(self._qualification) - {"product", "version", "driver", "capabilities", "qualification_ref", "consistent_snapshot", "actual_database_verified"}
                    or not {"product", "version", "driver", "capabilities"} <= set(self._qualification)
                    or not isinstance(self._qualification["capabilities"], dict)):
                raise LightGraphError("qualified_capability_manifest_required")
        options = _connect_options(dsn, self.limits)
        try:
            if connect_factory is None:
                import pymysql
                from importlib.metadata import version
                try:
                    driver_version = version("PyMySQL")
                    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", driver_version)
                    if not match or not (1, 2, 3) <= tuple(map(int, match.groups())) < (2, 0, 0):
                        raise ValueError
                except Exception:
                    # Earlier drivers can silently negotiate different TLS
                    # modes. Validate the distribution, not MySQLdb's alias.
                    raise LightGraphError("mysql_driver_version_unsupported") from None
                self._manifest.driver = "PyMySQL/" + driver_version
                options["cursorclass"] = pymysql.cursors.SSCursor
                connect_factory = pymysql.connect
            self.query_budget.check_time()
            options['connect_timeout'] = min(options['connect_timeout'],
                max(1, math.ceil(self.query_budget.deadline - time.monotonic())))
            self._connection = connect_factory(**options)
            if connection_is_closed(self._connection):
                raise LightGraphError('database_connection_failed')
            self.query_budget.check_time()
            control_options = {**options, "connect_timeout": min(5, options["connect_timeout"],
                               max(1, math.ceil(self.query_budget.deadline - time.monotonic()))),
                               "read_timeout": min(2, options["read_timeout"]),
                               "write_timeout": min(2, options["write_timeout"])}
            try:
                self._control = connect_factory(**control_options)
                if connection_is_closed(self._control):
                    raise LightGraphError('database_connection_failed')
            finally:
                control_options.clear()
            self.query_budget.check_time()
            self.preflight(probe_runtime_limits=probe_runtime_limits)
            if probe_runtime_limits and (not self._manifest.actual_database_verified or
                    any(self._manifest.capabilities.get(k) != 'verified' for k in REQUIRED_RUNTIME)):
                raise LightGraphError('runtime_capability_unverified')
        except BaseException as exc:
            self._close_connections()
            if isinstance(exc, LightGraphError):
                annotate_database_error(exc, engine='mysql', stage='connection_preflight')
                raise
            if not isinstance(exc, Exception):
                raise
            raise database_error('database_connection_failed', exc, engine='mysql',
                stage='connection_preflight') from None
        finally:
            options.clear()
        self.compiler = QueryCompiler(self._tables, dialect="mysql",
            physical_columns=self._physical_column_names)
        reopen_options = {'source_id': source_id, 'allowed_tables': list(self.allowed_tables),
            'allowed_columns': deepcopy(self.allowed_columns), 'limits': self.limits.model_copy(deep=True),
            'verified_capabilities': deepcopy(self._qualification), 'connect_factory': original_connect_factory,
            'metadata_only': metadata_only}
        configure_reconnection(self, lambda probe, budget: MySQLSource(dsn, **reopen_options,
            probe_runtime_limits=probe, query_budget=budget, _keep_connection=True))
        if not _keep_connection:
            self._release_snapshot_connection()

    def __repr__(self):
        return f"MySQLSource(source_id={self.source_id!r}, product={self._manifest.product!r})"

    def _cursor(self, *, count_returns=True):
        target = self._control, self._connection_id
        def cancel():
            if target[0] is not None and target[1] is not None:
                self._interrupt(target=target)
        return BudgetedCursor(self._connection.cursor(), self.query_budget,
                              count_returns=count_returns, cancel=cancel)

    def _rollback(self):
        if not self._broken:
            try:
                self._connection.rollback()
            except Exception as exc:
                self._discard_data_connection(reconnect_allowed=connection_is_closed(self._connection))
                raise database_error('snapshot_close_failed', exc, engine='mysql',
                    stage='snapshot_close', rollback_succeeded=False,
                    recovery='reconnect_required') from None

    @staticmethod
    def _close_cursor(cursor):
        if cursor is None:
            return
        try:
            cursor.close()
        finally:
            raw = getattr(cursor, "_cursor", cursor)
            # PyMySQL caches the interpolated statement for callproc/debugging.
            # It is unnecessary after completion and must not outlive this call.
            if hasattr(raw, "_executed"):
                raw._executed = None
            if hasattr(raw, "_rows"):
                raw._rows = None

    def _start_readonly(self, *, stage='snapshot_start'):
        cursor = self._cursor()
        try:
            # Guard and driver renderer use ordinary MySQL quoting. Fixed
            # modes prevent ANSI_QUOTES / NO_BACKSLASH_ESCAPES changing the
            # meaning of already-validated literal SQL at the server.
            cursor.execute("SET SESSION sql_mode = '" + ",".join(sorted(MYSQL_SQL_MODES)) + "'")
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute(f"SET SESSION max_execution_time = {min(4294967295, int(self.limits.timeout_ms))}")
            cursor.execute("SET SESSION lock_wait_timeout = 5")
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 5")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            cursor.execute("SELECT @@session.transaction_read_only, @@session.transaction_isolation, @@session.max_execution_time, @@session.sql_mode")
            row = cursor.fetchone()
            failure = readback_shape_failure(row, 4)
            if (failure or str(row[0]).lower() not in {"1", "on", "true"}
                    or str(row[1]).upper().replace("-", " ") != "REPEATABLE READ"
                    or str(row[2]) != str(min(4294967295, int(self.limits.timeout_ms)))
                    or set(str(row[3]).split(",")) != MYSQL_SQL_MODES):
                raise qualification_error('read_only_transaction_unverified', engine='mysql', stage=stage,
                    failure=failure or 'readback_mismatch', connection_closed=connection_is_closed(self._connection))
        finally:
            cleanup_preserving_error(self._close_cursor, cursor)

    def _apply_qualification(self):
        if self._qualification is None:
            return
        for key in ("product", "version", "driver"):
            if self._qualification[key] == "unknown" or self._qualification[key] != getattr(self._manifest, key):
                raise LightGraphError("capability_manifest_binding_mismatch")
        for key, state in self._qualification["capabilities"].items():
            if key not in self._manifest.capabilities or state not in {"unknown", "verified", "unsupported"}:
                raise LightGraphError("invalid_capability_manifest")
            if key in {"statement_timeout", "query_cancel"}:
                self._manifest.capabilities[key] = state

    def preflight(self, *, probe_runtime_limits=False):
        with self._lock:
            cursor = None
            try:
                self._rollback()
                self._start_readonly(stage='connection_preflight')
                cursor = self._cursor()
                cursor.execute("SELECT VERSION(), @@version_comment, CONNECTION_ID(), @@server_uuid, CURRENT_USER()")
                row = cursor.fetchone()
                self._manifest.product, self._manifest.version = product_version(row[0], row[1])
                if self._manifest.product != "MySQL":
                    raise LightGraphError("mysql_product_version_unsupported")
                if not isinstance(row[2], int) or not 1 <= row[2] <= 2**64 - 1:
                    raise LightGraphError("query_cancel_target_unverified")
                # A proxy may route identical DSNs to different servers. An
                # integer thread ID is safe only on the same server/account.
                control = BudgetedCursor(self._control.cursor(), self.query_budget)
                try:
                    control.execute("SELECT @@server_uuid, CURRENT_USER()")
                    identity = control.fetchone()
                    if (not identity or len(identity) != 2
                            or not all(isinstance(value, str) and value for value in row[3:5])
                            or tuple(row[3:5]) != tuple(identity)):
                        raise LightGraphError("query_cancel_connection_mismatch")
                finally:
                    cleanup_preserving_error(self._close_cursor, control)
                self._connection_id = row[2]
                self._manifest.capabilities["read_only_transaction"] = "verified"
                cursor.execute("SELECT %s", (71,))
                row = cursor.fetchone()
                failure = readback_shape_failure(row, 1)
                if failure or row[0] != 71:
                    raise qualification_error('parameter_binding_unverified', engine='mysql',
                        stage='connection_preflight', failure=failure or 'readback_mismatch',
                        connection_closed=connection_is_closed(self._connection))
                self._manifest.capabilities["parameter_binding"] = "verified"
                cursor.execute("SELECT DISTINCT 1 AS n")
                row = cursor.fetchone()
                failure = readback_shape_failure(row, 1)
                if failure or row[0] != 1 or isinstance(row[0], bool):
                    raise qualification_error('distinct_projection_unverified', engine='mysql',
                        stage='connection_preflight', failure=failure or 'readback_mismatch',
                        connection_closed=connection_is_closed(self._connection))
                self._manifest.capabilities["distinct_projection"] = "verified"
                self._tables = self._read_catalog(cursor)
                self._manifest.capabilities["metadata_reading"] = "verified"
                cursor.execute("WITH p AS (SELECT 1 AS n) SELECT n FROM p UNION ALL SELECT 2")
                if cursor.fetchall() != [(1,), (2,)]:
                    raise qualification_error('cte_union_unverified', engine='mysql', stage='connection_preflight',
                        failure='readback_mismatch', connection_closed=connection_is_closed(self._connection))
                self._manifest.capabilities["cte"] = self._manifest.capabilities["union_all"] = "verified"
                self._apply_qualification()
            except LightGraphError as exc:
                annotate_database_error(exc, engine='mysql', stage='connection_preflight')
                raise
            except Exception as exc:
                raise database_error('database_preflight_failed', exc, engine='mysql',
                    stage='connection_preflight', connection_closed=connection_is_closed(self._connection)) from None
            finally:
                active_error = sys.exc_info()[1]
                try:
                    self._close_cursor(cursor)
                    self._rollback()
                except Exception as exc:
                    if active_error is None: raise
                    if isinstance(active_error, LightGraphError):
                        active_error.database_diagnostic = {**getattr(active_error, 'database_diagnostic', {}),
                            'rollback_succeeded': False}
                finally:
                    self._manifest.consistent_snapshot = False
            if probe_runtime_limits:
                self.verify_runtime_controls()
            return self.capabilities()

    def _read_catalog(self, cursor):
        tables = []
        constraints = {}
        for qualified in self.allowed_tables:
            schema, name = qualified.split(".")
            cursor.execute("SELECT TABLE_TYPE, ENGINE, TABLE_COMMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (schema, name))
            row = cursor.fetchone()
            if not row or row[0] != "BASE TABLE":
                raise LightGraphError("authorized_table_unavailable_or_view")
            if str(row[1]).lower() != "innodb":
                raise LightGraphError("mysql_storage_engine_unsupported")
            comment = row[2] or ""
            if len(comment.encode("utf-8")) > self.limits.max_batch_bytes:
                raise LightGraphError("metadata_size_limit")
            cursor.execute("SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_COMMENT FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (schema, name))
            columns = []
            self._physical_column_names[qualified] = set()
            for cname, dtype, nullable, annotation in cursor.fetchall():
                self._physical_column_names[qualified].add(cname)
                if qualified in self.allowed_columns and cname not in self.allowed_columns[qualified]:
                    continue
                if len((annotation or "").encode("utf-8")) > self.limits.max_batch_bytes:
                    raise LightGraphError("metadata_size_limit")
                columns.append(ColumnSpec(name=cname, data_type=dtype, nullable=nullable == "YES", comment=annotation or ""))
            if not columns or qualified in self.allowed_columns and not set(self.allowed_columns[qualified]) <= {c.name for c in columns}:
                raise LightGraphError("authorized_column_unavailable")
            table = TableSpec(source_id=self.source_id, schema=schema, name=name, columns=columns, comment=comment)
            tables.append(table)
            cursor.execute("SELECT tc.CONSTRAINT_NAME, tc.CONSTRAINT_TYPE, k.COLUMN_NAME, k.ORDINAL_POSITION, k.REFERENCED_TABLE_SCHEMA, k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME FROM information_schema.TABLE_CONSTRAINTS tc JOIN information_schema.KEY_COLUMN_USAGE k ON tc.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND tc.TABLE_NAME=k.TABLE_NAME AND tc.CONSTRAINT_NAME=k.CONSTRAINT_NAME WHERE tc.TABLE_SCHEMA=%s AND tc.TABLE_NAME=%s AND tc.CONSTRAINT_TYPE IN ('PRIMARY KEY','UNIQUE','FOREIGN KEY') ORDER BY tc.CONSTRAINT_NAME,k.ORDINAL_POSITION", (schema, name))
            constraints[qualified] = cursor.fetchall()
        catalog = {t.qualified_name: t for t in tables}
        for table in tables:
            groups = {}
            for row in constraints[table.qualified_name]:
                groups.setdefault((row[0], row[1]), []).append(row)
            for (name, kind), rows in groups.items():
                rows.sort(key=lambda r: int(r[3]))
                if len(rows) > 64 or [int(r[3]) for r in rows] != list(range(1, len(rows) + 1)):
                    raise LightGraphError("constraint_metadata_unsupported")
                local = [r[2] for r in rows]
                if any(c not in table.column_names for c in local):
                    continue
                if kind in {"PRIMARY KEY", "UNIQUE"}:
                    table.unique_keys.append(local)
                    if kind == "PRIMARY KEY":
                        for col in table.columns:
                            col.primary_key = col.name in local
                elif kind == "FOREIGN KEY":
                    targets = {(r[4], r[5]) for r in rows}
                    if len(targets) != 1:
                        raise LightGraphError("constraint_metadata_unsupported")
                    schema, remote = next(iter(targets))
                    target = catalog.get(f"{schema}.{remote}")
                    remote_columns = [r[6] for r in rows]
                    if target is not None and all(c in target.column_names for c in remote_columns):
                        table.foreign_keys.append(ForeignKeySpec(name=name, columns=local,
                            target_schema=schema, target_table=remote, target_columns=remote_columns))
        return tables

    def _interrupt(self, *, allow_broken=False, target=None):
        # Cancellation must remain possible after the query budget is exhausted.
        # This private control connection can only target this adapter's own ID.
        with self._control_lock:
            if target is None:
                if (self._closed or self._data_closed or (self._broken and not allow_broken)
                        or self._connection_id is None):
                    return
                target = self._control, self._connection_id
            control, connection_id = target
            cursor = control.cursor()
            try:
                cursor.execute(f"KILL QUERY {int(connection_id)}")
            finally:
                cleanup_preserving_error(self._close_cursor, cursor)

    def _discard_data_connection(self, *, reconnect_allowed=False):
        self._broken = True
        self._reconnect_allowed = reconnect_allowed
        self._manifest.consistent_snapshot = False
        if not self._data_closed:
            try:
                self._connection.close()
                self._data_closed = True
            except Exception:
                pass

    def _reconnect(self):
        def adopt(replacement):
            for field in ('_connection', '_control', '_connection_id', '_manifest',
                          '_tables', '_physical_column_names', 'compiler'):
                setattr(self, field, getattr(replacement, field))
            self._closed = self._broken = self._cancelled = self._data_closed = False
            replacement._closed = replacement._disposed = True
            replacement._reopen = None
        reconnect_at_boundary(self, engine='mysql', discard=self._close_connections, adopt=adopt)

    def _probe_runtime_control(self, *, cancel):
        self._rollback()
        self._start_readonly(stage='connection_preflight')
        cursor = self._cursor()
        timer = None
        cancel_sent = threading.Event()
        cancel_errors = []
        interrupted = False
        probe_code = 'query_cancel_unverified' if cancel else 'statement_timeout_unverified'
        try:
            cursor.execute(f"SET SESSION max_execution_time = {1000 if cancel else 50}")
            if cancel:
                cancel_target = self._control, self._connection_id
                def interrupt():
                    try:
                        self._interrupt(target=cancel_target)
                        cancel_sent.set()
                    except Exception as exc:
                        cancel_errors.append(exc)
                timer = threading.Timer(.05, interrupt)
                timer.daemon = True
                timer.start()
            try:
                # Sole SELECT SLEEP returns 1 on interruption; adding a second
                # expression requires the documented cancellation error instead.
                cursor.execute("SELECT SLEEP(0.25), 1")
                cursor.fetchall()
            except LightGraphError: raise
            except Exception as exc:
                interrupted = _errno(exc) == (1317 if cancel else 3024)
                # A timeout during client cancellation does not prove cancel
                # works. Authentication/permission and other driver errors must
                # retain their category instead of becoming a failed readback.
                if not interrupted and not (cancel and _errno(exc) == 3024):
                    raise database_error(probe_code, exc, engine='mysql', stage='connection_preflight',
                        connection_closed=connection_is_closed(self._connection)) from None
        finally:
            active_error = sys.exc_info()[1]
            if timer:
                timer.cancel(); timer.join(timeout=min(2, max(.001, self.query_budget.deadline - time.monotonic())))
                if timer.is_alive():
                    self._discard_data_connection()
            # A completed cancel callback may already have the primary error,
            # even though no exception is active on this thread yet.
            active_error = active_error or (cancel_errors[0] if cancel_errors else None)
            try:
                self._close_cursor(cursor)
                self._rollback()
            except Exception:
                if active_error is None: raise
        if cancel_errors:
            if isinstance(cancel_errors[0], LightGraphError): raise cancel_errors[0]
            raise database_error(probe_code, cancel_errors[0], engine='mysql',
                stage='connection_preflight', connection_closed=connection_is_closed(self._connection)) from None
        failure = ('cancel_not_completed' if self._broken else
            'cancel_not_sent' if cancel and not cancel_sent.is_set() else
            'interrupt_not_observed' if not interrupted else None)
        if failure:
            raise qualification_error(probe_code, engine='mysql', stage='connection_preflight',
                failure=failure, connection_closed=connection_is_closed(self._connection))
        self._start_readonly(stage='connection_preflight')
        check = self._cursor()
        try:
            check.execute("SELECT 1")
            row = check.fetchone()
            failure = readback_shape_failure(row, 1)
            if failure or row[0] != 1 or isinstance(row[0], bool):
                raise qualification_error('runtime_control_unverified', engine='mysql', stage='connection_preflight',
                    failure=failure or 'readback_mismatch', connection_closed=connection_is_closed(self._connection))
        finally:
            active_error = sys.exc_info()[1]
            try: self._close_cursor(check); self._rollback()
            except Exception:
                if active_error is None: raise
        self._manifest.capabilities["query_cancel" if cancel else "statement_timeout"] = "verified"

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
                self._boundary_recoveries = 0
                self._reconnect()
            try:
                # A previously runtime-qualified source repeats these probes
                # inside replacement construction, once per physical session.
                if acquired and self._manifest.actual_database_verified:
                    return self.capabilities()
                return self._verify_runtime_controls_current()
            finally:
                if acquired: self._release_snapshot_connection()

    def _verify_runtime_controls_current(self):
        with self._lock:
            if self._closed or self._broken:
                raise LightGraphError("source_closed")
            if self._depth:
                raise LightGraphError("capability_probe_during_snapshot")
            self._manifest.actual_database_verified = False
            for key in ("statement_timeout", "query_cancel"):
                self._manifest.capabilities[key] = "unknown"
            try:
                self._probe_runtime_control(cancel=False)
                self._probe_runtime_control(cancel=True)
                self._manifest.actual_database_verified = all(self._manifest.capabilities[k] == "verified" for k in REQUIRED_RUNTIME)
            except LightGraphError as exc:
                annotate_database_error(exc, engine='mysql', stage='connection_preflight')
                raise
            except Exception as exc:
                raise database_error('database_runtime_preflight_failed', exc, engine='mysql',
                    stage='connection_preflight', connection_closed=connection_is_closed(self._connection)) from None
            finally:
                active_error = sys.exc_info()[1]
                self._manifest.consistent_snapshot = False
                try: self._rollback()
                except Exception:
                    if active_error is None: raise
                    if isinstance(active_error, LightGraphError):
                        annotate_database_error(active_error, engine='mysql', stage='connection_preflight')
                        active_error.database_diagnostic['rollback_succeeded'] = False
            return self.capabilities()

    def describe(self):
        return [t.model_copy(deep=True) for t in self._tables]

    def capabilities(self):
        return self._manifest.model_copy(deep=True)

    @property
    def terminal_failure_code(self):
        return self._terminal_failure_code

    @property
    def terminal_failure_diagnostic(self):
        return deepcopy(self._terminal_failure_diagnostic)

    def _release_snapshot_connection(self):
        """Release only the physical lease; authorized metadata stays available."""
        eligible = self._reconnect_allowed
        self._close_connections()
        self._idle_released = True
        self._reconnect_allowed = eligible
        self._manifest.consistent_snapshot = False

    @property
    def new_snapshot_recovery_allowed(self):
        return new_snapshot_recovery_allowed(self, required_capabilities=REQUIRED_RUNTIME,
            disconnected=not self._closed and not self._broken and (
                connection_is_closed(self._connection) or connection_is_closed(self._control)))

    def _begin_snapshot(self):
        try:
            self._start_readonly()
        except BaseException as exc:
            self._manifest.consistent_snapshot = False
            was_closed = connection_is_closed(self._connection) or connection_is_closed(self._control)
            try:
                self._rollback()
                rolled_back = not self._broken
            except LightGraphError:
                rolled_back = False
            if isinstance(exc, LightGraphError) or not isinstance(exc, Exception):
                self._reconnect_allowed = False
                if isinstance(exc, LightGraphError) and exc.code == 'read_only_transaction_unverified':
                    self._manifest.capabilities['read_only_transaction'] = 'unknown'
                raise
            diagnostic = database_diagnostic(exc, engine='mysql', stage='snapshot_start')
            reconnect = (diagnostic['category'] == 'connection'
                or diagnostic['category'] == 'unknown' and (was_closed or not rolled_back))
            self._reconnect_allowed = reconnect
            if reconnect: self._discard_data_connection(reconnect_allowed=True)
            raise database_error('read_only_transaction_failed', exc, engine='mysql',
                stage='snapshot_start', rollback_succeeded=rolled_back,
                recovery='reconnect_required' if reconnect else 'rolled_back',
                connection_closed=was_closed) from None

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
                prepare_snapshot(self, engine='mysql', begin=self._begin_snapshot,
                    disconnected=connection_is_closed(self._connection) or connection_is_closed(self._control))
                self._manifest.consistent_snapshot = True
            elif self._closed or self._broken:
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
                        try:
                            self._rollback()
                        except LightGraphError:
                            self._reconnect_allowed = not self._cancelled
                            if active_error is None: raise
                            if isinstance(active_error, LightGraphError):
                                active_error.database_diagnostic = {**getattr(active_error, 'database_diagnostic', {}),
                                    'rollback_succeeded': False, 'recovery': 'reconnect_required'}
                    finally:
                        # No transaction or network connection survives Agent
                        # think-time. Nested queries share this lease only.
                        self._release_snapshot_connection()

    def execute(self, query, *, row_limit=None):
        from lightgraph.query.compiler import mysql_driver_sql
        # Re-authorize before opening a transaction or sending any user SQL.
        self.compiler.guard.validate(query.sql, query.parameters)
        sql = mysql_driver_sql(query)
        with self.snapshot():
            self.query_budget.check_time()
            cursor = None
            finished = False
            reconnect_allowed = False
            timed_out = threading.Event()
            self._cancelled = False
            started = time.monotonic()
            self.stats.queries += 1
            cancel_target = self._control, self._connection_id
            def timeout():
                timed_out.set()
                try:
                    self._interrupt(target=cancel_target)
                except Exception:
                    pass
            timer = threading.Timer(min(self.limits.timeout_ms / 1000,
                max(.001, self.query_budget.deadline - time.monotonic())), timeout)
            timer.daemon = True
            try:
                cursor = self._cursor(count_returns=False)
                timer.start()
                cursor.execute(sql, query.parameters)
                columns = tuple(col[0] for col in cursor.description)
                yield from cursor_batches(cursor, limits=self.limits, columns=columns,
                    row_limit=row_limit or self.limits.max_result_rows, stats=self.stats,
                    deadline=started + self.limits.timeout_ms / 1000,
                    cancelled=lambda: self._cancelled, budget=self.query_budget)
                if self._cancelled:
                    raise LightGraphError("query_cancelled")
                if timed_out.is_set():
                    raise LightGraphError("query_timeout")
                finished = True
                self._manifest.capabilities["cursor_paging"] = "verified"
            except LightGraphError as exc:
                reconnect_allowed = exc.code == 'query_timeout'
                raise
            except Exception as exc:
                self.query_budget.check_time()
                code = ("query_cancelled" if self._cancelled else "query_timeout"
                        if timed_out.is_set() or _errno(exc) in {3024, 1205} else "query_execution_failed")
                diagnostic = database_diagnostic(exc, engine='mysql', stage='query')
                reconnect_allowed = (not self._cancelled and (code == 'query_timeout'
                    or diagnostic['category'] == 'connection'
                    or diagnostic['category'] == 'unknown' and connection_is_closed(self._connection)))
                raise database_error(code, exc, engine='mysql', stage='query',
                    recovery='reconnect_required', connection_closed=connection_is_closed(self._connection)) from None
            finally:
                timer.cancel()
                if timer.ident:
                    timer.join(timeout=2)
                if not finished or timed_out.is_set() or timer.is_alive() or self._cancelled or self._broken:
                    # SSCursor.close drains unread results. Drop this failed or
                    # abandoned connection first to avoid reading extra rows.
                    # A late KILL must never reach a later query. Discard a
                    # timed-out/cancelled connection even if rows just ended.
                    if not timer.is_alive():
                        try:
                            self._interrupt()
                        except Exception:
                            pass
                    self._discard_data_connection(reconnect_allowed=reconnect_allowed and not timer.is_alive())
                try:
                    self._close_cursor(cursor)
                except Exception:
                    pass
                self.stats.elapsed_seconds += time.monotonic() - started

    def cancel(self, query_id=None):
        if query_id is not None:
            raise LightGraphError("query_cancel_target_forbidden")
        self._cancelled = True
        self._reconnect_allowed = False
        # Revoke reuse before I/O: cancellation can race with query completion.
        self._broken = True
        try:
            self._interrupt(allow_broken=True)
        except Exception:
            raise LightGraphError("query_cancel_failed") from None

    def _close_connections(self, *, strict=False):
        failed = False
        for connection in (self._connection, self._control):
            if connection is not None and not (connection is self._connection and self._data_closed):
                try:
                    closed = getattr(connection, 'closed', None)
                    already_closed = (type(closed) in {bool, int} and closed > 0
                                      or getattr(connection, 'open', None) is False)
                    if not already_closed:
                        connection.close()
                    if connection is self._connection:
                        self._data_closed = True
                except Exception:
                    failed = True
            if connection is not None and hasattr(connection, "_result"):
                result = connection._result
                if result is not None and hasattr(result, "rows"):
                    result.rows = None
                connection._result = None
        self._closed = True
        if strict and failed:
            raise LightGraphError("source_close_failed") from None

    def close(self):
        with self._lock:
            self._disposed = True
            self._reopen = None
            if not self._closed:
                self._close_connections(strict=True)
