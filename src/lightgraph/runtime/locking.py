"""Process leases which survive parent exit while a worker owns an inherited handle.

Windows uses an exclusive file open, rather than byte-range locks (which are
not inherited). Closing one owner never explicitly unlocks the other owners.
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import subprocess
import threading

from ..errors import LightGraphError


_SPAWN_LOCK = threading.RLock()
_REPARSE_POINT = 0x400


def _platform():
    return os.name


def _check_path(path: Path):
    """Reject links in the requested path without resolving them away first."""
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & _REPARSE_POINT:
            raise OSError(errno.ELOOP, 'linked lock path')
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise OSError(errno.ENOTDIR, 'invalid lock parent')


class _WindowsAPI:
    """Small, lazy Win32 boundary; importing this module does not require Windows."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                           wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.GetFileType.argtypes = [wintypes.HANDLE]
        self.kernel.GetFileType.restype = wintypes.DWORD
        self.kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                           wintypes.LPVOID, wintypes.DWORD]
        self.kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL

        class AttributeTagInfo(ctypes.Structure):
            _fields_ = [('FileAttributes', wintypes.DWORD), ('ReparseTag', wintypes.DWORD)]

        self.attribute_type = AttributeTagInfo
        self.invalid_handle = ctypes.c_void_p(-1).value

    def open(self, path):
        # OPEN_ALWAYS, no sharing, non-inheritable by default. OPEN_REPARSE_POINT
        # lets us reject the link itself rather than opening its target.
        filename = str(path)
        if not filename.startswith('\\\\?\\'):
            filename = ('\\\\?\\UNC\\' + filename[2:] if filename.startswith('\\\\')
                        else '\\\\?\\' + filename)
        handle = self.kernel.CreateFileW(filename, 0x80000000 | 0x40000000, 0, None,
                                         4, 0x80 | 0x00200000, None)
        if handle == self.invalid_handle:
            error = self.ctypes.get_last_error()
            exc = OSError(error, 'lock open failed')
            exc.winerror = error
            raise exc
        return handle

    def regular_file(self, handle):
        info = self.attribute_type()
        if not self.kernel.GetFileInformationByHandleEx(handle, 9, self.ctypes.byref(info),
                                                        self.ctypes.sizeof(info)):
            raise OSError('lock attributes unavailable')
        return self.kernel.GetFileType(handle) == 1 and not info.FileAttributes & (0x10 | _REPARSE_POINT)

    def close(self, handle):
        self.kernel.CloseHandle(handle)

    def inheritable(self, handle):
        return os.get_handle_inheritable(handle)

    def set_inheritable(self, handle, value):
        os.set_handle_inheritable(handle, value)


class FileLock:
    """Nonblocking, cross-process lease on an existing parent directory.

    Use ``child_process_options`` around Popen to explicitly transfer another
    ownership reference to a worker. The child keeps it until process exit.
    """

    def __init__(self, path, busy_code='file_lock_busy', invalid_code='file_lock_invalid'):
        self.path = Path(os.path.abspath(path))
        self.busy_code = busy_code
        self.invalid_code = invalid_code
        self._handle = None
        self._windows = None
        self._mutex = threading.RLock()

    def acquire(self):
        with self._mutex:
            if self._handle is not None:
                return self
            handle = None
            windows = None
            try:
                if _platform() == 'nt':
                    _check_path(self.path.parent)
                    windows = _WindowsAPI()
                    handle = windows.open(self.path)
                    if not windows.regular_file(handle):
                        raise OSError('invalid lock file')
                elif _platform() == 'posix':
                    import fcntl

                    _check_path(self.path)
                    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
                    handle = os.open(self.path, flags, 0o600)
                    os.set_inheritable(handle, False)
                    if not stat.S_ISREG(os.fstat(handle).st_mode):
                        raise OSError('invalid lock file')
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    raise OSError('unsupported lock platform')
                # A separate stat/open of an exclusively opened Windows file
                # may itself fail with sharing violation; its handle was checked.
                _check_path(self.path.parent if windows is not None else self.path)
            except OSError as exc:
                if handle is not None:
                    windows.close(handle) if windows is not None else os.close(handle)
                busy = (getattr(exc, 'winerror', None) in (32, 33) if windows is not None
                        else isinstance(exc, BlockingIOError) or exc.errno == errno.EAGAIN)
                raise LightGraphError(self.busy_code if busy else self.invalid_code) from None
            self._handle = handle
            self._windows = windows
            return self

    def close(self):
        with self._mutex:
            if self._handle is None:
                return
            handle, self._handle = self._handle, None
            if self._windows is not None:
                self._windows.close(handle)
            else:
                os.close(handle)
            # Do not LOCK_UN / UnlockFile: an inherited worker still owns its
            # reference to the same open file description or Windows file object.

    @contextmanager
    def child_process_options(self):
        """Popen options with only this lease explicitly inherited by a child.

        All application subprocess launches that inherit handles must use this
        window; the process-wide mutex avoids overlapping inheritance windows.
        """
        with _SPAWN_LOCK, self._mutex:
            if self._handle is None:
                raise LightGraphError(self.invalid_code)
            if self._windows is None:
                yield {'pass_fds': (self._handle,), 'start_new_session': True}
                return
            old = self._windows.inheritable(self._handle)
            try:
                self._windows.set_inheritable(self._handle, True)
                startup = subprocess.STARTUPINFO()
                startup.lpAttributeList = {'handle_list': [self._handle]}
                yield {'startupinfo': startup, 'close_fds': True,
                       'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
            finally:
                self._windows.set_inheritable(self._handle, old)


@contextmanager
def run_lock(directory):
    path = Path(os.path.abspath(directory))
    try:
        _check_path(path)
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise LightGraphError('output_directory_lock_invalid') from None
    lock = FileLock(path / '.writer.lock', busy_code='output_directory_busy',
                    invalid_code='output_directory_lock_invalid').acquire()
    try:
        yield
    finally:
        lock.close()
