"""Entry point for the OpenMinis desktop application.

    python desktop/app.py              # native window
    python desktop/app.py --browser    # serve + open a browser tab
    python desktop/app.py --port 9000  # pin the backend port

Sequence (window mode): create the window **first** with a self-contained splash
page, boot the kernel behind it on a background thread, then ``load_url`` to the
real UI — see ``desktop/launcher.py`` for why.  ``OPENMINIS_NO_SPLASH=1`` restores
the older serial order (start the server, then open the window).

``--no-window`` and ``--browser`` keep that serial order and change nothing else:
CI's startup probe runs ``--no-window``. If pywebview is unavailable we degrade to
a browser tab rather than exiting with a traceback — the backend is the valuable
part and it works either way.
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

from desktop import __version__, paths  # noqa: E402
from desktop import startup_trace as trace  # noqa: E402
from desktop.launcher import (  # noqa: E402
    BootPlan,
    SplashUnsupported,
    run_serial_window,
    run_window_first,
    splash_enabled,
)
from desktop.server_runner import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_PORT,
    DesktopServer,
    port_in_use,
    start_server,
)

logger = logging.getLogger("openminis.desktop")


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
    # 时间戳不能省：用户把 desktop.log 发过来时，"第几行"没有意义，"几秒"才是证据。
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
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
    # A frozen windowed build starts with sys.stdout/stderr set to None; the
    # kernel logs during startup, so this has to happen first.
    from desktop.stdio import ensure_console_streams  # noqa: PLC0415

    # Also before anything can spawn a child: a GUI-subsystem build has no
    # console, and Windows would give every shell the agent runs its own
    # console window (one black box per message).
    from desktop.no_console import install as _install_no_console  # noqa: PLC0415

    _no_console_status = _install_no_console()

    log_file = ensure_console_streams()

    args = _parse_args(argv)
    _configure_logging(args.debug)
    if log_file is not None:
        logger.info("no console attached — logs are in %s", log_file)
    # Worth a line in the log: when this is "skipped: process has a console"
    # the user is running a console build, and a stray child window (if any)
    # has a different cause.
    logger.info("child console windows: %s", _no_console_status)
    trace.mark("imports")

    desktop_dir = paths.desktop_web_dir()
    if desktop_dir is None:
        logger.warning("web/desktop not found — the desktop UI will not be available")
    ui_active = desktop_dir is not None and not args.upstream_ui

    # Single instance: never start a second kernel on the same port.
    existing = _reuse_running(args.host, args.port)
    if existing is not None:
        logger.info("reusing the running backend at %s", existing)

    plan = BootPlan(
        host=args.host,
        port=args.port,
        desktop_dir=desktop_dir,
        ui_active=ui_active,
        existing=existing,
        debug=args.debug,
    )

    if args.no_window or args.browser or not _window_available():
        return _present_headless(plan, args)

    storage = paths.data_root() / "webview"
    app_root = paths.app_root()

    # Window first: the user gets a window in the time it takes to unpack the
    # exe and start WebView2, instead of waiting for the whole kernel as well.
    if splash_enabled():
        try:
            logger.info("showing the window first — the kernel boots behind it")
            return run_window_first(
                plan=plan, args=args, storage_path=storage, app_root=app_root
            )
        except SplashUnsupported as exc:
            logger.warning(
                "window-first startup unavailable (%s) — using the serial order", exc
            )

    return run_serial_window(plan=plan, args=args, storage_path=storage, app_root=app_root)


def _window_available() -> bool:
    from desktop.window import pywebview_available  # noqa: PLC0415

    return pywebview_available()


def _park(server: DesktopServer | None) -> int:
    """Idle until Ctrl+C, then stop the backend. Used by --no-window/--browser."""
    import time  # noqa: PLC0415

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0
    finally:
        if server is not None:
            server.shutdown()


def _present_headless(plan: BootPlan, args: argparse.Namespace) -> int:
    """无窗口路径：只跑后端，或者再开一个浏览器标签页。

    行为与原 ``_present`` 的这两个分支逐字一致 —— CI 的启动探活走的就是
    ``--no-window``，它同时也是量启动耗时的那条路径。
    """
    if plan.existing is not None:
        server: DesktopServer | None = None
        url = plan.url_for(plan.existing)
    else:
        try:
            server = start_server(
                host=plan.host,
                port=plan.port,
                desktop_dir=plan.desktop_dir,
                ui_active=plan.ui_active,
                log_level="debug" if plan.debug else "info",
            )
        except Exception as exc:
            logger.error("could not start the OpenMinis backend: %s", exc)
            return 1
        url = plan.url_for(server.url)

    logger.info("OpenMinis Desktop ready — %s", url)
    trace.report(version=__version__)

    if args.no_window:
        logger.info("headless mode: backend running at %s (Ctrl+C to stop)", url)
        return _park(server)

    if not args.browser:
        logger.warning(
            "pywebview is not installed — falling back to a browser tab.\n"
            "  install it with:  pip install \"pywebview>=5.0\""
        )
    webbrowser.open(url)
    return _park(server)


if __name__ == "__main__":
    os.environ.setdefault("OPENMINIS_DESKTOP", "1")
    raise SystemExit(main())
