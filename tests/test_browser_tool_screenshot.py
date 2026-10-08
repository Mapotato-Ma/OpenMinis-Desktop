"""浏览器截图必须落在**会话工作区**里 —— 否则 read_image 读不回来。

2026-10-08 现场（用户截图里那条红箭头）::

    read_image \\tmp\\minis-shots\\shot-1791439350802.png
    → Error: Cannot resolve path

根因两条：
1. 驱动默认写 POSIX 的 ``/tmp/minis-shots``，Windows 上 ``Path("/tmp/…")`` 是
   盘根的 ``\\tmp\\minis-shots``，跟 shell 认的 ``%TEMP%`` 根本不是一回事；
2. 更要命的是**读不回来**：read_image 只在会话工作区内解析路径，截图落在外面
   就永远报错 —— 截图工具不能看，等于白截。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from openminis.core import context


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIS_HOME", str(tmp_path))
    context.set_app_context(context.AppContext(data_dir=tmp_path, cache_dir=tmp_path))
    yield tmp_path
    context._context = None


def test_screenshot_defaults_into_the_session_workspace(env, monkeypatch, tmp_path):
    from openminis.tools import browser_use_tool as bt
    from openminis.tools import shell_execute_tool as st
    from openminis.tools.browser import driver as bt_driver

    class _FakeCoordinator:
        def cwd_for(self, session_id: str) -> str:
            assert session_id == "db-s1", "工作区要用 db-<sid> 去查（工具侧的 key）"
            return str(tmp_path)

    monkeypatch.setattr(st, "get_coordinator", lambda: _FakeCoordinator())
    captured: dict = {}

    async def fake_run(args, session=None):  # noqa: ANN001 - 测试替身
        captured.update(args)
        return "截图已保存：x.png"

    monkeypatch.setattr(bt_driver, "run", fake_run)

    res = asyncio.run(
        bt.BrowserUseTool.execute(
            json.dumps({"action": "screenshot", "tool_title": "截个图"}), "db-s1"
        )
    )
    assert res.success is True
    shots = tmp_path / ".minis" / "shots"
    assert captured["path"] == str(shots)
    assert shots.is_dir(), "默认目录要真的建出来"

    # 模型显式给了 path 就照它的来（它可能有意放到别处）
    captured.clear()
    asyncio.run(
        bt.BrowserUseTool.execute(
            json.dumps(
                {
                    "action": "screenshot",
                    "path": str(tmp_path / "mine"),
                    "tool_title": "t",
                }
            ),
            "db-s1",
        )
    )
    assert captured["path"] == str(tmp_path / "mine")


def test_non_screenshot_actions_are_untouched(env, monkeypatch, tmp_path):
    """只有截图动作才注入默认目录，别的动作不该被塞一个 path 参数。"""
    from openminis.tools import browser_use_tool as bt
    from openminis.tools.browser import driver as bt_driver

    captured: dict = {}

    async def fake_run(args, session=None):  # noqa: ANN001 - 测试替身
        captured.update(args)
        return "ok"

    monkeypatch.setattr(bt_driver, "run", fake_run)
    asyncio.run(
        bt.BrowserUseTool.execute(
            json.dumps({"action": "navigate", "url": "https://example.com",
                        "tool_title": "打开"}),
            "db-s1",
        )
    )
    assert "path" not in captured


def test_screenshot_path_is_readable_by_the_file_tools(env, monkeypatch, tmp_path):
    """跨工具契约：截图落点必须能被 read_image/file_read 解析到。

    这才是这次改动的**目的** —— 只断言"参数等于某个目录"是在钉入参，钉不住
    "能不能读回来"。现场那次失败正是"目录建出来了、图也在，就是读不到"。
    """
    from openminis.tools import browser_use_tool as bt
    from openminis.tools import file_read_tool as frt
    from openminis.tools import read_image_tool as rit
    from openminis.tools import shell_execute_tool as st

    class _Ctx:
        external_files_dir = tmp_path

    class _FakeCoordinator:
        def cwd_for(self, session_id: str) -> str:
            return str(tmp_path)

        def sandbox_root_for(self, session_id: str):
            return tmp_path

    monkeypatch.setattr(st, "get_coordinator", lambda: _FakeCoordinator())
    monkeypatch.setattr(frt, "app_context", lambda: _Ctx())
    monkeypatch.setattr(rit, "app_context", lambda: _Ctx())

    target = bt._screenshot_dir("db-s1")
    assert target is not None
    shot = target / "shot-1.png"
    shot.write_bytes(b"x")

    # file_read 侧（read_image 与它共用同一个解析器）
    resolved = frt._resolve_session_host_path("db-s1", str(shot))
    assert resolved is not None, "截图路径解析不出来 —— 又会变成 Cannot resolve path"
    assert resolved == shot


def test_screenshot_dir_helper_never_raises(env, monkeypatch):
    """协调器不可用 / 建目录失败时返回 None（退回驱动默认值），不能让工具炸。"""
    from openminis.tools import browser_use_tool as bt
    from openminis.tools import shell_execute_tool as st

    def _boom():
        raise RuntimeError("coordinator down")

    monkeypatch.setattr(st, "get_coordinator", _boom)
    assert bt._screenshot_dir("db-s1") is None

    # cwd_for 返回空串也要当"拿不到"：Path("") 是 "."，会在服务进程的工作目录
    # 里建目录并返回相对路径 —— read_image 照样读不回来（独立复核指出）。
    class _EmptyCoordinator:
        def cwd_for(self, session_id: str) -> str:
            return ""

    monkeypatch.setattr(st, "get_coordinator", lambda: _EmptyCoordinator())
    assert bt._screenshot_dir("db-s1") is None
