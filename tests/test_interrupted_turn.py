"""R10：用户按停止之后，这一轮已经积累的内容必须落库。

改前的取消路径：``CancelledError`` 从 ``runtime.run`` 传出来，``finally`` 只收子回合
与 emitter，**在那之后**的"把最终正文 append_turn"整段被跳过 —— 用户屏幕上明明看到了
回答，刷新/切会话之后那段历史没了（实时与历史不一致）。

落库要带一个标记：界面据它显示「已中断」。标记用 ``parts_to_text`` 不认识的 part 类型，
所以**模型上下文一个字节都不变**（这条也在这里钉住）。
"""
from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from openminis.core import context

    monkeypatch.setenv("MINIS_HOME", str(tmp_path))
    context.set_app_context(context.AppContext(data_dir=tmp_path, cache_dir=tmp_path))
    yield tmp_path
    context._context = None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_cancelled_turn_keeps_what_it_had(home, monkeypatch):
    from openminis.data.model import LLMMessage
    from openminis.server import chat_store
    from openminis.server import main as server_main

    session = await chat_store.create_session()
    partial = "前半段已经写在屏幕上了"

    class _Provider:
        async def aclose(self) -> None:  # pragma: no cover - 不该被调用（R14）
            raise AssertionError("回合不该关共享 provider")

    class _Runtime:
        async def run(self, provider, messages, session_id, options=None, **kw):  # noqa: ANN001, ANN003, ANN202
            # 模拟"流到一半用户按了停止"：正文已经在消息列表里了
            messages.append(LLMMessage(LLMMessage.Role.ASSISTANT, partial))
            raise asyncio.CancelledError

    monkeypatch.setattr(
        server_main, "build_chat_setup",
        lambda store, **kw: (_Provider(), _Runtime(), object(), None, {}),
    )
    sent: list[dict] = []

    async def _fake_send(_cid: str, payload: dict) -> None:
        sent.append(payload)

    monkeypatch.setattr(server_main, "_safe_send", _fake_send)
    server_main._RUNNING_CHATS.clear()

    with pytest.raises(asyncio.CancelledError):
        await server_main._run_chat("c1", {"text": "帮我写点东西", "session_id": session.id})

    rows = await chat_store.load_messages(session.id)
    assistant = [r for r in rows if r.role == "assistant"]
    assert assistant, "被中断的那一轮整段没落库（R10 又回来了）"
    assert partial in assistant[-1].text, "落库了，但已积累的正文丢了"
    assert assistant[-1].interrupted is True, "没有标『已中断』—— 界面会以为它正常跑完了"

    # 标记不许污染模型上下文（parts_to_text 只认 text part）
    raw = await chat_store.load_runtime_history(session.id)
    for msg in raw:
        assert "interrupted" not in (msg.content or "")


@pytest.mark.asyncio
async def test_the_interrupted_marker_is_a_part_the_model_never_sees(isolated_chat_db):
    from openminis.server import chat_store

    session = await chat_store.create_session()
    await chat_store.append_turn(session.id, "assistant", "半句话", interrupted=True)

    rows = await chat_store.load_messages(session.id)
    assert rows[-1].interrupted is True
    assert rows[-1].text == "半句话"
    # parts_json 里确实有那个标记（界面靠它），而 parts_to_text 不认识它
    assert json.loads(chat_store._text_parts("x"))  # noqa: SLF001 - 只是确保函数在
    raw = await chat_store.load_runtime_history(session.id)
    assert raw[-1].content == "半句话"
