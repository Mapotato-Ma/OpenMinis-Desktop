"""js_api 桥上的缩放方法 —— 「每个窗口缩自己」的契约测试。

为什么要有这一层：v0.4.23 的 bug 是**双开时 HTTP 缩放落到了别的窗口上**
（`POST /api/desktop/zoom` 由后端进程处理，而它只能操作自己进程里的 WebView2
控件 → 它回 ok:true，缩的却是别人）。正解是让**每个窗口缩自己** —— 也就是走
``window.pywebview.api``（pywebview 的 js_api，见 ``WindowAPI``）。

这里钉三件事（真控件在 iSH 里不存在，那部分留给 Windows 真机）：
  * 两个方法真的**代理**到 ``native_zoom``，不另写一套缩放逻辑；
  * 宿主说不行时**如实**把 ok=False + reason 交回去（界面才会回落 CSS）；
  * 结果必须是**可 JSON 序列化**的 —— 桥的返回值 pywebview 要编码成 JSON，
    塞个不可序列化的东西进去就是界面卡死。
"""
from __future__ import annotations

import json
from pathlib import Path

from desktop import native_zoom, window as window_mod


def _api():
    return window_mod.WindowAPI(None, app_root=Path.cwd())


def test_set_zoom_delegates_to_native_zoom(monkeypatch):
    seen: list[object] = []
    monkeypatch.setattr(native_zoom, "set_zoom",
                        lambda factor: (seen.append(factor),
                                        {"ok": True, "applied": float(factor), "reason": ""})[1])
    assert _api().set_zoom(1.2) == {"ok": True, "applied": 1.2, "reason": ""}
    assert seen == [1.2], "桥必须把参数原样交给 native_zoom（别自己夹取/改写）"


def test_set_zoom_reports_host_refusal_verbatim(monkeypatch):
    """宿主说「设不进去」时不许粉饰 —— 界面靠这个 reason 决定回落 CSS。"""
    refusal = {"ok": False, "applied": None, "reason": "拿不到 WebView2 控件"}
    monkeypatch.setattr(native_zoom, "set_zoom", lambda factor: dict(refusal))
    assert _api().set_zoom(1.2) == refusal


def test_zoom_capability_delegates(monkeypatch):
    monkeypatch.setattr(native_zoom, "capability",
                        lambda: {"handle": True, "ready": True, "applied": 1.728, "reason": ""})
    assert _api().zoom_capability()["applied"] == 1.728


def test_no_control_means_honest_no_not_an_exception():
    """没有控件（非 Windows / 非 Edge 后端）时不抛：桥是被 JS 调的，抛出去就是界面死。"""
    native_zoom.reset_for_tests()
    api = _api()
    assert api.zoom_capability()["handle"] is False
    result = api.set_zoom(1.2)
    assert result["ok"] is False and result["reason"], "失败必须带原因，不能只说不行"


def test_results_are_json_serializable():
    """pywebview 会把返回值编码成 JSON 交给 JS —— 不可序列化 = 前端永远挂在那里。"""
    native_zoom.reset_for_tests()
    api = _api()
    json.dumps(api.zoom_capability())
    json.dumps(api.set_zoom(1.2))
    json.dumps(api.set_zoom("nonsense"))


def test_both_methods_are_public_so_js_can_see_them():
    """pywebview 只暴露公开方法（下划线开头的不给 JS）。改名/加前缀会让桥静默消失。"""
    for name in ("set_zoom", "zoom_capability"):
        assert callable(getattr(window_mod.WindowAPI, name, None)), f"{name} 不是可调用的公开方法"
        assert not name.startswith("_")
