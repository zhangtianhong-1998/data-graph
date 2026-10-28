"""Independent resource accounting with validation reservations before exploration."""
from __future__ import annotations
from dataclasses import dataclass, asdict
import math
import threading
import time
from lightgraph.errors import BudgetExceeded

@dataclass(frozen=True)
class Cost:
    db_cu: float = 0
    # None is valid only in limits, never in a reservation or measured charge.
    llm_calls: int | None = 0
    llm_tokens: int | None = 0
    memory_bytes: int = 0
    seconds: float = 0

class BudgetAllocator:
    """Reservations prevent speculative work from consuming its verification budget.

    Memory is a peak admission limit, not a cumulative token-like resource. Actual
    external work is charged even on errors; a caller must release reservations.
    """
    def __init__(self, limits: Cost | None = None):
        self.limits = limits or Cost(db_cu=160, llm_calls=32, llm_tokens=100000,
                                    memory_bytes=8 * 1024 * 1024, seconds=600)
        self.used = Cost()
        self._reserved: dict[str, Cost] = {}
        self._lock = threading.RLock()
        self._started = time.monotonic()
        self._deadline = self._started + self.limits.seconds

    @property
    def deadline_monotonic(self) -> float:
        """The effective deadline, in the same clock domain as time.monotonic()."""
        with self._lock:
            return self._deadline

    def tighten_deadline(self, deadline_monotonic: float) -> float:
        """Join an external run deadline without ever extending this budget.

        An already expired deadline is accepted; the next admission, charge or
        explicit check fails. This lets an orchestrator share one run boundary.
        """
        if not math.isfinite(deadline_monotonic):
            raise ValueError('invalid_budget_deadline')
        with self._lock:
            self._deadline = min(self._deadline, deadline_monotonic)
            return self._deadline

    def remaining_seconds(self) -> float:
        """Return positive remaining wall time, or reject an expired budget."""
        with self._lock:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise BudgetExceeded('wall_time_budget_exhausted')
            return remaining

    def check_deadline(self) -> None:
        self.remaining_seconds()

    def _check(self, cost: Cost, excluding: str | None = None) -> None:
        values = asdict(cost)
        if any(v is None or v < 0 for v in values.values()):
            raise ValueError("negative_budget_cost")
        if cost.seconds > self.remaining_seconds():
            raise BudgetExceeded("wall_time_budget_exhausted")
        for name in ("db_cu", "llm_calls", "llm_tokens"):
            limit = getattr(self.limits, name)
            if limit is None:
                continue
            reserved = sum(getattr(c, name) for k, c in self._reserved.items() if k != excluding)
            if getattr(self.used, name) + reserved + getattr(cost, name) > limit:
                raise BudgetExceeded(f"{name}_budget_exhausted")
        if cost.memory_bytes > self.limits.memory_bytes:
            raise BudgetExceeded("memory_budget_exhausted")

    def reserve(self, reservation_id: str, minimum_validation: Cost) -> None:
        with self._lock:
            if reservation_id in self._reserved:
                raise ValueError("duplicate_budget_reservation")
            self._check(minimum_validation)
            self._reserved[reservation_id] = minimum_validation

    def admit(self, cost: Cost, reservation_id: str | None = None) -> None:
        with self._lock:
            self._check(cost, reservation_id)
            if reservation_id and reservation_id not in self._reserved:
                raise ValueError("unknown_budget_reservation")

    def charge(self, cost: Cost, reservation_id: str | None = None) -> None:
        with self._lock:
            if any(v is None or v < 0 for v in asdict(cost).values()):
                raise ValueError("negative_budget_cost")
            if reservation_id and reservation_id not in self._reserved:
                raise ValueError("unknown_budget_reservation")
            # Record actual cost before detecting an overrun, never hide consumed work.
            used = asdict(self.used)
            for name, value in asdict(cost).items():
                used[name] = max(used[name], value) if name == "memory_bytes" else used[name] + value
            self.used = Cost(**used)
            if reservation_id:
                reserved = self._reserved[reservation_id]
                self._reserved[reservation_id] = Cost(**{
                    name: max(0, value - getattr(cost, name)) for name, value in asdict(reserved).items()
                })
            try:
                self.check_deadline()
                for name in ("db_cu", "llm_calls", "llm_tokens", "memory_bytes"):
                    limit = getattr(self.limits, name)
                    if limit is not None and getattr(self.used, name) > limit:
                        raise BudgetExceeded(f"{name}_budget_exhausted")
            except BudgetExceeded:
                # A failed charge must not pin an unusable reservation. The
                # actual counters above remain visible even when work was late.
                if reservation_id:
                    self._reserved.pop(reservation_id, None)
                raise

    def release(self, reservation_id: str) -> None:
        with self._lock:
            self._reserved.pop(reservation_id, None)

    def snapshot(self) -> dict:
        with self._lock:
            return {"limits": asdict(self.limits), "used": asdict(self.used),
                    "reserved": {k: asdict(v) for k, v in self._reserved.items()},
                    "elapsed_seconds": round(time.monotonic() - self._started, 3)}
