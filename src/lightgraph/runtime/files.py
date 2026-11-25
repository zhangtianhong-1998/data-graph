"""OS-specific path and replacement semantics shared by local stores."""
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import sys
import time


def file_stamp(path: Path):
    """Filesystem identity of the local file that supplied an input snapshot."""
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def system_directory_path(path: Path) -> Path:
    """Expand only root-owned Darwin aliases with their expected literal target."""
    if sys.platform == 'darwin' and path.is_absolute() and len(path.parts) >= 2:
        name = path.parts[1]
        if name in {'tmp', 'var', 'etc'}:
            alias = Path('/') / name
            try:
                if alias.lstat().st_uid == 0 and os.readlink(alias) in (f'private/{name}', f'/private/{name}'):
                    return Path('/private', *path.parts[1:])
            except OSError:
                pass
    return path


_outer_retry = ContextVar('file_write_retry_owner', default=False)


@contextmanager
def caller_retries_files():
    token = _outer_retry.set(True)
    try:
        yield
    finally:
        _outer_retry.reset(token)


def replace_staged_file(staged, target):
    """Retry ordinary Windows sharing violations on the same completed staging file."""
    delays = () if _outer_retry.get() else (0.05, 0.15, 0.45)
    for attempt in range(len(delays) + 1):
        try:
            os.replace(staged, target)
            return
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in {32, 33} or attempt == len(delays):
                raise
            time.sleep(delays[attempt])
