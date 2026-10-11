"""把配置文件的权限收紧到"只有本用户"（R17）。

产品决定（2026-10-11）：**API Key 仍然明文存在 ``settings.json``**，但这件事要
① 在文档里明说、② 把这份文件的权限收到只有当前用户可读 —— 不再让它跟着目录继承
（多用户机器/共享目录下，"同一个用户目录"并不等于"只有我能读"）。

为什么不做 DPAPI/Credential Manager：那要动内核的存储层（``settings/store.py`` 是
上游代码）并引入迁移与降级路径，而本轮的目标是"承诺与实现一致"。收紧 ACL + 明说
明文，是**这一步能兑现的承诺**；加密存储作为后续独立方向（见 ADR/文档）。

实现：Windows 上用 ``icacls``（系统自带，不需要管理员权限改自己的文件）：

    icacls <path> /inheritance:r /grant:r "<user>":(F)

非 Windows 上直接跳过（发布目标只有 Windows），并**如实返回"没做"** —— 不假装。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _current_user() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


def tighten(path: Path) -> str:
    """把 ``path`` 的权限收到当前用户独占。返回一句人话（也进日志）。"""
    target = Path(path)
    if sys.platform != "win32":
        return "skipped: 非 Windows（发布目标只有 Windows）"
    if not target.exists():
        return "skipped: 路径不存在"
    user = _current_user()
    if not user:
        return "skipped: 拿不到当前用户名"
    argv = [
        "icacls", str(target),
        # 先掐掉继承（父目录上的 Everyone/Users 权限不再漏进来）
        "/inheritance:r",
        # 再授权：只有当前用户，完全控制
        "/grant:r", f"{user}:(F)",
    ]
    try:
        proc = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("icacls failed", exc_info=True)
        return f"failed: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return f"failed: icacls 退出码 {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
    return f"ok: {target} 现在只有 {user} 可访问"


def tighten_data_root(*, data_root: Path | None = None) -> list[str]:
    """把数据目录、配置文件和它的备份一起收紧（尽力而为，逐项如实回报）。"""
    notes: list[str] = []
    if data_root is None:
        from .paths import data_root as _root  # noqa: PLC0415

        try:
            data_root = _root()
        except Exception:  # pragma: no cover - 目录都解析不出来就别提了
            return ["skipped: 数据目录解析失败"]
    root = Path(data_root)
    files = root / "files"          # 内核的 filesDir（Kotlin 对齐）
    targets = [
        root,
        root / "settings.json",
        root / "settings.json.tmp",
        # 「环境变量」页（技能密钥 / API token 都往这儿填）存在内核的 prefs 里：
        # `files/minis_prefs.json`。它和 settings.json 一样是**明文**的凭据文件 ——
        # 漏了它，收紧权限这件事就只做了一半。
        files,
        files / "minis_prefs.json",
        files / "minis_prefs.json.tmp",
    ]
    for target in targets:
        if target.exists():
            notes.append(tighten(target))
    return notes
