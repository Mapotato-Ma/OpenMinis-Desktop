"""终端抽屉的命令**不能把 WS 收帧循环堵住**。

缺陷现场（独立复核 2026-10-08）：收帧循环里是一句同步 ``await _handle_shell(...)``，
而 ``_handle_shell`` 内部逐行 await 命令输出 —— 命令没跑完，**下一帧**根本读不到。
后果：终端里跑 ``sleep 6``，0.3 秒后发 ping，pong **5.84 秒**才回来；「停止」帧同理
进不来，v0.4.18 刚修好的「停止真的能停」在这条路径上原样失效。

这里**不 mock 派发逻辑**，而是真跑 ``websocket_endpoint``：命令还在跑的时候，ping
必须立刻被读到。把 ``_dispatch_shell`` 换回 inline ``await _handle_shell(...)``，
本文件第一条测试立刻变红（实测见 commit message）。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

import pytest
from starlette.websockets import WebSocketDisconnect

from openminis.server import main as server_main

_TABLES = ("_RUNNING_CHATS", "_RUNNING_BY_CLIENT", "_RUNNING_SHELL")


class _FakeWS:
    """``websocket_endpoint`` 用到的那一点点 WebSocket：收帧排队，发帧记下来。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.cookies: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self._queue: list[str] = []
        self._closed = asyncio.Event()

    def push(self, frame: dict[str, Any]) -> None:
        self._queue.append(json.dumps(frame))

    def close_input(self) -> None:
        """让 ``receive_text`` 抛 WebSocketDisconnect（模拟客户端断开）。"""
        self._closed.set()

    async def accept(self) -> None:
        pass

    async def close(self, code: int = 1000) -> None:  # pragma: no cover - 用不到
        self._closed.set()

    async def receive_text(self) -> str:
        while True:
            if self._queue:
                return self._queue.pop(0)
            if self._closed.is_set():
                raise WebSocketDisconnect(code=1000)
            await asyncio.sleep(0.01)

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


@pytest.fixture()
def ws_env(monkeypatch):
    """一张干净的连接表 + 三张空的运行登记表（都是模块级全局）。"""
    monkeypatch.setattr(server_main, "manager", server_main.ConnectionManager())
    for name in _TABLES:
        getattr(server_main, name).clear()
    try:
        yield
    finally:
        for name in _TABLES:
            getattr(server_main, name).clear()


async def _until(pred: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


def _kinds(ws: _FakeWS) -> list[str]:
    return [str(f.get("type")) for f in ws.sent]


async def _shutdown(endpoint: asyncio.Task[Any], ws: _FakeWS) -> None:
    ws.close_input()
    try:
        await asyncio.wait_for(asyncio.gather(endpoint, return_exceptions=True), 5)
    except asyncio.TimeoutError:  # pragma: no cover - 收帧循环真卡死才会走到
        endpoint.cancel()


async def test_ping_is_answered_while_a_terminal_command_runs(ws_env, tmp_path):
    """命令跑着的时候 ping 必须马上回来，且停止帧能真的停掉它（含子孙）。

    命令故意写成 ``(sleep 2; touch m) & wait``：``touch`` 跑在**孙**进程里 ——
    只 ``proc.kill()``（不杀进程组）的话，它会在两秒后写出这个文件。
    """
    marker = tmp_path / "late.txt"
    ws = _FakeWS()
    ws.push({"type": "shell", "command": f"(sleep 2; touch {marker}) & wait"})
    ws.push({"type": "ping"})
    started = time.monotonic()
    endpoint = asyncio.create_task(server_main.websocket_endpoint(ws))
    try:
        got_pong = await _until(lambda: "pong" in _kinds(ws), 1.0)
        assert got_pong, (
            "命令还在跑的时候 ping 进不来 —— 收帧循环被 _handle_shell 堵住了"
            f"（1 秒内只收到 {_kinds(ws)}，而命令要跑 2 秒）"
        )
        assert time.monotonic() - started < 1.0, "pong 是命令跑完之后才回来的"
        assert server_main._RUNNING_SHELL, (
            "命令没登记进 _RUNNING_SHELL —— 停止/断开就找不到它"
        )

        ws.push({"type": "stop"})
        assert await _until(lambda: not server_main._RUNNING_SHELL, 2.0), (
            "停止帧没能把在跑的终端命令停掉"
        )
        assert await _until(
            lambda: any(
                f.get("type") == "error" and "停止" in str(f.get("error"))
                for f in ws.sent
            ),
            2.0,
        ), f"界面没收到「已停止」({_kinds(ws)})"
        done = [f for f in ws.sent if f.get("type") == "done"]
        assert done and done[-1].get("stopped") is True, "停了却回报 stopped=False"
    finally:
        await _shutdown(endpoint, ws)

    # 整棵进程树都得死：孙进程若活着，会在命令开始后 2 秒写下这个文件。
    left = 2.2 - (time.monotonic() - started)
    if left > 0:
        await asyncio.sleep(left)
    assert not marker.exists(), (
        "命令的**子孙**还活着（孙进程写出了文件）—— 停的只是 shell，不是进程树"
    )


async def test_shell_registration_is_cleaned_up_when_the_command_finishes(ws_env):
    """跑完要清登记（任务别泄漏），输出照样流回界面。"""
    ws = _FakeWS()
    ws.push({"type": "shell", "command": "echo OM-HELLO"})
    endpoint = asyncio.create_task(server_main.websocket_endpoint(ws))
    try:
        assert await _until(lambda: "done" in _kinds(ws), 20.0), "命令没跑完"
        text = "".join(str(f.get("text") or "") for f in ws.sent)
        assert "OM-HELLO" in text, f"输出没流回界面：{ws.sent}"
        done = [f for f in ws.sent if f.get("type") == "done"][-1]
        assert done.get("exitCode") == 0
        assert await _until(lambda: not server_main._RUNNING_SHELL, 2.0), (
            "命令跑完了还挂在 _RUNNING_SHELL 里 —— 任务登记泄漏"
        )
    finally:
        await _shutdown(endpoint, ws)


async def test_second_shell_frame_does_not_clobber_the_running_command(ws_env):
    """同一条连接重复发 shell 帧：拒绝新的，在跑的那条不受影响（前端也是这么说的）。"""
    ws = _FakeWS()
    ws.push({"type": "shell", "command": "sleep 2"})
    endpoint = asyncio.create_task(server_main.websocket_endpoint(ws))
    try:
        assert await _until(lambda: bool(server_main._RUNNING_SHELL), 5.0), "命令没跑起来"
        cid, first = next(iter(server_main._RUNNING_SHELL.items()))

        ws.push({"type": "shell", "command": "echo SECOND"})
        assert await _until(
            lambda: any(
                f.get("type") == "error" and "还在执行" in str(f.get("error"))
                for f in ws.sent
            ),
            3.0,
        ), f"重复的 shell 帧没被挡下：{ws.sent}"
        assert server_main._RUNNING_SHELL.get(cid) is first, "新帧把在跑的那条踩掉了"
        assert not first.done(), "在跑的那条被新帧取消了"
        assert not any("SECOND" in str(f.get("text") or "") for f in ws.sent), (
            "第二条命令居然也跑了 —— 两条命令的输出会互相踩"
        )
        ws.push({"type": "stop"})
        await _until(lambda: not server_main._RUNNING_SHELL, 3.0)
    finally:
        await _shutdown(endpoint, ws)


async def test_client_disconnect_kills_the_running_command(ws_env, tmp_path):
    """客户端断开 → 这条连接上在跑的命令要被取消（登记也要清掉）。"""
    marker = tmp_path / "after-disconnect.txt"
    ws = _FakeWS()
    ws.push({"type": "shell", "command": f"(sleep 2; touch {marker}) & wait"})
    started = time.monotonic()
    endpoint = asyncio.create_task(server_main.websocket_endpoint(ws))
    try:
        assert await _until(lambda: bool(server_main._RUNNING_SHELL), 5.0), "命令没跑起来"
    finally:
        await _shutdown(endpoint, ws)

    assert await _until(lambda: not server_main._RUNNING_SHELL, 3.0), (
        "连接断了，登记还挂着（命令也还在跑）"
    )
    left = 2.2 - (time.monotonic() - started)
    if left > 0:
        await asyncio.sleep(left)
    assert not marker.exists(), "断开之后命令的子孙还在跑"


async def test_stop_while_the_process_is_still_spawning_still_tells_the_ui(
    ws_env, monkeypatch
):
    """停止帧撞进「进程还没起来」的窗口时，界面也必须收到「已停止」。

    现场（CI 的 windows-latest）：Windows 上 spawn 要现找 Git Bash，几秒才起来，
    停止帧正好落进那个窗口 → 命令本来就没进程可杀，而代码只在 ``proc is not None``
    那一支回话 → **一句话都不回**，抽屉的「正在跑」永远不灭（界面像卡住）。
    这里把 spawn 人为变慢来**确定性地**复现那个窗口，不依赖平台速度。
    """
    real_spawn = asyncio.create_subprocess_shell
    entered = asyncio.Event()

    async def slow_spawn(*args, **kwargs):
        entered.set()
        await asyncio.sleep(5)   # 假装 Git Bash discovery 很慢
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", slow_spawn)
    ws = _FakeWS()
    ws.push({"type": "shell", "command": "echo hi"})
    endpoint = asyncio.create_task(server_main.websocket_endpoint(ws))
    try:
        assert await _until(lambda: bool(server_main._RUNNING_SHELL), 1.0), "命令没登记进表"
        assert await _until(entered.is_set, 1.0), "还没进到 spawn 就被别的东西挡住了"

        ws.push({"type": "stop"})
        assert await _until(
            lambda: any(
                f.get("type") == "error" and "停止" in str(f.get("error"))
                for f in ws.sent
            ),
            2.0,
        ), f"窗口期按下停止后界面没收到「已停止」({_kinds(ws)})"
        assert await _until(lambda: not server_main._RUNNING_SHELL, 2.0), "登记没清掉"
    finally:
        await _shutdown(endpoint, ws)
