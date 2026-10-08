"""沙箱「当场确认」的**生产端接线**。

背景：前端有整套 ``guard_confirm`` 问询 UI、内核 ``_ask_guard_confirm`` 也会建帧、
发帧、等答案、180 秒超时 —— 但 ``confirm.set_asker(...)`` 从没人调用，于是
``sandbox/confirm.ask()`` 恒返回 ``unavailable``：命令被沙箱拦下时界面**不弹确认**
（工具侧永远硬拦），而界面上写着"会当场弹确认"，等于对用户说谎。

为什么 ``tests/test_guard_confirm.py`` 抓不到：那里全靠**手工** ``confirm.set_asker``
来量工具侧行为，接口实现有没有被接上是另一回事（手工塞 future 也是同理）—— 接线
漏了它照样全绿。

这条测试**一个 asker 都不塞**：跑真实 ``lifespan``（注册就在那里发生），发一条真会被
守卫拦下的命令，然后像界面那样回答，看命令是不是真的放行了。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest

from openminis.core import context
from openminis.sandbox import confirm
from openminis.sandbox.guard import check_shell_command, guard
from openminis.server import chat_store  # noqa: F401  (db init consistency)
from openminis.server import main as server_main
from openminis.tools.shell_execute_tool import _ask_user_to_allow


class _FakeSink:
    """只当「界面」：收帧记下来（``manager.attach`` 那种订阅者）。"""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.frames.append(payload)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIS_HOME", str(tmp_path))
    context.set_app_context(context.AppContext(data_dir=tmp_path, cache_dir=tmp_path))
    guard.reset()
    confirm.set_asker(None)
    yield tmp_path
    guard.reset()
    confirm.set_asker(None)
    context._context = None


async def _until(pred: Callable[[], bool], timeout: float) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


async def test_lifespan_wires_the_confirm_asker_so_the_ui_really_gets_asked(env, monkeypatch):
    """服务起好后：被拦的命令 → 界面收到 guard_confirm 帧 → 用户点「本次允许」→ 放行。"""
    cmd = "rm -rf C:/Windows/System32/important"
    async with server_main.lifespan(server_main.app):
        assert confirm.has_asker(), (
            "lifespan 没把 asker 接上 —— confirm.ask() 只会返回 unavailable，"
            "前端那套确认框是死代码"
        )

        manager = server_main.ConnectionManager()
        sink = _FakeSink()
        manager.active["c1"] = sink
        monkeypatch.setattr(server_main, "manager", manager)

        blocked = check_shell_command(cmd, session_id="db-s1", cwd="C:/work")
        # 自检：守卫规则万一改了、这条不再被拦，测试要**当场说出来**，而不是静默全绿。
        assert blocked, "这条命令本该被沙箱拦下"

        answer = asyncio.create_task(
            _ask_user_to_allow(blocked, command=cmd, session_id="db-s1", cwd="C:/work")
        )
        assert await _until(lambda: bool(sink.frames), 5.0), (
            "沙箱拦下了命令，界面却一个帧都没收到（asker 没接线 / 帧没发出去）"
        )
        frame = sink.frames[0]
        assert frame["type"] == "guard_confirm", frame
        assert frame.get("requestId"), "确认帧没带 requestId，界面回不了答案"
        assert frame.get("command") == cmd, "确认帧里没带上要问的那条命令"
        assert frame.get("sessionId") == "db-s1"

        # 界面点「本次允许」—— 走的是前端真正回发的那个帧
        await server_main._handle_guard_answer(
            "c1", {"requestId": frame["requestId"], "decision": "once"}
        )
        assert await asyncio.wait_for(answer, 5) is None, (
            "用户放行了，命令却还是被拦住 —— 接线只做了一半"
        )


async def test_without_the_asker_a_blocked_command_is_still_hard_blocked(env):
    """没接客户端（纯 CLI）时行为不变：照旧硬拦，绝不许把 agent 挂住。"""
    cmd = "rm -rf C:/Windows/System32/important"
    confirm.set_asker(None)
    blocked = check_shell_command(cmd, session_id="db-s1", cwd="C:/work")
    assert blocked
    assert await _ask_user_to_allow(
        blocked, command=cmd, session_id="db-s1", cwd="C:/work"
    ) == blocked
