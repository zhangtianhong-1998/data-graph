"""Rebuild a failed DB connection only at an empty snapshot boundary."""
import logging
from lightgraph.errors import BudgetExceeded, LightGraphError
from .database_errors import database_error, safe_database_diagnostic


def configure_reconnection(source, factory):
    # The closure retains connection credentials only in this live adapter. It
    # is never serialized and is released by close(), like driver credentials.
    source._reopen = factory
    source._disposed = False
    source._reconnect_allowed = False
    source._reconnect_blocked = False
    source._reconnect_attempts = 0
    source.connection_generation = 0


def new_snapshot_recovery_allowed(source, *, disconnected, required_capabilities):
    """Report eligibility for a new complete operation; never retry current rows."""
    if (source._depth or source._disposed or source._reconnect_blocked or source._reopen is None
            or source._cancelled or source.metadata_only
            or any(source._manifest.capabilities.get(key) != 'verified' for key in required_capabilities)
            or not (source._reconnect_allowed or disconnected)):
        return False
    budget = source.query_budget.snapshot()
    return budget['remaining_queries'] != 0 and budget['remaining_seconds'] > 0


def reconnect_at_boundary(source, *, engine, discard, adopt):
    """One attempt; a failed rebuild stays latched until the source is reopened.

    The constructor rechecks readonly controls, authorized catalog, qualification
    and (if previously enabled) runtime timeout/cancellation on the new session.
    No data SQL or partially consumed generator is replayed by this function.
    """
    if source._depth or source._disposed or source._reconnect_blocked or source._reopen is None:
        raise LightGraphError('source_closed')
    source.query_budget.check_time()
    if source.query_budget.snapshot()['remaining_queries'] == 0:
        raise BudgetExceeded('query_budget_exhausted')
    source._reconnect_blocked = True
    source._reconnect_attempts += 1
    original_manifest = source.capabilities()
    original_tables = source.describe()
    physical_columns = source._physical_column_names
    replacement = None
    try:
        discard()
        replacement = source._reopen(original_manifest.actual_database_verified, source.query_budget)
        current = replacement.capabilities()
        if any(getattr(current, field) != getattr(original_manifest, field)
               for field in ('product', 'version', 'driver')):
            raise LightGraphError('capability_manifest_binding_mismatch')
        if (replacement.describe() != original_tables
                or replacement._physical_column_names != physical_columns
                or replacement.allowed_tables != source.allowed_tables
                or replacement.allowed_columns != source.allowed_columns
                or replacement.metadata_only != source.metadata_only):
            raise LightGraphError('source_catalog_changed')
        if any(current.capabilities.get(key) != 'verified' for key, value
               in original_manifest.capabilities.items() if value == 'verified' and key != 'cursor_paging'):
            raise LightGraphError('runtime_capability_unverified')
        adopt(replacement)
        source.connection_generation += 1
        source._reconnect_blocked = False
        source._reconnect_allowed = False
        logging.getLogger(__name__).info('database connection requalified', extra={
            'database_diagnostic': {'engine': engine, 'stage': 'reconnect',
                                    'recovery': 'reconnected', 'connection_closed': False},
            'connection_generation': source.connection_generation,
            'reconnect_attempts': source._reconnect_attempts})
    except BaseException as exc:
        source._reopen = None
        if replacement is not None:
            try: replacement.close()
            except Exception: pass
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, LightGraphError):
            if exc.code == 'database_connection_failed':
                raise database_error('database_reconnect_failed', exc.__cause__ or exc,
                    engine=engine, stage='reconnect', recovery='reconnect_failed') from None
            diagnostic = safe_database_diagnostic(exc)
            if diagnostic is None:
                diagnostic = database_error(exc.code, exc.__cause__ or exc,
                    engine=engine, stage='reconnect').database_diagnostic
            exc.database_diagnostic = {**diagnostic, 'stage': 'reconnect', 'recovery': 'reconnect_failed'}
            exc.retryable = False
            raise exc from None
        raise database_error('database_reconnect_failed', exc, engine=engine,
            stage='reconnect', recovery='reconnect_failed') from None
