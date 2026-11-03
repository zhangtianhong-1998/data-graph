"""Install the vendored Harness's locked Node dependencies without starting an Agent."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def pnpm_command(node: str, executable: str, *, windows: bool | None = None) -> list[str]:
    """Run Windows npm/Corepack shims through Node, without a command shell."""
    if windows is None:
        windows = os.name == "nt"
    path = Path(executable)
    if windows and path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        candidates = [path.parent / "node_modules/corepack/dist/pnpm.js",
                      path.parent / "node_modules/pnpm/bin/pnpm.cjs"]
        script = next((candidate for candidate in candidates if candidate.is_file()), None)
        if script is None:
            raise RuntimeError("未找到 pnpm/Corepack 的 JavaScript 入口；请通过 npm 安装 pnpm 或启用 Corepack")
        return [node, str(script)]
    return [executable]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="检查已安装依赖，不下载")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    vendor = root / "vendor" / "deepseek-harness"
    node = shutil.which("node")
    pnpm = shutil.which("pnpm")
    if not node or not pnpm or not (vendor / "pnpm-lock.yaml").is_file():
        parser.error("需要 Node.js 22.19+/24+、pnpm 和仓库内 vendor/deepseek-harness")
    node_version = subprocess.check_output([node, "--version"], text=True).strip()
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", node_version)
    if not match or not (int(match[1]) >= 24 or (int(match[1]) == 22 and int(match[2]) >= 19)):
        parser.error("Node.js 版本不满足已锁定内核要求")
    expected = json.loads((vendor / "package.json").read_text())["packageManager"].split("@", 1)[1]
    env = {**os.environ, "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"}
    command = pnpm_command(node, pnpm)
    # pnpm/Corepack selects the exact packageManager version from this cwd.
    actual = subprocess.check_output([*command, "--version"], cwd=vendor, env=env, text=True).strip()
    if actual != expected:
        parser.error(f"需要 pnpm {expected}，当前为 {actual}；请启用 Corepack 或安装对应版本")
    if not args.check:
        subprocess.run([*command, "install", "--frozen-lockfile", "--ignore-scripts"],
                       cwd=vendor, env=env, check=True)
        # Session JSONL uses this small host addon for durable locking. It is
        # required even though shell/PTY/browser tools are disabled.
        subprocess.run([*command, "run", "build:native-system"], cwd=vendor, env=env, check=True)
    loader = vendor / "node_modules" / "tsx" / "dist" / "esm" / "index.mjs"
    if not loader.is_file():
        parser.error("缺少 tsx 源码加载器，请运行不带 --check 的安装命令")
    from lightgraph.construction.launcher import verify_native_dependencies
    verify_native_dependencies(vendor, node)
    if os.name == "nt":
        # Windows uses the upstream Koffi/kernel32 semaphore, not POSIX flock.
        # Exercise that actual binding instead of assuming --version loads it.
        script = "import {acquireLockHandleWin32,releaseLockHandleWin32} from './packages/session/session-persistence-jsonl/src/win32.ts'; const h=await acquireLockHandleWin32('lightgraph-bootstrap-'+process.pid); await releaseLockHandleWin32(h)"
        subprocess.run([node, "--import", "tsx/esm", "--input-type=module", "-e", script],
                       cwd=vendor, check=True, timeout=20)
    subprocess.run([sys.executable, "-m", "lightgraph.construction.launcher", "--version"],
                   cwd=root, check=True)
    print("Harness 锁定依赖与官方 CLI 启动检查通过；未调用模型。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
