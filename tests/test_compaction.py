"""20-turn compaction + memory extraction.

Compaction folds older turns into a ``compact_markers`` summary and distils
durable facts into the daily memory log. These tests drive it with a stub
provider so no real model is ever called.
"""

from __future__ import annotations

import pytest

from openminis.core import context
from openminis.data.model import LLMStreamChunk
from openminis.server import chat_store, compaction


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated data dir + throwaway chat database."""
    monkeypatch.setenv("MINIS_HOME", str(tmp_path))
    context.set_app_context(context.AppContext(data_dir=tmp_path, cache_dir=tmp_path))
    chat_store.set_database_path(tmp_path / "chat.db")
    yield tmp_path
    chat_store.set_database_path(None)
    context._context = None


class StubProvider:
    """Records the prompt it was given and yields one canned text chunk."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.transcripts: list[str] = []

    async def stream_message(self, messages, system_prompt, max_tokens, temperature,
                             tools=None, thinking_level=None):
        self.transcripts.append(messages[-1].content if messages else "")
        yield LLMStreamChunk.Text(self.reply)


REPLY = """===SUMMARY===
用户在移植 OpenMinis，已完成 config 与 data 模块，当前在补 server 层。
===MEMORIES===
- 项目：OpenMinis Kotlin→Python 移植，工作区在 data_dir/workspace
- 约定：测试用 uv 跑，隔离依赖 MINIS_HOME
"""


async def _seed(session_id: str, turns: int) -> None:
    for i in range(turns):
        await chat_store.append_turn(session_id, "user", f"用户第 {i} 轮")
        await chat_store.append_turn(session_id, "assistant", f"助手第 {i} 轮")


@pytest.mark.asyncio
async def test_turns_since_last_compact_counts_user_turns(env):
    await chat_store.ensure_db()
    sid = (await chat_store.create_session()).id
    await _seed(sid, 3)
    assert await compaction.turns_since_last_compact(sid) == 3


@pytest.mark.asyncio
async def test_maybe_compact_waits_for_the_threshold(env):
    await chat_store.ensure_db()
    sid = (await chat_store.create_session()).id
    await _seed(sid, compaction.COMPACT_EVERY_TURNS - 1)
    provider = StubProvider(REPLY)
    assert await compaction.maybe_compact(sid, provider) is None
    assert provider.transcripts == []
    assert await chat_store.latest_compact_marker(sid) is None


@pytest.mark.asyncio
async def test_compact_writes_marker_and_memories(env):
    await chat_store.ensure_db()
    sid = (await chat_store.create_session()).id
    await _seed(sid, compaction.COMPACT_EVERY_TURNS)

    provider = StubProvider(REPLY)
    result = await compaction.maybe_compact(sid, provider)

    assert result is not None and result.applied
    # 20 turns minus the 2 most recent messages kept verbatim
    assert result.compacted == compaction.COMPACT_EVERY_TURNS * 2 - 2
    assert result.memories == (
        "项目：OpenMinis Kotlin→Python 移植，工作区在 data_dir/workspace",
        "约定：测试用 uv 跑，隔离依赖 MINIS_HOME",
    )
    # The model saw the folded region only.
    assert "用户第 0 轮" in provider.transcripts[0]
    assert "用户第 19 轮" not in provider.transcripts[0]

    marker = await chat_store.latest_compact_marker(sid)
    assert marker is not None
    assert marker.version == 2
    assert marker.summary.startswith("用户在移植 OpenMinis")
    assert marker.last_compacted_message_id

    daily = (env / "memory" / "daily").glob("*.md")
    bodies = "\n".join(p.read_text(encoding="utf-8") for p in daily)
    assert "会话压缩记忆提取" in bodies
    assert "约定：测试用 uv 跑" in bodies


@pytest.mark.asyncio
async def test_history_starts_at_marker_with_summary(env):
    await chat_store.ensure_db()
    sid = (await chat_store.create_session()).id
    await _seed(sid, compaction.COMPACT_EVERY_TURNS)
    await compaction.compact_session(sid, provider=StubProvider(REPLY))

    history = await chat_store.load_runtime_history(sid)
    assert history[0].content.startswith(chat_store.COMPACT_SUMMARY_PREFIX)
    assert "用户在移植 OpenMinis" in history[0].content
    joined = "\n".join(m.content for m in history)
    # folded away: the oldest turn, kept: the newest one
    assert "用户第 0 轮" not in joined
    assert "用户第 19 轮" in joined


@pytest.mark.asyncio
async def test_compact_skips_when_region_is_tiny(env):
    await chat_store.ensure_db()
    sid = (await chat_store.create_session()).id
    await chat_store.append_turn(sid, "user", "只有一轮")
    result = await compaction.compact_session(sid, provider=StubProvider(REPLY))
    assert not result.applied
    assert result.skipped


# ---------------------------------------------------------------------------
# 出站脱敏：压缩 transcript 同样是外发给厂商的 prompt（PORT-FIX:
# compaction-redaction）。压缩的输入是 chat_store.load_raw_messages —— 库里的
# 原文，用户粘过的密钥就在里面；不脱敏就是全自动明文外发。
# ---------------------------------------------------------------------------
#: 形态与真实 OpenAI 风格密钥一致（``sk-`` + 28 位），不命中任何"路径/文件名"豁免。
PLAINTEXT_KEY = "sk-live7Q2mN8vX4zR6tY1pK9wB3dF5"


@pytest.mark.asyncio
async def test_compaction_redacts_plaintext_credentials(env):
    """压缩 prompt 里不能有库里的明文密钥；库里原文不被动；留一条拦截事件。"""
    from openminis.sandbox.guard import guard

    guard.reset()
    await chat_store.ensure_db()
    sid = (await chat_store.create_session()).id
    await chat_store.append_turn(
        sid, "user", f"这是我的 key，帮我配一下：{PLAINTEXT_KEY}"
    )
    await chat_store.append_turn(sid, "assistant", "收到")
    await _seed(sid, compaction.COMPACT_EVERY_TURNS - 1)

    provider = StubProvider(REPLY)
    result = await compaction.maybe_compact(sid, provider)

    assert result is not None and result.applied, "压缩没跑，下面的断言没有意义"
    prompt = provider.transcripts[0]

    # ① 明文本身一个字符都没有出去（不是"某个词没出现"这种弱断言）
    assert PLAINTEXT_KEY not in prompt
    assert "sk-live" not in prompt
    # ② 替成了部分显示，说明是"遮"而不是"整段删掉"
    assert "「已拦截」" in prompt
    # ③ 其余正文照常发给模型（没有把整个 transcript 干掉）
    assert "用户第 0 轮" in prompt
    assert "用户第 1 轮" in prompt

    # ④ 库里原文保留：压缩只折 context，不动数据
    rows = await chat_store.load_raw_messages(sid)
    assert any(
        PLAINTEXT_KEY in chat_store.parts_to_text(r.parts_json) for r in rows
    )

    # ⑤ 记录了一条 outbound 出站拦截事件（沙箱页可追溯）
    events = [e for e in guard.events(family="secret") if e.tool == "outbound:llm"]
    assert events, "压缩外发明文凭据却没有记录 guard 拦截事件"
    assert events[0].session_id == sid
    assert "[llm]" in events[0].output
    assert PLAINTEXT_KEY not in events[0].output  # 事件里也只留部分显示
    guard.reset()


@pytest.mark.asyncio
async def test_compaction_survives_a_broken_redactor(env, monkeypatch):
    """脱敏自己炸了也不能中断压缩 —— 漏一次拦截可以，挡住用户的回合不行。"""
    import openminis.sandbox.guard as guard_mod

    def _boom(*_args, **_kwargs):
        raise RuntimeError("redactor exploded")

    monkeypatch.setattr(guard_mod, "sanitize_message_parts", _boom)
    await chat_store.ensure_db()
    sid = (await chat_store.create_session()).id
    await _seed(sid, compaction.COMPACT_EVERY_TURNS)

    provider = StubProvider(REPLY)
    result = await compaction.compact_session(sid, provider=provider)

    assert result.applied
    assert "用户第 0 轮" in provider.transcripts[0]


@pytest.mark.asyncio
async def test_compact_survives_a_silent_model(env):
    await chat_store.ensure_db()
    sid = (await chat_store.create_session()).id
    await _seed(sid, compaction.COMPACT_EVERY_TURNS)
    result = await compaction.compact_session(sid, provider=StubProvider(""))
    assert not result.applied
    assert await chat_store.latest_compact_marker(sid) is None
