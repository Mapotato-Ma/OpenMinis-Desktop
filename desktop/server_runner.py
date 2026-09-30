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

from . import paths
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
    #: 本次运行的访问令牌。窗口地址要用它换 cookie（见 access_gate 模块）。
    access_token: str = ""

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


def build_app(
    *,
    desktop_dir: Path | None = None,
    ui_active: bool = False,
    access_token: str | None = None,
    access_host: str = DEFAULT_HOST,
    access_port: int = 0,
):
    """Import the kernel app and bolt the desktop routes onto it.

    Import is inside the function on purpose: ``openminis.server.main`` does
    real work at import time (logging setup, frontend resolution, router
    wiring) and importing it lazily keeps ``--help`` fast and failure modes
    obvious.

    ``ui_mount.attach()`` 装的是**一个清单里的全部路由**，并且在装完后自查
    「有没有哪条排在内核兜底路由之后」—— 那会导致请求被静默吞掉。

    ``access_token`` 交给 ``attach()`` 装访问闸门；``None`` 时**不装**并在日志里
    报警（见 :mod:`desktop.access_gate`）。正式启动路径由 ``start_server()`` 兜底，
    所以这里为空只可能是测试或别处直接建 app。
    """
    from openminis.server.main import app  # noqa: PLC0415

    from .ui_mount import attach  # noqa: PLC0415

    attach(
        app,
        desktop_dir=desktop_dir,
        ui_active=ui_active,
        access_token=access_token,
        access_host=access_host,
        access_port=access_port,
    )
    return app


def start_server(
    *,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    desktop_dir: Path | None = None,
    ui_active: bool = False,
    log_level: str = "info",
    should_stop: Callable[[], bool] | None = None,
    access_token: str | None = None,
) -> DesktopServer:
    """Start uvicorn in a daemon thread and block until it is healthy.

    ``access_token`` 不给就**现场生成一个** —— 正式路径上不该存在"忘了传令牌"这种
    状态，那等于把闸门关了。
    """
    import uvicorn  # noqa: PLC0415

    from .access_gate import new_token  # noqa: PLC0415

    token = access_token or new_token()
    chosen = find_free_port(host) if port in (None, 0) else port
    app = build_app(
        desktop_dir=desktop_dir,
        ui_active=ui_active,
        access_token=token,
        access_host=host,
        access_port=chosen,
    )
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
    handle = DesktopServer(
        host=host, port=chosen, access_token=token, _server=server, _thread=thread
    )
    publish_access(handle)
    return handle


def publish_access(handle: DesktopServer) -> None:
    """把这个实例的访问地址落盘（用户数据目录）。

    两处要用：**第二个实例**（端口被占时它会复用第一个实例的服务 —— 没有令牌的话
    那个窗口的界面会整片 403）、以及 CI 的启动探针。落盘失败只记日志：写不了一个
    文件不该拦住用户开窗口。
    """
    from .access_gate import access_file_path, publish_access_url  # noqa: PLC0415

    try:
        root = paths.data_root()
    except Exception:  # pragma: no cover - 数据目录都解析不出来就别提了
        logger.debug("数据目录解析失败，跳过访问地址落盘", exc_info=True)
        return
    publish_access_url(access_file_path(root), handle.url, handle.access_token, handle.port)
