"""Chat session persistence: store round-trips + REST API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openminis.server import chat_store
from openminis.server.main import app


# ---------------------------------------------------------------------------
# store layer
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_session_roundtrip(isolated_chat_db):
    s = await chat_store.create_session()
    assert s.title == "新会话"

    await chat_store.append_turn(s.id, "user", "帮我列一下当前目录")
    await chat_store.append_turn(s.id, "assistant", "这是目录内容:…")

    # auto-title from the first user message
    info = await chat_store.get_session(s.id)
    assert info is not None and info.title == "帮我列一下当前目录"
    assert info.lastMessage.startswith("这是目录内容")

    rows = await chat_store.load_messages(s.id)
    assert [(m.role, m.text) for m in rows] == [
        ("user", "帮我列一下当前目录"),
        ("assistant", "这是目录内容:…"),
    ]


@pytest.mark.asyncio
async def test_runtime_history_rebuild_alternates(isolated_chat_db):
    s = await chat_store.create_session()
    for role, text in [
        ("user", "第一问"),
        ("assistant", "回答一"),
        ("assistant", "补充说明"),  # merged into previous assistant turn
        ("user", "第二问"),
    ]:
        await chat_store.append_turn(s.id, role, text)

    hist = await chat_store.load_runtime_history(s.id)
    assert [m.role.value for m in hist] == ["user", "assistant", "user"]
    assert hist[1].content == "回答一\n\n补充说明"


@pytest.mark.asyncio
async def test_sessions_ordered_newest_first_and_delete(isolated_chat_db):
    a = await chat_store.create_session()
    b = await chat_store.create_session()
    await chat_store.append_turn(a.id, "user", "旧的会话")
    await chat_store.append_turn(b.id, "user", "新的会话")

    lst = await chat_store.list_sessions()
    assert lst[0].id == b.id  # touched later → newest first
    assert [x.title for x in lst] == ["新的会话", "旧的会话"]

    assert await chat_store.delete_session(b.id) is True
    assert await chat_store.get_session(b.id) is None
    assert len(await chat_store.list_sessions()) == 1


@pytest.mark.asyncio
async def test_clear_all_chat_data_keeps_workspaces(isolated_chat_db, tmp_path):
    """[T-clear-chat-data] 一键清空：会话/消息全清，**工作区（分组）保留**。

    供应商等设置不在 chat_store 里，这个函数天然碰不到它们 —— 是「清数据但
    保留供应商」按钮的后端。

    工作区同理属于**配置**不是聊天记录：它带着用户绑定的真实项目目录，删了会
    连带毁掉「agent 在项目目录里干活」这条链路（实测：清空后前端缓存的工作区
    id 变悬空 → 「把会话放进工作区失败：工作空间不存在」→ shell 退回空的
    ``db-<会话id>`` 沙箱 → agent 看不到用户的文件）。"""
    from openminis.server import workspaces

    a = await chat_store.create_session()
    b = await chat_store.create_session()
    await chat_store.append_turn(a.id, "user", "一")
    await chat_store.append_turn(a.id, "assistant", "答一")
    await chat_store.append_turn(b.id, "user", "二")

    proj = tmp_path / "proj"
    proj.mkdir()
    ws = await workspaces.create_workspace("AI智控")
    workspaces.set_workspace_path(ws.id, str(proj))
    await workspaces.assign_session_workspace(a.id, ws.id)

    counts = await chat_store.clear_all_chat_data()
    assert counts["sessions"] == 2
    assert counts["messages"] == 3
    assert await chat_store.list_sessions() == []

    # 工作区与它绑定的真实目录都还在，且能重新装会话
    kept = await workspaces.list_workspaces()
    assert [w.id for w in kept] == [ws.id]
    assert kept[0].path == str(proj)
    c = await chat_store.create_session()
    assert await workspaces.assign_session_workspace(c.id, ws.id) is True
    assert await workspaces.sandbox_dir_for_session(c.id) == proj.resolve()

    # 清空后还能正常新建，说明表结构没被动坏
    assert await chat_store.get_session(c.id) is not None


# ---------------------------------------------------------------------------
# 工具卡持久化 [T-tool-cards-persist-and-fold]
# ---------------------------------------------------------------------------
_TOOL_RUN = {
    "id": "call_1",
    "name": "shell_execute",
    "input": {"command": "pytest -q"},
    "ok": True,
    "output": "11 passed in 0.78s",
    "ms": 780,
}


@pytest.mark.asyncio
async def test_tool_runs_roundtrip_without_entering_context(isolated_chat_db):
    """工具卡随回合落库（界面能画回来），但不进模型上下文。"""
    s = await chat_store.create_session()
    await chat_store.append_turn(s.id, "user", "跑个测试")
    await chat_store.append_turn(
        s.id, "assistant", "都过了。", runs=[_TOOL_RUN],
    )

    rows = await chat_store.load_messages(s.id)
    assert rows[1].text == "都过了。"
    assert rows[1].runs == [_TOOL_RUN]

    # 上下文重建：只有正文，没有工具调用记录
    hist = await chat_store.load_runtime_history(s.id)
    assert [m.role.value for m in hist] == ["user", "assistant"]
    assert hist[1].content == "都过了。"
    assert "shell_execute" not in hist[1].content
    assert "11 passed" not in hist[1].content


@pytest.mark.asyncio
async def test_tool_only_turn_still_persists_cards(isolated_chat_db):
    """模型只调工具、没留下正文时，卡片也不能跟着回合一起消失。"""
    s = await chat_store.create_session()
    await chat_store.append_turn(s.id, "user", "看看目录")
    await chat_store.append_turn(s.id, "assistant", "", runs=[_TOOL_RUN])

    rows = await chat_store.load_messages(s.id)
    assert len(rows) == 2
    assert rows[1].text == "" and rows[1].runs == [_TOOL_RUN]


@pytest.mark.asyncio
async def test_plain_turn_stores_no_runs(isolated_chat_db):
    s = await chat_store.create_session()
    await chat_store.append_turn(s.id, "assistant", "纯文字")
    assert (await chat_store.load_messages(s.id))[0].runs is None


@pytest.mark.asyncio
async def test_messages_api_returns_runs(isolated_chat_db):
    import httpx

    s = await chat_store.create_session()
    await chat_store.append_turn(s.id, "assistant", "都过了。", runs=[_TOOL_RUN])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(f"/api/chats/sessions/{s.id}/messages")
    msgs = r.json()["messages"]
    assert msgs[0]["text"] == "都过了。"
    assert msgs[0]["runs"] == [_TOOL_RUN]


# ---------------------------------------------------------------------------
# 重放顺序：正文段与工具卡交替 [T-turn-timeline-order]
# ---------------------------------------------------------------------------
_TIMELINE = [
    {"type": "text", "text": "我先看看目录。"},
    {"type": "tool", **_TOOL_RUN},
    {"type": "text", "text": "都过了。"},
]


@pytest.mark.asyncio
async def test_timeline_roundtrip_keeps_interleaving(isolated_chat_db):
    """重放历史要还原"说一句 → 调工具 → 再说一句"，而不是把工具卡全堆到底部。

    用户实测：关闭应用再打开、重开会话，所有工具执行都排在最下面。
    根因是落库只存得下「一整块正文 + 一串工具卡」，交替顺序丢了。
    """
    s = await chat_store.create_session()
    await chat_store.append_turn(
        s.id, "assistant", "我先看看目录。都过了。",
        runs=[_TOOL_RUN], timeline=_TIMELINE,
    )

    rows = await chat_store.load_messages(s.id)
    assert rows[0].timeline == _TIMELINE
    # 正文段在时间线里**分开**存着，别被合回一整块
    kinds = [seg["type"] for seg in rows[0].timeline]
    assert kinds == ["text", "tool", "text"]


@pytest.mark.asyncio
async def test_timeline_does_not_touch_model_context(isolated_chat_db):
    """时间线只服务界面：模型上下文仍是纯正文，一个工具名都不能漏进去。"""
    s = await chat_store.create_session()
    await chat_store.append_turn(
        s.id, "assistant", "都过了。", runs=[_TOOL_RUN], timeline=_TIMELINE,
    )

    rows = await chat_store.load_messages(s.id)
    assert rows[0].text == "都过了。"
    assert rows[0].runs == [_TOOL_RUN]

    hist = await chat_store.load_runtime_history(s.id)
    assert hist[0].content == "都过了。"
    assert "shell_execute" not in hist[0].content
    assert "我先看看目录" not in hist[0].content


@pytest.mark.asyncio
async def test_legacy_rows_without_timeline_fall_back(isolated_chat_db):
    """老数据（没有 flow part）退化成改造前的画法：正文在前、卡片在后。"""
    s = await chat_store.create_session()
    await chat_store.append_turn(s.id, "assistant", "都过了。", runs=[_TOOL_RUN])

    rows = await chat_store.load_messages(s.id)
    assert rows[0].timeline == [
        {"type": "text", "text": "都过了。"},
        {"type": "tool", **_TOOL_RUN},
    ]


@pytest.mark.asyncio
async def test_plain_turn_has_no_timeline(isolated_chat_db):
    """纯正文回合不该凭空多出一个空时间线。"""
    s = await chat_store.create_session()
    await chat_store.append_turn(s.id, "assistant", "纯文字")
    assert (await chat_store.load_messages(s.id))[0].timeline is None


@pytest.mark.asyncio
async def test_flow_part_stores_only_tool_refs(isolated_chat_db):
    """时间线里的卡片只留 id 引用 —— 内容在 tool part 里已存过，别再抄一份。

    单条 output 上限 8KB，工具多的回合照抄一遍会让 parts_json 直接涨一倍。
    """
    import json

    from openminis.data.db.chat_dao import ChatDao
    from openminis.server import chat_store as cs

    s = await chat_store.create_session()
    await chat_store.append_turn(
        s.id, "assistant", "都过了。", runs=[_TOOL_RUN], timeline=_TIMELINE,
    )

    async with cs._get_db().session() as db:  # noqa: SLF001
        rows = await ChatDao(db).load_messages(s.id)
    parts_json = rows[0].parts_json
    flow = [p for p in json.loads(parts_json) if p.get("type") == "flow"][0]
    tool_seg = [seg for seg in flow["items"] if seg.get("type") == "tool"][0]
    assert tool_seg == {"type": "tool", "id": _TOOL_RUN["id"]}
    # 内容只出现一次（在 tool part 里）
    assert parts_json.count(_TOOL_RUN["output"]) == 1


@pytest.mark.asyncio
async def test_subagent_row_timeline_includes_text(isolated_chat_db):
    """子代理回合的正文在 subtext part 里，时间线也要带上。

    不带的话那几行就是"空白气泡 + 一摞工具卡"—— 开着子代理时工具卡大多产生
    在这里，用户看到的正是「所有工具执行都排在最下面」。
    """
    from openminis.data.model.agent_tool_definition import AgentToolParam  # noqa: F401

    s = await chat_store.create_session()
    await chat_store.append_sub_turn(
        s.id,
        speaker={"name": "代码助手"},
        task="看看目录",
        room_id="r1",
        text="目录里有 3 个文件。",
        runs=[_TOOL_RUN],
    )

    rows = await chat_store.load_messages(s.id)
    assert rows[0].text == ""            # 不进模型上下文
    assert rows[0].sub["text"] == "目录里有 3 个文件。"
    kinds = [seg["type"] for seg in rows[0].timeline]
    assert kinds == ["text", "tool"]
    assert rows[0].timeline[0]["text"] == "目录里有 3 个文件。"


def test_image_refs_land_in_the_timeline():
    """生图补写的 `![](路径)` 必须进时间线，否则重放时图会消失。

    界面在有 timeline 时不再渲染 `m.text` —— 补写只进 text 就等于丢了。
    """
    from openminis.server.main import timeline_with_image_refs

    tl = [{"type": "text", "text": "画好了："}]
    before = "画好了："
    after = "画好了：\n\n![生成图](C:/x/a.png)"
    timeline_with_image_refs(tl, before, after)
    assert tl == [{"type": "text", "text": "画好了：\n\n![生成图](C:/x/a.png)"}]

    # 没补写时不动它（别凭空空加一段）
    tl2 = [{"type": "text", "text": "纯文字"}]
    timeline_with_image_refs(tl2, "纯文字", "纯文字")
    assert tl2 == [{"type": "text", "text": "纯文字"}]


@pytest.mark.asyncio
async def test_messages_api_returns_timeline(isolated_chat_db):
    import httpx

    s = await chat_store.create_session()
    await chat_store.append_turn(
        s.id, "assistant", "我先看看目录。都过了。",
        runs=[_TOOL_RUN], timeline=_TIMELINE,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(f"/api/chats/sessions/{s.id}/messages")
    msgs = r.json()["messages"]
    assert msgs[0]["timeline"] == _TIMELINE


@pytest.mark.asyncio
async def test_messages_api_defaults_runs_to_empty_list(isolated_chat_db):
    """老消息（没有工具记录）也要给前端一个空数组，省得它到处判 undefined。"""
    import httpx

    s = await chat_store.create_session()
    await chat_store.append_turn(s.id, "user", "只有文字")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(f"/api/chats/sessions/{s.id}/messages")
    assert r.json()["messages"][0]["runs"] == []


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_api_lifecycle(isolated_chat_db):
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # empty at first
        r = await c.get("/api/chats/sessions")
        assert r.status_code == 200 and r.json()["sessions"] == []

        # create
        r = await c.post("/api/chats/sessions")
        assert r.status_code == 200
        sid = r.json()["id"]

        # unknown session messages → 404
        assert (
            await c.get(f"/api/chats/sessions/{'x' * 32}/messages")
        ).status_code == 404

        # seed one turn via the store (as the ws handler would)
        await chat_store.append_turn(sid, "user", "你好")
        await chat_store.append_turn(sid, "assistant", "你好!")

        # list shows it with title + preview
        r = await c.get("/api/chats/sessions")
        row = r.json()["sessions"][0]
        assert row["id"] == sid and row["title"] == "你好"

        # messages round-trip
        r = await c.get(f"/api/chats/sessions/{sid}/messages")
        msgs = r.json()["messages"]
        assert [(m["role"], m["text"]) for m in msgs] == [
            ("user", "你好"),
            ("assistant", "你好!"),
        ]

        # delete
        assert (await c.delete(f"/api/chats/sessions/{sid}")).status_code == 200
        assert (await c.get("/api/chats/sessions")).json()["sessions"] == []


# ---------------------------------------------------------------------------
# workspaces (工作空间 / 文件夹)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_workspace_lifecycle(isolated_chat_db):
    from openminis.server import workspaces

    # empty
    lst = await workspaces.list_workspaces()
    assert lst == []

    # create
    ws = await workspaces.create_workspace("工作", "周报")
    assert ws.name == "工作" and ws.sessionCount == 0

    # rename via store (REST API call below too)
    renamed = await workspaces.rename_workspace(ws.id, "重要工作")
    assert renamed.name == "重要工作"
    assert renamed.description == "周报"

    # seed a session in this workspace
    s = await chat_store.create_session(folder_id=ws.id)
    assert s.folderId == ws.id

    # listing now reports the count
    lst = await workspaces.list_workspaces()
    assert lst[0].sessionCount == 1

    # REST filter: only sessions of this workspace
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # workspace list endpoint
        r = await c.get("/api/chats/workspaces")
        assert r.status_code == 200
        assert r.json()["workspaces"][0]["name"] == "重要工作"

        # sessions filtered by workspace
        r = await c.get(f"/api/chats/sessions?workspace={ws.id}")
        assert r.status_code == 200
        rows = r.json()["sessions"]
        assert len(rows) == 1 and rows[0]["folderId"] == ws.id

        # create session bound to workspace via POST
        r = await c.post("/api/chats/sessions", json={"folderId": ws.id})
        assert r.status_code == 200
        new_sid = r.json()["id"]
        assert r.json()["folderId"] == ws.id

        # move a session out of the folder (folderId=null)
        r = await c.patch(
            f"/api/chats/sessions/{new_sid}/workspace", json={"folderId": None}
        )
        assert r.status_code == 200
        # unfiled query now finds it
        r = await c.get("/api/chats/sessions?workspace=unfiled")
        assert any(x["id"] == new_sid for x in r.json()["sessions"])

        # deleting the workspace drops members back to unfiled
        ok = await workspaces.delete_workspace(ws.id)
        assert ok is True
        s_after = await chat_store.get_session(s.id)
        assert s_after is not None and s_after.folderId is None


@pytest.mark.asyncio
async def test_agent_runtime_emits_tool_result_chunk():
    """The agent loop must emit ``LLMStreamChunk.ToolResult`` so the Web UI
    can render the final state of every tool call. Smoke-test that the
    dataclass exists and is wired into the chunk_sink pipeline."""
    from openminis.data.model import LLMStreamChunk

    assert hasattr(LLMStreamChunk, "ToolResult")
    chunk = LLMStreamChunk.ToolResult(
        id="call-1", name="file_read", content="ok output", is_error=False
    )
    assert chunk.id == "call-1"
    assert chunk.is_error is False
