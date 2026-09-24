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
    (``LOCALAPPDATA`` on Windows, ``XDG_DATA_HOME`` elsewhere) — we only
    re-export it so the shell can point a window title at it.
"""

from __future__ import annotations

import sys
from pathlib import Path


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


def _first_existing(*candidates: Path) -> Path | None:
    for cand in candidates:
        if cand.is_dir():
            return cand
    return None


def desktop_web_dir() -> Path | None:
    """Locate the desktop UI assets (``web/desktop``).

    Order matters: a user who drops a rebuilt ``web/desktop`` next to the exe
    gets it picked up without us re-bundling anything.
    """
    return _first_existing(
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

        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        else:
            base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return base / "openminis"
