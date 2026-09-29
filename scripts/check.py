#!/usr/bin/env python3
"""一条命令跑完这个仓库的全部检查。

    python scripts/check.py              # 全部
    python scripts/check.py --fast       # 跳过要起服务的冒烟测试
    python scripts/check.py --only pytest
    python scripts/check.py --list

这是唯一的验证入口：CI 跑的就是它，本地也跑它。加一项新检查 = 往下面
``STEPS`` 里加一条声明，不需要再教任何人"该跑哪几个命令"。

退出码：任何一步失败则非 0。**跳过的步骤会在总结里显式列出来** ——
跳过不是通过，尤其是前端检查在没装 node 的机器上会被跳过。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Windows 的控制台默认是 cp1252，中文/长行会让 print 抛 UnicodeEncodeError，
# 而一条"通过"的断言就能把整个套件挂掉。先把自己和子进程都钉成 utf-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
    except Exception:  # pragma: no cover — 非 tty / 老版本
        pass

CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
if str(ROOT) not in CHILD_ENV.get("PYTHONPATH", ""):
    CHILD_ENV["PYTHONPATH"] = str(ROOT) + os.pathsep + CHILD_ENV.get("PYTHONPATH", "")


@dataclass
class Step:
    name: str
    label: str
    cmd: list[str]
    cwd: Path = field(default_factory=lambda: ROOT)
    needs: str | None = None      # 需要的外部命令，缺了就跳过
    fast_skips: bool = False      # --fast 时跳过（要起服务的那些）
    timeout: int = 900
    hint: str = ""                # 失败时补一句「那要去装什么」


STEPS = [
    Step(
        name="pytest",
        label="全部测试（pyproject 的 testpaths：tests/ + desktop/tests）",
        # 不写路径：交给 pyproject 的 testpaths 决定「什么算测试套件」。
        # 写死 tests/ 会让 desktop/tests（桌面壳自己的测试）悄悄掉在门外。
        cmd=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        timeout=1800,
        hint="pytest 没装：pip install -e '.[dev]'",
    ),
    Step(
        name="lint",
        label="静态检查：未定义 / 重复定义的名字（ruff F821/F811）",
        # 只挑"一定是 bug"的两条规则。全量 ruff 会淹在历史噪声里（未用 import、
        # import 排序），而门禁的价值在于**每次都真的会跑**。
        # 这一条本来就能拦住 v0.3.1 那次 `NameError: name '__version__' is not defined`：
        # 它只在冻结版走 --no-window 那条路径时才炸，本地测试与冒烟测试都没走到。
        cmd=[
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "F821,F811",
            "desktop",
            "desktop_main.py",
            "scripts",
        ],
        needs="ruff",
        timeout=180,
        hint="pip install ruff（CI 的 verify.yml 装 dev 依赖，那边一定会跑）",
    ),
    Step(
        name="frontend",
        label="前端检查 (npm run check)",
        cmd=["npm", "run", "check"],
        cwd=ROOT / "web",
        needs="node",
        timeout=600,
        hint="需要 node + web/node_modules：cd web && npm ci",
    ),
    Step(
        name="smoke",
        label="桌面壳冒烟测试 (scripts/smoke_test.py)",
        cmd=[sys.executable, "scripts/smoke_test.py"],
        fast_skips=True,
        timeout=600,
    ),
]


def which(cmd: str) -> bool:
    from shutil import which as _which

    return _which(cmd) is not None


def resolve_argv(cmd: list[str]) -> list[str] | None:
    """把命令名解析成真实可执行文件；找不到返回 ``None``。

    Windows 上 ``npm`` 实际是 ``npm.cmd`` 这种批处理垫片，而
    ``subprocess`` 不带 shell 时只认 ``.exe`` —— 直接跑 ``"npm"`` 会
    报 ``WinError 2``（「系统找不到指定的文件」），看起来像环境缺东西，
    其实只是垫片没被解析。``shutil.which`` 会按 PATHEXT 找到它。
    """
    from shutil import which as _which

    head = cmd[0]
    resolved = _which(head)
    if resolved is None:
        return None
    return [resolved, *cmd[1:]]


def rule(title: str = "") -> None:
    line = "─" * max(4, 62 - len(title))
    print(f"\n── {title} {line}" if title else "─" * 62)


def run(step: Step) -> tuple[str, float]:
    """跑一步；返回 (结果, 秒)。结果 ∈ {PASS, FAIL, SKIP}。"""
    rule(step.label)
    argv = resolve_argv(step.cmd)
    if argv is None:
        print(f"!! 找不到命令：{step.cmd[0]} —— 装它，或用 --only 跑别的步骤")
        if step.hint:
            print(f"   {step.hint}")
        return "FAIL", 0.0
    print(f"$ {' '.join(argv)}  (cwd={step.cwd.relative_to(ROOT) or '.'})")
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=step.cwd, env=CHILD_ENV, timeout=step.timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        print(f"!! 超时（{step.timeout}s）")
        code = -1
    except OSError as exc:
        print(f"!! 起不来：{exc}")
        code = -1
    elapsed = time.monotonic() - started
    result = "PASS" if code == 0 else "FAIL"
    if result == "FAIL" and step.hint:
        print(f"   提示：{step.hint}")
    return result, elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑完这个仓库的全部检查")
    parser.add_argument("--fast", action="store_true", help="跳过要起服务的步骤（冒烟测试）")
    parser.add_argument("--only", action="append", metavar="NAME", help="只跑某一步（可重复）")
    parser.add_argument("--list", action="store_true", help="列出所有步骤后退出")
    args = parser.parse_args(argv)

    if args.list:
        for s in STEPS:
            print(f"{s.name:10} {s.label}")
        return 0

    selected = [s for s in STEPS if not args.only or s.name in args.only]
    if not selected:
        print(f"没有匹配的步骤：{args.only}（用 --list 看有哪些）")
        return 2

    print("OpenMinis Desktop · 检查")
    results: list[tuple[Step, str, float, str]] = []
    for step in selected:
        if step.fast_skips and args.fast:
            results.append((step, "SKIP", 0.0, "--fast"))
            continue
        if step.needs and not which(step.needs):
            results.append((step, "SKIP", 0.0, f"缺 {step.needs}"))
            continue
        result, elapsed = run(step)
        results.append((step, result, elapsed, ""))

    rule("总结")
    failed = 0
    for step, result, elapsed, why in results:
        mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "·"}[result]
        tail = f"  ({why})" if why else ""
        print(f"  {mark} {result:4} {elapsed:6.1f}s  {step.label}{tail}")
        if result == "FAIL":
            failed += 1

    skipped = [s.label for s, r, _, _ in results if r == "SKIP"]
    if skipped:
        print("\n  跳过的步骤不算通过：")
        for label in skipped:
            print(f"    · {label}")
    print()
    if failed:
        print(f"结果：{failed} 步失败")
        return 1
    print("结果：全部通过" + ("（有步骤被跳过）" if skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
