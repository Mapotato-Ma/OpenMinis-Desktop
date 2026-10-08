"""一键还原出厂：把**运行期产生的数据**全部清掉，回到刚装好的状态。

## 与「清空所有对话数据」的区别

* ``清空所有对话数据``（``chat_store.clear_all_chat_data``）：只动会话/消息表，
  用来排除"历史数据把模型带偏"的怀疑；
* ``还原出厂``（本模块）：**除了用户配置，什么运行痕迹都不留** —— 记忆、知识库、
  定时任务、已安装技能、沙箱拦截记录与白名单、配置变更审计、缓存、工作区分组……

## 保留清单（都是「用户配置」而不是「跑出来的数据」）

``settings.json``（模型服务/密钥/偏好）、``memory/SOUL.md``（人设）、
``skills.yaml``（技能库根配置）、``plugins/``（通道插件，里面有凭据）、
``auth_gates.json``（控制台口令）、``logs/``（排障要用，且正被日志器持有）。

## 两个工程上的坑

1. **Windows 上被占用的文件删不掉**（SQLite 开着、日志器持有句柄）。所以
   * 会话/消息走 SQL 清空，**不删库文件**；
   * 其余删不掉的路径记进 ``pending-reset.json``，**下次开机再删一遍**
     （``run_pending_reset``，由 ``lifespan`` 调用）。绝不因为删不掉就谎报成功。
2. **技能库"删干净再装一遍"**：内置技能放在包里的 ``skills/builtin``，
   启动时会自动补装（``SkillStore.ensure_installed``）。所以"清掉已安装技能 =
   清空技能根目录 + 重新 ensure_installed"，内置的回来、用户装的不回来。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

from ..core.context import app_context
from ..core.logging import get_logger

logger = get_logger(__name__)

#: 删不掉的路径记在这里，下次启动重试。
PENDING_FILE = "pending-reset.json"

#: 数据目录里**不动**的东西（见模块 docstring 的保留清单）。
KEEP_TOP_LEVEL = (
    "settings.json",
    "SOUL.md",
    "skills.yaml",
    "plugins",
    "auth_gates.json",
    "logs",
    PENDING_FILE,
)


def _bytes_of(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


def _remove(path: Path, pending: list[str]) -> bool:
    """删文件/目录。删不掉（被占用）就记进 ``pending``，返回 False。"""
    if not path.exists():
        return True
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=False)
        else:
            path.unlink()
        return True
    except OSError as exc:
        logger.warning("factory-reset: 删不掉 %s（%s），留到下次启动", path, exc)
        pending.append(str(path))
        return False


def _wipe_dir(path: Path, *, keep: Iterable[str] = (), pending: list[str]) -> dict[str, int]:
    """清空目录**内容**（保留目录本身），返回 {files, dirs, bytes}。"""
    stat = {"files": 0, "dirs": 0, "bytes": 0}
    if not path.is_dir():
        return stat
    keep_set = set(keep)
    for item in sorted(path.iterdir()):
        if item.name in keep_set:
            continue
        stat["bytes"] += _bytes_of(item)
        if item.is_dir():
            stat["dirs"] += 1
        else:
            stat["files"] += 1
        _remove(item, pending)
    return stat


def _schedule_pending(paths: list[str]) -> None:
    if not paths:
        return
    target = app_context().data_dir / PENDING_FILE
    existing: list[str] = []
    try:
        if target.is_file():
            existing = [str(p) for p in json.loads(target.read_text(encoding="utf-8"))]
    except Exception:  # pragma: no cover - 坏文件就当空的
        existing = []
    merged = sorted({*existing, *paths})
    target.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


def run_pending_reset() -> list[str]:
    """上次没删掉的东西，这次开机再删一遍。返回这次真删掉的路径。"""
    target = app_context().data_dir / PENDING_FILE
    if not target.is_file():
        return []
    try:
        paths = [Path(p) for p in json.loads(target.read_text(encoding="utf-8"))]
    except Exception:  # pragma: no cover - 坏文件就丢掉
        target.unlink(missing_ok=True)
        return []
    still: list[str] = []
    removed: list[str] = []
    for path in paths:
        if path.exists() and not _remove(path, still):
            continue
        removed.append(str(path))
    if still:
        target.write_text(json.dumps(still, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        target.unlink(missing_ok=True)
    if removed:
        logger.info("factory-reset: 补删上次遗留的 %d 项", len(removed))
    return removed


async def factory_reset() -> dict[str, Any]:
    """清掉一切运行期数据。返回逐项报告（给界面显示"到底删了什么"）。"""
    ctx = app_context()
    data = ctx.data_dir
    pending: list[str] = []
    report: list[dict[str, Any]] = []

    def note(label: str, detail: str, ok: bool = True) -> None:
        report.append({"label": label, "detail": detail, "ok": ok})

    # ① 会话 / 消息 / 压缩标记 —— 走 SQL，不删库文件（Windows 上开着删不掉）。
    from . import chat_store

    try:
        counts = await chat_store.clear_all_chat_data()
        note("会话与消息",
             f"{counts.get('sessions', 0)} 个会话 / {counts.get('messages', 0)} 条消息")
    except Exception as exc:  # noqa: BLE001 - 一项失败不该让整件事停摆
        logger.exception("factory-reset: 清空会话失败")
        note("会话与消息", f"失败：{exc}", ok=False)

    # ② 工作区分组与文件夹绑定（会话都没了，绑定留着只会指向空壳）。
    try:
        n = await chat_store.clear_all_workspaces()
        note("工作区分组与绑定", f"{n} 个分组")
    except Exception as exc:  # noqa: BLE001
        logger.exception("factory-reset: 清空工作区失败")
        note("工作区分组与绑定", f"失败：{exc}", ok=False)

    # ③ 记忆（保留 SOUL.md —— 那是人设，属于配置）。
    st = _wipe_dir(data / "memory", keep={"SOUL.md"}, pending=pending)
    note("记忆", f"{st['files']} 个文件（人设 SOUL.md 保留）")

    # ④ 知识库。
    st = _wipe_dir(data / "knowledge", pending=pending)
    note("知识库", f"{st['files']} 个文件")

    # ⑤ 定时任务（含运行历史）。
    st = _wipe_dir(data / "scheduled", pending=pending)
    note("定时任务", f"{st['files']} 个任务文件")

    # ⑥ 已安装技能：清空技能根目录 → 重新装回内置技能。
    try:
        from ..skills.store import SkillStore, configured_skills_root

        root = configured_skills_root()
        st = _wipe_dir(root, pending=pending)
        reinstalled = SkillStore().ensure_installed()
        note("已安装技能",
             f"清掉 {st['dirs']} 个技能；内置技能已装回 {len(reinstalled)} 个"
             if reinstalled else f"清掉 {st['dirs']} 个技能（内置技能本来就在）")
    except Exception as exc:  # noqa: BLE001
        logger.exception("factory-reset: 清技能失败")
        note("已安装技能", f"失败：{exc}", ok=False)

    # ⑦ 沙箱拦截记录 / 白名单 / 危险模式开关。
    guard_files = ["guard_events.json", "guard_allowlist.json", "guard_mode.json"]
    gone = sum(1 for name in guard_files if _remove(data / name, pending))
    try:
        from ..sandbox.guard import guard

        guard.reset()
    except Exception:  # pragma: no cover - 守卫重置是尽力而为
        logger.debug("guard reset failed", exc_info=True)
    note("沙箱拦截记录与白名单", f"清掉 {gone} 份状态文件")

    # ⑧ 配置变更审计（它自己的 SQLite，走它的 API 而不是删文件）。
    try:
        from ..config.audit.config_audit_log import ConfigAuditLog

        try:
            audit = ConfigAuditLog.get_instance()
        except Exception:
            # 启动流程里由 ConfigRegistry.init() 建；这里兜一下，
            # 免得"没被初始化"被当成"清空失败"。
            audit = ConfigAuditLog.init()
        audit.clear_all()
        note("配置变更审计", "已清空")
    except Exception as exc:  # BLE001
        logger.debug("audit clear failed", exc_info=True)
        note("配置变更审计", f"未启用，跳过（{exc}）")

    # ⑨ 缓存（**保留 logs/** —— 排障要用，而且日志器正持有它）。
    st = _wipe_dir(ctx.cache_dir, keep={"logs"}, pending=pending)
    note("缓存", f"{st['files']} 个文件（日志保留）")

    # ⑩ 内置代理的"已播种"标记：下次启动会重新播种内置通用代理。
    _remove(data / "subagents.seeded", pending)

    _schedule_pending(pending)
    logger.info("factory-reset 完成：%s 项，%d 个路径留到下次启动",
                len(report), len(pending))
    return {
        "ok": True,
        "report": report,
        "pending": pending,
        "kept": ["模型服务与密钥", "设置与偏好", "人设 SOUL.md", "通道插件", "日志"],
        "at": int(time.time() * 1000),
    }
