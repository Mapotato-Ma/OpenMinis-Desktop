"""壳层暴露给界面的 JS API（``window.pywebview.api``）的契约测试。

这些方法跑在 GUI 进程里，CI 上没有窗口 —— 所以这里用**假窗口**测：
调用形状、取消时的返回值、以及"没有 pywebview 时不许炸"。真对话框只能在
Windows 上手点，但"点取消/不可用时返回空字符串"这种边界必须是确定的。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from desktop.window import WindowAPI


class FakeWindow:
    """记录调用，并按 pywebview 的约定返回（文件夹对话框给的是元组）。"""

    def __init__(self, result: Any = None, *, boom: bool = False) -> None:
        self.result = result
        self.boom = boom
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def create_file_dialog(self, dialog_type: Any, **kw: Any) -> Any:
        self.calls.append(("create_file_dialog", {"dialog_type": dialog_type, **kw}))
        if self.boom:
            raise RuntimeError("no dialog on this host")
        return self.result


def api(window: FakeWindow) -> WindowAPI:
    return WindowAPI(window, app_root=Path("/tmp"))


def test_pick_folder_returns_the_chosen_path():
    win = FakeWindow(("/tmp/some-dir",))
    assert api(win).pick_folder() == "/tmp/some-dir"
    assert win.calls[0][0] == "create_file_dialog"


def test_pick_folder_reports_cancel_as_empty_string():
    """点取消 / 没有对话框 / 抛异常 —— 都必须是空字符串，不能是 None 或异常。

    界面用 `if (!p)` 判断取消，返回 None 会让它拼出 "undefined" 写进输入框。
    """
    assert api(FakeWindow(None)).pick_folder() == ""
    assert api(FakeWindow(())).pick_folder() == ""
    assert api(FakeWindow(boom=True)).pick_folder() == ""


def test_pick_folder_tolerates_a_bare_string():
    """有的 pywebview 后端回单个字符串而不是元组。别在这里炸。"""
    assert api(FakeWindow("/tmp/plain")).pick_folder() == "/tmp/plain"


def test_pick_folder_passes_a_usable_start_directory(tmp_path: Path):
    win = FakeWindow((str(tmp_path),))
    api(win).pick_folder(str(tmp_path))
    assert win.calls[0][1]["directory"] == str(tmp_path)


def test_pick_folder_ignores_a_bogus_start_directory(tmp_path: Path):
    """起始目录不存在时不要把它传给对话框（某些后端会因此失败）。"""
    win = FakeWindow((str(tmp_path),))
    api(win).pick_folder(str(tmp_path / "does-not-exist"))
    assert win.calls[0][1]["directory"] == ""
