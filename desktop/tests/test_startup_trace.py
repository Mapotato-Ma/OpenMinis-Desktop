"""启动时间线自身的测试。

这里只能验「格式与副作用」：真实的 bootloader 解包耗时必须在 Windows 上跑才知道，
本地没有 GetProcessTimes，非 Windows 一律退回 ``origin=python``。
"""

from __future__ import annotations

import sys

from desktop import paths
from desktop import startup_trace as trace


def setup_function() -> None:
    trace.reset_for_tests()


def teardown_function() -> None:
    trace.reset_for_tests()


def test_summary_reports_an_origin_and_every_mark():
    trace.mark("python")
    trace.mark("backend-ready")
    line = trace.summary()

    assert line.startswith("[startup] origin=")
    assert "total=" in line
    assert "python=+" in line and "backend-ready=+" in line
    # 非 Windows 没有进程创建时刻可用，就不许假装有
    if sys.platform != "win32":
        assert line.startswith("[startup] origin=python")


def test_marks_are_monotonic():
    trace.mark("a")
    trace.mark("b")
    line = trace.summary()
    first = int(line.split("a=+")[1].split("ms")[0])
    second = int(line.split("b=+")[1].split("ms")[0])
    assert second >= first


def test_report_appends_one_line_with_the_version(tmp_path):
    trace.mark("python")
    line = trace.report(version="9.9.9")

    assert "version=9.9.9" in line
    log = paths.data_root() / "logs" / "startup.log"
    assert log.is_file(), "时间线要落到 logs/startup.log，用户直接发这个文件就行"
    content = log.read_text(encoding="utf-8")
    assert line in content
    assert content.endswith("\n")


def test_report_only_writes_once():
    trace.mark("python")
    trace.report()
    first = (paths.data_root() / "logs" / "startup.log").read_text(encoding="utf-8")
    trace.report()
    assert (paths.data_root() / "logs" / "startup.log").read_text(encoding="utf-8") == first


def test_report_never_raises_when_the_data_dir_is_broken(monkeypatch):
    def broken():
        raise OSError("no data dir for you")

    monkeypatch.setattr(paths, "data_root", broken)
    trace.mark("python")
    assert trace.report()  # 量不到时间不能反过来把启动搞挂
