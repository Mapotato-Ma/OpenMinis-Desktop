"""Filesystem layout resolution for both a source checkout and a frozen exe.

Three different roots matter and they are *not* the same once PyInstaller has
had its way with us:

``bundle_root()``
    Where read-only resources we shipped live (``web/desktop``, ``src``).
    Inside a onefile exe this is the temporary ``_MEIPASS`` extraction dir.

``app_root()``
    Where the user's copy of the app lives and where we are allowed to write
    (the ``.exe``'s own folder). Never ``_MEIPASS`` — that one is wiped on
    exit, so a PID file written there is useless by the time anyone reads it.

``data_root()``
    Per-user application data. The kernel already resolves this itself
    (``~\\openminis`` on Windows, ``XDG_DATA_HOME`` elsewhere) — we only
    re-export it so the shell can point a window title at it.
"""

from __future__ import annotations

import sys
from pathlib import Path


#: exe 旁边那个"可以被整体替换"的目录名。见 :func:`payload_root`。
PAYLOAD_DIR_NAME = "payload"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Read-only resource root (``_MEIPASS`` when frozen)."""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def app_root() -> Path:
    """Writable root: next to the exe, or the checkout in dev mode."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def payload_root() -> Path | None:
    """可覆盖载荷的根目录（exe 旁边的 ``payload/``）。

    这是"更新只换几 MB"的落脚点。内核（``openminis``）、界面（``web/desktop``）
    与壳（``desktop``）都从这里**优先**加载，而这个目录**不在 PyInstaller 的包内**
    —— 所以替换它不需要重新打包那个 37MB 的 exe（内核的 ``.pyc`` 是被打进 exe 里
    的，见 ``packaging/OpenMinisDesktop.spec`` 的 ``PYZ`` 那一行）。

    没有这个目录时一切照旧：走 bundle 里冻住的那一份。所以它天然向后兼容 ——
    老安装包不会被它弄坏。
    """
    cand = app_root() / PAYLOAD_DIR_NAME
    return cand if cand.is_dir() else None


def install_payload_path() -> Path | None:
    """把载荷目录插到 ``sys.path`` **最前面**。

    **必须在任何 ``openminis`` / ``desktop`` 的 import 之前调用**：Python 一旦
    导入过某个包就把它记在 ``sys.modules`` 里，之后再改 ``sys.path`` 也换不掉。
    引导脚本 ``desktop_main.py`` 与 ``desktop/app.py`` 都在 import 别的东西之前
    调用它 —— 这两处是唯一的入口。

    注意：引导阶段自己用到的 ``desktop.paths`` / ``desktop.stdio`` /
    ``desktop.startup_trace`` 会先被导入，因此它们**永远取自 exe**（载荷里即使有
    也不会生效）。这是刻意的：能决定"去哪里找载荷"的那几个模块，不能由载荷自己
    提供。
    """
    root = payload_root()
    if root is None:
        return None
    for path in _payload_path_entries(root):
        entry = str(path)
        if entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)
    return root


def _payload_path_entries(root: Path) -> list[Path]:
    """载荷里要进 ``sys.path`` 的目录。

    ``root`` 本身是 ``openminis`` / ``desktop`` 的父目录；``root/src`` 留着是因为
    源码树布局是 ``src/openminis`` —— 打包脚本可以直接把 ``src`` 里的东西铺进
    ``payload/``，两种摆法都能用。
    """
    entries = [root]
    src = root / "src"
    if src.is_dir():
        entries.append(src)
    return entries


def module_origin(name: str) -> str | None:
    """``name`` 这个包**实际**是从哪儿加载的。

    给 ``/api/desktop/info`` 用：一眼看出"更新到底生效没有"。没有它的话，
    载荷没被用上时表现是"改的东西没反应"，只能靠猜（这个项目已经吃过一次
    "WebView 跑旧界面"的亏）。
    """
    import importlib  # noqa: PLC0415

    try:
        return getattr(importlib.import_module(name), "__file__", None)
    except Exception:  # pragma: no cover - 诊断信息不该反过来把启动弄挂
        return None


def _first_existing(*candidates: Path) -> Path | None:
    for cand in candidates:
        if cand.is_dir():
            return cand
    return None


def desktop_web_dir() -> Path | None:
    """Locate the desktop UI assets (``web/desktop``).

    Order matters:

    1. ``payload/web/desktop`` —— 更新下来的那一份（见 :func:`payload_root`）；
    2. ``<exe 旁边>/web/desktop`` —— 手动丢一个重编好的界面也能生效；
    3. bundle 里冻住的那一份 —— 兜底。
    """
    root = payload_root()
    return _first_existing(
        *([root / "web" / "desktop"] if root else []),
        app_root() / "web" / "desktop",
        bundle_root() / "web" / "desktop",
        Path(__file__).resolve().parent.parent / "web" / "desktop",
    )


def mobile_web_dist() -> Path | None:
    """Locate the upstream mobile web build (``web/dist``), if present."""
    return _first_existing(
        app_root() / "web" / "dist",
        bundle_root() / "web" / "dist",
        Path(__file__).resolve().parent.parent / "web" / "dist",
    )


def data_root() -> Path:
    """Per-user app data dir — mirrors the kernel's own resolution."""
    from openminis.core.context import app_context  # noqa: PLC0415

    try:
        return Path(app_context().data_dir)
    except Exception:  # pragma: no cover - never let a path lookup kill startup
        import os

        # 退路要和内核 ``_default_data_dir()`` 逐字一致，否则一旦上面那条路失败，
        # 日志就会写到另一个目录 —— 用户按文档找过去什么都没有。
        if sys.platform == "win32":
            return Path.home() / "openminis"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "openminis"
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return base / "openminis"
