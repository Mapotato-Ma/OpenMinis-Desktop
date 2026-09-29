"""原生缩放（WebView2 ZoomFactor）的契约测试。

这里能测的是**逻辑**：失败一律变成 reason、只有读回一致才算成功、
跨线程时走 UI 线程兜底、越界/脏参数不进控件。真正的 .NET 控件在 iSH 里
不存在 —— 那部分靠 CI（windows-latest 上跑真 exe）拿 ``GET /api/desktop/zoom``
的实测回答，别在这里假装测过。
"""
from __future__ import annotations

import sys
import types

import pytest

from desktop import native_zoom


class FakeWidget:
    """最小 WebView2 替身：ZoomFactor 可读可写。"""

    CoreWebView2 = object()

    def __init__(self, *, sticky: float | None = None, cross_thread: bool = False):
        self._value = 1.0
        self._sticky = sticky
        self._cross_thread = cross_thread
        self.on_ui_thread = False
        self.sets: list[float] = []

    def _guard(self) -> None:
        if self._cross_thread and not self.on_ui_thread:
            raise RuntimeError("Cross-thread operation not valid")

    @property
    def ZoomFactor(self) -> float:
        self._guard()
        return self._sticky if self._sticky is not None else self._value

    @ZoomFactor.setter
    def ZoomFactor(self, value: float) -> None:
        self._guard()
        self.sets.append(value)
        self._value = value


class FakeForm:
    def __init__(self, widgets: list[FakeWidget]):
        self._widgets = widgets

    def Invoke(self, action):  # noqa: ANN001, ANN201 - 模拟 WinForms 的回到 UI 线程
        for widget in self._widgets:
            widget.on_ui_thread = True
        action()


class FakeChrome:
    def __init__(self, widget: FakeWidget):
        self.webview = widget
        self.form = FakeForm([widget])


@pytest.fixture(autouse=True)
def _fake_pythonnet(monkeypatch):
    """真 pythonnet 只在 Windows 上；测试里给个 ``System.Action`` 替身，
    这样「回到 UI 线程」这条路径在本地也被真的走到。"""
    fake = types.ModuleType("System")
    fake.Action = lambda fn: fn
    monkeypatch.setitem(sys.modules, "System", fake)
    yield fake


@pytest.fixture(autouse=True)
def _clean():
    native_zoom.reset_for_tests()
    yield
    native_zoom.reset_for_tests()


def _attach(widget: FakeWidget) -> FakeChrome:
    chrome = FakeChrome(widget)
    native_zoom._state["chrome"] = chrome
    return chrome


def test_no_window_reports_why_and_never_raises():
    """拿不到控件时：说清原因，且不许抛（这模块在启动路径上）。"""
    cap = native_zoom.capability()
    assert cap["handle"] is False
    assert cap["reason"], "失败必须给人话原因，否则界面上只能显示“不可用”"
    result = native_zoom.set_zoom(1.44)
    assert result["ok"] is False
    assert result["reason"] == cap["reason"]


def test_install_hook_is_safe_without_edge_backend():
    """非 Windows / 没装 pywebview 时安装挂钩是空操作，不是错误。"""
    native_zoom.install_hook()
    native_zoom.install_hook()  # 幂等
    assert native_zoom.capability()["handle"] is False


def test_sets_and_reads_back_the_exact_factor():
    widget = FakeWidget()
    _attach(widget)
    cap = native_zoom.capability()
    assert cap["handle"] is True and cap["ready"] is True and cap["applied"] == 1.0

    result = native_zoom.set_zoom(1.44)
    assert result == {"ok": True, "applied": 1.44, "reason": ""}
    assert widget.sets == [1.44], "应当只调一次控件"


def test_lying_control_is_a_failure_not_a_shrug():
    """设进去但读回来不一致 —— 不接受“可能生效”（不然界面会同时什么都不做）。"""
    widget = FakeWidget(sticky=1.0)
    _attach(widget)
    result = native_zoom.set_zoom(1.44)
    assert result["ok"] is False
    assert "不接受" in result["reason"]


def test_cross_thread_falls_back_to_the_ui_thread():
    """WinForms 控件跨线程改属性会抛 —— 要能自己回到 UI 线程再设。"""
    widget = FakeWidget(cross_thread=True)
    _attach(widget)
    result = native_zoom.set_zoom(1.2)
    assert result["ok"] is True, result["reason"]
    assert result["applied"] == 1.2
    assert widget.on_ui_thread is True, "没有回到 UI 线程"


@pytest.mark.parametrize("bad", ["nope", None, {}, float("nan")])
def test_dirty_factor_never_reaches_the_control(bad):
    widget = FakeWidget()
    _attach(widget)
    result = native_zoom.set_zoom(bad)
    assert result["ok"] is False
    assert widget.sets == [], "脏参数不该碰控件"


@pytest.mark.parametrize("factor", [0.4, 5.5, 100])
def test_out_of_range_is_refused(factor):
    widget = FakeWidget()
    _attach(widget)
    assert native_zoom.set_zoom(factor)["ok"] is False
    assert widget.sets == []


def test_reason_distinguishes_not_installed_from_no_window():
    """「没控件」有三种来路，日志里必须能分清 —— 别只报一句没法定位的话。"""
    fresh = native_zoom.capability()
    assert "挂钩未安装" in fresh["reason"], fresh["reason"]

    native_zoom.install_hook()          # 装了挂钩，但窗口还没建
    after = native_zoom.capability()
    assert "挂钩未安装" not in after["reason"]
    assert after["reason"], "装了挂钩也得说清为什么还没接住控件"


def _fake_edge_module(monkeypatch, arity: int = 3):
    """装一个假的 ``webview.platforms.edgechromium``（`__init__` 形状可控）。"""
    calls: dict[str, tuple] = {}

    class FakeEdgeChrome:
        def __init__(self, *args):  # noqa: ANN002
            calls["args"] = args

    if arity == 3:  # 真 pywebview 6.2.1 的形状：(self, form, window, cache_dir)
        def _init(self, form, window, cache_dir):  # noqa: ANN001
            calls["args"] = (form, window, cache_dir)

        FakeEdgeChrome.__init__ = _init

    edge = types.ModuleType("webview.platforms.edgechromium")
    edge.EdgeChrome = FakeEdgeChrome
    platforms = types.ModuleType("webview.platforms")
    platforms.edgechromium = edge
    monkeypatch.setitem(sys.modules, "webview", types.ModuleType("webview"))
    monkeypatch.setitem(sys.modules, "webview.platforms", platforms)
    monkeypatch.setitem(sys.modules, "webview.platforms.edgechromium", edge)
    return FakeEdgeChrome, calls


@pytest.mark.parametrize("args", [(), ("w",), ("form", "window", "cache"), ("f", "w", "c", "extra")])
def test_wrapper_passes_whatever_pywebview_sends(monkeypatch, args):
    """包装器必须签名无关 —— 参数原样透传，一个都不许少。"""
    cls, calls = _fake_edge_module(monkeypatch, arity=99)  # 随便什么参数都吃
    native_zoom.install_hook()
    cls(*args)
    assert calls["args"] == args


def test_patched_init_accepts_the_real_pywebview_signature(monkeypatch):
    """v0.3.5 的线上事故回归：真签名是 ``(self, form, window, cache_dir)``，
    而包装器当时写成了 ``(self, window)`` → 一调就 TypeError，**窗口建不出来**，
    双击 exe 毫无反应（而且是窗口模式，没有控制台，什么都看不到）。"""
    cls, calls = _fake_edge_module(monkeypatch, arity=3)
    native_zoom.install_hook()
    chrome = cls("form", "window", "cache")
    assert calls["args"] == ("form", "window", "cache")
    assert native_zoom._state["chrome"] is chrome, "包装器没能接住后端对象"
    assert native_zoom._state["signature"], "没把真实签名记下来（诊断要用）"
    assert native_zoom.capability()["signature"], "capability 里应带上签名"


def test_our_bookkeeping_can_never_break_window_creation(monkeypatch):
    """我们加的记账代码绝不许把建窗口搞挂 —— 例外一律吞掉，原实现照跑。"""
    cls, calls = _fake_edge_module(monkeypatch)
    native_zoom.install_hook()

    class Exploding(dict):
        def __setitem__(self, key, value):  # noqa: ANN001
            raise RuntimeError("磁盘炸了之类的")

    monkeypatch.setattr(native_zoom, "_state", Exploding(local=1, installed=True))
    cls("f", "w", "c")          # 不许抛
    assert calls["args"] == ("f", "w", "c"), "原实现必须照常跑完"


def test_reads_also_go_through_the_ui_thread():
    """CI 实测（v0.3.6）：在 uvicorn 线程里**读** ZoomFactor / CoreWebView2 也是
    InvalidOperationException —— 所以读和「是否就绪」同样得回 UI 线程，
    不然能力查询永远报 ready:false，界面白白回落 CSS。"""
    widget = FakeWidget(cross_thread=True)
    _attach(widget)
    cap = native_zoom.capability()
    assert widget.on_ui_thread is True, "读没有回到 UI 线程"
    assert cap["handle"] is True and cap["ready"] is True, cap
    assert cap["applied"] == 1.0


def test_ui_thread_readback_is_not_a_cross_thread_illusion():
    """设 + 读回必须在同一次 UI 线程调用里完成，否则读回的是跨线程假象。"""
    widget = FakeWidget(cross_thread=True)
    _attach(widget)
    result = native_zoom.set_zoom(1.44)
    assert result["ok"] is True and result["applied"] == 1.44, result


def test_without_pythonnet_it_falls_back_to_direct_access(monkeypatch):
    """没有 pythonnet（非 Windows）时「回 UI 线程」那跳会 ImportError —— 那就直着读，
    不能因为平台差异就永远报「读不到」。"""
    widget = FakeWidget()
    chrome = _attach(widget)
    chrome.form = None                      # 非 WinForms 后端没有窗体
    monkeypatch.setitem(sys.modules, "System", None)
    monkeypatch.delitem(sys.modules, "System", raising=False)
    cap = native_zoom.capability()
    assert cap["handle"] is True and cap["applied"] == 1.0, cap
    assert native_zoom.set_zoom(1.2)["ok"] is True
