"""One shared, value-free budget for all sources in a run."""
from __future__ import annotations
import threading
import time
from contextlib import contextmanager
from lightgraph.errors import BudgetExceeded, LightGraphError

class QueryBudget:
    def __init__(self, *, max_queries: int = 240, max_run_seconds: float = 1800, max_returned_rows: int = 400000, max_returned_bytes: int = 64 * 1024 * 1024, deadline: float | None = None):
        if min(max_queries, max_run_seconds, max_returned_rows, max_returned_bytes) <= 0:
            raise LightGraphError("invalid_query_budget")
        self.max_queries=max_queries;self.max_returned_rows=max_returned_rows;self.max_returned_bytes=max_returned_bytes
        self.deadline=min(deadline, time.monotonic()+max_run_seconds) if deadline else time.monotonic()+max_run_seconds
        self.queries=0;self.returned_rows=0;self.returned_bytes=0;self._lock=threading.RLock()
        # Hold references so Python cannot reuse an absorbed object's id.
        self._absorbed_budgets: dict[int, QueryBudget] = {}
    def check_time(self):
        if time.monotonic() > self.deadline: raise BudgetExceeded("run_time_budget_exhausted")
    @contextmanager
    def exploration(self, *, deadline: float, query_limit: int):
        """Apply a smaller phase ceiling without resetting consumed resources.

        The run orchestrator is sequential. All source adapters share this
        object, so tool queries and database cancellation see the same deadline.
        Restoring the ceiling releases the reserved capacity to validation.
        """
        previous_deadline, previous_limit = self.deadline, self.max_queries
        self.deadline = min(previous_deadline, deadline)
        self.max_queries = min(previous_limit, query_limit)
        try:
            self.check_time()
            if self.queries >= self.max_queries:
                raise BudgetExceeded('discovery_query_budget_exhausted')
            yield
        except BudgetExceeded as exc:
            if exc.code == 'run_time_budget_exhausted':
                raise BudgetExceeded('discovery_time_budget_exhausted') from None
            if exc.code == 'query_budget_exhausted':
                raise BudgetExceeded('discovery_query_budget_exhausted') from None
            raise
        finally:
            self.deadline, self.max_queries = previous_deadline, previous_limit
    def charge_query(self):
        with self._lock:
            self.check_time()
            if self.queries >= self.max_queries: raise BudgetExceeded("query_budget_exhausted")
            self.queries += 1
    def charge_return(self, rows: int, byte_count: int):
        with self._lock:
            self.check_time()
            self.returned_rows += rows; self.returned_bytes += byte_count
            if self.returned_rows > self.max_returned_rows: raise BudgetExceeded("returned_rows_budget_exhausted")
            if self.returned_bytes > self.max_returned_bytes: raise BudgetExceeded("returned_bytes_budget_exhausted")
    def absorb(self, previous: "QueryBudget"):
        if previous is self: return
        with self._lock:
            self.deadline=min(self.deadline,previous.deadline)
            if id(previous) not in self._absorbed_budgets:
                prior=previous.snapshot()
                self._absorbed_budgets[id(previous)]=previous
                self.queries+=prior["queries"];self.returned_rows+=prior["returned_rows"];self.returned_bytes+=prior["returned_bytes"]
            self.check_time()
            if self.queries > self.max_queries: raise BudgetExceeded("query_budget_exhausted")
            if self.returned_rows > self.max_returned_rows: raise BudgetExceeded("returned_rows_budget_exhausted")
            if self.returned_bytes > self.max_returned_bytes: raise BudgetExceeded("returned_bytes_budget_exhausted")
    def snapshot(self):
        with self._lock:
            return {"queries":self.queries,"returned_rows":self.returned_rows,"returned_bytes":self.returned_bytes,"remaining_queries":max(0,self.max_queries-self.queries),"remaining_seconds":max(0,self.deadline-time.monotonic())}

class BudgetedCursor:
    """Metadata/control cursor wrapper; no SQL or parameter values are retained."""
    def __init__(self, cursor, budget: QueryBudget, *, count_returns: bool = True, cancel=None):
        self._cursor=cursor;self._budget=budget;self._count_returns=count_returns;self._cancel=cancel
    def execute(self, *args, **kwargs):
        self._budget.charge_query()
        def stop():
            if self._cancel:
                try: self._cancel()
                except Exception: pass
        timer=threading.Timer(max(0.001,self._budget.deadline-time.monotonic()),stop)
        timer.daemon=True;timer.start()
        try:
            result=self._cursor.execute(*args, **kwargs)
            self._budget.check_time()
            return result
        except Exception:
            self._budget.check_time()
            raise
        finally:
            timer.cancel();timer.join(timeout=1)
    def fetchone(self):
        row=self._cursor.fetchone()
        if row is not None and self._count_returns:
            from .base import row_bytes
            self._budget.charge_return(1,row_bytes(tuple(row)))
        return row
    def fetchall(self):
        result=[]
        while (row:=self.fetchone()) is not None: result.append(row)
        return result
    def close(self): return self._cursor.close()
    def __getattr__(self,name): return getattr(self._cursor,name)
