"""A single writer owns one output directory; crash recovery runs under this lock."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
from ..errors import LightGraphError

@contextmanager
def run_lock(directory):
    path=Path(directory);path.mkdir(parents=True,exist_ok=True)
    with (path/'.writer.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise LightGraphError('output_directory_busy') from None
        try:yield
        finally:fcntl.flock(lock,fcntl.LOCK_UN)
