"""Apply extraction-goal exclusions at the common source execution boundary."""
from __future__ import annotations

from contextlib import contextmanager

from lightgraph.contracts import ExtractionGoal
from lightgraph.errors import LightGraphError
from lightgraph.query import SQLGuard
from .base import SourceAdapter


class GoalScopedSource(SourceAdapter):
    """Borrow an adapter while narrowing every query to explicit goal selectors.

    Original metadata stays available for stable schema hashes and auditing. It
    never grants query permission: even a query compiled with the original
    catalog is revalidated against the narrowed catalog immediately on execute.
    This is an additional restriction, not a replacement for database grants.
    """

    def __init__(self, adapter: SourceAdapter, goal: ExtractionGoal):
        self._adapter = adapter
        self._goal = goal.model_copy(deep=True)
        self._closed = False

    @property
    def compiler(self):
        return self._adapter.compiler

    @property
    def limits(self):
        return self._adapter.limits

    @property
    def stats(self):
        return self._adapter.stats

    @property
    def query_budget(self):
        return self._adapter.query_budget

    @query_budget.setter
    def query_budget(self, budget):
        self._adapter.query_budget = budget

    def _check_open(self):
        if self._closed:
            raise LightGraphError("source_closed")

    def describe(self):
        self._check_open()
        return self._adapter.describe()

    def capabilities(self):
        self._check_open()
        return self._adapter.capabilities()

    def _validate(self, query):
        from lightgraph.discovery.goal import source_in_goal, column_is_excluded

        self._check_open()
        tables = []
        for table in self._adapter.describe():
            if not source_in_goal(self._goal, table):
                continue
            filtered = table.model_copy(deep=True)
            filtered.columns = [column for column in table.columns
                                if not column_is_excluded(self._goal, table, column.name)]
            # Keep a zero-column table to permit COUNT(*) without exposing any
            # excluded column. Other projections still fail column authorization.
            tables.append(filtered)
        guard = SQLGuard(tables, dialect=self.compiler.dialect,
                         physical_columns=self.compiler.guard.physical_columns)
        return guard.validate(query.sql, query.parameters, operator=query.operator,
                              result_columns=query.result_columns)

    def execute(self, query, *, row_limit=None):
        yield from self._adapter.execute(self._validate(query), row_limit=row_limit)

    def project(self, table, columns, predicate=None, distinct=True, limit=None):
        # Preserve source-specific projection hooks (cache validity, cancellation
        # and failure semantics) after authorizing the exact requested operation.
        # The adapter still owns execution and its database security boundary.
        self._check_open()
        columns = list(columns)
        cap = min(limit or self.limits.max_result_rows, self.limits.max_result_rows)
        self._validate(self.compiler.projection(table, columns, predicate=predicate,
                                               distinct=distinct, limit=cap))
        stream = self._adapter.project(table, columns, predicate=predicate, distinct=distinct, limit=cap)
        try:
            for batch in stream:
                if tuple(batch.columns) != tuple(columns):
                    batch.release()
                    raise LightGraphError("batch_shape_invalid")
                yield batch
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()

    @contextmanager
    def snapshot(self):
        self._check_open()
        with self._adapter.snapshot():
            yield self

    def cancel(self, query_id=None):
        return self._adapter.cancel(query_id)

    def close(self):
        # The orchestrator owns the connection and can share it with other
        # components. Closing this view must not destroy the caller's adapter.
        self._closed = True

    def __repr__(self):
        return f"GoalScopedSource(closed={self._closed})"

    def __reduce_ex__(self, protocol):
        raise LightGraphError("ephemeral_serialization_forbidden")
