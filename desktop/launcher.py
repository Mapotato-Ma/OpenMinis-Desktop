"""窗口先行：先把窗口给用户，再在背后把内核 boot 起来。

原来的顺序是「起内核 → 端口通 → 才 create_window」，于是 onefile 解包、内核
import、uvicorn 的 lifespan 文件 I/O 那几秒里，用户盯着桌面什么都没有。这里把顺序
倒过来：主线程立刻建一个**自带 HTML** 的 splash 窗口（不依赖后端），boot 交给后台
线程 —— WebView2 的初始化和内核 import 就能并行，boot 好了再 ``load_url`` 到真实
界面。

三件必须守住的事：

* **boot 失败要看得见。** 错误写进窗口（``__bootFail``）并指向日志文件，而不是让
  splash 永远转圈 —— 无人值守的失败模式是最糟的。
* **用户在 boot 期间关窗。** pywebview 关掉最后一个窗口就结束 GUI 循环、主线程随即
  返回；内核线程若被硬杀，可能死在写 SQLite/JSON 的中间。所以 (a) boot 线程**不是**
  daemon，(b) 关窗立刻打标记，boot 线程看到就不再起服务、健康检查也提前放弃，
  (c) 主线程返回前 join 它并收掉已经起来的服务。
* **``OPENMINIS_NO_SPLASH=1`` 退回老顺序。** 真机出问题时第一个要试的开关，也是
  A/B 量启动耗时的对照组。

窗口本身由 ``desktop.window`` 建，这里只管顺序和状态机。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import startup_trace as trace
from .server_runner import BootCancelled, DesktopServer, start_server
from .window import run_window, start_gui, create_window
from . import __version__

logger = logging.getLogger(__name__)

DESKTOP_PATH = "/_desktop/"
NO_SPLASH_ENV = "OPENMINIS_NO_SPLASH"

#: 关窗后等 boot 线程收尾的上限。内核 import 是不可中断的，所以这只是个保险丝。
BOOT_JOIN_TIMEOUT = 30.0

#: 先给 splash 一点时间加载，否则下面那句"正在加载内核…"会打在还没准备好的
#: 页面上（文案落空），用户只看得到默认那句话。
SPLASH_GRACE = 1.5

#: 等 splash 的 loaded 事件的上限。**等不到也要往下走** —— 老版本 pywebview 可能
#: 没这个事件，而"事件没来"绝不该等于"界面永远出不来"。正常情况下这里只花几百毫秒。
READY_TIMEOUT = 5.0

_FALLBACK_SPLASH = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>OpenMinis</title></head>"
    "<body style='background:#141517;color:#d8dade;font:13px system-ui;"
    "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
    "<div id='status'>正在启动…</div>"
    "<script>window.__bootStatus=function(t){document.getElementById('status').textContent=t;};"
    "window.__bootFail=function(t){document.body.innerHTML='<pre style=\"color:#f0616d;"
    "white-space:pre-wrap\">'+t+'</pre>';};</script></body></html>"
)


class SplashUnsupported(RuntimeError):
    """这个 pywebview 建不出 splash 窗口 —— 调用方应当退回串行顺序。"""


@dataclass(frozen=True)
class BootPlan:
    """boot 需要知道的一切（不含 UI 状态）。"""

    host: str
    port: int
    desktop_dir: Path | None
    ui_active: bool
    existing: str | None
    debug: bool = False

    def url_for(self, base: str) -> str:
        return f"{base}{DESKTOP_PATH if self.ui_active else ''}"


def splash_enabled() -> bool:
    """默认开。``OPENMINIS_NO_SPLASH=1`` 退回老顺序（诊断用，也是 A/B 的对照组）。"""
    return os.environ.get(NO_SPLASH_ENV, "").strip().lower() not in {"1", "true", "yes", "on"}


def splash_html() -> str:
    """读 ``web/desktop/splash.html``；读不到就用内置的最小版本。"""
    from . import paths  # noqa: PLC0415

    desktop_dir = paths.desktop_web_dir()
    if desktop_dir is not None:
        candidate = desktop_dir / "splash.html"
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError:
            logger.debug("could not read %s", candidate, exc_info=True)
    return _FALLBACK_SPLASH


class BootState:
    """GUI 线程与 boot 线程之间唯一的共享状态。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._server: DesktopServer | None = None
        self.error: BaseException | None = None
        self.closing = False
        #: splash 加载完成 —— 这时 ``evaluate_js`` / ``load_url`` 才是安全的。
        self.ready = threading.Event()
        self.done = threading.Event()

    def mark_closing(self) -> None:
        self.closing = True

    def is_closing(self) -> bool:
        return self.closing

    def attach(self, server: DesktopServer) -> None:
        with self._lock:
            self._server = server

    def take_server(self) -> DesktopServer | None:
        with self._lock:
            server, self._server = self._server, None
            return server


def _push_js(window: Any, js: str) -> None:
    try:
        window.evaluate_js(js)
    except Exception:  # pragma: no cover - 窗口可能已经关了
        logger.debug("evaluate_js failed", exc_info=True)


def _status(window: Any, state: BootState, text: str) -> None:
    if not state.ready.is_set():
        # 还没加载完就算了：文案是锦上添花，绝不能让它拖住 boot。
        return
    _push_js(window, f"window.__bootStatus && window.__bootStatus({json.dumps(text)})")


def _fail(window: Any, state: BootState, exc: BaseException) -> None:
    log_hint = ""
    try:
        from .paths import data_root  # noqa: PLC0415

        log_hint = str(data_root() / "logs" / "desktop.log")
    except Exception:  # pragma: no cover
        pass
    text = f"{type(exc).__name__}: {exc}".strip()
    if log_hint:
        text = f"{text}\n\n日志：{log_hint}"
    state.ready.wait(READY_TIMEOUT)
    _push_js(window, f"window.__bootFail && window.__bootFail({json.dumps(text)})")


def _boot_once(state: BootState, plan: BootPlan, window: Any) -> None:
    """后台线程的全部工作 —— 抽成函数是为了测试能直接调它。"""
    trace.mark("boot-start")
    try:
        state.ready.wait(SPLASH_GRACE)
        if plan.existing is not None:
            url = plan.url_for(plan.existing)
        else:
            if state.is_closing():
                logger.info("window closed before the backend started — nothing to boot")
                return
            _status(window, state, "正在加载内核…")
            server = start_server(
                host=plan.host,
                port=plan.port,
                desktop_dir=plan.desktop_dir,
                ui_active=plan.ui_active,
                log_level="debug" if plan.debug else "info",
                should_stop=state.is_closing,
            )
            state.attach(server)
            if state.is_closing():
                return
            url = plan.url_for(server.url)

        _status(window, state, "正在载入界面…")
        if not state.ready.wait(READY_TIMEOUT):
            logger.warning("splash never reported loaded — loading the UI anyway")
        trace.mark("ui-load")
        window.load_url(url)
    except BootCancelled:
        logger.info("startup cancelled — the window was closed before the backend was ready")
    except Exception as exc:  # noqa: BLE001 - 任何 boot 失败都要给用户看
        state.error = exc
        logger.exception("desktop startup failed")
        try:
            _fail(window, state, exc)
        except Exception:  # pragma: no cover - 报错动作本身不能炸
            logger.debug("could not report the failure to the window", exc_info=True)
    finally:
        state.done.set()


def run_window_first(
    *,
    plan: BootPlan,
    args: argparse.Namespace,
    storage_path: Path | None = None,
    app_root: Path | None = None,
) -> int:
    """开窗口 → 后台 boot → 界面就绪。返回进程退出码。

    只在「连窗口都建不出来」时抛 :class:`SplashUnsupported`，让调用方退回串行顺序；
    窗口建出来之后的失败都在窗口里报，不往上抛。
    """
    try:
        window, _api = create_window(
            html=splash_html(),
            width=getattr(args, "width", 1440),
            height=getattr(args, "height", 900),
            app_root=app_root,
        )
    except TypeError as exc:
        raise SplashUnsupported(str(exc)) from exc

    state = BootState()
    seen = {"loaded": 0}

    def _on_loaded(*_args: Any, **_kwargs: Any) -> None:
        seen["loaded"] += 1
        if seen["loaded"] == 1:
            trace.mark("splash-loaded")
            state.ready.set()
        else:
            # load_url 之后真实界面加载完 —— 这才是「用户能用了」。
            trace.mark("ui-loaded")

    window.events.loaded += _on_loaded
    window.events.shown += lambda *_a, **_k: trace.mark("window-shown")
    window.events.closing += lambda *_a, **_k: state.mark_closing()

    trace.mark("window-created")
    boot = threading.Thread(
        target=_boot_once,
        args=(state, plan, window),
        name="openminis-boot",
        # 故意不是 daemon：关窗时内核线程可能正写在盘上，硬杀比多活一会儿危险得多。
        daemon=False,
    )
    boot.start()

    exit_code = 0
    try:
        start_gui(storage_path=storage_path, debug=plan.debug)
    except Exception:
        logger.exception("the GUI loop failed")
        exit_code = 1
    finally:
        state.mark_closing()
        if boot.is_alive():
            logger.info("waiting for the boot thread to unwind")
            boot.join(BOOT_JOIN_TIMEOUT)
        server = state.take_server()
        if server is not None:
            logger.info("window closed — stopping backend")
            server.shutdown()

    trace.mark("exit")
    trace.report(version=__version__)
    return exit_code


def run_serial_window(
    *,
    plan: BootPlan,
    args: argparse.Namespace,
    storage_path: Path | None = None,
    app_root: Path | None = None,
) -> int:
    """老顺序：先起服务、健康了再开窗口。留着当 ``OPENMINIS_NO_SPLASH=1`` 的对照组。"""
    if plan.existing is not None:
        url = plan.url_for(plan.existing)
        return run_window(
            url,
            width=getattr(args, "width", 1440),
            height=getattr(args, "height", 900),
            storage_path=storage_path,
            app_root=app_root,
            debug=plan.debug,
        )

    server = start_server(
        host=plan.host,
        port=plan.port,
        desktop_dir=plan.desktop_dir,
        ui_active=plan.ui_active,
        log_level="debug" if plan.debug else "info",
    )
    try:
        trace.mark("window-open")
        return run_window(
            plan.url_for(server.url),
            width=getattr(args, "width", 1440),
            height=getattr(args, "height", 900),
            storage_path=storage_path,
            app_root=app_root,
            debug=plan.debug,
        )
    finally:
        logger.info("window closed — stopping backend")
        server.shutdown()
        trace.mark("exit")
        trace.report(version=__version__)
