"""内置「通用代理」：沿用**派发时那一轮**的模型。

用户要的是"别让我再挑一次模型" —— 所以内置代理用哨兵 ``@current``，派发时
跟着当前对话走。这里钉住四件事：

1. 没配任何模型服务也能建出来（哨兵不做实例/Key 校验）；
2. 播种只播一次（用户删了不该每次启动又冒出来；还原出厂删掉标记 → 重新播种）；
3. 派发时用的就是**本轮**对话的 provider/model（含会话级选择），而不是"设置里
   当前选中的那个"；
4. 没在对话里（纯 CLI / 定时任务）时退回当前设置。
"""

from __future__ import annotations

import pytest

from openminis.agent.subagents import (
    BUILTIN_SUBAGENT,
    BUILTIN_SUBAGENT_ID,
    INHERIT_MODEL,
    SubagentError,
    _resolve_inherited_model,
    delete_subagent,
    ensure_builtin_subagent,
    get_subagent,
    set_current_model,
    upsert_subagent,
)
from openminis.core.context import app_context
from openminis.settings.store import SettingsStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = SettingsStore(path=tmp_path / "settings.json")
    monkeypatch.setattr(SettingsStore, "get", classmethod(lambda cls: s))
    return s


def test_seed_needs_no_provider_at_all(store):
    """哨兵代理不该要求先配好模型服务 —— 否则装完开不了、还得先配一遍。"""
    assert store.provider_instances() == []
    ensure_builtin_subagent(store)
    cfg = get_subagent(store, BUILTIN_SUBAGENT_ID)
    assert cfg is not None, "没播种出来"
    assert cfg["providerId"] == INHERIT_MODEL
    assert cfg["model"] == INHERIT_MODEL
    assert cfg["name"] == BUILTIN_SUBAGENT["name"]


def test_seed_happens_once_and_stays_deleted(store):
    assert ensure_builtin_subagent(store) == BUILTIN_SUBAGENT_ID
    assert ensure_builtin_subagent(store) is None, "播种了两次"

    # 用户手工删掉：标记还在 → 不该每次启动又冒出来
    assert delete_subagent(store, BUILTIN_SUBAGENT_ID) is True
    assert ensure_builtin_subagent(store) is None, "删掉的代理又自己回来了"
    assert get_subagent(store, BUILTIN_SUBAGENT_ID) is None

    # 还原出厂会删掉标记 → 重新播种（这正是"回到刚装好的状态"）
    (app_context().data_dir / "subagents.seeded").unlink()
    assert ensure_builtin_subagent(store) == BUILTIN_SUBAGENT_ID


def test_seed_does_not_clobber_a_user_edited_same_id(store):
    """老版本里用户已经手工建过同 id 的：认下来，别覆盖他的配置。"""
    store.apply_full({
        "providers": [{"id": "gw", "type": "openAI", "apiKey": "k", "model": "m"}],
        "activeProviderId": "gw",
    })
    upsert_subagent(store, {
        "id": BUILTIN_SUBAGENT_ID, "name": "我自己的通用", "providerId": "gw",
        "providerType": "openAI", "model": "m", "tools": [], "skills": [],
    })
    marker = app_context().data_dir / "subagents.seeded"
    assert not marker.exists()
    assert ensure_builtin_subagent(store) is None
    assert get_subagent(store, BUILTIN_SUBAGENT_ID)["name"] == "我自己的通用"
    assert marker.exists(), "认下来之后要打标记，避免每次启动重复判断"


def test_seed_accepts_the_sentinel_through_public_upsert(store):
    """哨兵走公开的保存路径也不能报"模型服务未配置"。"""
    rec = upsert_subagent(store, dict(BUILTIN_SUBAGENT))
    assert rec["providerId"] == INHERIT_MODEL
    assert rec["providerType"] == ""


def test_resolve_prefers_the_current_turn_then_falls_back(store):
    store.apply_full({
        "providers": [{"id": "gw", "type": "openAI", "apiKey": "k", "model": "set-model"}],
        "activeProviderId": "gw",
    })
    # 不在对话里（CLI/定时任务）→ 用当前设置
    assert _resolve_inherited_model(store) == ("gw", "set-model")
    # 在对话里 → 用**这一轮**的模型（可能与会话级选择/兜底链有关）
    set_current_model("gw", "turn-model")
    try:
        assert _resolve_inherited_model(store) == ("gw", "turn-model")
    finally:
        set_current_model("", "")


def test_run_subagent_uses_the_inherited_model(store, monkeypatch):
    """派发那一刻真正传给 provider 的，是本轮对话的 pid + model。"""
    import asyncio

    from openminis.settings import chat_service

    store.apply_full({
        "providers": [
            {"id": "gw", "type": "openAI", "apiKey": "k", "model": "set-model"},
            {"id": "other", "type": "openAI", "apiKey": "k", "model": "other-model"},
        ],
        "activeProviderId": "other",
    })
    ensure_builtin_subagent(store)
    captured: dict = {}

    def _fake_build(pid, conf):  # noqa: ANN001
        captured["pid"] = pid
        captured["model"] = conf.get("model")
        raise RuntimeError("到此为止（测试只关心选到哪个模型）")

    monkeypatch.setattr(chat_service, "build_provider", _fake_build)
    set_current_model("gw", "turn-model")  # 本轮对话用 gw / turn-model
    try:
        with pytest.raises(Exception):
            asyncio.run(__import__(
                "openminis.agent.subagents", fromlist=["run_subagent"]
            ).run_subagent(store, BUILTIN_SUBAGENT_ID, "干点活", "s1"))
    finally:
        set_current_model("", "")

    assert captured == {"pid": "gw", "model": "turn-model"}, captured


def test_run_subagent_inherit_without_any_model_says_what_to_do(store):
    """一个模型服务都没配的时候，报错要说清"先去哪配"，而不是崩在 provider 里。"""
    import asyncio

    from openminis.agent.subagents import run_subagent

    ensure_builtin_subagent(store)
    set_current_model("", "")
    with pytest.raises(SubagentError) as err:
        asyncio.run(run_subagent(store, BUILTIN_SUBAGENT_ID, "干点活", "s1"))
    assert "模型服务" in str(err.value)
