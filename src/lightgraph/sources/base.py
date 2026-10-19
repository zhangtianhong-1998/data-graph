"""Bounded transient batches and common registered-operator interface."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
import time
from typing import Any, Iterable, Iterator
from lightgraph.contracts import QueryLimits, TableSpec
from lightgraph.errors import LightGraphError
from lightgraph.query import QueryCompiler, ValidatedQuery


COUNT_AGGREGATE_OPERATORS = frozenset({"profile_column", "profile_column_group", "check_fd",
    "check_reference", "check_value_domain", "path_probe", "compare_attribute_role"})


def aggregate_count(value: Any) -> int | None:
    """Normalize only compiler-owned nonnegative count/length fields.

    Gauss SUM(bigint) may return Decimal, which cursor_batches preserves as text.
    Counts must regain their integer semantics here: the string "0" is otherwise
    truthy during FD/reference validation. Raw source projections never call this.
    """
    if value is None:
        return None
    if not isinstance(value, (int, float, str, Decimal)) or isinstance(value, bool):
        raise LightGraphError("invalid_aggregate_shape")
    if len(str(value)) > 128:
        raise LightGraphError("invalid_aggregate_shape")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number.adjusted() > 38 or number != number.to_integral_value():
            raise LightGraphError("invalid_aggregate_shape")
        return int(number)
    except (InvalidOperation, ValueError):
        raise LightGraphError("invalid_aggregate_shape") from None


def row_bytes(row: tuple) -> int:
    total = 8 * len(row)
    for v in row:
        if v is None: total += 1
        elif isinstance(v, str): total += len(v.encode("utf-8"))
        elif isinstance(v, bytes): total += len(v)
        else: total += len(str(v).encode("utf-8"))
    return total

class EphemeralBatch:
    """Rows are live only until release or the next producer iteration.

    This controls ordinary APIs; deployment-level memory dumps and observers
    require separate controls. No serialization or value-bearing repr exists.
    """
    __slots__ = ("columns", "_rows", "_expires", "byte_count", "_released")
    def __init__(self, columns: Iterable[str], rows: Iterable[tuple], *, max_rows: int = 5000, max_bytes: int = 65536, ttl_seconds: float = 60):
        self.columns = tuple(columns); self._rows = []; self.byte_count = 0
        self._released = False; self._expires = time.monotonic() + ttl_seconds
        for item in rows:
            row = tuple(item)
            if len(row) != len(self.columns): raise LightGraphError("batch_shape_invalid")
            self.byte_count += row_bytes(row)
            if self.byte_count > max_bytes or len(self._rows) >= max_rows:
                self.release(); raise LightGraphError("memory_budget_exhausted")
            self._rows.append(row)
    def _check(self):
        if self._released or time.monotonic() > self._expires:
            self.release(); raise LightGraphError("ephemeral_batch_expired")
    def rows(self) -> Iterator[tuple]:
        self._check()
        for row in self._rows:
            self._check(); yield row
    def __iter__(self): return self.rows()
    def __len__(self): self._check(); return len(self._rows)
    def stats(self):
        self._check(); return {"rows": len(self._rows), "bytes": self.byte_count}
    def release(self): self._rows.clear(); self._released = True
    def __enter__(self): self._check(); return self
    def __exit__(self, *args): self.release()
    def __repr__(self): return f"EphemeralBatch(columns={len(self.columns)}, released={self._released})"
    def __reduce_ex__(self, protocol): raise LightGraphError("ephemeral_serialization_forbidden")
    def __getstate__(self): raise LightGraphError("ephemeral_serialization_forbidden")

@dataclass
class QueryStats:
    queries: int = 0
    returned_rows: int = 0
    returned_bytes: int = 0
    elapsed_seconds: float = 0
    scanned_rows: int | None = None
    scan_measurement: str = "unknown"
    def safe_dict(self): return dict(vars(self))

class SourceAdapter:
    """Adapters implement catalog, capability, snapshot and guarded execution."""
    limits: QueryLimits
    compiler: QueryCompiler
    stats: QueryStats
    def install_budget(self, budget):
        if hasattr(self, "query_budget"): budget.absorb(self.query_budget)
        self.query_budget = budget
        return self
    def describe(self) -> list[TableSpec]: raise NotImplementedError
    def capabilities(self): raise NotImplementedError
    def execute(self, query: ValidatedQuery, *, row_limit: int | None = None): raise NotImplementedError
    def project(self, table: TableSpec, columns: list[str], predicate: dict | None = None, distinct: bool = True, limit: int | None = None) -> Iterator[EphemeralBatch]:
        cap = min(limit or self.limits.max_result_rows, self.limits.max_result_rows)
        yield from self.execute(self.compiler.projection(table, columns, predicate=predicate, distinct=distinct, limit=cap), row_limit=cap)
    def _aggregate(self, query: ValidatedQuery) -> dict[str, int | None]:
        if query.operator not in COUNT_AGGREGATE_OPERATORS:
            raise LightGraphError("unregistered_aggregate_operator")
        result = None
        for batch in self.execute(query):
            with batch:
                for row in batch:
                    if result is not None: raise LightGraphError("invalid_aggregate_shape")
                    result = {name: aggregate_count(value) for name, value in zip(batch.columns, row)}
        return result or {}
    def profile(self, table: TableSpec, columns: list[str] | None = None, predicate=None) -> dict:
        with self.snapshot():
            return {c: self._aggregate(self.compiler.profile_column(table, c, predicate)) for c in columns or table.column_names}
    def check_fd(self, table: TableSpec, keys: list[str], values: list[str] | str, predicate=None) -> dict:
        values = [values] if isinstance(values, str) else values
        with self.snapshot():
            null_key_rows = self._aggregate(self.compiler.profile_column_group(table, keys, predicate)).get("null_key_rows", 0)
            checks = {v: {**self._aggregate(self.compiler.check_fd(table, keys, v, predicate)), "null_key_rows": null_key_rows} for v in values}
            if len(values) == 1: return checks[values[0]]
            return {"columns": checks, "violating_groups": sum(c["violating_groups"] for c in checks.values()), "null_key_rows": null_key_rows, "all_null_groups": sum(c["all_null_groups"] for c in checks.values())}
    def check_reference(self, source: TableSpec, source_columns: list[str], target: TableSpec, target_columns: list[str], **kwargs) -> dict:
        return self._aggregate(self.compiler.check_reference(source, source_columns, target, target_columns, **kwargs))
    def probe(self, operator: str, table: TableSpec, args: dict | None = None, predicate=None):
        args = dict(args or {})
        allowed = {
            "profile_column": {"column"}, "profile_column_group": {"columns"},
            "check_fd": {"keys", "values"}, "check_value_domain": {"column", "allowed"},
            "sample_strata": {"columns", "limit", "strata"}, "distinct_projection": {"columns", "limit"},
            "path_probe": {"columns"}, "level_probe": {"columns"},
            "unpivot_probe": {"levels", "limit"}, "compare_attribute_role": {"column", "target", "target_column", "target_predicate"},
            "check_reference": {"source_columns", "target", "target_columns", "discriminator_columns"},
        }
        if operator not in allowed or set(args) - allowed[operator]: raise LightGraphError("unknown_operator_or_argument")
        if operator == "check_fd": return self.check_fd(table, args["keys"], args["values"], predicate)
        if operator in ("sample_strata", "distinct_projection"):
            cap = min(args.get("limit", 32 if operator == "sample_strata" else self.limits.max_result_rows), 32 if operator == "sample_strata" else self.limits.max_result_rows)
            if operator == "sample_strata":
                # Caller supplies bounded authorized predicates for strata; no
                # claim of representative randomness is made for a first page.
                strata = args.get("strata") or [predicate]
                if len(strata) > 4: raise LightGraphError("sample_strata_limit")
                return self._sample(table, args["columns"], strata, cap)
            return self.project(table, args["columns"], predicate, limit=cap)
        if operator == "level_probe":
            columns = args["columns"]
            return {"per_level": self.profile(table, columns, predicate), "parent_consistency_by_pair": [self.check_fd(table, [child], [parent], predicate) for parent, child in zip(columns, columns[1:])]}
        if operator == "compare_attribute_role":
            target = self._find_table(args["target"])
            return {**self._aggregate(self.compiler.compare_attribute_role(table, args["column"], target, args["target_column"], predicate, args.get("target_predicate"))), "semantic_identity_confirmed": False}
        if operator == "check_reference":
            target = self._find_table(args.pop("target")); return self.check_reference(table, args.pop("source_columns"), target, args.pop("target_columns"), predicate=predicate, **args)
        if operator == "unpivot_probe":
            cap = min(args.get("limit", self.limits.max_result_rows), self.limits.max_result_rows)
            return self.execute(self.compiler.unpivot_probe(table, args["levels"], predicate, cap), row_limit=cap)
        method = getattr(self.compiler, operator)
        return self._aggregate(method(table, **args, predicate=predicate))
    def _find_table(self, name):
        if isinstance(name, TableSpec): name = name.qualified_name
        for table in self.describe():
            if table.qualified_name == name: return table
        raise LightGraphError("unauthorized_object")
    def _sample(self, table, columns, strata, cap):
        seen = set(); rows = []; per = max(1, cap // len(strata)); total_bytes = 0
        # Sampling is deliberately incomplete; its capped query does not claim
        # completeness and must not be reused for partition reconciliation.
        for pred in strata:
            q = self.compiler.projection(table, columns, predicate=pred, limit=per)
            q.parameters["cap"] = per
            from lightgraph.query.compiler import ident
            ordered_sql = q.sql.rsplit(" LIMIT ", 1)[0] + " ORDER BY " + ", ".join(ident(c) for c in columns) + " LIMIT :cap"
            q = self.compiler.finish(ordered_sql, q.parameters, "sample_strata", columns)
            for batch in self.execute(q, row_limit=per):
                with batch:
                    for row in batch:
                        if row not in seen and len(rows) < cap:
                            seen.add(row); rows.append(row); total_bytes += row_bytes(row)
                            if total_bytes > self.limits.max_batch_bytes: raise LightGraphError("memory_budget_exhausted")
        batch = EphemeralBatch(columns, rows, max_rows=cap, max_bytes=self.limits.max_batch_bytes)
        try: yield batch
        finally: batch.release()
    @contextmanager
    def snapshot(self): yield self
    def cancel(self, query_id: str | None = None): raise NotImplementedError
    def close(self): raise NotImplementedError
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def cursor_batches(cursor, *, limits: QueryLimits, columns: tuple[str, ...], row_limit: int, stats: QueryStats, deadline: float | None = None, cancelled=None, budget=None) -> Iterator[EphemeralBatch]:
    total_rows = total_bytes = 0; pending = []; pending_bytes = 0
    while True:
        if budget is not None: budget.check_time()
        if cancelled is not None and cancelled(): raise LightGraphError("query_cancelled")
        if deadline is not None and time.monotonic() > deadline: raise LightGraphError("query_timeout")
        # fetchone avoids a driver-side fetchmany allocation exceeding byte cap.
        raw = cursor.fetchone()
        if raw is None: break
        from decimal import Decimal
        from datetime import date, datetime, time as daytime
        def scalar(v):
            if isinstance(v, Decimal):
                if not v.is_finite(): raise LightGraphError("non_finite_source_value")
                return str(v)
            if isinstance(v, float) and not math.isfinite(v):
                raise LightGraphError("non_finite_source_value")
            if isinstance(v, (date, datetime, daytime)): return v.isoformat()
            if isinstance(v, (bytes, bytearray, memoryview)):
                raise LightGraphError("source_value_type_unsupported")
            if not isinstance(v, (str, int, float, bool, type(None))):
                raise LightGraphError("source_value_type_unsupported")
            return v
        row = tuple(scalar(v) for v in raw); size = row_bytes(row)
        total_rows += 1; total_bytes += size
        if budget is not None: budget.charge_return(1, size)
        stats.returned_rows += 1; stats.returned_bytes += size
        if total_rows > row_limit: raise LightGraphError("result_truncated")
        if size > limits.max_batch_bytes or total_bytes > limits.max_result_bytes: raise LightGraphError("memory_budget_exhausted")
        if pending and (len(pending) >= limits.max_batch_rows or pending_bytes + size > limits.max_batch_bytes):
            batch = EphemeralBatch(columns, pending, max_rows=limits.max_batch_rows, max_bytes=limits.max_batch_bytes, ttl_seconds=limits.timeout_ms / 1000)
            try: yield batch
            finally: batch.release()
            pending = []; pending_bytes = 0
        pending.append(row); pending_bytes += size
    if pending:
        batch = EphemeralBatch(columns, pending, max_rows=limits.max_batch_rows, max_bytes=limits.max_batch_bytes, ttl_seconds=limits.timeout_ms / 1000)
        try: yield batch
        finally: batch.release()
