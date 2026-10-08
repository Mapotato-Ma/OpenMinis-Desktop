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
            task = asyncio.create_task(
                shell.execute_command("sleep 4; echo x > leaked1.txt", timeout=60)
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

            # ④ 被杀掉的 shell 要能自动重建，后续命令照常
            out, code = await shell.execute_command("echo alive", timeout=30)
            assert code == 0 and "alive" in out
        finally:
            await shell.stop()

    asyncio.run(scenario())


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
