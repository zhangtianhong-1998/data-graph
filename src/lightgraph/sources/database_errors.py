"""Value-free driver diagnostics; classification never authorizes query replay."""
import re
import logging
import sys
from lightgraph.errors import LightGraphError
from .gauss_compat import sqlstate


PROBE_CODES = frozenset({'parameter_binding_unverified', 'distinct_projection_unverified',
    'read_only_transaction_unverified', 'statement_timeout_unverified', 'query_cancel_unverified',
    'runtime_control_unverified', 'cte_union_unverified'})
PROBE_FAILURES = frozenset({'no_row', 'malformed_row', 'readback_mismatch',
    'interrupt_not_observed', 'cancel_not_sent', 'cancel_not_completed'})
# Public diagnostic codes are program constants, never arbitrary exception text.
DATABASE_CAUSE_CODES = PROBE_CODES | frozenset({
    'database_connection_failed', 'database_preflight_failed', 'database_runtime_preflight_failed',
    'read_only_transaction_failed', 'snapshot_close_failed', 'runtime_capability_unverified',
    'capability_manifest_binding_mismatch', 'qualified_capability_manifest_required',
    'invalid_capability_manifest', 'source_catalog_changed', 'authorized_catalog_required',
    'duplicate_authorized_table', 'unauthorized_column_scope', 'authorized_table_unavailable_or_view',
    'authorized_column_unavailable', 'database_catalog_encoding_invalid', 'database_catalog_boolean_invalid',
    'metadata_size_limit', 'constraint_metadata_unsupported', 'product_version_unverified',
    'mysql_product_version_unsupported', 'mysql_driver_version_unsupported', 'mysql_storage_engine_unsupported',
    'mysql_dsn_invalid', 'system_catalog_forbidden', 'query_cancel_target_unverified',
    'query_cancel_connection_mismatch', 'query_execution_failed', 'query_cancel_failed',
    'query_timeout', 'query_cancelled', 'capability_probe_during_snapshot', 'source_closed',
    'database_reconnect_failed', 'unclassified_application_error'})
RECOVERY_DECISIONS = frozenset({'retry_new_session', 'retry_limit_exhausted',
    'qualification_rejected', 'deterministic_rejection', 'unclassified_failure'})
APPLICATION_REJECTIONS = frozenset({'source_catalog_changed', 'capability_manifest_binding_mismatch',
    'invalid_capability_manifest', 'qualified_capability_manifest_required', 'runtime_capability_unverified',
    'authorized_catalog_required', 'duplicate_authorized_table', 'unauthorized_column_scope',
    'authorized_table_unavailable_or_view', 'authorized_column_unavailable',
    'database_catalog_encoding_invalid', 'database_catalog_boolean_invalid',
    'metadata_size_limit', 'constraint_metadata_unsupported', 'product_version_unverified',
    'mysql_product_version_unsupported', 'mysql_driver_version_unsupported', 'mysql_storage_engine_unsupported',
    'mysql_dsn_invalid', 'system_catalog_forbidden', 'query_cancel_target_unverified',
    'query_cancel_connection_mismatch', 'capability_probe_during_snapshot'})


def cleanup_preserving_error(cleanup, *args):
    """Close resources without replacing an in-flight permission/budget error."""
    active_error = sys.exc_info()[1]
    try:
        cleanup(*args)
    except Exception:
        if active_error is None:
            raise


def diagnostic_cause_code(error):
    code = getattr(error, 'code', None)
    return code if isinstance(code, str) and code in DATABASE_CAUSE_CODES else 'unclassified_application_error'


def annotate_database_error(error, *, engine, stage):
    """Preserve a bare application rejection without making it retryable.

    This is diagnostic completion only. Recovery still needs an explicit
    session probe or driver classification; unknown application bugs stop.
    """
    current = safe_database_diagnostic(error)
    if current is None or current['stage'] == 'unknown':
        current = {**database_diagnostic(error, engine=engine, stage=stage),
                   **(current or {})}
        current['engine'], current['stage'] = engine, stage
    current.setdefault('cause_code', diagnostic_cause_code(error))
    if current['category'] == 'unknown' and current['cause_code'] in APPLICATION_REJECTIONS:
        current['category'] = 'configuration_or_catalog'
    error.database_diagnostic = current
    return error


def readback_shape_failure(row, width):
    """Describe fixed control-probe shape failures, never retain returned values."""
    if row is None:
        return 'no_row'
    if not isinstance(row, (tuple, list)) or len(row) != width:
        return 'malformed_row'
    return None


def qualification_error(probe_code, *, engine, stage, failure, connection_closed=None):
    """A probe result did not qualify this session; it proves no network cause.

    New-session readbacks may trigger another full qualification attempt.
    Readonly attestation keeps its own code rather than becoming a transient IO
    error. Every unqualified connection is discarded before any fact SQL.
    """
    if probe_code not in PROBE_CODES or failure not in PROBE_FAILURES:
        raise ValueError('invalid_qualification_diagnostic')
    code = 'database_preflight_failed' if probe_code in {'parameter_binding_unverified', 'distinct_projection_unverified'} else probe_code
    error = LightGraphError(code)
    error.database_diagnostic = {'engine': engine, 'stage': stage, 'category': 'qualification',
        'sqlstate': 'unknown', 'errno': None, 'rollback_succeeded': None,
        'recovery': 'none', 'connection_closed': connection_closed,
        'probe_code': probe_code, 'probe_failure': failure, 'cause_code': probe_code}
    logging.getLogger(__name__).error('database qualification failed: %s', probe_code,
        extra={'error_code': code, 'database_diagnostic': safe_database_diagnostic(error)})
    return error


def database_diagnostic(exc, *, engine, stage, rollback_succeeded=None, recovery='none', connection_closed=None):
    state = sqlstate(exc)
    args = getattr(exc, 'args', ())
    number = args[0] if engine == 'mysql' and args and type(args[0]) is int and 0 <= args[0] <= 65535 else None
    category = 'unknown'
    if state:
        category = ({'08': 'connection', '28': 'authentication', '42': 'configuration_or_catalog',
                     '53': 'resource', '0A': 'unsupported'}.get(state[:2], 'unknown'))
        category = {'25P02': 'failed_transaction', '25P03': 'idle_transaction_timeout',
                    '42501': 'permission', '57014': 'interrupted', '57P01': 'shutdown',
                    '57P02': 'shutdown', '57P03': 'unavailable', '40001': 'serialization',
                    '40P01': 'deadlock', '55P03': 'lock_unavailable'}.get(state, category)
    if number is not None:
        category = {1044: 'permission', 1045: 'authentication', 1142: 'permission',
                    1143: 'permission', 1227: 'permission', 1064: 'configuration_or_catalog',
                    1193: 'configuration_or_catalog', 1235: 'unsupported',
                    1205: 'lock_timeout', 1213: 'deadlock', 1317: 'interrupted',
                    3024: 'statement_timeout', 2006: 'connection', 2013: 'connection',
                    2055: 'connection'}.get(number, category)
    return {'engine': engine, 'stage': stage, 'category': category,
            'sqlstate': state or 'unknown', 'errno': number,
            'rollback_succeeded': rollback_succeeded, 'recovery': recovery,
            'connection_closed': connection_closed}


def connection_is_closed(connection):
    """Use explicit driver state, never exception wording, to detect disconnects."""
    if connection is None:
        return False
    for name in ('closed', 'broken'):
        value = getattr(connection, name, None)
        if type(value) in {bool, int} and value > 0:
            return True
    return getattr(connection, 'open', None) is False


def database_error(code, exc, *, engine, stage, rollback_succeeded=None, recovery='none', connection_closed=None):
    diagnostic = database_diagnostic(exc, engine=engine, stage=stage,
        rollback_succeeded=rollback_succeeded, recovery=recovery, connection_closed=connection_closed)
    # Only a fresh, rolled-back transaction setup can be retried here. Never
    # infer safety of replaying a query that may already have emitted batches.
    retryable = (stage == 'snapshot_start' and rollback_succeeded is True
        and diagnostic['category'] in {'serialization', 'deadlock', 'lock_unavailable', 'lock_timeout'})
    error = LightGraphError(code, retryable=retryable)
    error.database_diagnostic = diagnostic
    logging.getLogger(__name__).error('database operation failed: %s', code,
        extra={'error_code': code, 'database_diagnostic': safe_database_diagnostic(error)})
    return error


def safe_database_diagnostic(error):
    """Allowlist diagnostic fields at public tool/report boundaries."""
    value = getattr(error, 'database_diagnostic', None)
    if not isinstance(value, dict):
        return None
    categories = {'unknown', 'connection', 'authentication', 'configuration_or_catalog',
                  'resource', 'unsupported', 'failed_transaction', 'idle_transaction_timeout',
                  'permission', 'interrupted', 'shutdown', 'unavailable', 'serialization',
                  'deadlock', 'lock_unavailable', 'lock_timeout', 'statement_timeout', 'qualification'}
    state, number = value.get('sqlstate'), value.get('errno')
    def choice(name, allowed, default='unknown'):
        selected = value.get(name)
        return selected if isinstance(selected, str) and selected in allowed else default
    result = {
        'engine': choice('engine', {'gauss', 'mysql'}),
        'stage': choice('stage', {'snapshot_start', 'snapshot_close', 'connection_preflight', 'query', 'reconnect'}),
        'category': choice('category', categories),
        'sqlstate': state if isinstance(state, str) and re.fullmatch(r'[0-9A-Z]{5}', state) else 'unknown',
        'errno': number if type(number) is int and 0 <= number <= 65535 else None,
        'rollback_succeeded': value.get('rollback_succeeded') if type(value.get('rollback_succeeded')) is bool else None,
        'terminal': value.get('terminal') is True,
        'connection_closed': value.get('connection_closed') if type(value.get('connection_closed')) is bool else None,
        'recovery': choice('recovery', {
            'none', 'rolled_back', 'rollback_rebegin_failed', 'reconnect_required', 'reconnect_failed', 'reconnected'}, 'none'),
    }
    for name in ('boundary_recovery_attempts', 'recovery_attempt_limit'):
        if type(value.get(name)) is int and 0 <= value[name] <= 100:
            result[name] = value[name]
    for name, allowed in (('probe_code', PROBE_CODES), ('probe_failure', PROBE_FAILURES),
                          ('cause_code', DATABASE_CAUSE_CODES), ('recovery_decision', RECOVERY_DECISIONS)):
        if isinstance(value.get(name), str) and value[name] in allowed:
            result[name] = value[name]
    return result
