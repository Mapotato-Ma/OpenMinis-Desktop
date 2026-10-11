#!/usr/bin/env python3
"""更新助手的真机演练：真跑一遍 ``cmd`` 助手，看它到底有没有把目录换对 [R01–R05]。

    python scripts/update_drill.py            # 两条分支都跑
    python scripts/update_drill.py --keep     # 留下临时目录，自己去翻日志

为什么必须真跑：助手脚本做的事（等旧进程退出 / 改名让位 / 就位 / 启动 / 等启动标记 /
删旧目录）**只能在 Windows 的 cmd 里验证** —— 单元测试只能断言"脚本里写了哪几行"。
改前踩过的坑就是这种：

* 用 ``timeout /t`` 当 sleep，而助手是 ``stdin=DEVNULL`` 起的（``timeout`` 在没有
  控制台时直接报错返回）→ 90 次等待循环毫秒级空转完、"等超时放弃"变成"立刻放弃"；
* 路径写进 ANSI 的 ``.cmd`` → 中文安装路径下 ``move`` 连试 15 次全失败。

演练跑在**临时目录**里的假安装上：绝不动真正的安装目录，也不需要管理员权限。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认是 cp936，中文/长行会让 print 抛 UnicodeEncodeError（或输出乱码）。
# 与 scripts/check.py 同一处理。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:  # pragma: no cover - 非 tty / 老版本
        pass

from desktop import updater  # noqa: E402


def _fake_app(body: str) -> str:
    """假的"新版应用"：跑起来写 / 不写那份启动标记（保持纯 ASCII）。"""
    return "@echo off\n" + body + "\nexit /b 0\n"


def _dead_pid() -> int:
    """一个**已经退出**的进程号 —— 模拟"旧版已经关掉"。"""
    proc = subprocess.Popen(["cmd", "/c", "exit"],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    proc.wait(timeout=10)
    return int(proc.pid)


def _run_helper(*, work: Path, target: Path, staged: Path, staging_root: Path,
                app_root: Path, exe_name: str, boot_ok: Path, boot_max: int,
                ) -> tuple[int, str]:
    script = work / f"drill-{target.name}.cmd"
    updater.write_helper_script(script, updater.swap_script_text())
    env = updater.helper_env(**{
        updater._ENV_PID: _dead_pid(),
        updater._ENV_EXE: exe_name,
        updater._ENV_APPROOT: app_root,
        updater._ENV_TARGET: target,
        updater._ENV_NEW: staged,
        updater._ENV_STAGING: staging_root,
        updater._ENV_BOOTOK: boot_ok,
        updater._ENV_STAMP: "20261011-120000",
        updater._ENV_WAITMAX: 10,
        updater._ENV_BOOTMAX: boot_max,
    })
    # 三个流都接 DEVNULL —— 与**生产**里 schedule_swap 起助手的方式一致。
    # 用管道捕获输出会把这条演练挂住：脚本里 ``start`` 出来的那个孙进程会继承
    # 管道句柄，``subprocess.run`` 于是等到孙子退出才肯返回（实测挂了两分钟）。
    # 它也顺带证明了一件事：这个脚本**不能**用 ``timeout /t`` 当 sleep（见下）。
    proc = subprocess.run(
        ["cmd", "/c", str(script)], env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    log = ""
    # 日志名由脚本自己定（`%~dp0openminis-swap.log`，和脚本同目录）。
    log_file = script.with_name("openminis-swap.log")
    if log_file.is_file():
        # 日志是 cmd 按**控制台码页**写的（脚本里的 %DATE% 也是）。用 utf-8 读会
        # 把 GBK 的双字节序列当成合法的 UTF-8 前缀，**吃掉后面那些 ASCII 字符**
        # ——于是"日志里有没有某句英文"这种断言会莫名其妙地假红（实测踩到）。
        try:
            log = log_file.read_bytes().decode(updater.script_encoding(), errors="replace")
        except (LookupError, UnicodeDecodeError):  # pragma: no cover
            log = log_file.read_bytes().decode("utf-8", errors="replace")
    return proc.returncode, log


def _make_case(work: Path, *, name: str, marker: bool, boot_max: int) -> dict:
    """搭一个假安装 + 假整包暂存，然后真跑助手。"""
    install = work / name / "OpenMinisDesktop"
    staging = install.with_name(f"_openminis-update-{name}")
    staged = staging / updater.PORTABLE_DIR_NAME
    staged.mkdir(parents=True)
    install.mkdir(parents=True)
    (install / "OpenMinisDesktop.exe").write_text("OLD BUILD", encoding="utf-8")
    (install / "用户的数据.txt").write_text("这不是安装目录的一部分", encoding="utf-8")
    (staged / "OpenMinisDesktop.exe").write_text("NEW BUILD", encoding="utf-8")
    (staged / "payload").mkdir()
    (staged / "payload" / "payload.json").write_text(
        json.dumps({"appVersion": "9.9.9"}), encoding="utf-8"
    )
    boot_ok = work / name / "boot-ok.json"
    if marker:
        # 真应用是在后端健康之后写这个文件（desktop/updater.mark_boot_ok）。
        body = f'echo {{"jobId": "drill", "pid": 1}}> "{boot_ok}"\n'
    else:
        body = "rem this build deliberately reports nothing\n"
    exe = "drillapp.cmd"
    updater.write_helper_script(staged / exe, _fake_app(body))
    # 兄弟文件：R01 那条越界清理会连它一起删掉，演练顺手盯着
    sibling = install.parent / "隔壁目录里的东西.txt"
    sibling.write_text("别删我", encoding="utf-8")

    code, log = _run_helper(
        work=work / name, target=install, staged=staged, staging_root=staging,
        app_root=install, exe_name=exe, boot_ok=boot_ok, boot_max=boot_max,
    )
    return {
        "name": name, "code": code, "log": log, "install": install,
        "staging": staging, "staged": staged, "boot_ok": boot_ok, "sibling": sibling,
        "old_dirs": sorted(p.name for p in install.parent.glob(f"{install.name}.old-*")),
        "body": (install / "OpenMinisDesktop.exe").read_text(encoding="utf-8")
        if (install / "OpenMinisDesktop.exe").is_file() else "",
    }


def _check_success(case: dict) -> list[str]:
    bad: list[str] = []
    if case["body"] != "NEW BUILD":
        bad.append(f"安装目录里不是新版本：{case['body']!r}")
    if case["old_dirs"]:
        bad.append(f"新版已确认启动，旧目录却没收拾：{case['old_dirs']}")
    if case["staging"].exists():
        bad.append(f"事务暂存根没清掉：{case['staging']}")
    if case["sibling"].read_text(encoding="utf-8") != "别删我":
        bad.append("动了安装目录的兄弟文件（R01 那条清理越界）")
    if "reported a boot" not in case["log"]:
        bad.append("日志里没有收到启动标记的记录 —— 它是怎么删旧版的？")
    if case["code"] != 0:
        bad.append(f"助手退出码 {case['code']}")
    return bad


def _check_no_boot(case: dict) -> list[str]:
    bad: list[str] = []
    if case["body"] != "NEW BUILD":
        bad.append("新版没就位 —— 换了目录但没换上")
    if not case["old_dirs"]:
        bad.append("新版**没有**报告启动，旧版本却被删了（R04 又回来了）")
    if "no boot report" not in case["log"]:
        bad.append("日志里没有「等不到启动标记」的记录")
    if case["boot_ok"].exists():
        bad.append("这一条本来就不该有启动标记")
    return bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="更新助手真机演练")
    parser.add_argument("--keep", action="store_true", help="留下临时目录")
    parser.add_argument("--boot-max", type=int, default=4,
                        help="等启动标记的上限（秒，默认 4 —— 演练别等两分钟）")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("这个演练只能在 Windows 上跑（助手是 .cmd）")
        return 2

    work = Path(tempfile.mkdtemp(prefix="openminis-drill-"))
    failures: list[str] = []
    cases: list[tuple[str, dict, list[str]]] = []
    try:
        started = time.time()
        ok_case = _make_case(work, name="success", marker=True,
                             boot_max=args.boot_max)
        cases.append(("新版正常报告启动", ok_case, _check_success(ok_case)))
        silent = _make_case(work, name="silent", marker=False, boot_max=args.boot_max)
        cases.append(("新版起不来（不写标记）", silent, _check_no_boot(silent)))

        for title, case, bad in cases:
            mark = "OK  " if not bad else "FAIL"
            print(f"[{mark}] {title}（助手退出码 {case['code']}，"
                  f"耗时 {time.time() - started:.1f}s 合计）")
            if case["old_dirs"]:
                print(f"       保留的旧目录：{case['old_dirs']}")
            for line in bad:
                print(f"       - {line}")
                failures.append(f"{title}: {line}")
        print(f"\n工作目录：{work}")
    finally:
        if not args.keep and not failures:
            shutil.rmtree(work, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} 项没通过 —— 临时目录留在 {work}")
        return 1
    print("两条分支都符合预期。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
