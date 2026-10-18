"""Safe error codes: never retain source values or provider response bodies."""
class LightGraphError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)

class BudgetExceeded(LightGraphError):
    def __init__(self, code: str = "query_budget_exhausted"):
        super().__init__(code, retryable=True)
