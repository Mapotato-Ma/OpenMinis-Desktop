"""批 3 的两条身份契约：帧的通道归属（R08）与执行根的作用域（R16）。

两条都是"归属"问题：一条帧属于哪个通道、一个 shell 属于哪个项目。归属必须由
**帧/绑定自己**说清，而不是靠"收帧那一刻界面忙不忙"或"上一次绑过谁"去推断。
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# R08 · 帧的通道归属
# ---------------------------------------------------------------------------
async def test_routed_frames_always_carry_a_channel(monkeypatch):
    """会分流的帧必须自带 channel —— 前端不再拿"终端忙不忙"猜。"""
    from openminis.server import main as server_main

    sent: list[dict] = []

    async def fake_send_json(_client_id: str, payload: dict) -> None:
        sent.append(payload)

    monkeypatch.setattr(server_main.manager, "send_json", fake_send_json)

    # 聊天帧：没标的一律补 chat（唯一会自己标的是终端那条路）
    await server_main._safe_send("c1", {"type": "delta", "text": "hi", "sessionId": "s1"})
    assert sent[-1]["channel"] == server_main.CHAT_CHANNEL, sent[-1]

    # 终端帧：走 _shell_frame，带 shell
    await server_main._safe_send("c1", server_main._shell_frame({"type": "delta", "text": "ls"}))
    assert sent[-1]["channel"] == server_main.SHELL_CHANNEL, sent[-1]
    assert sent[-1]["channel"] != sent[-2]["channel"], "两个通道的帧标成了同一个"

    # 不分流的帧（pong / guard_confirm）不该被硬塞一个通道
    await server_main._safe_send("c1", {"type": "pong"})
    assert "channel" not in sent[-1], sent[-1]


def test_shell_frames_are_marked_at_every_emit_site():
    """终端那条路的每个出口都要过 ``_shell_frame``（漏一个就退回"猜归属"）。"""
    import inspect

    from openminis.server import main as server_main

    source = inspect.getsource(server_main._handle_shell)
    assert source.count("_shell_frame(") >= 5, "终端帧有的地方没盖通道标记"
    assert '{"type": "done", "exitCode"' in source.replace(" ", "").replace("\n", "") or (
        '"exitCode"' in source
    )


# ---------------------------------------------------------------------------
# R16 · 换工作目录必须把旧 shell 一起换掉
# ---------------------------------------------------------------------------
class _FakeShell:
    """只记「有没有被收掉」的假 shell。"""

    def __init__(self) -> None:
        self.killed = 0

    def interrupt_now(self) -> str:
        self.killed += 1
        return "已杀进程树"


def test_binding_a_new_root_drops_the_shell_left_in_the_old_one(tmp_path):
    """R16：绑定变了 → 旧 shell 收掉；绑定没变 → 什么都不做（否则每轮都白等重建）。"""
    from openminis.sandbox.execution_coordinator import ExecutionCoordinator

    coord = ExecutionCoordinator()
    project_a = tmp_path / "A"
    project_b = tmp_path / "B"
    project_a.mkdir()
    project_b.mkdir()
    shell = _FakeShell()
    coord._shells["db-s1"] = shell  # noqa: SLF001 - 现场就这么存的
    coord.set_session_cwd("db-s1", project_a)
    assert shell.killed == 1, "第一次绑定没有把默认根里那个 shell 收掉"

    # 同一个根反复设（每一轮开头都会设一次）：不许再杀
    coord._shells["db-s1"] = _FakeShell()  # noqa: SLF001
    again = coord._shells["db-s1"]  # noqa: SLF001
    coord.set_session_cwd("db-s1", project_a)
    assert again.killed == 0, "绑定没变却把预热好的 shell 杀了"

    # 换根：必须收掉（否则文件工具看 B、shell 还在 A 里跑）
    coord.set_session_cwd("db-s1", project_b)
    assert again.killed == 1, "换项目之后旧 shell 还活着 —— 读写不是一个项目"
    assert "db-s1" not in coord._shells  # noqa: SLF001
    assert coord.sandbox_root_for("db-s1") == project_b


def test_clearing_the_binding_drops_the_shell_too(tmp_path):
    """撤销绑定（这一轮没给这个成员分配项目）也要收掉 shell。"""
    from openminis.sandbox.execution_coordinator import ExecutionCoordinator

    coord = ExecutionCoordinator()
    project = tmp_path / "A"
    project.mkdir()
    coord.set_session_cwd("db-s1:sub:writer", project)
    shell = _FakeShell()
    coord._shells["db-s1:sub:writer"] = shell  # noqa: SLF001

    coord.clear_session_cwd("db-s1:sub:writer")

    assert shell.killed == 1, "撤销绑定之后 shell 还停在旧项目里"
    assert coord.sandbox_root_for("db-s1:sub:writer") is None
    # 本来就没绑定 → 不做事（避免每轮无谓地杀 shell）
    coord.clear_session_cwd("db-s1:sub:writer")
    assert shell.killed == 1


def test_subagent_without_a_project_clears_the_previous_binding(monkeypatch):
    """子代理这次没被分配项目 → 撤销上一次的绑定（R16 的另一半）。"""
    from openminis.agent import subagent_events, subagents

    calls: list[tuple[str, str]] = []

    class _Coord:
        def set_session_cwd(self, key: str, path: Path) -> None:
            calls.append(("set", f"{key}->{path}"))

        def clear_session_cwd(self, key: str) -> None:
            calls.append(("clear", key))

    monkeypatch.setattr("openminis.tools.shell_execute_tool.get_coordinator", lambda: _Coord())

    # 这一轮没给 writer 分配项目 → 必须**撤销**之前那次绑定
    subagent_events.set_projects({})
    subagents.bind_subagent_project("db-s1", "writer")
    assert calls == [("clear", "db-s1:sub:writer")], calls

    # 分配了 → 设上去（并且用的是同一个 key 规则）
    calls.clear()
    subagent_events.set_projects({"writer": "/tmp/proj"})
    subagents.bind_subagent_project("db-s1", "writer")
    assert calls and calls[0][0] == "set" and calls[0][1].startswith("db-s1:sub:writer->"), calls
    subagent_events.set_projects(None)
