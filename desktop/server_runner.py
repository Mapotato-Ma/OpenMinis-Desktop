"""Run the OpenMinis FastAPI backend on a background thread.

The desktop app needs the kernel's HTTP + WebSocket surface (that is where
every tool, session and provider lives) but it must not own the main thread —
the GUI toolkit does. So uvicorn runs in a daemon thread and the shell talks
to it over ``127.0.0.1``.

Everything here is deliberately boring: pick a port, start, wait until
``/api/health`` answers, hand back a handle that can stop it again.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_HEALTH_PATH = "/api/health"


def find_free_port(host: str = DEFAULT_HOST, preferred: int = DEFAULT_PORT) -> int:
    """Return ``preferred`` if it is free, else an arbitrary free port.

    Bind-then-close has an inherent race, but the window between closing the
    probe socket and uvicorn binding is microseconds on loopback and the
    fallback path re-probes anyway. Good enough, and it keeps the common case
    at a stable, memorable port.
    """

    def _free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
                return True
            except OSError:
                return False

    if _free(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def port_in_use(host: str, port: int) -> bool:
    """True when something is already listening — used for single-instance."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.35)
        return probe.connect_ex((host, port)) == 0


def wait_for_health(host: str, port: int, timeout: float = 40.0) -> bool:
    """Poll ``/api/health`` until the server answers or ``timeout`` elapses."""
    url = f"http://{host}:{port}{_HEALTH_PATH}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.15)
    return False


@dataclass
class DesktopServer:
    """Handle to the background uvicorn server."""

    host: str
    port: int
    _server: object = field(repr=False)
    _thread: threading.Thread = field(repr=False)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def shutdown(self, timeout: float = 6.0) -> None:
        """Ask uvicorn to stop and wait briefly for the thread to unwind."""
        try:
            self._server.should_exit = True  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            logger.debug("server.should_exit unavailable", exc_info=True)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():  # pragma: no cover - daemon thread, so fine
            logger.warning("uvicorn thread did not exit within %.1fs", timeout)


def build_app(*, desktop_dir: Path | None = None, ui_active: bool = False):
    """Import the kernel app and bolt the desktop routes onto it.

    Import is inside the function on purpose: ``openminis.server.main`` does
    real work at import time (logging setup, frontend resolution, router
    wiring) and importing it lazily keeps ``--help`` fast and failure modes
    obvious.
    """
    from openminis.server.main import app  # noqa: PLC0415

    from .ui_mount import (
        attach_desktop_api,
        attach_desktop_ui,
        attach_provider_probe,
        attach_window_bootstrap,
    )

    if desktop_dir is not None and ui_active:
        attach_desktop_ui(app, desktop_dir)
    attach_window_bootstrap(app)
    attach_desktop_api(app, desktop_dir=desktop_dir, ui_active=ui_active)
    attach_provider_probe(app)
    return app


def start_server(
    *,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    desktop_dir: Path | None = None,
    ui_active: bool = False,
    log_level: str = "info",
) -> DesktopServer:
    """Start uvicorn in a daemon thread and block until it is healthy."""
    import uvicorn  # noqa: PLC0415

    chosen = find_free_port(host) if port in (None, 0) else port
    app = build_app(desktop_dir=desktop_dir, ui_active=ui_active)

    config = uvicorn.Config(
        app,
        host=host,
        port=chosen,
        log_level=log_level,
        # The WebView and the page share one origin on loopback; the kernel
        # already gates access with its own console password, so no proxy
        # headers or forwarded-allow-ips juggling is needed here.
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="openminis-uvicorn", daemon=True)
    thread.start()

    if not wait_for_health(host, chosen):
        server.should_exit = True
        raise RuntimeError(
            f"OpenMinis backend did not become healthy on http://{host}:{chosen} within 40s"
        )
    logger.info("backend ready on http://%s:%s", host, chosen)
    return DesktopServer(host=host, port=chosen, _server=server, _thread=thread)
