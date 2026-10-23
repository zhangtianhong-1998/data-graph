"""Encrypted, project-scoped database connection profiles.

Only connection configuration enters this store, never query results. The local
key and ciphertext require the same host account to restore a connection; this
protects against accidental plaintext exposure, not a compromised host account.
POSIX uses owner-only modes. Windows inherits the workspace directory's ACLs.
The workbench service lease provides the cross-process single-writer boundary;
the module lock also coordinates separate store instances in one process.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading

from cryptography.fernet import Fernet, InvalidToken

from ..errors import LightGraphError
from ..runtime.config import SourceConfig
from .database_access import ConnectionSpec


_LOCK = threading.RLock()
_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_MAX_PROFILE_BYTES = 128 * 1024


@contextmanager
def _safe_storage_errors():
    try:
        yield
    except LightGraphError:
        raise
    except Exception:
        # Filesystem and validation diagnostics may include private paths or
        # connection values. Neither exception chaining nor log output is safe.
        raise LightGraphError("saved_connection_storage_failed") from None


def _safe_path(path: Path) -> None:
    for entry in (path, *path.parents):
        if entry.is_symlink():
            raise LightGraphError("saved_connection_path_unsafe")


def _private_directory(path: Path) -> None:
    _safe_path(path)
    if not path.parent.exists():
        _private_directory(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)
    _safe_path(path)
    if not path.is_dir():
        raise LightGraphError("saved_connection_path_unsafe")
    if os.name == "posix":
        path.chmod(0o700)


def _read(path: Path, limit: int) -> bytes:
    _safe_path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise LightGraphError("saved_connection_path_unsafe")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise LightGraphError("saved_connection_path_unsafe")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise LightGraphError("saved_connection_corrupt")
    return data


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ciphertext/key only, retaining the old file if publication fails."""
    _safe_path(path)
    fd, temporary = tempfile.mkstemp(prefix=".connection-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _safe_path(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _connection_dict(connection: ConnectionSpec) -> dict:
    result = connection.model_dump(by_alias=True, exclude_none=True)
    # SecretStr.model_dump deliberately masks secrets unless unwrapped. This
    # dictionary must only enter encrypted serialization or private memory.
    for name in ("dsn", "password"):
        value = getattr(connection, name)
        if value is not None:
            result[name] = value.get_secret_value()
    return result


class SavedConnections:
    def __init__(self, workspace_root: Path):
        # Do not resolve before checking: that would hide a symlink supplied by
        # the caller. Construction stays read-only until the first saved profile.
        self.root = Path(workspace_root).absolute()
        _safe_path(self.root)
        self.key_path = self.root / ".connection-key"

    def _path(self, pid: str, sid: str) -> Path:
        if (not isinstance(pid, str) or not _PROJECT_ID.fullmatch(pid) or
                not isinstance(sid, str) or not _SOURCE_ID.fullmatch(sid)):
            raise LightGraphError("saved_connection_invalid")
        path = self.root / pid / ".connections" / (sid + ".enc")
        _safe_path(path)
        return path

    def _cipher(self, *, create: bool) -> Fernet:
        _safe_path(self.key_path)
        if not self.key_path.exists():
            # Never silently rotate a lost key: all existing profiles would
            # become unreadable. Inspect profile paths only, never project data.
            if not create or next(self.root.glob("*/.connections/*.enc"), None) is not None:
                raise LightGraphError("saved_connection_key_missing")
            _atomic_write(self.key_path, Fernet.generate_key())
        try:
            return Fernet(_read(self.key_path, 128))
        except (ValueError, TypeError):
            pass
        except LightGraphError as exc:
            # Corrupt key contents can be replaced after the last profile was
            # explicitly deleted. Never treat unsafe paths as damaged contents.
            if exc.code != "saved_connection_corrupt":
                raise
        if create and next(self.root.glob("*/.connections/*.enc"), None) is None:
            key = Fernet.generate_key()
            _atomic_write(self.key_path, key)
            return Fernet(key)
        raise LightGraphError("saved_connection_corrupt") from None

    def save(self, pid: str, source: SourceConfig, connection: ConnectionSpec, dsn: str) -> None:
        with _LOCK, _safe_storage_errors():
            path = self._path(pid, source.id)
            if (source.credential_mode != "session" or not source.credential_revision or
                    source.kind != connection.kind or not isinstance(dsn, str) or not dsn or
                    len(dsn) > 32768):
                raise LightGraphError("saved_connection_invalid")
            _private_directory(path.parent)
            cipher = self._cipher(create=True)
            payload = {"version": 1, "project_id": pid, "source_id": source.id,
                       "kind": source.kind, "credential_revision": source.credential_revision,
                       "connection": _connection_dict(connection), "dsn": dsn}
            encoded = cipher.encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            if len(encoded) > _MAX_PROFILE_BYTES:
                raise LightGraphError("saved_connection_invalid")
            _atomic_write(path, encoded)

    def load(self, pid: str, source: SourceConfig) -> dict | None:
        with _LOCK, _safe_storage_errors():
            path = self._path(pid, source.id)
            if not path.exists():
                return None
            cipher = self._cipher(create=False)
            try:
                payload = json.loads(cipher.decrypt(_read(path, _MAX_PROFILE_BYTES)))
                if (not isinstance(payload, dict) or payload.get("version") != 1 or
                        not isinstance(payload.get("dsn"), str) or not payload["dsn"] or
                        len(payload["dsn"]) > 32768):
                    raise ValueError("invalid_profile")
                connection = ConnectionSpec.model_validate(payload["connection"])
                if payload.get("kind") != connection.kind:
                    raise ValueError("invalid_profile")
            except (InvalidToken, ValueError, TypeError, KeyError):
                raise LightGraphError("saved_connection_corrupt") from None
            binding = (payload.get("project_id"), payload.get("source_id"), payload.get("kind"),
                       payload.get("credential_revision"))
            if (source.credential_mode != "session" or not source.credential_revision or
                    binding != (pid, source.id, source.kind, source.credential_revision)):
                return None
            return {"connection": _connection_dict(connection), "dsn": payload["dsn"]}

    def delete(self, pid: str, sid: str) -> None:
        with _LOCK, _safe_storage_errors():
            self._path(pid, sid).unlink(missing_ok=True)
