"""Windows extended-length paths at Python IO boundaries, not in user metadata.

Node already supports long session paths. Python's recursive glob can silently
skip their directories when the Windows long-path policy is disabled. Prefixing
absolute paths avoids that policy dependency for scanning *and* later reads.
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path


_WINDOWS = os.name == "nt"


def _extended_windows_path(path: str) -> str:
    if path.startswith(("\\\\?\\", "\\\\.\\")):
        return path
    absolute = ntpath.abspath(path)
    return ("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\")
            else "\\\\?\\" + absolute)


def io_path(path: str | os.PathLike[str]) -> Path:
    """Use this path for open/stat/mkdir/glob, including short scan roots."""
    value = os.fspath(path)
    return Path(_extended_windows_path(value) if _WINDOWS else value)


def logical_path(path: str | os.PathLike[str]) -> Path:
    """Keep OS-specific IO prefixes out of SDK settings and exported paths."""
    value = os.fspath(path)
    if _WINDOWS:
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return Path(value)


def resolve_path(path: str | os.PathLike[str]) -> Path:
    return logical_path(io_path(path).resolve())
