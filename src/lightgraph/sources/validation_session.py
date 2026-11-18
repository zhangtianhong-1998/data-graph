"""Keep qualified physical sessions only while one synchronous validation runs.

Each projection still starts and finishes its own read-only transaction. Failed
transactions and cancellations follow the adapter's ordinary discard path.
"""
from contextvars import ContextVar
from functools import wraps

_active = ContextVar('validation_sessions', default=None)


def keep_session(source):
    sessions = _active.get()
    if (sessions is None or source._closed or source._cancelled or source._reconnect_allowed
            or getattr(source, '_broken', False) or source._disposed):
        return False
    sessions[id(source)] = source
    return True


def reuse_validation_sessions(function):
    @wraps(function)
    def run(*args, **kwargs):
        sessions = {}
        token = _active.set(sessions)
        try:
            return function(*args, **kwargs)
        finally:
            _active.reset(token)
            for source in sessions.values():
                with source._lock:
                    if not source._closed:
                        source._release_snapshot_connection()
    return run
