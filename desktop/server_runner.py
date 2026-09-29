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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import startup_trace as trace

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_HEALTH_PATH = "/api/health"

#: 前几秒用这个间隔轮询。uvicorn 是「lifespan 跑完才 bind 端口」，所以从"端口通"
#: 到"我们能拿到 200"通常只差一个轮询周期 —— 150ms 的固定间隔平均白等 75ms，
#: 而这正是用户唯一能感知的那一小段。
_FAST_POLL_S = 0.025
_SLOW_POLL_S = 0.15
_FAST_WINDOW_S = 3.0


class BootCancelled(RuntimeError):
    """内核启动被主动取消（用户在 boot 期间把窗口关了）。"""


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


def wait_for_health(
    host: str,
    port: int,
    timeout: float = 40.0,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """Poll ``/api/health`` until the server answers or ``timeout`` elapses.

    ``should_stop`` 是给"窗口先行"用的：用户在启动期间关掉窗口时，我们不该继续
    干等一个没人要的后端。
    """
    url = f"http://{host}:{port}{_HEALTH_PATH}"
    started = time.monotonic()
    deadline = started + timeout
    while True:
        if should_stop is not None and should_stop():
            return False
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        now = time.monotonic()
        if now >= deadline:
            return False
        elapsed = now - started
        time.sleep(_FAST_POLL_S if elapsed < _FAST_WINDOW_S else _SLOW_POLL_S)


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

    ``ui_mount.attach()`` 装的是**一个清单里的全部路由**，并且在装完后自查
    「有没有哪条排在内核兜底路由之后」—— 那会导致请求被静默吞掉。
    """
    from openminis.server.main import app  # noqa: PLC0415

    from .ui_mount import attach  # noqa: PLC0415

    attach(app, desktop_dir=desktop_dir, ui_active=ui_active)
    return app


def start_server(
    *,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    desktop_dir: Path | None = None,
    ui_active: bool = False,
    log_level: str = "info",
    should_stop: Callable[[], bool] | None = None,
) -> DesktopServer:
    """Start uvicorn in a daemon thread and block until it is healthy."""
    import uvicorn  # noqa: PLC0415

    chosen = find_free_port(host) if port in (None, 0) else port
    app = build_app(desktop_dir=desktop_dir, ui_active=ui_active)
    trace.mark("kernel-import")

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

    if not wait_for_health(host, chosen, should_stop=should_stop):
        server.should_exit = True
        if should_stop is not None and should_stop():
            raise BootCancelled("startup cancelled before the backend became healthy")
        raise RuntimeError(
            f"OpenMinis backend did not become healthy on http://{host}:{chosen} within 40s"
        )
    trace.mark("backend-ready")
    logger.info("backend ready on http://%s:%s", host, chosen)
    return DesktopServer(host=host, port=chosen, _server=server, _thread=thread)
