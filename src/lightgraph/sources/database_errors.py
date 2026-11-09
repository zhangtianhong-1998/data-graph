"""Value-free driver diagnostics; classification never authorizes query replay."""
import re
import logging
from lightgraph.errors import LightGraphError
from .gauss_compat import sqlstate


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
                  'deadlock', 'lock_unavailable', 'lock_timeout', 'statement_timeout'}
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
    return result
