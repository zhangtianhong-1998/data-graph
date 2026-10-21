"""Bounded reference checks across independently authorized source snapshots.

Only registered local aggregate/DISTINCT operators run on either source. No
federated SQL, row transfer to another database, files, or raw result persistence
is provided. Successful coverage is an instance observation, not semantic proof.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import math
import sys

from .contracts import canonical, digest
from .errors import LightGraphError
from .sources.base import aggregate_count, row_bytes


_RECOVERABLE = frozenset({
    "result_truncated", "memory_budget_exhausted", "query_timeout",
    "query_cancelled", "query_execution_failed", "query_budget_exhausted",
    "run_time_budget_exhausted", "returned_rows_budget_exhausted",
    "returned_bytes_budget_exhausted", "runtime_capability_unverified",
    "read_only_transaction_failed", "snapshot_close_failed",
    "formula_cache_missing_in_projection", "cross_source_incomplete",
    "cross_source_snapshot_unverified", "cross_source_source_changed",
    "cross_source_value_unsupported", "invalid_aggregate_shape",
})
_COUNT_FIELDS = ("source_ref_tuples", "null_refs", "unmatched", "ambiguous", "unique_matches")


def _registered_table(adapter, requested):
    matches = [table for table in adapter.describe()
               if (table.source_id, table.schema_name, table.name) ==
               (requested.source_id, requested.schema_name, requested.name)]
    if len(matches) != 1:
        raise LightGraphError("unauthorized_object")
    return matches[0]


def _cell(value):
    # Never coerce codes, booleans or numbers across sources. A caller must
    # establish and implement any domain conversion outside this checker.
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, int):
        return ("integer", value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LightGraphError("cross_source_value_unsupported")
        return ("real", value)
    if isinstance(value, str):
        return ("text", value)
    raise LightGraphError("cross_source_value_unsupported")


def _key(values):
    return canonical([_cell(value) for value in values])


def _coverage(table, columns, predicate):
    from .query.predicate import ScopeConjunction
    scope = ({"user_predicate": predicate.user_predicate,
              "partition_filter": predicate.partition_filter}
             if isinstance(predicate, ScopeConjunction) else predicate)
    return {
        "source_id": table.source_id, "schema": table.schema_name, "table": table.name,
        "columns": list(columns), "scope_fingerprint": digest(scope),
        "schema_version": digest(table.model_dump(mode="json", by_alias=True)), "projection_version": None,
        "expected_distinct_tuples": None, "observed_distinct_tuples": 0,
        "returned_rows": 0, "returned_bytes": 0,
        "complete": False, "consistent": False,
        "scanned_rows": None, "scan_measurement": "unknown",
    }


def check_cross_source_reference(
    source_adapter, source_table, source_columns,
    target_adapter, target_table, target_columns, *,
    discriminator_columns=None, predicate=None, target_predicate=None,
    max_working_bytes=32 * 1024 * 1024, max_distinct_rows=None,
    metadata_only=False,
):
    """Return only counts and coverage, with no returned rows or value samples.

    Counts have the same DISTINCT-tuple semantics as ``check_reference``.
    Target duplicates alone are harmless; conflicting discriminator tuples make
    a key ambiguous. Any NULL key component makes a source tuple a NULL
    reference, and a target tuple ineligible for matching.

    ``confirmed`` means complete unambiguous reference coverage in the two
    local snapshots, never semantic identity or a distributed point-in-time
    snapshot. Incomplete work returns None counts and ``status='unresolved'``;
    it must not publish/retract graph facts. Authorization errors still raise.
    """
    if metadata_only:
        raise LightGraphError("metadata_only_query_forbidden")
    if (not isinstance(max_working_bytes, int) or isinstance(max_working_bytes, bool) or
            max_working_bytes <= 0 or (max_distinct_rows is not None and
            (not isinstance(max_distinct_rows, int) or isinstance(max_distinct_rows, bool) or
             max_distinct_rows <= 0))):
        raise LightGraphError("invalid_result_limit")
    source_columns = list(source_columns)
    target_columns = list(target_columns)
    discriminator_columns = list(discriminator_columns or [])
    if not source_columns or len(source_columns) != len(target_columns):
        raise LightGraphError("reference_arity_mismatch")
    if len(source_columns) > 32 or len(discriminator_columns) > 32:
        raise LightGraphError("invalid_column_selection")
    source_table = _registered_table(source_adapter, source_table)
    target_table = _registered_table(target_adapter, target_table)
    # The compiler binds columns to the registered catalog and source ID.
    source_adapter.compiler.columns(source_table, source_columns)
    target_adapter.compiler.columns(target_table, target_columns)
    if discriminator_columns:
        target_adapter.compiler.columns(target_table, discriminator_columns)
    target_projection = list(dict.fromkeys(target_columns + discriminator_columns))
    target_key_indexes = [target_projection.index(column) for column in target_columns]
    target_discriminator_indexes = [target_projection.index(column) for column in discriminator_columns]
    source_coverage = _coverage(source_table, source_columns, predicate)
    target_coverage = _coverage(target_table, target_projection, target_predicate)
    result = {
        **dict.fromkeys(_COUNT_FIELDS), "complete": False, "consistent": False,
        "confirmed": False, "semantic_identity_confirmed": False,
        "status": "unresolved", "reasons": [],
        "consistency_scope": "independent_source_snapshots",
        "coverage": {"source": source_coverage, "target": target_coverage},
    }
    source_keys = {}
    target_bindings = {}
    held_bytes = 0

    def reserve(size):
        nonlocal held_bytes
        held_bytes += size
        if held_bytes > max_working_bytes:
            raise LightGraphError("memory_budget_exhausted")

    def read_projection(adapter, table, columns, scope, coverage, consume):
        nonlocal held_bytes
        cap = min(max_distinct_rows or adapter.limits.max_result_rows,
                  adapter.limits.max_result_rows)
        before = adapter.probe("profile_column_group", table, {"columns": columns}, scope)
        expected = aggregate_count(before.get("distinct_tuples"))
        if expected is None:
            raise LightGraphError("invalid_aggregate_shape")
        coverage["expected_distinct_tuples"] = expected
        if expected > cap:
            raise LightGraphError("result_truncated")
        seen = set()
        set_bytes = 0
        hash_sum = 0
        stream = adapter.project(table, columns, predicate=scope, distinct=True, limit=cap)
        try:
            for batch in stream:
                with batch:
                    if tuple(batch.columns) != tuple(columns):
                        raise LightGraphError("batch_shape_invalid")
                    for row in batch:
                        if len(row) != len(columns):
                            raise LightGraphError("batch_shape_invalid")
                        coverage["returned_rows"] += 1
                        coverage["returned_bytes"] += row_bytes(row)
                        if coverage["returned_rows"] > cap:
                            raise LightGraphError("result_truncated")
                        encoded = _key(row)
                        if encoded in seen:
                            raise LightGraphError("cross_source_incomplete")
                        size = sys.getsizeof(encoded) + 256
                        reserve(size)
                        set_bytes += size
                        seen.add(encoded)
                        coverage["observed_distinct_tuples"] += 1
                        hash_sum = (hash_sum + int.from_bytes(
                            hashlib.sha256(encoded.encode()).digest(), "big")) % (1 << 256)
                        consume(row)
            if len(seen) != expected:
                raise LightGraphError("cross_source_incomplete")
            after = adapter.probe("profile_column_group", table, {"columns": columns}, scope)
            if before != after:
                coverage["consistent"] = False
                raise LightGraphError("cross_source_source_changed")
            coverage["projection_version"] = digest(
                "typed_distinct_set_v1", len(seen), f"{hash_sum:064x}")
            coverage["complete"] = True
        finally:
            seen.clear()
            held_bytes -= set_bytes
            close = getattr(stream, "close", None)
            if close is not None:
                close()

    def collect_source(row):
        encoded = _key(row)
        reserve(sys.getsizeof(encoded) + 256)
        source_keys[encoded] = any(value is None for value in row)

    def collect_target(row):
        key_values = tuple(row[index] for index in target_key_indexes)
        if any(value is None for value in key_values):
            return
        encoded = _key(key_values)
        discriminator = _key(tuple(row[index] for index in target_discriminator_indexes))
        if encoded not in target_bindings:
            reserve(sys.getsizeof(encoded) + 512)
            target_bindings[encoded] = set()
        if discriminator not in target_bindings[encoded]:
            reserve(sys.getsizeof(discriminator) + 256)
            target_bindings[encoded].add(discriminator)

    try:
        with ExitStack() as snapshots:
            # A deterministic order avoids opposing lock acquisition by callers.
            for adapter in sorted({id(source_adapter): source_adapter,
                                   id(target_adapter): target_adapter}.values(), key=id):
                snapshots.enter_context(adapter.snapshot())
            for adapter, coverage in ((source_adapter, source_coverage),
                                      (target_adapter, target_coverage)):
                coverage["consistent"] = bool(adapter.capabilities().consistent_snapshot)
            if not all(side["consistent"] for side in result["coverage"].values()):
                raise LightGraphError("cross_source_snapshot_unverified")
            read_projection(source_adapter, source_table, source_columns, predicate,
                            source_coverage, collect_source)
            read_projection(target_adapter, target_table, target_projection, target_predicate,
                            target_coverage, collect_target)
            for adapter, table, coverage in (
                (source_adapter, source_table, source_coverage),
                (target_adapter, target_table, target_coverage),
            ):
                if (not adapter.capabilities().consistent_snapshot or
                        digest(_registered_table(adapter, table).model_dump(
                            mode="json", by_alias=True)) != coverage["schema_version"]):
                    coverage["consistent"] = False
                    raise LightGraphError("cross_source_source_changed")
            counts = dict.fromkeys(_COUNT_FIELDS, 0)
            counts["source_ref_tuples"] = len(source_keys)
            for encoded, missing in source_keys.items():
                if missing:
                    counts["null_refs"] += 1
                    continue
                matches = len(target_bindings.get(encoded, ()))
                counts["unmatched" if not matches else "ambiguous" if matches > 1 else "unique_matches"] += 1
        # Snapshot-close failures cannot leave a successful publication signal.
        result.update(counts)
        result["complete"] = result["consistent"] = True
        result["confirmed"] = bool(counts["source_ref_tuples"]) and not any(
            counts[field] for field in ("null_refs", "unmatched", "ambiguous"))
        result["status"] = "confirmed" if result["confirmed"] else "candidate"
        if not counts["source_ref_tuples"]:
            result["reasons"].append("empty_source_reference")
    except LightGraphError as error:
        if error.code not in _RECOVERABLE:
            raise
        result["reasons"].append(error.code)
        if error.code == "snapshot_close_failed":
            for coverage in result["coverage"].values():
                coverage["consistent"] = False
        result["consistent"] = (
            error.code not in {"cross_source_source_changed", "snapshot_close_failed"} and
            all(side["consistent"] for side in result["coverage"].values()))
    finally:
        source_keys.clear()
        for values in target_bindings.values():
            values.clear()
        target_bindings.clear()
    return result
