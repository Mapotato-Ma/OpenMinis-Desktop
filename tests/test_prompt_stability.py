"""提示前缀必须逐字节稳定 —— 缓存命中率的全部机制都压在这一条上。

提示缓存（Anthropic 的 ``cache_control``、OpenAI/DeepSeek 的自动前缀缓存）只认
**最长公共前缀**。任何"每轮都变"的东西只要出现在前缀里，从它开始往后全部按未命中
计费；而系统提示 + 工具 schema + 历史正好就是那个前缀。

改前有两条每轮都变的东西在系统提示里：分钟级的当前时间、以及客户端每轮现传的群成员
块 —— 于是跨过一分钟，整段历史就重算一次。这里把它钉住。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """把数据目录挪到 tmp：技能清单/设置都不碰真实的用户目录。"""
    from openminis.core import context

    monkeypatch.setenv("MINIS_HOME", str(tmp_path))
    context.set_app_context(context.AppContext(data_dir=tmp_path, cache_dir=tmp_path))
    yield tmp_path
    context._context = None  # type: ignore[attr-defined]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from openminis.settings.store import SettingsStore

    settings = SettingsStore(path=tmp_path / "settings.json")
    monkeypatch.setattr(SettingsStore, "get", classmethod(lambda cls: settings))
    return settings


# ---------------------------------------------------------------------------
# 系统提示：一天之内逐字节不变
# ---------------------------------------------------------------------------
def test_the_date_block_does_not_change_between_two_minutes(home, monkeypatch):
    """两分钟之后重建的日期块必须与两分钟前一模一样（改前它带着时分，每轮都变）。"""
    import datetime as datetime_module

    from openminis.settings.chat_service import current_date_block

    moment = datetime(2026, 10, 11, 21, 30)

    class _Frozen(datetime_module.datetime):
        _moment = moment

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN003, ARG003
            return cls._moment

    # `current_date_block` 里是函数内 `from datetime import datetime` —— 换掉模块属性
    # 就生效了（datetime 是 C 类型，不能直接往它身上挂属性）。
    monkeypatch.setattr(datetime_module, "datetime", _Frozen)
    first = current_date_block()
    _Frozen._moment = moment + timedelta(minutes=2)
    second = current_date_block()

    assert first == second, "系统提示在一天之内不许变（这正是缓存命中的前提）"
    assert "2026-10-11" in first


def test_identity_prompt_has_no_clock_but_keeps_the_date(store):
    """整个系统提示（身份 + 纪律 + 技能清单）里不许出现 "现在是 … HH:MM"。"""
    import re

    from openminis.settings.chat_service import identity_system_prompt

    prompt = identity_system_prompt(store)

    assert datetime.now().strftime("%Y-%m-%d") in prompt, "日期还是要有（模型没有时钟）"
    assert not re.search(r"\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}", prompt), (
        "系统提示里出现了时分 —— 前缀会每分钟变一次，缓存全部失效"
    )


# ---------------------------------------------------------------------------
# 每轮注记：发给模型的那份 == 落库的那份（否则重建历史就换了字节）
# ---------------------------------------------------------------------------
async def test_rebuilt_history_is_byte_identical_to_what_was_sent(isolated_chat_db):
    """下一轮重建出来的用户消息，必须与上一轮真正发出去的**逐字节相同**。"""
    from openminis.server import chat_store
    from openminis.settings.chat_service import turn_note_block

    session = await chat_store.create_session()
    sent = "帮我看下这个文件" + turn_note_block()

    await chat_store.append_turn(session.id, "user", sent)
    chat_store.drop_runtime(session.id)          # 模拟重启/切会话后的重建
    rebuilt = await chat_store.load_runtime_history(session.id)

    assert rebuilt, "重建后一条都没有？"
    assert rebuilt[-1].content == sent, "重建出来的字节和发出去的不一样 —— 缓存必失效"


async def test_the_note_is_stripped_for_the_ui_but_not_for_the_model(isolated_chat_db):
    """界面看用户自己打的那句话；模型上下文里必须留着注记（保前缀稳定）。"""
    from openminis.server import chat_store
    from openminis.settings.chat_service import turn_note_block

    session = await chat_store.create_session()
    await chat_store.append_turn(session.id, "user", "今天有什么新闻" + turn_note_block())

    shown = await chat_store.load_messages(session.id)
    assert shown[-1].text == "今天有什么新闻", shown[-1].text

    raw = await chat_store.load_runtime_history(session.id)
    assert "【本轮注记】" in raw[-1].content


async def test_session_title_ignores_the_turn_note(isolated_chat_db):
    """会话标题来自用户那句话，不该变成「【当前时间】2026-10-11 21:30…」。"""
    from openminis.server import chat_store
    from openminis.settings.chat_service import turn_note_block

    session = await chat_store.create_session()
    await chat_store.append_turn(session.id, "user", "帮我查一下天气" + turn_note_block())

    refreshed = await chat_store.get_session(session.id)
    assert refreshed is not None and refreshed.title == "帮我查一下天气", refreshed


def test_strip_turn_notes_keeps_a_user_text_that_mentions_the_marker():
    """用户自己打的字里出现同样的字样时，只切**最后一个**标记之后的部分。"""
    from openminis.server.chat_store import strip_turn_notes

    text = ("【本轮注记】这四个字是我自己打的\n\n"
            "【本轮注记】\n【当前时间】2026-10-11 21:30")
    assert strip_turn_notes(text) == "【本轮注记】这四个字是我自己打的"
    assert strip_turn_notes("没有注记") == "没有注记"
    assert strip_turn_notes("") == ""


def test_group_members_travel_in_the_note_not_in_the_system_prompt():
    """群成员块走尾部注记 —— 留在系统提示里就会让前缀每轮都变。"""
    from openminis.settings.chat_service import turn_note_block

    note = turn_note_block(group="【群聊成员】\n- `writer`（写手✍️）")

    assert "群聊成员" in note and "writer" in note
    assert note.index("【本轮注记】") < note.index("群聊成员"), "注记标记要在最前"


# ---------------------------------------------------------------------------
# Anthropic：1 小时 TTL 是接通的，且能在设置里切回 5 分钟
# ---------------------------------------------------------------------------
def test_anthropic_ttl_defaults_to_one_hour():
    from openminis.provider.anthropic.anthropic_provider import AnthropicProvider

    cache = AnthropicProvider(api_key="k")._ephemeral_cache_control()  # noqa: SLF001

    assert cache == {"type": "ephemeral", "ttl": "1h"}, cache


def test_anthropic_ttl_can_be_switched_back_to_five_minutes():
    from openminis.provider.anthropic.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(api_key="k", prompt_cache_ttl="5m")

    assert provider._ephemeral_cache_control() == {"type": "ephemeral"}  # noqa: SLF001
    assert provider.enhanced_cache is False


def test_provider_cache_ttl_reaches_the_provider_and_the_fingerprint(home):
    """设置里那个 TTL 要真的传到 provider 上，而且改了要换实例（不然改了没反应）。"""
    from openminis.settings.chat_service import build_provider, reset_provider_cache

    reset_provider_cache()
    conf = {"id": "anthropic", "type": "anthropic", "apiKey": "k", "baseUrl": "",
            "model": "claude-sonnet-4-5", "promptCacheTtl": "5m"}
    provider = build_provider("anthropic", conf)
    assert provider.enhanced_cache is False, "设置里的 5m 没生效"
    assert build_provider("anthropic", conf) is provider, "同配置应复用实例（连池要活着）"

    other = build_provider("anthropic", {**conf, "promptCacheTtl": "1h"})
    assert other is not provider, "改了 TTL 还在复用旧实例 —— 界面上改了没反应"
    assert other.enhanced_cache is True
    reset_provider_cache()


# ---------------------------------------------------------------------------
# 附件的 modified 用文件真实 mtime
# ---------------------------------------------------------------------------
def test_attachment_modified_uses_the_file_mtime(tmp_path):
    """改前写的是「组装这段 XML 的时刻」——语义不对，也让同一条消息的字节不稳。"""
    import os
    import time as _time

    from openminis.settings import attachments

    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")
    stamp = _time.mktime((2026, 3, 4, 5, 6, 7, 0, 0, -1))
    os.utime(shot, (stamp, stamp))
    ref = attachments.AttachmentRef(alt="shot", src=str(shot), kind="image",
                                    host_path=Path(shot), size=3)

    xml = attachments._attached_files_xml([ref])  # noqa: SLF001

    assert 'modified="2026-03-04T05:06:07"' in xml, xml


def test_attachment_modified_is_empty_when_the_file_is_gone(tmp_path):
    """取不到就留空 —— 不拿「现在」去凑（那正是改前的行为）。"""
    from openminis.settings import attachments

    missing = tmp_path / "nope.png"
    ref = attachments.AttachmentRef(alt="gone", src=str(missing), kind="image",
                                    host_path=missing, size=0)

    xml = attachments._attached_files_xml([ref])  # noqa: SLF001

    assert 'modified=""' in xml, xml
