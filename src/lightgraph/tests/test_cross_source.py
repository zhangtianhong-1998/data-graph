from __future__ import annotations

from contextlib import contextmanager
import json

import pytest

from lightgraph.contracts import ColumnSpec, ExtractionGoal, QueryLimits, TableSpec
from lightgraph.cross_source import check_cross_source_reference
from lightgraph.errors import LightGraphError
from lightgraph.query.predicate import combine_scope_predicates
from lightgraph.sources import CSVSource, EphemeralBatch, MemorySource, QueryBudget
from lightgraph.sources.scoped import GoalScopedSource


def table(source_id, columns=("tenant", "code", "label")):
    return TableSpec(source_id=source_id, name="same_name",
                     columns=[ColumnSpec(name=column) for column in columns])


@contextmanager
def pair(source_rows, target_rows, *, source_class=MemorySource,
         target_class=MemorySource, limits=None, query_budget=None):
    a, b = table("left"), table("right")
    with source_class([a], {a.qualified_name: source_rows}, limits=limits,
                      query_budget=query_budget) as left:
        with target_class([b], {b.qualified_name: target_rows}, limits=limits,
                          query_budget=query_budget) as right:
            yield left, a, right, b


def check(inputs, **kwargs):
    left, a, right, b = inputs
    return check_cross_source_reference(left, a, ["tenant", "code"],
                                        right, b, ["tenant", "code"], **kwargs)


def test_composite_nulls_ambiguity_and_same_names_are_separate_sources():
    refs = [("A", "001", "PRIVATE_MARKER")] * 3 + [
        ("B", "001", None), ("A", "002", None), ("A", None, None)]
    targets = [("A", "001", "x")] * 10 + [
        ("B", "001", "y"), ("B", "001", "z"), (None, "001", "ignored")]
    with pair(refs, targets) as inputs:
        result = check(inputs, discriminator_columns=["label"])
    assert {name: result[name] for name in (
        "source_ref_tuples", "null_refs", "unmatched", "ambiguous", "unique_matches"
    )} == dict(source_ref_tuples=4, null_refs=1, unmatched=1, ambiguous=1, unique_matches=1)
    assert result["complete"] and result["consistent"]
    assert not result["confirmed"] and not result["semantic_identity_confirmed"]
    assert "PRIVATE_MARKER" not in json.dumps(result)
    assert result["coverage"]["source"]["source_id"] == "left"
    assert result["coverage"]["target"]["source_id"] == "right"
    assert all(side["projection_version"] and side["scanned_rows"] is None
               for side in result["coverage"].values())


def test_complete_scoped_reference_has_no_raw_values():
    with pair([("A", "001", None), ("B", "404", None)],
              [("A", "001", "PRIVATE_LABEL")]) as inputs:
        result = check(inputs, predicate=combine_scope_predicates(None, {"tenant": "A"}),
                       target_predicate={"tenant": "A"}, discriminator_columns=["label"])
        assert result["confirmed"] and result["source_ref_tuples"] == 1
        assert "PRIVATE_LABEL" not in json.dumps(result)
        assert result["consistency_scope"] == "independent_source_snapshots"


@pytest.mark.parametrize("kwargs", [
    {"max_distinct_rows": 1}, {"max_working_bytes": 10},
])
def test_budget_failures_cannot_confirm_or_publish_counts(kwargs):
    with pair([("A", "001", None), ("A", "002", None)],
              [("A", "001", None), ("A", "002", None)]) as inputs:
        result = check(inputs, **kwargs)
    assert result["status"] == "unresolved"
    assert not result["complete"] and not result["confirmed"]
    assert result["unique_matches"] is None
    assert result["reasons"]


def test_global_query_budget_failure_is_unresolved():
    with pair([("A", "001", None)], [("A", "001", None)],
              query_budget=QueryBudget(max_queries=2)) as inputs:
        result = check(inputs)
    assert result["reasons"] == ["query_budget_exhausted"]
    assert result["source_ref_tuples"] is None and not result["confirmed"]


def test_silent_incomplete_projection_fails_count_reconciliation():
    class TruncatingSource(MemorySource):
        def project(self, table, columns, predicate=None, distinct=True, limit=None):
            batch = EphemeralBatch(columns, [("A", "001")])
            try:
                yield batch
            finally:
                batch.release()

    with pair([("A", "001", None), ("A", "002", None)],
              [("A", "001", None)], source_class=TruncatingSource) as inputs:
        result = check(inputs)
    assert result["reasons"] == ["cross_source_incomplete"]
    assert not result["complete"] and not result["confirmed"]
    assert result["coverage"]["source"]["observed_distinct_tuples"] == 1


def test_unverified_snapshot_never_queries_or_confirms():
    class UnverifiedSource(MemorySource):
        def capabilities(self):
            result = super().capabilities()
            result.consistent_snapshot = False
            return result

    with pair([("A", "001", None)], [("A", "001", None)],
              target_class=UnverifiedSource) as inputs:
        result = check(inputs)
        assert inputs[0].stats.queries == inputs[2].stats.queries == 0
    assert result["reasons"] == ["cross_source_snapshot_unverified"]
    assert not result["consistent"] and not result["confirmed"]


def test_source_change_during_projection_is_unresolved():
    class ChangingSource(MemorySource):
        def probe(self, operator, table, args=None, predicate=None):
            result = super().probe(operator, table, args, predicate)
            if self.stats.queries >= 3:
                result["rows"] += 1
            return result

    with pair([("A", "001", None)], [("A", "001", None)],
              source_class=ChangingSource) as inputs:
        result = check(inputs)
    assert result["reasons"] == ["cross_source_source_changed"]
    assert not result["consistent"] and not result["confirmed"]


def test_goal_exclusion_and_forged_source_id_cannot_query():
    with pair([("A", "001", None)], [("A", "001", None)]) as inputs:
        left, a, right, b = inputs
        scoped = GoalScopedSource(right, ExtractionGoal(excluded_columns=["right.public.same_name.code"]))
        with pytest.raises(LightGraphError, match="unauthorized_column"):
            check((left, a, scoped, b))
        forged = b.model_copy(update={"source_id": "left"})
        with pytest.raises(LightGraphError, match="unauthorized_object"):
            check((left, a, right, forged))


def test_metadata_only_is_checked_before_all_source_access():
    class Forbidden:
        def __getattribute__(self, name):
            raise AssertionError("source access forbidden")

    with pytest.raises(LightGraphError, match="metadata_only_query_forbidden"):
        check_cross_source_reference(Forbidden(), None, ["a"],
                                     Forbidden(), None, ["a"], metadata_only=True)


def test_csv_and_memory_compare_codes_without_numeric_coercion(tmp_path):
    path = tmp_path / "refs.csv"
    path.write_text("tenant,code\nA,001\nA,1\n")
    options = dict(encoding="utf-8", delimiter=",", header=True,
                   null_values=[], type_inference="text")
    target = table("db")
    with CSVSource(path, source_id="csv", **options) as left:
        with MemorySource([target], {target.qualified_name: [("A", "001", "x"), ("A", 1, "y")]}) as right:
            result = check((left, left.describe()[0], right, target))
    assert result["unique_matches"] == 1 and result["unmatched"] == 1
    assert not result["confirmed"]


def test_exact_limit_and_empty_reference_have_honest_boundaries():
    with pair([("A", "001", None)], [("A", "001", None)],
              limits=QueryLimits(max_result_rows=1)) as inputs:
        assert check(inputs, max_distinct_rows=1)["confirmed"]
    with pair([], []) as inputs:
        result = check(inputs)
    assert result["complete"] and not result["confirmed"]
    assert result["reasons"] == ["empty_source_reference"]


def test_same_adapter_and_nested_snapshot_are_supported():
    a = table("shared")
    b = TableSpec(source_id="shared", name="target", columns=a.columns)
    with MemorySource([a, b], {a.qualified_name: [("A", "001", None)],
                              b.qualified_name: [("A", "001", None)]}) as source:
        with source.snapshot():
            assert source._depth == 1
            result = check((source, a, source, b))
            assert result["confirmed"] and source._depth == 1
        assert source._depth == 0


def test_same_underlying_source_through_two_scoped_views():
    a = table("shared")
    with MemorySource([a], {a.qualified_name: [("A", "001", None)]}) as source:
        first = GoalScopedSource(source, ExtractionGoal())
        second = GoalScopedSource(source, ExtractionGoal())
        with source.snapshot():
            assert check((first, a, second, a))["confirmed"]
            assert source._depth == 1


def test_snapshot_close_failure_cannot_leave_confirmed_result():
    class FailingClose(MemorySource):
        @contextmanager
        def snapshot(self):
            with super().snapshot():
                yield self
            raise LightGraphError("snapshot_close_failed")

    with pair([("A", "001", None)], [("A", "001", None)],
              target_class=FailingClose) as inputs:
        result = check(inputs)
    assert result["reasons"] == ["snapshot_close_failed"]
    assert not result["complete"] and not result["consistent"] and not result["confirmed"]
    assert result["source_ref_tuples"] is None


@pytest.mark.parametrize('side', ['source', 'target'])
@pytest.mark.parametrize('phase', ['snapshot_open', 'snapshot_close', 'projection'])
def test_recoverable_failure_identifies_only_the_actual_endpoint_without_raw_driver_text(side, phase):
    class FaultSource(MemorySource):
        def failure(self):
            code = {'snapshot_open': 'read_only_transaction_failed',
                    'snapshot_close': 'snapshot_close_failed', 'projection': 'query_execution_failed'}[phase]
            error = LightGraphError(code, retryable=False)
            error.database_diagnostic = {
                'engine': 'gauss', 'stage': {'snapshot_open': 'snapshot_start', 'snapshot_close': 'snapshot_close',
                                           'projection': 'query'}[phase],
                'category': 'unknown', 'sqlstate': 'unknown', 'errno': None,
                'rollback_succeeded': True, 'connection_closed': False, 'recovery': 'rolled_back',
                'message': 'PRIVATE_PASSWORD SELECT private_column', 'sql': 'SELECT PRIVATE_VALUE'}
            return error

        @contextmanager
        def snapshot(self):
            if phase == 'snapshot_open':
                raise self.failure()
            with super().snapshot():
                yield self
            if phase == 'snapshot_close':
                raise self.failure()

        def project(self, *args, **kwargs):
            if phase == 'projection':
                raise self.failure()
            yield from super().project(*args, **kwargs)

    classes = {f'{side}_class': FaultSource}
    with pair([('A', '001', None)], [('A', '001', None)], **classes) as inputs:
        result = check(inputs)
    failure = result['failure']
    assert failure['source_id'] == ('left' if side == 'source' else 'right')
    assert failure['phase'] == phase and not failure['retryable']
    assert result['reasons'] == [failure['code']]
    assert result['status'] == 'unresolved' and not result['complete'] and not result['confirmed']
    assert result['unique_matches'] is None and result['source_ref_tuples'] is None
    assert failure['database']['category'] == 'unknown'
    assert 'PRIVATE' not in json.dumps(result)
    assert 'message' not in failure['database'] and 'sql' not in failure['database']


def test_shared_adapter_snapshot_failure_does_not_invent_an_endpoint_owner():
    class SharedFailure(MemorySource):
        @contextmanager
        def snapshot(self):
            raise LightGraphError('read_only_transaction_failed')
            yield self
    left = table('left')
    right = table('right').model_copy(update={'name': 'other_table'})
    with SharedFailure([left, right], {left.qualified_name: [('A', '001', None)],
                                      right.qualified_name: [('A', '001', None)]}) as source:
        result = check_cross_source_reference(source, left, ['code'], source, right, ['code'])
    assert result['reasons'] == ['read_only_transaction_failed']
    assert result['complete'] is False and 'failure' not in result


def test_successful_cross_source_result_has_no_failure_object():
    with pair([('A', '001', None)], [('A', '001', None)]) as inputs:
        result = check(inputs)
    assert result['confirmed'] and 'failure' not in result
