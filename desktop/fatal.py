"""启动期的严重错误：既要留下证据，也要让**用户看得见**。

无控制台的窗口版崩起来的样子是「双击没反应、任务管理器里也没有」—— 同一个坑已经
咬过三次：

* v0.3.5 原生缩放钩子按错的签名包 ``EdgeChrome.__init__`` → ``TypeError`` → 窗口
  建不出来；
* v0.4.6 便携版第一次发到新电脑上，Windows 的 MOTW 让 ``import clr`` 失败
  （见 :mod:`desktop.motw`）；
* 以及任何一次「窗口还没画出来就抛异常」。

三次的共同点是：错误**只写进了日志**，而用户端没有控制台、也不知道日志在哪。

所以这里提供一个不依赖 WebView2、也不依赖 pythonnet 的兜底 —— ``user32.MessageBoxW``，
它只需要 user32.dll。凡是能在窗口出现之前抛出来的异常，都用它告诉用户发生了什么。
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)

TITLE = "OpenMinis Desktop"

MB_ICONERROR = 0x10
MB_ICONWARNING = 0x30
MB_YESNO = 0x04
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000

_IDYES = 6

#: 一个进程只弹一次 —— 顶层兜底和调用方各报一次是最常见的「弹两遍」来源。
_shown: set[str] = set()


def message_box(text: str, *, title: str = TITLE, yes_no: bool = False) -> str | None:
    """弹一个原生对话框。返回 ``"yes"`` / ``"no"``；弹不出来时返回 ``None``。

    ``MessageBoxW`` 会阻塞，所以只在「窗口路径」上调用它 —— 无头/服务场景下没人
    点得到它。
    """
    if os.name != "nt":  # pragma: no cover - 非 Windows
        return None
    try:
        import ctypes  # noqa: PLC0415

        flags = MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST
        if yes_no:
            flags |= MB_YESNO
        result = ctypes.windll.user32.MessageBoxW(None, str(text), str(title), flags)
    except Exception:  # pragma: no cover - 连 user32 都没有的极端情况
        logger.debug("message box unavailable", exc_info=True)
        return None
    if yes_no:
        return "yes" if result == _IDYES else "no"
    return "ok" if result else None


def log_path() -> Path | None:
    """无控制台版把 stderr 重定向到的那个文件（见 :mod:`desktop.stdio`）。"""
    try:
        from .stdio import log_path as _lp  # noqa: PLC0415

        return _lp()
    except Exception:  # pragma: no cover - 早期失败时连 paths 都导不进来
        return None


def format_exc(exc: BaseException | None = None, *, limit: int = 1600) -> str:
    if exc is None:
        text = traceback.format_exc()
    else:
        text = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    last_line = text.rsplit("\n", 1)[-1]
    # 错误信息本身很长（比如这一条）：只留尾部会把异常类型截掉，而那是最该看的。
    # 这种情况保留最后一行的**开头**。
    if len(last_line) > limit // 2:
        return last_line[: limit - 12].rstrip() + "\n…（已截断）"
    return "…" + text[-limit:]


def report_fatal(
    exc: BaseException | None = None,
    *,
    context: str = "",
    show_dialog: bool = True,
    hint: str = "",
) -> str:
    """把致命错误写进日志并弹给用户看。永不抛异常，返回正文（方便测试断言）。"""
    detail = format_exc(exc)
    path = log_path()
    body = "\n".join(
        part
        for part in (
            context.strip(),
            detail,
            hint.strip(),
            f"日志：{path}" if path else "",
        )
        if part
    )

    # 先留证据。stderr 在无控制台时已重定向到 desktop.log，但这里是**兜底**：
    # 重定向本身可能还没发生，所以再显式追加一次。
    try:
        logger.error("fatal: %s\n%s", context or "startup failed", detail)
    except Exception:  # pragma: no cover
        pass
    try:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(f"\n[fatal] {context}\n{detail}\n")
    except Exception:  # pragma: no cover
        pass

    if show_dialog and body not in _shown:
        _shown.add(body)
        message_box(body, title=f"{TITLE} — 启动失败")
    return body


def install() -> None:
    """兜住所有没人接的异常 —— 包括 ``main()`` 之外的导入期错误。"""

    def _hook(exc_type, exc, tb):  # type: ignore[no-untyped-def]
        # 已经有人处理过的（比如我们自己包过的）不重复弹。
        report_fatal(
            exc,
            context="启动过程中出现未处理的异常。",
            hint="把上面这段和日志文件发给开发者即可定位。",
        )

    sys.excepthook = _hook
