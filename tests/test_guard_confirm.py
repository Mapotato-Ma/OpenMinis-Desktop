"""沙箱拦截改「当场问用户」+ 危险模式（2026-10-06 用户第 2、3 件）。

用户原话：
  「沙箱拦截不要直接硬拦，执行命令时增加一个工具来请求用户确认。用户确认执行就不要拦了。」
  「增加一个危险模式，切换到危险模式之后沙箱不要拦任何东西。」

这条测试要钉住三件事：
1. **危险模式真的一条都不拦**，但仍然进实时流水（放行也要留痕，否则开了危险模式就查不到
   agent 跑过什么了）；
2. **用户放行之后不再拦**（本次会话 / 永久 两种 scope 都要真的写进白名单）；
3. **没人可问时行为不变**（纯 CLI 场景照旧硬拦，不许把 agent 挂住）。
"""

from __future__ import annotations

import asyncio

import pytest

from openminis.core import context
from openminis.sandbox import confirm
from openminis.sandbox.guard import DANGER_MODE, NORMAL_MODE, check_shell_command, guard
from openminis.server import chat_store  # noqa: F401  (db init consistency)
from openminis.tools.shell_execute_tool import _ask_user_to_allow


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


def _blocked_command() -> str:
    """一条一定会被拦的命令（越界删除）。

    ``assert`` 是自检：万一守卫规则改了、这条不再被拦，测试要**当场说出来**，
    而不是静默地全部通过。
    """
    cmd = "rm -rf C:/Windows/System32/important"
    assert check_shell_command(cmd, session_id="s1", cwd="C:/work"), "这条本该被拦"
    return cmd


# ── 危险模式 ────────────────────────────────────────────────────────────
def test_default_mode_is_normal(env):
    assert guard.mode() == NORMAL_MODE


def test_danger_mode_blocks_nothing(env):
    cmd = _blocked_command()
    guard.set_mode(DANGER_MODE)
    assert guard.mode() == DANGER_MODE
    assert check_shell_command(cmd, session_id="s1", cwd="C:/work") is None


def test_danger_mode_still_leaves_a_trace(env):
    """放行也要留痕 —— 否则用户开了危险模式就再也查不到 agent 跑过什么。"""
    guard.set_mode(DANGER_MODE)
    check_shell_command("rm -rf C:/Windows/System32/important", session_id="s1", cwd="C:/work")
    live = guard.live(20)
    assert any(row.get("family") == "danger" for row in live), live


def test_danger_mode_survives_a_restart(env):
    """模式要落盘：进程重启后不能悄悄退回「照常拦」。"""
    guard.set_mode(DANGER_MODE)
    guard.reset()  # 丢掉内存状态，模拟重启
    assert guard.mode() == DANGER_MODE, "危险模式没落盘 —— 重启后会偷偷变回拦截模式"


def test_switching_back_to_normal_restores_blocking(env):
    cmd = _blocked_command()
    guard.set_mode(DANGER_MODE)
    assert check_shell_command(cmd, session_id="s1", cwd="C:/work") is None
    guard.set_mode(NORMAL_MODE)
    assert check_shell_command(cmd, session_id="s1", cwd="C:/work") is not None


# ── 问话通道 ────────────────────────────────────────────────────────────
def test_ask_without_asker_is_unavailable(env):
    """没有客户端（纯 CLI）时不许挂住 —— 直接告诉调用方「没人可问」。"""
    assert asyncio.run(confirm.ask({"command": "x"})) == "unavailable"


def test_ask_times_out_instead_of_hanging(env, monkeypatch):
    monkeypatch.setattr(confirm, "TIMEOUT_SECONDS", 0.05)

    async def never(_payload):
        await asyncio.sleep(30)
        return "once"

    confirm.set_asker(never)
    assert asyncio.run(confirm.ask({"command": "x"})) == "timeout"


# ── 放行之后不要再拦 ────────────────────────────────────────────────────
def _answer(decision: str):
    async def asker(_payload):
        return decision

    confirm.set_asker(asker)


def test_allow_once_lets_this_call_through_but_not_the_next(env):
    cmd = _blocked_command()
    _answer("once")
    blocked = check_shell_command(cmd, session_id="s1", cwd="C:/work")
    assert blocked is not None
    out = asyncio.run(
        _ask_user_to_allow(blocked, command=cmd, session_id="s1", cwd="C:/work")
    )
    assert out is None, "用户放行了，工具却还是把命令拦住了"
    # once = 只放行这一次，同类命令下一次仍然要拦
    assert check_shell_command(cmd, session_id="s1", cwd="C:/work") is not None


def test_allow_session_is_remembered(env):
    """「本次会话允许」之后同类命令不再拦 —— 这正是用户要的「确认执行就不要拦了」。"""
    cmd = _blocked_command()
    _answer("session")
    blocked = check_shell_command(cmd, session_id="s1", cwd="C:/work")
    out = asyncio.run(
        _ask_user_to_allow(blocked, command=cmd, session_id="s1", cwd="C:/work")
    )
    assert out is None
    assert check_shell_command(cmd, session_id="s1", cwd="C:/work") is None


def test_deny_keeps_the_block_and_says_so(env):
    cmd = _blocked_command()
    _answer("deny")
    blocked = check_shell_command(cmd, session_id="s1", cwd="C:/work")
    out = asyncio.run(
        _ask_user_to_allow(blocked, command=cmd, session_id="s1", cwd="C:/work")
    )
    assert out is not None, "用户拒绝了，命令却还是放行了"
    assert "拒绝" in out, "回给模型的文案里没说清是被拒绝的"
    assert check_shell_command(cmd, session_id="s1", cwd="C:/work") is not None


def test_unavailable_asker_falls_back_to_the_old_hard_block(env):
    """没人可问 → 行为与加这个功能之前一模一样。"""
    cmd = _blocked_command()
    confirm.set_asker(None)
    blocked = check_shell_command(cmd, session_id="s1", cwd="C:/work")
    out = asyncio.run(
        _ask_user_to_allow(blocked, command=cmd, session_id="s1", cwd="C:/work")
    )
    assert out == blocked
