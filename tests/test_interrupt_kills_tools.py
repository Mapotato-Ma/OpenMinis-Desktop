"""停止要**真的杀掉**工具调用，而不是只"不再等它"。

对应 2026-10-08 用户反馈：「现在停止停不下来，停止要把所有的工具调用全部都
杀掉」。当天现场日志：``stop requested but nothing is running``，然后工具继续
执行到用户重启 App —— 一半是登记表里没有这一轮（见 test_ws_control.py），
另一半就是这里：``cancel()`` 只停住等待，命令本身在会话级持久 shell 里继续跑。

判定标准（很硬，不靠"看起来"）：让命令在 4 秒后写一个文件。如果真的杀了，
等 5 秒后那个文件**永远不该出现**；如果只是丢下等待，文件照样会出现。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from openminis.core import context


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIS_HOME", str(tmp_path))
    context.set_app_context(context.AppContext(data_dir=tmp_path, cache_dir=tmp_path))
    yield tmp_path
    context._context = None


def test_interrupt_now_hands_the_pid_to_kill_process_tree(env, monkeypatch, tmp_path):
    """``interrupt_now`` 要把 **pid** 交给杀树函数，并让 shell 下次重建。"""
    from openminis.sandbox import persistent_shell as ps

    calls: dict[str, int] = {}

    def fake_kill(pid, proc=None):  # noqa: ANN001 - 测试替身
        calls["pid"] = pid
        return "已终止进程组"

    monkeypatch.setattr(ps, "kill_process_tree", fake_kill)

    class _Proc:
        pid = 4321
        returncode = None

    shell = ps.PersistentShell(session_id="s1", cwd=tmp_path)
    shell._process = _Proc()  # type: ignore[assignment]
    assert shell.interrupt_now() == "已终止进程组"
    assert calls["pid"] == 4321, "没把真正的 pid 传下去"
    # 第二次调用不该重复杀（shell 已经收掉了）
    assert shell.interrupt_now() == "shell 已经停了"


@pytest.mark.skipif(os.name == "nt", reason="用 POSIX shell 语义验证进程树")
def test_cancelled_and_timed_out_commands_really_die(env, tmp_path):
    """取消 / 超时都必须把命令真的收掉，而且 shell 还能继续用。"""
    from openminis.sandbox.persistent_shell import PersistentShell

    async def scenario() -> None:
        shell = PersistentShell(session_id="t1", cwd=tmp_path)
        try:
            # ① 取消路径（用户按停止）
            # 命令里带一个**孙进程**（子 shell 后台任务）——只杀直接子进程的实现
            # 会把它漏掉，而真实场景里干活的恰恰是这些孙子（curl / python / node）。
            task = asyncio.create_task(
                shell.execute_command(
                    "( sleep 4; echo x > leaked1.txt ) & sleep 4; "
                    "echo x > leaked1b.txt",
                    timeout=60,
                )
            )
            await asyncio.sleep(1.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # ② 超时路径：以前只返回 124 就把命令丢在 shell 里继续跑，
            #    它接下来的输出还会掺进下一条命令的回显。
            out, code = await shell.execute_command(
                "sleep 4; echo x > leaked2.txt", timeout=1
            )
            assert code == 124 and "timed out" in out

            # ③ 等过两条命令的自然完成时间：痕迹必须不存在
            await asyncio.sleep(5)
            assert not (tmp_path / "leaked1.txt").exists(), (
                "取消后命令还在跑 —— 说明只丢下了等待，没杀进程"
            )
            assert not (tmp_path / "leaked2.txt").exists(), (
                "超时后命令还在跑 —— 会污染下一条命令的输出"
            )
            assert not (tmp_path / "leaked1b.txt").exists(), (
                "孙进程（子 shell 后台任务）没被杀 —— 只杀了直接子进程"
            )

            # ④ 被杀掉的 shell 要能自动重建，后续命令照常
            #    （也是那条"读循环把下一条命令判成 exit=-1 → 一次性兜底把命令
            #     再执行一遍"的回归位：这里必须拿到真回显，不能是 exit=-1）
            out, code = await shell.execute_command("echo alive", timeout=30)
            assert code == 0 and "alive" in out
        finally:
            await shell.stop()

    asyncio.run(scenario())


def test_new_group_kwargs_matches_the_platform():
    """进程组参数按平台给：POSIX 用 start_new_session（否则 killpg 会自杀）。"""
    import os as _os

    from openminis.sandbox.persistent_shell import new_group_kwargs

    kwargs = new_group_kwargs()
    if _os.name == "nt":
        assert kwargs == {"creationflags": 0x00000200}
    else:
        assert kwargs == {"start_new_session": True}


def test_coordinator_interrupt_covers_shell_and_fallback(env, monkeypatch, tmp_path):
    """协调器的 interrupt 要同时覆盖：持久 shell 与一次性兜底子进程。"""
    from openminis.sandbox import execution_coordinator as ec
    from openminis.sandbox import persistent_shell as ps

    killed: list[int] = []

    class _Proc:
        pid = 99
        returncode = None

        def kill(self) -> None:  # pragma: no cover - 只验证被调用
            killed.append(self.pid)

    monkeypatch.setattr(ps, "kill_process_tree", lambda pid, proc=None: "已终止进程组")
    coord = ec.ExecutionCoordinator()
    shell = ps.PersistentShell(session_id="k1", cwd=tmp_path)
    shell._process = _Proc()  # type: ignore[assignment]
    coord._shells["db-k1"] = shell
    coord.register_fallback("db-k1", _Proc())

    note = coord.interrupt("db-k1")
    assert "已终止进程组" in note
    assert killed == [99], "一次性兜底的子进程没被杀"
    assert "db-k1" not in coord._shells, "杀完要把 shell 从表里摘掉（下次重建）"
    assert coord.interrupt("db-k1") == "没有正在跑的命令"


def test_coordinator_interrupt_reaches_subagent_shells(env, monkeypatch, tmp_path):
    """子代理的 shell（``<key>:sub:<id>``）也要在射程内。

    开着子代理时**绝大部分工具调用发生在子代理里**（见 main.py 的群聊可视化
    注释）—— 只按精确 key 杀的话，用户按了停止，子代理那条长命令还在跑，
    正是「停止要把所有工具调用全部杀掉」没做到的那一半。
    """
    from openminis.sandbox import execution_coordinator as ec
    from openminis.sandbox import persistent_shell as ps

    monkeypatch.setattr(ps, "kill_process_tree", lambda pid, proc=None: "已终止进程组")

    class _Proc:
        pid = 7
        returncode = None

    coord = ec.ExecutionCoordinator()
    for key in ("db-k1", "db-k1:sub:abc", "db-k1:sub:def", "db-k10"):
        shell = ps.PersistentShell(session_id=key, cwd=tmp_path)
        shell._process = _Proc()  # type: ignore[assignment]
        coord._shells[key] = shell

    note = coord.interrupt("db-k1")
    # 自己 + 两个子代理，都被收掉
    assert note.count("已终止进程组") == 3, note
    assert "db-k10" in coord._shells, "前缀匹配不能误伤 db-k10（冒号才是分隔符）"
    assert set(coord._shells) == {"db-k10"}


def test_one_shot_fallback_cancel_kills_the_registered_process(env, monkeypatch, tmp_path):
    """持久 shell 失效时走的一次性兜底：取消/超时必须杀掉子进程，且登记过。

    独立复核（2026-10-08）的突变实测：把这条改动（kill + register_fallback）撤掉，
    **57 个用例全绿** —— 也就是说「停止杀不掉」的另一条路径一行断言都没有。
    """
    import json
    import types as _types

    from openminis.sandbox import persistent_shell as ps
    from openminis.tools import shell_execute_tool as st

    killed: list[int] = []
    events: dict = {}
    real_kill = ps.kill_process_tree

    def _fake_kill(pid, proc=None):  # noqa: ANN001 - 记账 + 真杀（别留残余进程）
        killed.append(pid)
        return real_kill(pid, proc)

    monkeypatch.setattr(ps, "kill_process_tree", _fake_kill)

    class _Coord:
        _cwd_overrides: dict = {}

        def cwd_for(self, session_id):  # noqa: ANN001
            return str(tmp_path)

        def execute(self, *a, **k):  # noqa: ANN002, ANN003
            async def _inner():
                return _types.SimpleNamespace(output="", exit_code=-1)
            return _inner()

        def register_fallback(self, session_id, proc):  # noqa: ANN001
            events["reg"] = (session_id, proc.pid)

        def unregister_fallback(self, session_id, proc):  # noqa: ANN001
            events["unreg"] = (session_id, proc.pid)

        def interrupt(self, session_id):  # noqa: ANN001
            return "stub"

    tool = st.ShellExecuteTool()
    tool.coordinator = _Coord()

    async def scenario() -> None:
        # 取消路径（用户按停止）
        task = asyncio.create_task(
            tool.execute('{"command": "sleep 30", "tool_title": "t"}', "db-s1")
        )
        for _ in range(300):
            if "reg" in events:
                break
            await asyncio.sleep(0.02)
        assert "reg" in events, "兜底子进程没登记 —— 停止按钮就杀不到它"
        assert events["reg"][0] == "db-s1", "登记的 key 要对得上（工具侧那个）"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert killed == [events["reg"][1]], "取消后没杀掉兜底子进程"

        # 超时路径
        killed.clear()
        events.clear()
        res = await tool.execute(
            json.dumps({"command": "sleep 30", "timeout": 1, "tool_title": "t"}), "db-s1"
        )
        assert not res.success, "超时必须如实失败"
        assert killed == [events["reg"][1]], "超时后没杀掉兜底子进程"
        assert events.get("unreg", (None,))[0] == "db-s1", "跑完要注销登记"

    asyncio.run(scenario())
