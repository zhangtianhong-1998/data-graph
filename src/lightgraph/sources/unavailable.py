"""Credential-free record of a database that failed initial qualification.

No catalog is invented from configuration. Only construction callers opt into
these records; metadata refresh and other strict callers still raise errors.
"""
from copy import deepcopy
from contextlib import contextmanager

from lightgraph.contracts import CapabilityManifest
from lightgraph.errors import BudgetExceeded, LightGraphError
from .base import SourceAdapter, QueryStats
from .database_errors import safe_database_diagnostic


_ISOLATABLE = frozenset({
    'database_connection_failed', 'database_preflight_failed',
    'database_runtime_preflight_failed', 'read_only_transaction_failed',
    'snapshot_close_failed', 'runtime_capability_unverified',
    'statement_timeout_unverified', 'query_cancel_unverified',
    'runtime_control_unverified', 'product_version_unverified',
})


def isolatable_initialization_failure(error):
    """Availability failures may be isolated; configuration/access errors may not."""
    if isinstance(error, BudgetExceeded) or not isinstance(error, LightGraphError):
        return False
    diagnostic = safe_database_diagnostic(error) or {}
    if diagnostic.get('category') in {
        'authentication', 'permission', 'configuration_or_catalog', 'unsupported',
    }:
        return False
    return error.code in _ISOLATABLE


class UnavailableSource(SourceAdapter):
    def __init__(self, source, error, *, limits, query_budget):
        self.source_id = source.id
        self.allowed_tables = tuple(source.allowed_tables)
        self.limits = limits.model_copy(deep=True)
        self.query_budget = query_budget
        self.stats = QueryStats()
        self.metadata_only = True
        self.terminal_failure_code = error.code
        self._diagnostic = safe_database_diagnostic(error)
        self.new_snapshot_recovery_allowed = False

    @property
    def terminal_failure_diagnostic(self):
        return deepcopy(self._diagnostic)

    @property
    def initialization_failure(self):
        return {'source_id': self.source_id, 'code': self.terminal_failure_code,
                'phase': 'initialization', 'retryable': False,
                'catalog_available': False, 'authorized_tables': list(self.allowed_tables),
                **({'database': self.terminal_failure_diagnostic} if self._diagnostic else {})}

    def describe(self):
        return []

    def capabilities(self):
        return CapabilityManifest()

    def _raise(self):
        error = LightGraphError(self.terminal_failure_code)
        error.database_diagnostic = self.terminal_failure_diagnostic
        raise error

    @contextmanager
    def snapshot(self):
        self._raise()
        yield self

    def execute(self, query, *, row_limit=None):
        self._raise()

    def cancel(self, query_id=None):
        pass

    def close(self):
        pass
