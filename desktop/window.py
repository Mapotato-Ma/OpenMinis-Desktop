"""The native window.

``pywebview`` gives us one small API that wraps WebView2 on Windows, WebKit on
macOS and GTK/Qt on Linux — which is exactly what a "desktop app" needs and
nothing more. On Windows 10/11 the WebView2 runtime ships with the OS, so the
exe stays small and needs no bundled Chromium.

Two deliberate choices:

* **Native window frame.** A frameless window with a hand-drawn title bar looks
  marginally more "IDE-like" but drag regions, snap layouts and high-DPI
  scaling all become our problem. Not worth it.
* **Persistent storage.** ``private_mode=False`` plus a ``storage_path`` under
  the user data dir keeps ``localStorage`` (panel widths, theme, last session)
  across restarts. The pywebview default is an ephemeral profile.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_WIDTH = 1440
DEFAULT_HEIGHT = 900
MIN_WIDTH = 940
MIN_HEIGHT = 600


class WindowAPI:
    """Exposed to JS as ``window.pywebview.api``.

    Keep this surface tiny. Anything that can be done over HTTP should be —
    this is only for things that genuinely require the host process (window
    chrome and shell integration).
    """

    def __init__(self, window: Any, *, app_root: Path) -> None:
        self._window = window
        self._app_root = app_root

    def minimize(self) -> None:
        try:
            self._window.minimize()
        except Exception:  # pragma: no cover
            logger.debug("minimize unsupported", exc_info=True)

    def toggle_maximize(self) -> None:
        try:
            if self._window.maximized:
                self._window.restore()
            else:
                self._window.maximize()
        except Exception:  # pragma: no cover
            logger.debug("maximize unsupported", exc_info=True)

    def close(self) -> None:
        try:
            self._window.destroy()
        except Exception:  # pragma: no cover
            logger.debug("destroy unsupported", exc_info=True)

    def open_in_file_manager(self, path: str) -> bool:
        """Reveal a path in Explorer / Finder / the desktop's file manager."""
        import subprocess  # noqa: PLC0415
        import sys  # noqa: PLC0415

        target = Path(path)
        if not target.exists():
            target = self._app_root
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", str(target)])  # noqa: S607
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])  # noqa: S607
            else:
                subprocess.Popen(["xdg-open", str(target)])  # noqa: S607
            return True
        except Exception:  # pragma: no cover
            logger.debug("open_in_file_manager failed", exc_info=True)
            return False

    def platform(self) -> str:
        import sys  # noqa: PLC0415

        return sys.platform


def pywebview_available() -> bool:
    try:
        import webview  # noqa: F401, PLC0415
    except Exception:
        return False
    return True


def run_window(
    url: str,
    *,
    title: str = "OpenMinis",
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    storage_path: Path | None = None,
    app_root: Path | None = None,
    debug: bool = False,
    on_closed: Any = None,
) -> None:
    """Open the window and block until the user closes it.

    Raises ``RuntimeError`` when pywebview is not importable so the caller can
    fall back to a browser tab instead of dying.
    """
    try:
        import webview  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - depends on optional dep
        raise RuntimeError("pywebview is not installed") from exc

    api = WindowAPI(None, app_root=app_root or Path.cwd())
    window = webview.create_window(
        title,
        url,
        width=width,
        height=height,
        min_size=(MIN_WIDTH, MIN_HEIGHT),
        js_api=api,
        text_select=True,
    )
    api._window = window

    if on_closed is not None:
        window.events.closed += on_closed

    start_kwargs: dict[str, Any] = {"debug": debug}
    if storage_path is not None:
        storage_path.mkdir(parents=True, exist_ok=True)
        start_kwargs["private_mode"] = False
        start_kwargs["storage_path"] = str(storage_path)

    try:
        webview.start(**start_kwargs)
    except TypeError:
        # Older pywebview builds reject private_mode/storage_path. Losing
        # persisted UI state is better than not starting at all.
        logger.warning("pywebview ignored storage options (older version?)")
        webview.start(debug=debug)
