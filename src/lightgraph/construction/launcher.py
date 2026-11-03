"""Public dsh source launcher for the version vendored with this checkout."""
from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess
import sys


def vendor_root() -> Path:
    return Path(__file__).resolve().parents[3] / "vendor" / "deepseek-harness"


def required_addon(root: Path, platform: str, arch: str, libc: str | None = None) -> Path | None:
    """Resolve the host lock addon from upstream metadata, including Linux libc."""
    if platform == "win32":
        return None  # Upstream Windows leases use Koffi and a kernel semaphore.
    folder = root / "native/system/packages" / f"{platform}-{arch}"
    spec = folder / "prebuilds.json"
    if not spec.is_file():
        raise RuntimeError("Unsupported Harness native host")
    for binary in json.loads(spec.read_text(encoding="utf-8"))["binaries"]:
        if binary.get("tool") == "flock" and binary.get("libc") == libc:
            return folder / binary["path"]
    raise RuntimeError("Unsupported Harness native libc")


def verify_native_dependencies(root: Path, node: str) -> None:
    script = "JSON.stringify({platform:process.platform,arch:process.arch,libc:process.platform==='linux'?(process.report.getReport().header.glibcVersionRuntime?'glibc':'musl'):null})"
    host = json.loads(subprocess.check_output([node, "-p", script], text=True, timeout=10))
    path = required_addon(root, **host)
    if path is not None and not path.is_file():
        raise RuntimeError("Harness session persistence addon missing; run python scripts/bootstrap_harness.py")


def launch_command() -> tuple[Path, list[str]]:
    """Use the upstream CLI and source loader; never bypass dsh profiles."""
    root = vendor_root()
    entry = root / "apps" / "cli" / "src" / "bin.ts"
    loader = root / "node_modules" / "tsx" / "dist" / "esm" / "index.mjs"
    node = shutil.which("node")
    if not node or not entry.is_file() or not loader.is_file():
        raise RuntimeError("Harness dependencies missing; run python scripts/bootstrap_harness.py")
    verify_native_dependencies(root, node)
    return root, [node, "--import", "tsx/esm", str(entry)]


def _windows_child_job():
    """Keep the launcher and its descendants in a kill-on-close Windows Job.

    Assigning this launcher before spawning Node eliminates the child-assignment
    race. Keep the handle until process exit: closing it also ends this process.
    """
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = Extended()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.get_last_error(); kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        error = ctypes.get_last_error(); kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    return job


def main() -> int:
    try:
        root, command = launch_command()
        command.extend(sys.argv[1:])
        if os.name == "nt":
            _job = _windows_child_job()
            return subprocess.call(command, cwd=root)
        os.chdir(root)
        os.execv(command[0], command)
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
