"""WebView2 原生缩放（ZoomFactor）接入 —— 界面缩放的「正统」实现。

为什么需要它：CSS `zoom` 是在页面内缩放，代价是视口单位不跟着缩小（`100vh`
会比视口高一截，用户实测 144% 下整页被顶上去）。而 WebView2 的 `ZoomFactor`
是**引擎级**缩放：CSS px 变大、布局视口变小，`vh` / `100%` / 媒体查询全部自洽，
文字也按真实字号渲染（dpr 感知）。

为什么之前没接：pywebview 6 的 Windows 后端（`edgechromium.py`）**不给
`window.native` 赋值**（只有 cocoa/gtk/qt/winforms 赋），所以壳层拿不到那个
WebView2 控件。这里在建窗口之前包一层 `EdgeChrome.__init__` 自己把它接住。

设计红线（这个模块**永远不许**影响启动或让界面变哑）：
  * 任何异常都变成 `reason` 字符串返回，绝不向外抛；
  * 只有「设进去 + 读回来一致」才算成功（不接受"可能生效"）；
  * 失败时界面自动回落 CSS zoom —— 也就是 v0.3.4 已修好的那条路。
"""
from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

_ZOOM_MIN = 0.5
_ZOOM_MAX = 5.0
_TOLERANCE = 0.005

_state: dict[str, Any] = {"chrome": None, "patch_error": None, "installed": False,
                           "signature": None}


def install_hook() -> None:
    """把 pywebview 的 Edge 后端接住（必须**在建窗口之前**调用）。幂等、不抛。"""
    if _state["installed"]:
        return
    try:
        from webview.platforms import edgechromium  # noqa: PLC0415
    except Exception as exc:  # 非 Windows / 没装 pywebview：正常，不是错误
        _state["patch_error"] = f"没有 Edge 后端（{type(exc).__name__}）"
        _state["installed"] = True
        return
    try:
        original = edgechromium.EdgeChrome.__init__
        if getattr(original, "__openminis_wrapped__", False):  # 已经有人包过
            _state["installed"] = True
            return
        try:
            _state["signature"] = str(inspect.signature(original))
        except (TypeError, ValueError):
            _state["signature"] = "?"

        def patched(self, *args, **kwargs):
            # 参数必须**完全照传**：pywebview 6.2.1 的签名是
            # ``(self, form, window, cache_dir)``。v0.3.5 我按 ``(self, window)``
            # 包过一层，结果 EdgeChrome(...) 一调就 TypeError，窗口建不出来 ——
            # 双击 exe 没反应。签名无关的 ``*args`` 是对这类改动的唯一防御。
            original(self, *args, **kwargs)
            _keep(self)

        patched.__openminis_wrapped__ = True  # type: ignore[attr-defined]
        edgechromium.EdgeChrome.__init__ = patched
        _state["installed"] = True
        logger.debug("native zoom hook installed (webview2)")
    except Exception as exc:  # noqa: BLE001
        _state["patch_error"] = f"挂钩失败：{type(exc).__name__}: {exc}"
        _state["installed"] = True


def _keep(chrome: Any) -> None:
    """把后端对象接住。**只做这件事** —— 我们加的代码不许影响建窗口。"""
    try:
        _state["chrome"] = chrome
    except BaseException:  # noqa: BLE001
        pass


def _why_no_control() -> str:
    """把「没控件」拆成三种情况 —— 否则日志里只能看到一句没法定位的话。"""
    if not _state["installed"]:
        return "壳层还没走建窗口的路径（挂钩未安装）"
    if _state["patch_error"]:
        return _state["patch_error"]
    return "挂钩已装，但 Edge 后端还没创建窗口"


def control() -> tuple[Any | None, str]:
    """返回 (WebView2 控件, 失败原因)。"""
    chrome = _state.get("chrome")
    if chrome is None:
        return None, _why_no_control()
    widget = getattr(chrome, "webview", None)
    if widget is None:
        return None, "拿不到 WebView2 控件"
    return widget, ""


def _read(widget: Any) -> float:
    return float(widget.ZoomFactor)


def _set_direct(widget: Any, factor: float) -> float:
    widget.ZoomFactor = factor
    return _read(widget)


def _on_ui_thread(chrome: Any, action: Any) -> Any:
    """在宿主窗体（UI 线程）上执行 action —— **读写都算**。

    WinForms 控件的跨线程访问会抛 ``InvalidOperationException``。CI 实测（v0.3.6）：
    在 uvicorn 线程里读 ``ZoomFactor`` / ``CoreWebView2`` 就是这个错 —— 所以不只
    「写」要回 UI 线程，「读」和「是否就绪」也一样。
    异常不扔进 .NET 委托里，而是带回来再抛，便于如实报告原因。
    """
    from System import Action  # noqa: PLC0415  (pythonnet)

    form = getattr(chrome, "form", None)
    if form is None:
        return action()  # 没有窗体（非 WinForms 后端）就直着来，失败会如实上报
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = action()
        except BaseException as exc:  # noqa: BLE001 - 带回去再抛
            box["error"] = exc

    form.Invoke(Action(run))
    if "error" in box:
        raise box["error"]
    return box.get("value")


def capability() -> dict[str, Any]:
    """给界面/CI 看的能力快照。**不声称支持**，只报告握到手的东西。"""
    widget, why = control()
    if widget is None:
        return {"handle": False, "ready": False, "applied": None, "reason": why,
                "signature": _state.get("signature")}
    chrome = _state.get("chrome")

    def probe() -> tuple[bool, float]:
        return (bool(getattr(widget, "CoreWebView2", None)), _read(widget))

    try:
        ready, applied = _on_ui_thread(chrome, probe)
    except Exception as exc:  # noqa: BLE001
        # 没有 pythonnet / 没有窗体时上一步会失败，直着再读一次
        # （非 WinForms 后端不走 UI 线程也没事）
        try:
            ready, applied = probe()
        except Exception:  # noqa: BLE001
            return {"handle": True, "ready": False, "applied": None,
                    "reason": f"读不到 ZoomFactor：{type(exc).__name__}",
                    "signature": _state.get("signature")}
    return {"handle": True, "ready": ready, "applied": applied, "reason": "",
            "signature": _state.get("signature")}


def set_zoom(factor: Any) -> dict[str, Any]:
    """把缩放交给 WebView2。只有「设进去 + 读回来一致」才回 ok=True。"""
    widget, why = control()
    if widget is None:
        return {"ok": False, "applied": None, "reason": why}
    try:
        want = float(factor)
    except (TypeError, ValueError):
        return {"ok": False, "applied": None, "reason": "factor 不是数字"}
    if not (_ZOOM_MIN - 1e-9 <= want <= _ZOOM_MAX + 1e-9):
        return {"ok": False, "applied": None,
                "reason": f"factor 超出 {_ZOOM_MIN}–{_ZOOM_MAX}"}

    errors: list[str] = []
    chrome = _state.get("chrome")
    attempts = (
        # UI 线程优先：Windows 上控件的跨线程访问一律抛 InvalidOperationException，
        # 而 form 为空时 _on_ui_thread 会直接执行（覆盖非 WinForms 后端）。
        ("UI 线程", lambda: _on_ui_thread(chrome, lambda: _set_direct(widget, want))),
        # 没有 pythonnet（非 Windows）时上一跳会 ImportError，再直着试一次
        ("直接", lambda: _set_direct(widget, want)),
    )
    for label, apply in attempts:
        try:
            got = float(apply())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}失败：{type(exc).__name__}: {exc}")
            continue
        if abs(got - want) <= _TOLERANCE:
            return {"ok": True, "applied": got, "reason": ""}
        errors.append(f"{label}设置后被读回 {got}（期望 {want}）—— 不接受“可能生效”")
    return {"ok": False, "applied": None, "reason": "；".join(errors)[:300]}


def reset_for_tests() -> None:
    """仅供测试：清掉接住的对象与补丁标记。"""
    _state.update({"chrome": None, "patch_error": None, "installed": False,
                   "signature": None})
