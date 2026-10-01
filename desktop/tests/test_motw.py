"""MOTW（Windows 网络来源标记）自检与解除。

真实事故（v0.4.6）：便携版第一次发到新电脑上，Windows 自带解压工具把 MOTW 传染给
每个文件 → .NET 拒绝加载 DLL → ``import clr`` 失败 → 窗口建不出来 → 用户看到的是
「双击没反应」。

测试在 Linux 上也能跑：POSIX 允许文件名里带冒号，所以 ``foo.dll:Zone.Identifier``
就是一个普通文件，正好用来模拟那个备用数据流。
"""

from __future__ import annotations

import sys

import pytest

from desktop import fatal, motw


@pytest.fixture(autouse=True)
def _pretend_windows(monkeypatch):
    """这些用例验的是 Windows 上的行为。"""
    monkeypatch.setattr(motw, "_supported", lambda: True)
    monkeypatch.delenv(motw.ENV, raising=False)


def _mark(path, content=b"[ZoneTransfer]\nZoneId=3\n"):
    (path.parent / f"{path.name}{motw.STREAM}").write_bytes(content)
    return path


def _marked(directory, name="a.dll"):
    f = directory / name
    f.write_bytes(b"x")
    return _mark(f)


# ── 探测 ────────────────────────────────────────────────────────────────


def test_plain_files_are_not_marked(tmp_path):
    (tmp_path / "a.dll").write_bytes(b"x")
    assert motw.is_marked(tmp_path / "a.dll") is False
    assert motw.find_marked(tmp_path) == []


def test_finds_only_the_marked_files(tmp_path):
    marked = _marked(tmp_path, "python.runtime.dll")
    (tmp_path / "plain.dll").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    nested = _marked(tmp_path / "sub", "webview2.dll")

    found = motw.find_marked(tmp_path)
    assert sorted(p.name for p in found) == ["python.runtime.dll", "webview2.dll"]
    assert marked in found and nested in found


def test_an_empty_stream_does_not_count_as_marked(tmp_path):
    """解除失败时我们会把流清空 —— 空流不该再被当成「来自互联网」。"""
    f = tmp_path / "a.dll"
    f.write_bytes(b"x")
    _mark(f, b"")
    assert motw.is_marked(f) is False


def test_non_windows_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setattr(motw, "_supported", lambda: False)
    _marked(tmp_path)
    assert motw.find_marked(tmp_path) == []
    assert motw.guard(tmp_path, ask=True) == "skipped"


# ── 解除 ────────────────────────────────────────────────────────────────


def test_unblock_removes_the_stream(tmp_path):
    files = [_marked(tmp_path, f"f{i}.dll") for i in range(3)]
    assert motw.unblock(files) == 3
    for f in files:
        assert not (f.parent / f"{f.name}{motw.STREAM}").exists()
    assert motw.find_marked(tmp_path) == []


# ── guard：决策表 ────────────────────────────────────────────────────────


def test_clean_tree_needs_no_question(tmp_path, monkeypatch):
    monkeypatch.setattr(fatal, "message_box", _boom)
    assert motw.guard(tmp_path) == "clean"


def test_ignore_mode_does_not_touch_anything(tmp_path, monkeypatch):
    monkeypatch.setenv(motw.ENV, "ignore")
    marked = _marked(tmp_path)
    assert motw.guard(tmp_path) == "skipped"
    assert (marked.parent / f"{marked.name}{motw.STREAM}").exists()


def test_unblock_mode_needs_no_dialog(tmp_path, monkeypatch):
    """CI 无人值守走这条 —— 不能弹窗把流水线挂住。"""
    monkeypatch.setenv(motw.ENV, "unblock")
    monkeypatch.setattr(fatal, "message_box", _boom)
    _marked(tmp_path)
    assert motw.guard(tmp_path) == "unblocked"
    assert motw.find_marked(tmp_path) == []


def test_declining_keeps_the_files_and_says_so(tmp_path, monkeypatch):
    marked = _marked(tmp_path)
    assert motw.guard(tmp_path, ask=False) == "declined"
    assert (marked.parent / f"{marked.name}{motw.STREAM}").exists()


def test_accepting_removes_them(tmp_path):
    _marked(tmp_path)
    assert motw.guard(tmp_path, ask=True) == "unblocked"
    assert motw.find_marked(tmp_path) == []


def test_prompt_falls_back_to_the_dialog(tmp_path, monkeypatch):
    seen = {}

    def fake_box(text, **kwargs):
        seen["text"] = text
        seen["kwargs"] = kwargs
        return "yes"

    monkeypatch.setattr(fatal, "message_box", fake_box)
    _marked(tmp_path)
    assert motw.guard(tmp_path) == "unblocked"
    assert "解除锁定" in seen["text"]
    assert str(tmp_path) in seen["text"]  # 告诉用户动的是哪个目录


def test_no_dialog_available_means_no_consent(tmp_path, monkeypatch):
    """弹不出来就当用户没同意 —— 绝不静默改文件的属性。"""
    monkeypatch.setattr(fatal, "message_box", lambda *a, **k: None)
    marked = _marked(tmp_path)
    assert motw.guard(tmp_path) == "declined"
    assert (marked.parent / f"{marked.name}{motw.STREAM}").exists()


def test_nothing_removed_reports_unfixable(tmp_path, monkeypatch):
    monkeypatch.setattr(motw, "unblock", lambda paths: 0)
    _marked(tmp_path)
    assert motw.guard(tmp_path, ask=True) == "unfixable"


def test_the_running_exe_is_left_alone(tmp_path, monkeypatch):
    """正在跑的 exe 自己带不带标记都无所谓：它已经起来了，而且文件被占用、去改它
    又正是杀软爱盯的动作（删除 MOTW = MITRE T1553.005）。"""
    exe = _marked(tmp_path, "OpenMinisDesktop.exe")
    dll = _marked(tmp_path, "Python.Runtime.dll")
    monkeypatch.setattr(motw, "_running_exe", lambda: exe)

    assert motw.guard(tmp_path, ask=True) == "unblocked"
    assert (exe.parent / f"{exe.name}{motw.STREAM}").exists(), "不该去动正在运行的 exe"
    assert not (dll.parent / f"{dll.name}{motw.STREAM}").exists(), "该动的必须动到"


def test_install_root_is_none_outside_a_frozen_build(monkeypatch):
    """源码运行时 ``sys.executable`` 是 python.exe —— 旁边是整个解释器目录，
    绝不能去动它。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert motw.install_root() is None


def _boom(*args, **kwargs):
    raise AssertionError("这一步不该弹对话框")


# ── fatal：兜底弹窗 ─────────────────────────────────────────────────────


def test_message_box_returns_none_off_windows():
    if sys.platform == "win32":  # pragma: no cover
        pytest.skip("Windows 上会真的弹窗")
    assert fatal.message_box("hello") is None


def test_report_fatal_writes_the_log_and_returns_the_text(tmp_path, monkeypatch):
    log = tmp_path / "logs" / "desktop.log"
    monkeypatch.setattr(fatal, "log_path", lambda: log)
    shown = []
    monkeypatch.setattr(fatal, "message_box", lambda text, **kw: shown.append(text) or "ok")

    try:
        raise RuntimeError("Failed to resolve Python.Runtime.Loader.Initialize")
    except RuntimeError as exc:
        body = fatal.report_fatal(exc, context="应用启动失败。")

    assert "Failed to resolve" in body
    assert "应用启动失败。" in body
    assert str(log) in body  # 告诉用户日志在哪
    assert "Failed to resolve" in log.read_text(encoding="utf-8")
    assert len(shown) == 1


def test_report_fatal_shows_one_dialog_per_failure(tmp_path, monkeypatch):
    """顶层兜底和调用方各报一次 —— 最常见的「弹两遍」。"""
    monkeypatch.setattr(fatal, "log_path", lambda: tmp_path / "desktop.log")
    shown = []
    monkeypatch.setattr(fatal, "message_box", lambda text, **kw: shown.append(text) or "ok")
    monkeypatch.setattr(fatal, "_shown", set())

    exc = RuntimeError("boom")
    fatal.report_fatal(exc, context="同一条")
    fatal.report_fatal(exc, context="同一条")
    assert len(shown) == 1


def test_format_exc_keeps_the_tail():
    try:
        raise ValueError("x" * 4000)
    except ValueError as exc:
        text = fatal.format_exc(exc, limit=200)
    assert len(text) <= 201
    assert "ValueError" in text
