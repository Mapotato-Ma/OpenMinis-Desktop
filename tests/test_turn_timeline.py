"""回合时间线：落库的顺序必须等于**流式发生**的顺序 [T-turn-timeline-order]。

现场两个毛病都出在这一条链上：

1. 流式正文重复 —— 工具卡之后的正文会把工具卡**之前**的正文再画一遍
   （前端拿整轮累计 `t.text` 去画"当前这一段"）。修在前端，见 app.js。
2. 关闭应用重开会话，所有工具执行都排在最下面 —— 落库只存得下「一整块正文 +
   一串工具卡」，正文段与卡片的**交替顺序丢了**。

这里用真实的 ``_run_chat`` 跑一遍（provider 换成桩，只把 chunk 灌进 sink），
断言落库的时间线顺序，以及生图补写的那段 `![](路径)` 确实进了时间线 ——
少了它，界面在有 timeline 时不再渲染 ``m.text``，重放时图片就没了。
"""

from __future__ import annotations

import pytest

from openminis.data.model import LLMMessage, LLMStreamChunk
from openminis.settings.store import SettingsStore

SID_KEY = "session_id"


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = SettingsStore(path=tmp_path / "settings.json")
    monkeypatch.setattr(SettingsStore, "get", classmethod(lambda cls: s))
    return s


@pytest.fixture
def env(tmp_path, monkeypatch):
    from openminis.core import context

    monkeypatch.setenv("MINIS_HOME", str(tmp_path))
    context.set_app_context(
        context.AppContext(data_dir=tmp_path, cache_dir=tmp_path)
    )
    yield tmp_path
    context._context = None


async def _async_value(value):
    return value


class _Bundle:
    prompt = "hi"
    text = "hi"
    image_parts = None


def _install_stubs(monkeypatch, script):
    """把 ``_run_chat`` 的外围依赖换掉，只留接线 + sink 被测。

    ``script`` 是喂给 sink 的 chunk 序列；运行器把这些 chunk 原样 emit，
    模拟"模型先说话 → 调工具 → 再说话"。
    """
    from openminis.agent.agent_runtime import AgentRuntimeOptions
    from openminis.server import main as server_main

    class _Provider:
        async def aclose(self):
            return None

    class _Runtime:
        async def run(self, provider, messages, session_id, options=None, **kw):
            for chunk in script:
                await self.chunk_sink(chunk)
            messages.append(LLMMessage(LLMMessage.Role.ASSISTANT, "结论如下。"))
            return messages, "end_turn"

    monkeypatch.setattr(
        server_main, "build_chat_setup",
        lambda s, **kw: (_Provider(), _Runtime(),
                         AgentRuntimeOptions(system_prompt="BASE"), {}, {}),
    )
    monkeypatch.setattr(server_main, "build_attachment_context",
                        lambda s, t: _async_value(_Bundle()))
    monkeypatch.setattr(server_main, "attribution_snapshot", lambda s, c: {})
    monkeypatch.setattr(server_main.compaction, "maybe_compact",
                        lambda *a, **k: _async_value(None))
    monkeypatch.setattr(server_main, "collect_recent_images", lambda **k: [])
    monkeypatch.setattr(server_main, "looks_like_image_generation",
                        lambda name, args: False)

    sent: list[dict] = []

    async def fake_send(_cid, payload):
        sent.append(payload)

    monkeypatch.setattr(server_main, "_safe_send", fake_send)
    server_main._RUNNING_CHATS.clear()
    return server_main, sent


async def _run(server_main, env):
    return await server_main._run_chat("c1", {
        SID_KEY: "",
        "text": "看看目录",
    })


@pytest.mark.asyncio
async def test_turn_timeline_persists_interleaved_order(env, store, monkeypatch):
    """正文段与工具卡必须**交替**落库，而不是"一整块正文 + 一串卡片"。"""
    from openminis.server import chat_store

    server_main, _sent = _install_stubs(monkeypatch, [
        LLMStreamChunk.Text("我先看看。"),
        LLMStreamChunk.ToolCallComplete("t1", "shell_execute", {"command": "ls"}),
        LLMStreamChunk.ToolResult("t1", "shell_execute", "a.txt\nb.txt", False),
        LLMStreamChunk.Text("目录里有 2 个文件。"),
    ])
    await _run(server_main, env)

    sessions = await chat_store.list_sessions()
    rows = await chat_store.load_messages(sessions[0].id)
    assistant = [r for r in rows if r.role == "assistant"][-1]
    kinds = [seg["type"] for seg in assistant.timeline]
    assert kinds == ["text", "tool", "text"], assistant.timeline
    assert assistant.timeline[0]["text"] == "我先看看。"
    assert assistant.timeline[2]["text"] == "目录里有 2 个文件。"
    # 卡片内容按 id 从 tool part 取回，没丢
    assert assistant.timeline[1]["name"] == "shell_execute"
    assert "a.txt" in assistant.timeline[1]["output"]


@pytest.mark.asyncio
async def test_image_refs_are_written_into_the_timeline(env, store, monkeypatch):
    """生图补写的 `![](路径)` 必须进时间线 —— 界面有 timeline 时不再渲染 m.text。

    漏了这步，生图回合（必然带工具调用 → 必然有 timeline）重放时图片就消失了，
    正好废掉 ``append_image_refs`` 存在的唯一理由。
    """
    from openminis.server import chat_store

    png = env / "a.png"
    png.write_bytes(b"\x89PNG")
    server_main, _sent = _install_stubs(monkeypatch, [
        LLMStreamChunk.ToolCallComplete("t1", "image_gen", {"prompt": "一只猫"}),
        LLMStreamChunk.ToolResult("t1", "image_gen", "ok", False),
        LLMStreamChunk.Text("画好了。"),
    ])
    # 这一轮确实是"生图调用"：工具名与返回的新图都摆上
    monkeypatch.setattr(server_main, "looks_like_image_generation",
                        lambda name, args: True)
    monkeypatch.setattr(server_main, "collect_recent_images", lambda **k: [str(png)])
    await _run(server_main, env)

    sessions = await chat_store.list_sessions()
    rows = await chat_store.load_messages(sessions[0].id)
    assistant = [r for r in rows if r.role == "assistant"][-1]

    joined = "".join(
        seg["text"] for seg in assistant.timeline if seg["type"] == "text"
    )
    assert "a.png" in joined, assistant.timeline
    assert "a.png" in assistant.text          # 模型上下文那份也还在
