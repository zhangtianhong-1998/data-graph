"""Real process ownership tests run unchanged on POSIX and Windows CI."""
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from lightgraph.errors import LightGraphError
from lightgraph.runtime import locking
from lightgraph.runtime.locking import FileLock, run_lock


def _env():
    return {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2])}


def _wait_file(path, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(.02)
    pytest.fail(f'child did not create {path.name}')


def _wait_acquire(path, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = FileLock(path).acquire()
        except LightGraphError as error:
            assert error.code == 'file_lock_busy'
            time.sleep(.02)
        else:
            result.close()
            return
    pytest.fail('child did not release its lease')


def test_run_lock_preserves_busy_code_and_releases_after_error(tmp_path):
    directory = tmp_path / 'new' / 'output'
    with pytest.raises(ValueError):
        with run_lock(directory):
            with pytest.raises(LightGraphError, match='^output_directory_busy$'):
                with run_lock(directory):
                    pytest.fail('second writer was admitted')
            raise ValueError('operation failed')
    with run_lock(directory):
        assert (directory / '.writer.lock').exists()


def test_close_idempotent_and_unacquired_inheritance_rejected(tmp_path):
    lock = FileLock(tmp_path / 'lease', invalid_code='invalid_lease')
    lock.close()
    with pytest.raises(LightGraphError, match='^invalid_lease$'):
        with lock.child_process_options():
            pytest.fail('unowned lease was offered')
    assert lock.acquire() is lock
    assert lock.acquire() is lock
    lock.close()
    lock.close()
    _wait_acquire(tmp_path / 'lease')


def test_other_process_excluded_and_crash_releases_lock(tmp_path):
    path, ready = tmp_path / 'lease', tmp_path / 'ready'
    code = '''
from pathlib import Path
import sys, time
from lightgraph.runtime.locking import FileLock
lease = FileLock(sys.argv[1]).acquire()
Path(sys.argv[2]).touch()
time.sleep(30)
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(path), str(ready)], env=_env())
    try:
        _wait_file(ready)
        with pytest.raises(LightGraphError, match='^file_lock_busy$'):
            FileLock(path).acquire()
    finally:
        child.kill()
        child.wait(timeout=10)
    _wait_acquire(path)


def test_inherited_worker_keeps_lock_after_parent_crash(tmp_path):
    path, ready, stop = (tmp_path / name for name in ('lease', 'ready', 'stop'))
    worker = '''
from pathlib import Path
import sys, time
Path(sys.argv[1]).touch()
deadline = time.monotonic() + 15
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(.02)
'''
    parent = '''
import os, subprocess, sys
from lightgraph.runtime.locking import FileLock
lease = FileLock(sys.argv[1]).acquire()
with lease.child_process_options() as options:
    subprocess.Popen([sys.executable, '-c', sys.argv[4], sys.argv[2], sys.argv[3]], **options)
os._exit(0)
'''
    process = subprocess.Popen([sys.executable, '-c', parent, str(path), str(ready), str(stop), worker], env=_env())
    try:
        assert process.wait(timeout=10) == 0
        _wait_file(ready)
        with pytest.raises(LightGraphError, match='^file_lock_busy$'):
            FileLock(path).acquire()
    finally:
        stop.touch()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    _wait_acquire(path)


def test_explicit_parent_close_does_not_unlock_inherited_worker(tmp_path):
    path, ready, stop = (tmp_path / name for name in ('lease', 'ready', 'stop'))
    code = '''
from pathlib import Path
import sys, time
Path(sys.argv[1]).touch()
deadline = time.monotonic() + 15
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(.02)
'''
    lease = FileLock(path).acquire()
    child = None
    try:
        with lease.child_process_options() as options:
            child = subprocess.Popen([sys.executable, '-c', code, str(ready), str(stop)], **options)
        _wait_file(ready)
        lease.close()
        with pytest.raises(LightGraphError, match='^file_lock_busy$'):
            FileLock(path).acquire()
    finally:
        stop.touch()
        lease.close()
        if child is not None:
            child.wait(timeout=10)
    _wait_acquire(path)


@pytest.mark.parametrize('parent_link', [False, True])
def test_symbolic_link_lock_or_parent_rejected(tmp_path, parent_link):
    target = tmp_path / 'target'
    target.mkdir() if parent_link else target.touch()
    link = tmp_path / 'link'
    try:
        link.symlink_to(target, target_is_directory=parent_link)
    except OSError as error:
        if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
            pytest.skip('symbolic links unavailable to this Windows account')
        raise
    path = link / 'lease' if parent_link else link
    with pytest.raises(LightGraphError, match='^file_lock_invalid$'):
        FileLock(path).acquire()


def test_directory_cannot_be_lock_file(tmp_path):
    with pytest.raises(LightGraphError, match='^file_lock_invalid$'):
        FileLock(tmp_path).acquire()


def test_reparse_point_ancestor_rejected_without_resolving(tmp_path, monkeypatch):
    original = Path.lstat

    def lstat(path):
        info = original(path)
        if path == tmp_path:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(LightGraphError, match='^file_lock_invalid$'):
        FileLock(tmp_path / 'lease').acquire()


class FakeWindows:
    def __init__(self):
        self.handle = 2 ** 36 + 7
        self.inherit = False
        self.closed = []
        self.valid = True

    def open(self, path):
        return self.handle

    def regular_file(self, handle):
        return self.valid

    def close(self, handle):
        self.closed.append(handle)

    def inheritable(self, handle):
        return self.inherit

    def set_inheritable(self, handle, value):
        self.inherit = value


def _windows_fixture(monkeypatch):
    api = FakeWindows()
    monkeypatch.setattr(locking, '_platform', lambda: 'nt')
    monkeypatch.setattr(locking, '_WindowsAPI', lambda: api)
    monkeypatch.setattr(subprocess, 'STARTUPINFO', SimpleNamespace, raising=False)
    monkeypatch.setattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200, raising=False)
    return api


def test_windows_only_explicit_handle_inherited_and_restored_on_spawn_failure(tmp_path, monkeypatch):
    api = _windows_fixture(monkeypatch)
    lease = FileLock(tmp_path / 'lease').acquire()
    try:
        with pytest.raises(RuntimeError, match='spawn failed'):
            with lease.child_process_options() as options:
                assert api.inherit is True
                assert options['close_fds'] is True
                assert options['startupinfo'].lpAttributeList == {'handle_list': [api.handle]}
                assert options['creationflags'] == 0x200
                assert 'pass_fds' not in options and 'start_new_session' not in options
                raise RuntimeError('spawn failed')
        assert api.inherit is False
        assert api.closed == []
    finally:
        lease.close()
        lease.close()
    assert api.closed == [api.handle]


def test_windows_invalid_file_closes_acquired_handle(tmp_path, monkeypatch):
    api = _windows_fixture(monkeypatch)
    api.valid = False
    with pytest.raises(LightGraphError, match='^file_lock_invalid$'):
        FileLock(tmp_path / 'lease').acquire()
    assert api.closed == [api.handle]


@pytest.mark.parametrize('winerror,code', [(32, 'file_lock_busy'), (33, 'file_lock_busy'), (5, 'file_lock_invalid')])
def test_windows_open_errors_have_safe_codes(tmp_path, monkeypatch, winerror, code):
    api = _windows_fixture(monkeypatch)

    def failed_open(path):
        error = OSError('private path must not escape')
        error.winerror = winerror
        raise error

    api.open = failed_open
    with pytest.raises(LightGraphError, match=f'^{code}$'):
        FileLock(tmp_path / 'lease').acquire()
    assert api.closed == []
