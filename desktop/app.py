"""Entry point for the OpenMinis desktop application.

    python desktop/app.py              # native window
    python desktop/app.py --browser    # serve + open a browser tab
    python desktop/app.py --port 9000  # pin the backend port

Sequence: resolve the desktop assets, start the kernel on a background thread,
then hand the main thread to the GUI toolkit. If the toolkit is unavailable we
degrade to a browser tab rather than exiting with a traceback — the backend is
the valuable part and it works either way.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import webbrowser
from pathlib import Path

# Allow running straight from a checkout: ``python desktop/app.py``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from desktop import paths  # noqa: E402
from desktop.server_runner import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_PORT,
    DesktopServer,
    port_in_use,
    start_server,
)

logger = logging.getLogger("openminis.desktop")

DESKTOP_PATH = "/_desktop/"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="openminis-desktop",
        description="OpenMinis Desktop — native window around the OpenMinis agent kernel.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"backend port (default {DEFAULT_PORT}; a busy port falls back to a free one)",
    )
    parser.add_argument("--browser", action="store_true", help="open a browser tab instead of a native window")
    parser.add_argument("--no-window", action="store_true", help="start the backend only (headless / server mode)")
    parser.add_argument("--width", type=int, default=1440, help="initial window width")
    parser.add_argument("--height", type=int, default=900, help="initial window height")
    parser.add_argument("--debug", action="store_true", help="enable WebView devtools and verbose logs")
    parser.add_argument(
        "--upstream-ui",
        action="store_true",
        help="serve the upstream mobile UI at / instead of the desktop UI",
    )
    return parser.parse_args(argv)


def _configure_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )


def _reuse_running(host: str, port: int) -> str | None:
    """If an OpenMinis backend already listens on ``host:port``, reuse it.

    Launching the app twice is a normal thing to do (double-clicked icon, a
    pinned taskbar entry plus a shortcut). Spinning up a second kernel on a
    random port would give the user two windows with two divergent session
    databases, so we point the new window at the existing server instead.
    """
    if not port_in_use(host, port):
        return None
    import urllib.request  # noqa: PLC0415

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=1.0) as resp:
            if resp.status != 200:
                return None
    except Exception:
        return None
    return f"http://{host}:{port}"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.debug)

    desktop_dir = paths.desktop_web_dir()
    if desktop_dir is None:
        logger.warning("web/desktop not found — the desktop UI will not be available")
    ui_active = desktop_dir is not None and not args.upstream_ui

    # Single instance: never start a second kernel on the same port.
    existing = _reuse_running(args.host, args.port)
    if existing is not None:
        logger.info("reusing the running backend at %s", existing)
        url = f"{existing}{DESKTOP_PATH if ui_active else ''}"
        return _present(url, args, server=None)

    try:
        server: DesktopServer | None = start_server(
            host=args.host,
            port=args.port,
            desktop_dir=desktop_dir,
            ui_active=ui_active,
            log_level="debug" if args.debug else "info",
        )
    except Exception as exc:
        logger.error("could not start the OpenMinis backend: %s", exc)
        return 1

    url = f"{server.url}{DESKTOP_PATH if ui_active else ''}"
    logger.info("OpenMinis Desktop ready — %s", url)
    return _present(url, args, server=server)


def _present(url: str, args: argparse.Namespace, *, server: DesktopServer | None) -> int:
    """Show the UI (window / browser / nothing) and clean up afterwards."""
    if args.no_window:
        logger.info("headless mode: backend running at %s (Ctrl+C to stop)", url)
        try:
            import time  # noqa: PLC0415

            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            if server is not None:
                server.shutdown()
        return 0

    if args.browser:
        webbrowser.open(url)
        try:
            import time  # noqa: PLC0415

            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            if server is not None:
                server.shutdown()
        return 0

    from desktop.window import pywebview_available, run_window  # noqa: PLC0415

    if not pywebview_available():
        logger.warning(
            "pywebview is not installed — falling back to a browser tab.\n"
            "  install it with:  pip install \"pywebview>=5.0\""
        )
        webbrowser.open(url)
        try:
            import time  # noqa: PLC0415

            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            if server is not None:
                server.shutdown()
        return 0

    # Keep the WebView's own profile out of the ephemeral default so panel
    # widths and the last-opened session survive a restart.
    storage = paths.data_root() / "webview"
    try:
        run_window(
            url,
            width=args.width,
            height=args.height,
            storage_path=storage,
            app_root=paths.app_root(),
            debug=args.debug,
        )
    finally:
        if server is not None:
            logger.info("window closed — stopping backend")
            server.shutdown()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("OPENMINIS_DESKTOP", "1")
    raise SystemExit(main())
