"""Bounded retries for replay-safe workflow file operations and value-free diagnostics."""
import errno
import time

from lightgraph.errors import LightGraphError


class WorkflowPersistenceError(LightGraphError):
    def __init__(self, code, detail, retryable=False):
        super().__init__(code, retryable=retryable)
        self.detail = detail


def failure_detail(exc, stage, attempts=1):
    if isinstance(exc, WorkflowPersistenceError):
        return exc.detail
    # Never format the exception: filesystem paths, Arrow messages and YAML
    # errors may include source values. Only retain program-owned categories.
    kind = next((t.__name__ for t in (PermissionError, FileNotFoundError, FileExistsError,
                OSError, ValueError, TypeError, KeyError, LightGraphError) if isinstance(exc, t)), 'Exception')
    result = {'stage': stage, 'exception_type': kind, 'attempts': attempts}
    for field in ('errno', 'winerror'):
        value = getattr(exc, field, None)
        if isinstance(exc, OSError) and type(value) is int:
            result[field] = value
    return result


def artifact_step(stage, operation, *args, **kwargs):
    """The caller must make re-entry safe even if an earlier write succeeded."""
    delays = (0.05, 0.15, 0.45)
    for attempt in range(1, len(delays) + 2):
        try:
            return operation(*args, **kwargs)
        except WorkflowPersistenceError:
            raise
        except (LightGraphError, OSError, ValueError, KeyError, TypeError) as exc:
            transient = getattr(exc, 'code', None) == 'workflow_checkpoint_busy' or isinstance(exc, OSError) and (
                getattr(exc, 'winerror', None) in {32, 33}
                or exc.errno in {errno.EAGAIN, errno.EINTR, errno.EBUSY})
            # Windows can return ACCESS_DENIED while another process holds the
            # state file. A real ACL denial has the same code. Retry only these
            # atomic state writes, and keep a persistent denial non-retryable.
            state_access_denied = (stage in {'state_current_write', 'state_history_write'}
                                   and isinstance(exc, OSError) and getattr(exc, 'winerror', None) == 5)
            if (transient or state_access_denied) and attempt <= len(delays):
                time.sleep(delays[attempt - 1])
                continue
            code = exc.code if isinstance(exc, LightGraphError) else f'workflow_{stage}_failed'
            raise WorkflowPersistenceError(code, failure_detail(exc, stage, attempt),
                                           transient or getattr(exc, 'retryable', False)) from None
