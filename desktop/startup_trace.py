"""启动时间线：把「双击 → 窗口能用」拆成可对比的毫秒数。

为什么需要它：用户报的是「窗口要好几秒才出来」，但这不是能靠读代码拍死的
问题 —— onefile 解包、杀软扫描、内核 import、WebView2 初始化，各自在不同机器
上占比完全不同。靠猜优化等于没优化，所以先让它自己报数。

三个刻意的选择：

* **0 点是进程创建时刻，不是解释器启动时刻。** onefile 的 bootloader 在起解释器
  之前就把载荷解包了（办公电脑上还要过一遍杀软实时扫描），那一段恰恰最可疑，
  从 Python 里计时根本看不到它。
* **优先用父进程的创建时刻。** PyInstaller onefile 是父进程解包、再 spawn 出真正
  跑 Python 的子进程，取自己的时刻会漏掉解包。只有确认父进程就是同一个 exe 时才
  这么做（``QueryFullProcessImageNameW``），否则退回自己的时刻 —— 例如从 Explorer
  直接跑 onedir 版时，父进程是开了很久的 Explorer，用它会把时间吹大。
* **只写一行。** 追加到 ``logs/startup.log``，用户把这一行发过来就够复盘，不用在
  几 MB 的 ``desktop.log`` 里翻。

非 Windows（开发机、Linux CI）没有 ``GetProcessTimes``，退化成「相对解释器启动」，
输出里的 ``origin`` 会写成 ``python`` —— 不假装自己有同样的精度。
"""

from __future__ import annotations

import logging
import sys
import time

logger = logging.getLogger(__name__)

#: 1601-01-01 → 1970-01-01 的秒数，FILETIME 换算用。
_FILETIME_EPOCH_DELTA = 11_644_473_600.0

#: 父进程时间超过这个岁数就不用它 —— 那多半是 Explorer / 终端，不是 bootloader。
_PARENT_MAX_AGE_S = 300.0

_T0 = time.monotonic()
_MARKS: list[tuple[str, float]] = []

_ORIGIN_EPOCH: float | None = None
_ORIGIN_SOURCE: str | None = None
_ORIGIN_RESOLVED = False

_REPORTED = False


def mark(name: str) -> None:
    """记一个时间点。可以随便加，成本是往列表里 append 一次。"""
    _MARKS.append((name, time.monotonic()))


def _filetime_to_epoch(ticks: int) -> float | None:
    if ticks <= 0:
        return None
    return ticks / 1e7 - _FILETIME_EPOCH_DELTA


def _process_creation_epoch(pid: int | None) -> float | None:
    """``pid=None`` 取当前进程；拿不到就返回 ``None``（非 Windows 恒为 None）。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE

        handle = None
        close_handle = False
        if pid is None:
            handle = kernel32.GetCurrentProcess()
        else:
            # PROCESS_QUERY_LIMITED_INFORMATION (0x1000)：权限足够读时间，不需要
            # 提权。拿不到就放弃父进程这条路。
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return None
            close_handle = True

        try:
            creation, exit_t, kernel_t, user_t = (FILETIME() for _ in range(4))
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_t),
                ctypes.byref(kernel_t),
                ctypes.byref(user_t),
            )
            if not ok:
                return None
            return _filetime_to_epoch((creation.hi << 32) | creation.lo)
        finally:
            if close_handle:
                kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover - 量不出来不该影响启动
        logger.debug("GetProcessTimes failed", exc_info=True)
        return None


def _is_our_own_image(pid: int) -> bool:
    """父进程是不是同一个 exe（onefile 的 bootloader）。"""
    try:
        import ctypes
        import os
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return False
            return os.path.normcase(buf.value) == os.path.normcase(sys.executable)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover
        logger.debug("QueryFullProcessImageNameW failed", exc_info=True)
        return False


def _resolve_origin() -> tuple[float | None, str]:
    """``(epoch 秒, 来源)``；来源是 ``parent`` / ``self`` / ``python``。"""
    global _ORIGIN_EPOCH, _ORIGIN_SOURCE, _ORIGIN_RESOLVED
    if _ORIGIN_RESOLVED:
        return _ORIGIN_EPOCH, _ORIGIN_SOURCE or "python"
    _ORIGIN_RESOLVED = True

    now = time.time()
    self_epoch = _process_creation_epoch(None)
    if self_epoch is not None and 0 <= now - self_epoch < _PARENT_MAX_AGE_S:
        _ORIGIN_EPOCH, _ORIGIN_SOURCE = self_epoch, "self"

    if getattr(sys, "frozen", False):
        import os

        ppid = os.getppid()
        if ppid > 0 and _is_our_own_image(ppid):
            parent_epoch = _process_creation_epoch(ppid)
            if parent_epoch is not None and 0 <= now - parent_epoch < _PARENT_MAX_AGE_S:
                _ORIGIN_EPOCH, _ORIGIN_SOURCE = parent_epoch, "parent"

    if _ORIGIN_SOURCE is None:
        _ORIGIN_SOURCE = "python"
    return _ORIGIN_EPOCH, _ORIGIN_SOURCE


def _pre_python_ms(origin: float | None) -> float:
    """0 点 → 本模块被 import（也就是 bootloader + 解释器启动）的毫秒数。

    两个时钟必须背靠背取：``time.time()`` 是墙钟（能和进程创建时刻比），
    ``time.monotonic()`` 是我们自己打点用的。两者的差值就是「Python 看到世界
    之前已经过去多久」。
    """
    if origin is None:
        return 0.0
    wall, mono = time.time(), time.monotonic()
    return max(0.0, (wall - origin - (mono - _T0)) * 1000.0)


def elapsed_ms() -> float:
    """相对 0 点已经过去多久（拿不到 0 点就是相对解释器启动）。"""
    origin, _ = _resolve_origin()
    return _pre_python_ms(origin) + (time.monotonic() - _T0) * 1000.0


def summary() -> str:
    """一行可读可 grep 的时间线。量不出来也不许抛。"""
    try:
        origin, source = _resolve_origin()
        pre = _pre_python_ms(origin)
        parts = [f"{name}=+{pre + (when - _T0) * 1000.0:.0f}ms" for name, when in _MARKS]
        return f"[startup] origin={source} total={elapsed_ms():.0f}ms | " + " ".join(parts)
    except Exception:  # pragma: no cover - 量时间不能反过来把启动搞挂
        logger.warning("could not build the startup timeline", exc_info=True)
        return "[startup] <unavailable>"


def report(*, version: str = "") -> str:
    """把时间线写进 ``logs/startup.log`` 与日志，返回那一行。永不抛异常。"""
    global _REPORTED
    line = summary()
    if version:
        line = f"{line} version={version}"
    if _REPORTED:  # pragma: no cover - 重复上报没有意义
        return line
    _REPORTED = True

    logger.info("%s", line)

    # 两段分开写、都记日志：CI 上曾经出现过"该有一行却找不到文件"的情况，
    # 分不清是没调到还是写失败。日志里留下真实路径，下次不用猜。
    try:
        from .paths import data_root  # noqa: PLC0415 - 内核此时已 import 完

        path = data_root() / "logs" / "startup.log"
    except Exception:
        logger.warning("startup timeline: cannot resolve the data dir", exc_info=True)
        return line

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"{stamp} {line}\n")
        logger.info("startup timeline appended to %s", path)
    except Exception:
        # 写不了不该影响应用，但必须留下证据（warning 而不是 debug）。
        logger.warning("could not append the startup timeline to %s", path, exc_info=True)
    return line


def reset_for_tests() -> None:
    """测试用：清空打点。"""
    global _MARKS, _REPORTED, _ORIGIN_RESOLVED, _ORIGIN_EPOCH, _ORIGIN_SOURCE
    _MARKS = []
    _REPORTED = False
    _ORIGIN_RESOLVED = False
    _ORIGIN_EPOCH = None
    _ORIGIN_SOURCE = None
