"""WebSocket 控制帧：生成中「暂停」（stop）。

对应前端发送键旁边的 ⏹：一轮对话跑在后台 task 里，所以 stop 帧能在本轮
生成过程中被读到并取消它；取消后由 stop 处理函数回一个
``{"type": "done", "stopped": true}``。
"""

from __future__ import annotations

import asyncio

import pytest

from openminis.core import context
from openminis.server import chat_store  # noqa: F401  (db init consistency)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIS_HOME", str(tmp_path))
    context.set_app_context(context.AppContext(data_dir=tmp_path, cache_dir=tmp_path))
    # 两张运行登记表都是模块级全局 —— 不清理就会串到后面的测试里
    # （原先只有 _RUNNING_CHATS，2026-10-08 加了按连接的兜底表）。
    from openminis.server import main as server_main

    server_main._RUNNING_CHATS.clear()
    server_main._RUNNING_BY_CLIENT.clear()
    yield tmp_path
    server_main._RUNNING_CHATS.clear()
    server_main._RUNNING_BY_CLIENT.clear()
    context._context = None


def test_stop_cancels_running_turn(env, monkeypatch):
    from openminis.server import main as server_main

    async def scenario() -> None:
        cancelled = asyncio.Event()

        async def stuck() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        sent: list[dict] = []

        async def fake_send(_cid: str, payload: dict) -> None:
            sent.append(payload)

        monkeypatch.setattr(server_main, "_safe_send", fake_send)
        server_main._RUNNING_CHATS.clear()
        task = asyncio.create_task(stuck())
        await asyncio.sleep(0)  # 让它真的跑起来
        server_main._RUNNING_CHATS["s1"] = task
        task.add_done_callback(lambda _t: server_main._RUNNING_CHATS.pop("s1", None))

        await server_main._handle_stop("c1", {"sessionId": "s1"})

        assert cancelled.is_set(), "运行中的轮次应被取消"
        assert sent and sent[-1]["type"] == "done"
        assert sent[-1]["stopped"] is True
        assert sent[-1]["sessionId"] == "s1"
        await asyncio.sleep(0)
        assert "s1" not in server_main._RUNNING_CHATS
        server_main._RUNNING_CHATS.clear()

    asyncio.run(scenario())


def test_stop_without_running_turn_still_acks(env, monkeypatch):
    from openminis.server import main as server_main

    async def scenario() -> None:
        sent: list[dict] = []

        async def fake_send(_cid: str, payload: dict) -> None:
            sent.append(payload)

        monkeypatch.setattr(server_main, "_safe_send", fake_send)
        server_main._RUNNING_CHATS.clear()
        await server_main._handle_stop("c1", {"sessionId": "nope"})
        # 没有在跑的轮次**仍要回 done**（前端据此把 busy 关掉），
        # 但不能再谎报 stopped=True —— 2026-10-06 之前这条断言写的就是 True，
        # 等于把「谎报」钉死在测试里，界面分不清「真停掉了」和「根本没事在跑」。
        assert sent == [{"type": "done", "sessionId": "nope", "stopped": False}]

    asyncio.run(scenario())


def test_duplicate_chat_frame_is_rejected_while_running(env, monkeypatch):
    """同一会话已有轮次在跑 → 直接拒绝（前端负责排队）。"""
    from openminis.server import main as server_main

    async def scenario() -> None:
        sent: list[dict] = []

        async def fake_send(_cid: str, payload: dict) -> None:
            sent.append(payload)

        monkeypatch.setattr(server_main, "_safe_send", fake_send)
        server_main._RUNNING_CHATS.clear()
        running = asyncio.create_task(asyncio.sleep(5))
        server_main._RUNNING_CHATS["s2"] = running
        try:
            await server_main._handle_chat(
                "c1", {"text": "再来一条", "sessionId": "s2"}
            )
            assert sent and sent[-1]["type"] == "error"
            assert "处理中" in sent[-1]["error"]
            assert not running.done()
        finally:
            running.cancel()
            await asyncio.wait({running})
            server_main._RUNNING_CHATS.clear()

    asyncio.run(scenario())

def test_stop_without_session_id_falls_back_to_this_client(env, monkeypatch):
    """没带 session_id 时按**连接**兜底：这条连接上真在跑的那一轮要被停掉。

    背景（2026-10-08 真机日志）：新会话的第一条消息**没有 session_id**（id 由
    服务端在 ``_run_chat`` 里创建、随 chatSession 帧回给界面）。旧实现只在 sid
    非空时登记，于是那一轮根本没进表 —— 界面随后从帧里学到 id、用户按停止，
    服务端回 "stop requested but nothing is running"，模型继续跑，直到用户
    重启 App。所以：按 id 找不到就**按连接**找，并且如实回报。
    """
    from openminis.server import main as server_main

    async def scenario() -> None:
        cancelled = asyncio.Event()

        async def stuck() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        sent: list[dict] = []

        async def fake_send(_cid: str, payload: dict) -> None:
            sent.append(payload)

        monkeypatch.setattr(server_main, "_safe_send", fake_send)
        monkeypatch.setattr(
            server_main, "_interrupt_session_shell", lambda sid: "stub"
        )
        server_main._RUNNING_CHATS.clear()
        server_main._RUNNING_BY_CLIENT.clear()
        task = asyncio.create_task(stuck())
        await asyncio.sleep(0)
        # 模拟 _run_chat 解析出 id 之后的登记（id + 连接两份）
        server_main._register_running("s1", task, "c1")

        try:
            await server_main._handle_stop("c1", {})  # 不带 id

            assert cancelled.is_set(), "这条连接上在跑的那一轮没被停掉"
            assert sent, "什么都没回"
            assert sent[-1]["type"] == "done"
            assert sent[-1]["stopped"] is True, "真停掉了就该说 True"
            # 回话里的 id 用登记里的那个，界面才能对上会话
            assert sent[-1]["sessionId"] == "s1"

            # 既没在跑、也不属于这条连接：如实说 stopped=False，不谎报 True
            sent.clear()
            await server_main._handle_stop("c9", {"session_id": "nothing-here"})
            assert sent[-1]["type"] == "done"
            assert sent[-1]["stopped"] is False, "没有任务却说停掉了"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            server_main._RUNNING_CHATS.clear()
            server_main._RUNNING_BY_CLIENT.clear()

    asyncio.run(scenario())


def test_stop_kills_the_shell_and_dismisses_confirmations(env, monkeypatch):
    """停止要**真的杀掉正在跑的命令**，并把挂着等用户点的确认框收掉。

    只 cancel() 协程是不够的：命令在会话级持久 shell 里继续跑（现场实测
    「按了停止，工具还在继续执行」），一次性兜底还占着工作线程。另外确认框
    若没人回答，界面会一直挂着而等它的工具早已取消 —— 点下去毫无反应。
    """
    from openminis.server import main as server_main

    async def scenario() -> None:
        sent: list[dict] = []
        seen: dict[str, str] = {}

        async def fake_send(_cid: str, payload: dict) -> None:
            sent.append(payload)

        monkeypatch.setattr(server_main, "_safe_send", fake_send)
        monkeypatch.setattr(
            server_main,
            "_interrupt_session_shell",
            lambda sid: seen.setdefault("sid", sid) or "已终止进程树",
        )
        server_main._RUNNING_CHATS.clear()
        server_main._RUNNING_BY_CLIENT.clear()
        server_main._PENDING_CONFIRM.clear()
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        server_main._PENDING_CONFIRM["gq-1"] = fut
        task = asyncio.create_task(asyncio.sleep(30))
        await asyncio.sleep(0)
        server_main._register_running("s7", task, "c1")

        try:
            await server_main._handle_stop("c1", {"sessionId": "s7"})
            assert seen.get("sid") == "s7", "没去杀这个会话的 shell"
            assert fut.done() and fut.result() == "deny", (
                "挂着的沙箱确认框应该按『拒绝』结掉"
            )
            assert sent[-1]["stopped"] is True
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            server_main._RUNNING_CHATS.clear()
            server_main._RUNNING_BY_CLIENT.clear()
            server_main._PENDING_CONFIRM.clear()

    asyncio.run(scenario())


def test_first_turn_of_a_new_session_is_registered_under_its_id(env, monkeypatch):
    """新会话首轮必须在 **sid 产生之后**补登记 —— 这就是"停不下来"的根因。

    现场（2026-10-08）：``_handle_chat`` 只在帧里带了 session_id 时才登记，而新
    会话的首条消息**没有 id**（id 是 ``_run_chat`` 里创建的），于是那一轮根本没
    进表。界面随后从 chatSession 帧学到 id，用户按停止 → "nothing is running"，
    模型继续跑，直到用户重启 App。

    做法：让 ``build_chat_setup`` 在"登记应该已经发生"的那一刻抛错并抓现场快照 ——
    断言的是**真实登记状态**，不是源码里有没有那行字。
    """
    from openminis.server import main as server_main

    async def scenario() -> None:
        seen: dict = {}

        def _snapshot_setup(*_a, **_k):
            seen["running"] = dict(server_main._RUNNING_CHATS)
            seen["by_client"] = dict(server_main._RUNNING_BY_CLIENT)
            seen["current"] = asyncio.current_task()
            raise server_main.ChatSetupError("到此为止（测试只关心登记）")

        async def fake_send(_cid: str, payload: dict) -> None:
            pass

        monkeypatch.setattr(server_main, "_safe_send", fake_send)
        monkeypatch.setattr(server_main, "build_chat_setup", _snapshot_setup)
        server_main._RUNNING_CHATS.clear()
        server_main._RUNNING_BY_CLIENT.clear()

        await server_main._handle_chat("c1", {"text": "你好"})  # 不带 session_id
        # 让后台那一轮真的跑起来（它会在 build_chat_setup 处抓快照然后结束）
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.01)
        assert seen, "首轮根本没跑起来"

        running = seen.get("running") or {}
        assert running, (
            "首轮没登记进 _RUNNING_CHATS —— 界面稍后带着 id 来停止会查不到"
        )
        (sid, task), = running.items()
        assert sid and sid == seen["by_client"]["c1"][0], "登记的 id 对不上"
        assert task is seen["current"], "登记的必须是当前这一轮"

    asyncio.run(scenario())


def test_interrupt_session_shell_uses_the_db_prefixed_key(env, monkeypatch):
    """杀 shell 用的 key 必须是 ``db-<sid>``。

    工具侧的 session_id 是 ``db-<sid>``（见 main.py 的 ``set_session_cwd``/
    ``runtime.run``），协调器里的 shell 就是按这个 key 存的 —— 写成裸 sid
    会静默地什么都不杀（而不是报错），这正是最难查的那类 bug。
    """
    import openminis.tools.shell_execute_tool as shell_tool

    seen: dict[str, str] = {}

    class _FakeCoordinator:
        def interrupt(self, key: str) -> str:
            seen["key"] = key
            return "已终止进程树 (pid=1)"

    monkeypatch.setattr(shell_tool, "get_coordinator", lambda: _FakeCoordinator())
    from openminis.server import main as server_main

    assert server_main._interrupt_session_shell("abc123") == "已终止进程树 (pid=1)"
    assert seen["key"] == "db-abc123"
    assert server_main._interrupt_session_shell("") == ""
