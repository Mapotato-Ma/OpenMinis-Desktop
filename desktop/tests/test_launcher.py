"""「窗口先行」启动顺序的契约测试。

本地没有 GUI，也没有 pywebview，所以这里注入一个假的 ``webview`` 模块 —— 能验的
是**顺序**（窗口先出现、内核在背后起）、**状态机**（关窗要取消、失败要写进窗口）、
以及**兜底**（建不出 splash 窗口时要能被调用方退回老顺序）。真正的 WebView2 行为
只能靠真机，这一点在 ``docs/`` 里写明了。
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop import __version__, launcher, paths
from desktop import startup_trace as trace
from desktop.server_runner import BootCancelled


# ---------------------------------------------------------------------------
# 假的 pywebview
# ---------------------------------------------------------------------------
class FakeEvent:
    def __init__(self) -> None:
        self._handlers: list = []

    def __iadd__(self, handler):  # pywebview 用 `window.events.loaded += f`
        self._handlers.append(handler)
        return self

    def fire(self, *args, **kwargs) -> None:
        for handler in list(self._handlers):
            handler(*args, **kwargs)


class FakeWindow:
    def __init__(self, title, url=None, html=None, **kwargs) -> None:
        self.title = title
        self.url = url
        self.html = html
        self.kwargs = kwargs
        self.loaded_urls: list[str] = []
        self.js: list[str] = []
        self.events = SimpleNamespace(
            loaded=FakeEvent(), shown=FakeEvent(), closing=FakeEvent(), closed=FakeEvent()
        )

    def load_url(self, url: str) -> None:
        self.loaded_urls.append(url)

    def evaluate_js(self, js: str) -> None:  # pragma: no cover - 被上面的子类覆盖
        self.js.append(js)


class FakeWebview(types.ModuleType):
    FOLDER_DIALOG = "FOLDER_DIALOG"

    def __init__(self) -> None:
        super().__init__("webview")
        self.windows: list[FakeWindow] = []
        self.start_kwargs: dict | None = None
        self.order: list[str] = []
        self.reject_html = False
        self.on_start = None

    def create_window(self, title, url=None, **kwargs):
        if self.reject_html and kwargs.get("html") is not None:
            raise TypeError("create_window() got an unexpected keyword argument 'html'")
        self.order.append("window")
        window = FakeWindow(title, url, **kwargs)
        self.windows.append(window)
        return window

    def start(self, debug=False, **kwargs) -> None:
        self.start_kwargs = {"debug": debug, **kwargs}
        window = self.windows[-1]
        window.events.loaded.fire()   # splash 的 DOM 就绪
        window.events.shown.fire()
        if self.on_start is not None:
            self.on_start(window)


class FakeServer:
    def __init__(self, url: str = "http://127.0.0.1:8765", token: str = "test-token") -> None:
        self.url = url
        # 窗口地址要带上它换 cookie（见 desktop/access_gate.py）：闸门默认开着，
        # 不带令牌的话界面里每个 /api/* 都会被拒。
        self.access_token = token
        self.shutdown_calls = 0

    def shutdown(self, timeout: float = 6.0) -> None:  # noqa: ARG002
        self.shutdown_calls += 1


@pytest.fixture(autouse=True)
def clean_trace():
    trace.reset_for_tests()
    yield
    trace.reset_for_tests()


@pytest.fixture
def fake_webview(monkeypatch):
    module = FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", module)
    return module


def make_plan(tmp_path: Path, **overrides) -> launcher.BootPlan:
    plan = {
        "host": "127.0.0.1",
        "port": 8765,
        "desktop_dir": tmp_path,
        "ui_active": True,
        "existing": None,
        "debug": False,
    }
    plan.update(overrides)
    return launcher.BootPlan(**plan)


def make_args() -> SimpleNamespace:
    return SimpleNamespace(width=1200, height=800, debug=False)


def _wait_for_outcome(window: FakeWindow, timeout: float = 5.0) -> None:
    """模拟"窗口一直开着"：等 boot 出结果（跳转或报错）再让 GUI 循环返回。

    真实 WebView2 里 ``webview.start()`` 会一直阻塞到用户关窗；假模块要是立刻返回，
    主线程会马上打上 closing 标记，boot 线程就"合理地"不干活了 —— 那样测的就不是
    启动顺序，而是取消逻辑。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if window.loaded_urls or any("__bootFail" in js for js in window.js):
            return
        time.sleep(0.01)


def run(plan, tmp_path, fake_webview):
    fake_webview.on_start = _wait_for_outcome
    return launcher.run_window_first(
        plan=plan,
        args=make_args(),
        storage_path=tmp_path / "webview",
        app_root=tmp_path,
    )


# ---------------------------------------------------------------------------
# 顺序
# ---------------------------------------------------------------------------
def test_window_comes_first_and_the_kernel_boots_behind_it(monkeypatch, tmp_path, fake_webview):
    server = FakeServer()

    def fake_start_server(**_kwargs):
        fake_webview.order.append("server")
        return server

    monkeypatch.setattr(launcher, "start_server", fake_start_server)

    assert run(make_plan(tmp_path), tmp_path, fake_webview) == 0

    window = fake_webview.windows[0]
    assert window.url is None, "窗口不该一开始就指向后端（那时后端还不存在）"
    assert "__bootStatus" in (window.html or ""), "窗口应当先渲染 splash"
    # 地址必须带上访问令牌 —— 少了它界面整片 403（闸门默认开着）。
    assert window.loaded_urls == ["http://127.0.0.1:8765/_desktop/?k=test-token"]
    assert fake_webview.order == ["window", "server"], "窗口必须先于内核出现"
    assert server.shutdown_calls == 1, "关窗后必须收掉后端"
    # 启动文案要真的推给界面，否则用户看到的是一个不动的 splash
    assert any("__bootStatus" in js for js in window.js)


def test_splash_is_not_used_when_the_backend_already_runs(monkeypatch, tmp_path, fake_webview):
    def explode(**_kwargs):  # pragma: no cover - 走到这里就是错
        raise AssertionError("复用已有后端时不该再起一个内核")

    monkeypatch.setattr(launcher, "start_server", explode)

    plan = make_plan(tmp_path, existing="http://127.0.0.1:9999")
    assert run(plan, tmp_path, fake_webview) == 0

    window = fake_webview.windows[0]
    assert window.loaded_urls == ["http://127.0.0.1:9999/_desktop/"]


def test_serial_path_also_carries_the_token(tmp_path, fake_webview, monkeypatch):
    """splash 建不出来时的**降级路径**也必须带令牌。

    复核抓到的：这条路径漏传 token，窗口会照常开出来，但界面里每个 API 调用
    都被闸门拒 —— 表现为"应用打开了但整个是坏的"，比起不来还难查。
    """
    monkeypatch.setattr(launcher, "start_server", lambda **_k: FakeServer())
    launcher.run_serial_window(
        plan=make_plan(tmp_path),
        args=make_args(),
        storage_path=tmp_path / "webview",
        app_root=tmp_path,
    )
    # 串行路径是把地址直接交给 create_window（不是 load_url），所以看 window.url。
    assert fake_webview.windows[0].url == "http://127.0.0.1:8765/_desktop/?k=test-token"


def test_ui_flag_off_keeps_the_upstream_surface(tmp_path, fake_webview, monkeypatch):
    monkeypatch.setattr(launcher, "start_server", lambda **_k: FakeServer())
    run(make_plan(tmp_path, ui_active=False), tmp_path, fake_webview)
    assert fake_webview.windows[0].loaded_urls == ["http://127.0.0.1:8765?k=test-token"]


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------
def test_boot_failure_is_shown_in_the_window(monkeypatch, tmp_path, fake_webview):
    def boom(**_kwargs):
        raise RuntimeError("kernel exploded")

    monkeypatch.setattr(launcher, "start_server", boom)
    assert run(make_plan(tmp_path), tmp_path, fake_webview) == 0

    window = fake_webview.windows[0]
    assert window.loaded_urls == [], "失败了就不能再往界面上跳"
    failure = [js for js in window.js if "__bootFail" in js]
    assert failure, "启动失败必须写进窗口，而不是让 splash 永远转圈"
    assert "kernel exploded" in failure[0]
    assert "desktop.log" in failure[0], "错误里要带上日志路径"


def test_closing_the_window_before_boot_starts_no_server(monkeypatch, tmp_path, fake_webview):
    monkeypatch.setattr(launcher, "SPLASH_GRACE", 0.01)
    calls: list[dict] = []
    monkeypatch.setattr(launcher, "start_server", lambda **kw: calls.append(kw) or FakeServer())

    window = FakeWindow("OpenMinis", html="x")
    state = launcher.BootState()
    state.mark_closing()          # 用户已经把窗口关了
    launcher._boot_once(state, make_plan(tmp_path), window)

    assert calls == [], "窗口都没了就不该再起内核"
    assert window.loaded_urls == []
    assert state.done.is_set()


def test_cancelled_boot_is_silent(monkeypatch, tmp_path, fake_webview):
    monkeypatch.setattr(launcher, "SPLASH_GRACE", 0.01)

    def cancelled(**_kwargs):
        raise BootCancelled("cancelled")

    monkeypatch.setattr(launcher, "start_server", cancelled)
    window = FakeWindow("OpenMinis", html="x")
    state = launcher.BootState()
    launcher._boot_once(state, make_plan(tmp_path), window)

    assert window.loaded_urls == []
    assert not [js for js in window.js if "__bootFail" in js], "主动取消不是错误，别弹错"
    assert state.done.is_set()


def test_window_events_drive_the_state(tmp_path, fake_webview, monkeypatch):
    """splash 的 loaded 事件置 ready；closing 置取消标记。"""
    scripted: dict = {}
    state_ref: dict = {}

    def on_start(window):
        scripted["ready"] = state_ref["state"].ready.is_set()
        window.events.closing.fire()
        scripted["closing"] = state_ref["state"].is_closing()

    monkeypatch.setattr(launcher, "start_server", lambda **_k: FakeServer())
    original = launcher.BootState

    class SpyState(original):  # type: ignore[misc,valid-type]
        def __init__(self):
            super().__init__()
            state_ref["state"] = self

    monkeypatch.setattr(launcher, "BootState", SpyState)
    fake_webview.on_start = on_start   # 不走 run()：这里要的正是"启动中就关窗"

    code = launcher.run_window_first(
        plan=make_plan(tmp_path),
        args=make_args(),
        storage_path=tmp_path / "webview",
        app_root=tmp_path,
    )
    assert code == 0
    assert scripted["ready"] is True
    assert scripted["closing"] is True


def test_evaluate_js_failure_does_not_break_startup(monkeypatch, tmp_path, fake_webview):
    """窗口已经关了（evaluate_js 抛异常）时，boot 仍要干净地结束。"""
    server = FakeServer()
    monkeypatch.setattr(launcher, "start_server", lambda **_k: server)

    def angry(self, js):  # noqa: ARG001
        raise RuntimeError("no webview")

    monkeypatch.setattr(FakeWindow, "evaluate_js", angry)
    assert run(make_plan(tmp_path), tmp_path, fake_webview) == 0
    assert fake_webview.windows[0].loaded_urls, "推文案失败不该影响跳转"


# ---------------------------------------------------------------------------
# 兜底
# ---------------------------------------------------------------------------
def test_unsupported_splash_window_is_reported_to_the_caller(tmp_path, fake_webview):
    fake_webview.reject_html = True
    with pytest.raises(launcher.SplashUnsupported):
        run(make_plan(tmp_path), tmp_path, fake_webview)


def test_splash_can_be_switched_off(monkeypatch):
    monkeypatch.delenv(launcher.NO_SPLASH_ENV, raising=False)
    assert launcher.splash_enabled() is True
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv(launcher.NO_SPLASH_ENV, value)
        assert launcher.splash_enabled() is False
    monkeypatch.setenv(launcher.NO_SPLASH_ENV, "0")
    assert launcher.splash_enabled() is True


def test_splash_page_exposes_the_hooks_the_shell_pushes():
    """页面里的函数名和壳层推的 JS 必须是一套 —— 改一边忘了另一边就是白屏。"""
    html = launcher.splash_html()
    assert "__bootStatus" in html and "__bootFail" in html

    source = Path(launcher.__file__).read_text(encoding="utf-8")
    for name in ("__bootStatus", "__bootFail"):
        assert f"window.{name} && window.{name}(" in source, f"壳层没有按约定的名字调 {name}"


def test_splash_falls_back_when_the_ui_dir_is_missing(monkeypatch):
    monkeypatch.setattr(paths, "desktop_web_dir", lambda: None)
    html = launcher.splash_html()
    assert "__bootStatus" in html and "__bootFail" in html


# ---------------------------------------------------------------------------
# 无窗口路径
# ---------------------------------------------------------------------------
def test_headless_path_runs_and_reports_the_timeline(monkeypatch, tmp_path):
    """`--no-window` 就是 CI 探活走的那条路，它必须真的跑得过。

    v0.3.1 的第一版在这里引用了没 import 的 ``__version__``：本地测试全绿、
    冒烟测试也全绿（它直接调 ``start_server``，不经过 ``main``），而冻结版一进
    ``--no-window`` 就 NameError —— 因为它是"进程起来、健康、然后马上死"，
    外面看到的是端口开着但请求没人应。这条测试就是那次的回归护栏。
    """
    from desktop import app as desktop_app

    monkeypatch.setattr(desktop_app, "_park", lambda server: 0)  # noqa: ARG005
    trace.reset_for_tests()

    plan = make_plan(tmp_path, existing="http://127.0.0.1:9999")
    args = SimpleNamespace(no_window=True, browser=False, debug=False)

    assert desktop_app._present_headless(plan, args) == 0

    log = paths.data_root() / "logs" / "startup.log"
    assert log.is_file()
    assert f"version={__version__}" in log.read_text(encoding="utf-8")
