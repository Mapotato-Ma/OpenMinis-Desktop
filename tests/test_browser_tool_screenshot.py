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
    assert captured["path"] == str(tmp_path / "browser-shots")
    assert (tmp_path / "browser-shots").is_dir(), "默认目录要真的建出来"

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


def test_screenshot_dir_helper_never_raises(env, monkeypatch):
    """协调器不可用 / 建目录失败时返回 None（退回驱动默认值），不能让工具炸。"""
    from openminis.tools import browser_use_tool as bt
    from openminis.tools import shell_execute_tool as st

    def _boom():
        raise RuntimeError("coordinator down")

    monkeypatch.setattr(st, "get_coordinator", _boom)
    assert bt._screenshot_dir("db-s1") is None
