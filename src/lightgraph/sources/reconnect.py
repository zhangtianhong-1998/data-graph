"""Snapshot-scoped database sessions and bounded, non-replaying recovery."""
from copy import deepcopy
import logging
import time
from lightgraph.errors import BudgetExceeded, LightGraphError
from .database_errors import database_error, safe_database_diagnostic


# Applies to one snapshot boundary, including connection qualification and
# BEGIN. Never resets inside a nested query or replays a partially read result.
MAX_BOUNDARY_RECOVERIES = 3


def configure_reconnection(source, factory):
    # Credentials stay only in this live adapter and are released on close or
    # terminal failure. Neither driver objects nor exception tracebacks persist.
    source._reopen = factory
    source._disposed = False
    source._idle_released = False
    source._reconnect_allowed = False
    source._reconnect_blocked = False
    source._boundary_recovery_used = False
    source._boundary_recoveries = 0
    source._reconnect_attempts = 0
    source._connection_acquisitions = 1
    source.connection_generation = 0
    source._terminal_failure_code = None
    source._terminal_failure_diagnostic = None


def terminal_failure(source):
    code = getattr(source, '_terminal_failure_code', None)
    if code is None:
        return None
    error = LightGraphError(code)
    error.database_diagnostic = deepcopy(source._terminal_failure_diagnostic)
    return error


def latch_terminal_failure(source, error, *, engine, code=None):
    """Remember safe terminal state, so another tool cannot start the loop again."""
    if source._terminal_failure_code is not None:
        return terminal_failure(source)
    diagnostic = safe_database_diagnostic(error)
    if diagnostic is None:
        diagnostic = database_error(code or 'database_reconnect_failed', error.__cause__ or error,
            engine=engine, stage='reconnect').database_diagnostic
    source._terminal_failure_code = code or getattr(error, 'code', 'database_reconnect_failed')
    source._terminal_failure_diagnostic = {**diagnostic, 'terminal': True,
        'boundary_recovery_attempts': source._boundary_recoveries,
        'recovery_attempt_limit': MAX_BOUNDARY_RECOVERIES,
        'recovery': 'reconnect_failed' if source._terminal_failure_code == 'database_reconnect_failed'
                    else diagnostic.get('recovery', 'none')}
    source._reconnect_blocked = True
    source._reconnect_allowed = False
    source._reopen = None
    logging.getLogger(__name__).error('database source stopped', extra={
        'error_code': source._terminal_failure_code,
        'database_diagnostic': source._terminal_failure_diagnostic,
        'connection_generation': source.connection_generation,
        'reconnect_attempts': source._reconnect_attempts})
    return terminal_failure(source)


def new_snapshot_recovery_allowed(source, *, disconnected, required_capabilities):
    """Eligibility for one new complete operation, never a partial-row replay."""
    if (source._depth or source._disposed or source._reconnect_blocked or source._reopen is None
            or source._cancelled or source.metadata_only or source._idle_released and not source._reconnect_allowed
            or any(source._manifest.capabilities.get(key) != 'verified' for key in required_capabilities)
            or not (source._reconnect_allowed or disconnected)):
        return False
    budget = source.query_budget.snapshot()
    return budget['remaining_queries'] != 0 and budget['remaining_seconds'] > 0


def _preflight_can_recover(error):
    # A new session has emitted no fact rows. A failed rollback is authoritative
    # evidence that a half-open/aborted session is unusable even if closed=False.
    diagnostic = safe_database_diagnostic(error) or {}
    if getattr(error, 'code', None) not in {'database_connection_failed', 'database_preflight_failed',
                                          'snapshot_close_failed', 'read_only_transaction_failed'}:
        return False
    category = diagnostic.get('category', 'unknown')
    if category == 'qualification':
        # A replacement has not emitted facts. Require all capabilities on a
        # new session; a failed constant probe never authorizes using this one.
        # Read-only attestation is deliberately absent from this allowlist.
        return (getattr(error, 'code', None) == 'database_preflight_failed'
            and diagnostic.get('stage') == 'connection_preflight'
            and diagnostic.get('probe_code') in {'parameter_binding_unverified', 'distinct_projection_unverified'}
            and diagnostic.get('probe_failure') in {'no_row', 'malformed_row', 'readback_mismatch'})
    return (category in {'connection', 'idle_transaction_timeout', 'shutdown', 'unavailable'}
            or category == 'unknown' and (diagnostic.get('rollback_succeeded') is False
                or diagnostic.get('connection_closed') is True
                # Failure to obtain a new session is safe to retry before any
                # rows exist. Unknown remains unknown; this is not a claim that
                # the network, rather than the driver/server, caused it.
                or getattr(error, 'code', None) == 'database_connection_failed'
                # Qualification belongs to this newly acquired session. No
                # fact rows exist yet, so an unclassified driver failure can
                # consume the same bounded recovery allowance as connect.
                # Keep unknown as unknown; explicit capability/catalog and
                # authentication failures remain non-recoverable above.
                or getattr(error, 'code', None) == 'database_preflight_failed'
                and diagnostic.get('stage') == 'connection_preflight'))


def _check_recovery_budget(source):
    if source._cancelled:
        raise LightGraphError('query_cancelled')
    if source._disposed:
        raise LightGraphError('source_closed')
    source.query_budget.check_time()
    if source.query_budget.snapshot()['remaining_queries'] == 0:
        raise BudgetExceeded('query_budget_exhausted')


def _recovery_pause(source):
    """Short backoff remains interruptible and consumes the existing deadline."""
    end = time.monotonic() + min(2 ** (source._boundary_recoveries - 1), 4)
    while True:
        _check_recovery_budget(source)
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.05, remaining))


def _reserve_recovery(source, *, engine, error=None):
    _check_recovery_budget(source)
    if source._boundary_recoveries >= MAX_BOUNDARY_RECOVERIES:
        raise latch_terminal_failure(source, error or LightGraphError('database_reconnect_failed'),
                                     engine=engine, code='database_reconnect_failed') from None
    source._boundary_recoveries += 1
    source._boundary_recovery_used = True
    source._reconnect_attempts += 1
    _recovery_pause(source)


def reconnect_at_boundary(source, *, engine, discard, adopt):
    """Acquire one qualified session, with bounded pre-data recovery.

    Idle release is normal lifecycle, not a recovery attempt. Every new physical
    session repeats catalog/readonly/qualification checks under the same budget.
    A failed recovery is latched and subsequent calls return the same safe code.
    """
    stopped = terminal_failure(source)
    if stopped is not None:
        raise stopped
    if source._depth or source._disposed or source._reconnect_blocked or source._reopen is None:
        raise LightGraphError('source_closed')
    _check_recovery_budget(source)
    if not source._idle_released or source._reconnect_allowed:
        _reserve_recovery(source, engine=engine)
    original_manifest = source.capabilities()
    original_tables = source.describe()
    physical_columns = source._physical_column_names
    while True:
        _check_recovery_budget(source)
        replacement = None
        try:
            discard()
            source._connection_acquisitions += 1
            replacement = source._reopen(original_manifest.actual_database_verified, source.query_budget)
            # A cancellation/deadline during connect or qualification must not
            # be cleared by adopt(), which transfers a healthy session's state.
            _check_recovery_budget(source)
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
            source._idle_released = False
            source._reconnect_allowed = False
            logging.getLogger(__name__).info('database snapshot connection qualified', extra={
                'database_diagnostic': {'engine': engine, 'stage': 'reconnect',
                    'recovery': 'reconnected' if source._boundary_recovery_used else 'none', 'connection_closed': False},
                'connection_generation': source.connection_generation,
                'reconnect_attempts': source._reconnect_attempts})
            return
        except BaseException as exc:
            if replacement is not None:
                try: replacement.close()
                except Exception: pass
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, BudgetExceeded) or getattr(exc, 'code', None) in {'query_cancelled', 'source_closed'}:
                raise
            if _preflight_can_recover(exc):
                _reserve_recovery(source, engine=engine, error=exc)
                continue
            preserved = {'source_catalog_changed', 'capability_manifest_binding_mismatch',
                'runtime_capability_unverified', 'read_only_transaction_unverified',
                'authorized_table_unavailable_or_view', 'authorized_column_unavailable',
                'query_cancel_connection_mismatch', 'mysql_product_version_unsupported'}
            code = exc.code if isinstance(exc, LightGraphError) and exc.code in preserved else 'database_reconnect_failed'
            raise latch_terminal_failure(source, exc, engine=engine, code=code) from None


def prepare_snapshot(source, *, engine, begin, disconnected):
    """Start a fresh operation; all acquisition/BEGIN retries share one bound."""
    stopped = terminal_failure(source)
    if stopped is not None: raise stopped
    if source._disposed: raise LightGraphError('source_closed')
    if source._cancelled: raise LightGraphError('query_cancelled')
    source._boundary_recovery_used = False
    source._boundary_recoveries = 0
    initial_generation = source.connection_generation
    try:
        if source._idle_released or source._reconnect_allowed or disconnected:
            source._reconnect()
        while True:
            try:
                _check_recovery_budget(source)
                begin()
                return
            except Exception as exc:
                if isinstance(exc, BudgetExceeded) or source._cancelled: raise
                diagnostic = safe_database_diagnostic(exc) or {}
                # A new session can pass qualification then fail the fixed
                # BEGIN/readonly setup with no classified driver state. A
                # successful rollback does not make that setup succeed. No
                # fact row has been emitted, so it can consume the same bounded
                # recovery allowance as qualification. Keep semantic capability
                # failures and classified permission/auth errors non-retryable.
                qualified_begin_failure = (
                    source.connection_generation > initial_generation
                    and getattr(exc, 'code', None) == 'read_only_transaction_failed'
                    and diagnostic.get('stage') == 'snapshot_start'
                    and diagnostic.get('category') == 'unknown')
                if not (_preflight_can_recover(exc) or qualified_begin_failure): raise
                if source._boundary_recoveries >= MAX_BOUNDARY_RECOVERIES:
                    raise latch_terminal_failure(source, exc, engine=engine,
                                                 code='database_reconnect_failed') from None
                # Eligibility is determined once here from the setup failure,
                # not independently by a flag a driver cleanup may have reset.
                source._reconnect_allowed = True
                source._reconnect()
    except BaseException as exc:
        source._release_snapshot_connection()
        if not isinstance(exc, Exception) or isinstance(exc, BudgetExceeded): raise
        if source._cancelled:
            raise LightGraphError('query_cancelled') from None
        if source._disposed:
            raise LightGraphError('source_closed') from None
        stopped = terminal_failure(source)
        if stopped is not None: raise stopped from None
        if getattr(exc, 'retryable', False) or getattr(exc, 'code', None) == 'query_cancelled': raise
        code = 'database_reconnect_failed' if source._boundary_recovery_used else getattr(exc, 'code', 'read_only_transaction_failed')
        raise latch_terminal_failure(source, exc, engine=engine, code=code) from None
