"""Synthetic and uploaded-file SQL execution, confined to an in-memory database."""
from __future__ import annotations
from contextlib import contextmanager
import sqlite3
import math
import threading
import time
from typing import Iterable, Mapping
from lightgraph.contracts import TableSpec, QueryLimits, CapabilityManifest
from lightgraph.errors import LightGraphError
from lightgraph.query import QueryCompiler
from lightgraph.query.compiler import ident
from .base import SourceAdapter, QueryStats, cursor_batches, row_bytes
from .budget import QueryBudget

class MemorySource(SourceAdapter):
    def __init__(self, tables: list[TableSpec], rows: Mapping[str, Iterable[dict | tuple]], *, limits: QueryLimits | None = None, max_memory_bytes: int = 128 * 1024 * 1024, query_budget: QueryBudget | None = None):
        self.limits = limits or QueryLimits(); self.stats = QueryStats(); self._closed = False
        self.query_budget = query_budget or QueryBudget()
        self._tables = [t.model_copy(deep=True) for t in tables]
        self._lock = threading.RLock(); self._depth = 0; self._cancelled = False
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.execute("PRAGMA temp_store=MEMORY")
        self._db.execute("PRAGMA cache_size=-16384")
        self._db.execute("PRAGMA trusted_schema=OFF")
        self._db.execute("PRAGMA case_sensitive_like=ON")
        self._db.execute("PRAGMA journal_mode=MEMORY")
        self._input_bytes = 0
        try:
            if len({t.qualified_name for t in tables}) != len(tables): raise LightGraphError("duplicate_table")
            schemas = {t.schema_name for t in tables}
            if {"main", "temp"} & schemas: raise LightGraphError("reserved_schema")
            for schema in sorted(schemas): self._db.execute(f"ATTACH DATABASE ':memory:' AS {ident(schema)}")
            for table in self._tables:
                if len(table.column_names) != len(set(table.column_names)): raise LightGraphError("duplicate_column")
                name = f"{ident(table.schema_name)}.{ident(table.name)}"
                self._db.execute(f"CREATE TABLE {name} ({', '.join(ident(c) + ' BLOB' for c in table.column_names)})")
                for raw in rows.get(table.qualified_name, ()):
                    self.query_budget.check_time()
                    row = tuple(raw.get(c) for c in table.column_names) if isinstance(raw, dict) else tuple(raw)
                    if len(row) != len(table.column_names): raise LightGraphError("source_shape_invalid")
                    if any(not isinstance(v, (str, int, float, bool, bytes, type(None))) for v in row): raise LightGraphError("source_value_type_unsupported")
                    if any(isinstance(v,float) and not math.isfinite(v) for v in row): raise LightGraphError("non_finite_source_value")
                    self._input_bytes += row_bytes(row) + 64
                    if self._input_bytes > max_memory_bytes: raise LightGraphError("memory_budget_exhausted")
                    self._db.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in row)})", row)
            self._db.commit()
            self._db.execute("PRAGMA query_only=ON")
            self._db.set_authorizer(self._authorize)
        except Exception as e:
            self._db.close()
            if isinstance(e, LightGraphError): raise
            raise LightGraphError("memory_source_initialization_failed") from None
        self.compiler = QueryCompiler(self._tables, dialect="sqlite")

    def _authorize(self, action, arg1, arg2, db, trigger):
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION}
        if action not in allowed: return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ:
            table = next((t for t in self._tables if t.schema_name == db and t.name == arg1), None)
            if table is None or arg2 and arg2 not in table.column_names: return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() not in {"count", "sum", "min", "max", "length", "coalesce", "abs", "like", "typeof"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    def describe(self): return [t.model_copy(deep=True) for t in self._tables]
    def capabilities(self):
        return CapabilityManifest(product="synthetic_memory", driver="sqlite3", version=sqlite3.sqlite_version,
            capabilities={k: "verified" for k in ["metadata_reading", "read_only_transaction", "parameter_binding", "distinct_projection", "cursor_paging", "statement_timeout", "query_cancel", "cte", "union_all"]}, consistent_snapshot=True, actual_database_verified=False)
    @contextmanager
    def snapshot(self):
        with self._lock:
            if self._closed: raise LightGraphError("source_closed")
            self._depth += 1
            try: yield self
            finally: self._depth -= 1
    def execute(self, query, *, row_limit=None):
        with self._lock:
            if self._closed: raise LightGraphError("source_closed")
            # Revalidate against this source's catalog; validated queries are not
            # transferable authority between adapters.
            self.compiler.guard.validate(query.sql, query.parameters)
            self.query_budget.charge_query()
            start = time.monotonic(); self._cancelled = False; cursor = self._db.cursor()
            self.stats.queries += 1
            self._db.set_progress_handler(lambda: int(self._cancelled or (time.monotonic() - start) * 1000 > self.limits.timeout_ms or time.monotonic() > self.query_budget.deadline), 500)
            try:
                cursor.execute(query.sql, query.parameters)
                columns = tuple(d[0] for d in cursor.description)
                yield from cursor_batches(cursor, limits=self.limits, columns=columns, row_limit=row_limit or self.limits.max_result_rows, stats=self.stats, deadline=start + self.limits.timeout_ms / 1000, cancelled=lambda: self._cancelled, budget=self.query_budget)
            except LightGraphError: raise
            except sqlite3.Error:
                self.query_budget.check_time()
                code = "query_cancelled" if self._cancelled else "query_timeout" if (time.monotonic()-start)*1000 > self.limits.timeout_ms else "query_execution_failed"
                raise LightGraphError(code) from None
            finally:
                cursor.close(); self._db.set_progress_handler(None, 0)
                self.stats.elapsed_seconds += time.monotonic() - start
    def cancel(self, query_id=None): self._cancelled = True; self._db.interrupt()
    def close(self):
        with self._lock:
            if not self._closed: self._db.close(); self._closed = True
