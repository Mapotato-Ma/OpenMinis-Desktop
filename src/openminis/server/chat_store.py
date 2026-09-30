"""Chat session + message persistence for the Web chat UI.

Sessions and turns live in the SQLite database (the same ``sessions`` /
``messages`` tables the ported Room schema defines). The Web chat stores
plain-text turns: ``parts_json`` keeps a minimal
``[{"type":"text","text": …}]`` array, which round-trips through
:func:`parts_to_text`. Sessions auto-title from the first user message and
carry a ``last_message`` preview for the sidebar.

Both the REST endpoints (``server/chat_api``) and the WebSocket chat handler
(``server/main._handle_chat``) go through this module so storage stays
consistent.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..data.db.app_database import AppDatabase
from ..data.db.chat_dao import ChatDao
from ..data.db.chat_session_entity import ChatSessionEntity
from ..data.db.compact_marker_entity import CompactMarkerEntity
from ..data.db.message_entity import MessageEntity
from ..data.model import LLMMessage

__all__ = [
    "ChatSessionInfo",
    "ChatMessageInfo",
    "ensure_db",
    "list_sessions",
    "create_session",
    "get_session",
    "delete_session",
    "load_messages",
    "append_turn",
    "append_sub_turn",
    "load_runtime_history",
    "drop_runtime",
    "parts_to_text",
    "parts_to_runs",
    "parts_to_timeline",
    "parts_to_sub",
    "TITLE_DEFAULT",
]

TITLE_DEFAULT = "新会话"
_TITLE_LEN = 30
_PREVIEW_LEN = 60

_db_ready = False
_db_lock = asyncio.Lock()

#: Process-wide engine for chat persistence (independent of the settings store;
#: tests may point it at a temp file via :func:`set_database_path`).
_engine_db: AppDatabase | None = None


def set_database_path(path: Any) -> None:
    """Point the store at a different SQLite file (used by tests)."""
    global _engine_db, _db_ready
    _engine_db = AppDatabase(path) if path is not None else None
    _db_ready = False


def _get_db() -> AppDatabase:
    global _engine_db
    if _engine_db is None:
        _engine_db = AppDatabase()
    return _engine_db


def database() -> AppDatabase:
    """The shared database instance (compaction writes markers through it)."""
    return _get_db()

#: In-process rich transcript per session (user text + full tool rounds),
#: so consecutive turns in one process keep complete tool context. Lost on
#: restart — history is then rebuilt from the persisted plain-text turns.
_RUNTIME: dict[str, list[LLMMessage]] = {}
_MAX_RUNTIME_SESSIONS = 200


def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def parts_to_text(parts_json: str) -> str:
    """Extract the human text from a stored ``parts_json`` payload."""
    try:
        parts = json.loads(parts_json or "[]")
    except json.JSONDecodeError:
        return ""
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "text":
            txt = p.get("text")
            if isinstance(txt, str):
                out.append(txt)
        elif isinstance(p, str):
            out.append(p)
    return "\n".join(out)


def _text_parts(text: str) -> str:
    return json.dumps([{"type": "text", "text": text}], ensure_ascii=False)


#: [T-tool-cards-persist-and-fold] 工具调用记录在 ``parts_json`` 里的类型标签。
#: 一条助手回合的 parts 形如 ``[{text}, {tool}…]`` —— 复用了 Room 时代就有的
#: 「parts 数组」结构，因此**不需要加列、不需要迁移**；``parts_to_text`` 本来
#: 就只挑 ``type == "text"`` 的部分，历史重建（进模型上下文的那条路）自动
#: 忽略它们，这正是我们要的：卡片留在界面上，不进上下文。
RUN_PART_TYPE = "tool"


#: [T-turn-timeline-order] 界面**可视时间线**在 ``parts_json`` 里的类型标签。
#:
#: 一条助手回合作品其实是「正文段 → 工具卡 → 正文段 → …」交替的，但原来只存
#: 得下两样东西：一整块正文（``text`` part）+ 一串工具卡（``tool`` part）。
#: 顺序信息就此丢失，重放历史只能"先画全部正文、再把所有工具卡堆在下面"——
#: 用户实测：重启后重开会话，所有工具执行都排在最下面。
#:
#: 这里额外存一份**有序片段**。刻意用一个 ``parts_to_text`` / ``parts_to_runs``
#: 都不认识的类型标签，于是：
#:   * 模型上下文（只读 text part）**一字不变**；
#:   * 工具卡（只读 tool part）**一字不变**；
#:   * 旧数据没有这个 part → 前端退回改造前的老画法，天然兼容。
FLOW_PART_TYPE = "flow"


def _parts_with_runs(
    text: str,
    runs: list[dict[str, Any]] | None,
    timeline: list[dict[str, Any]] | None = None,
) -> str:
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for run in runs or []:
        if isinstance(run, dict) and run:
            parts.append({"type": RUN_PART_TYPE, **run})
    if timeline:
        parts.append({
            "type": FLOW_PART_TYPE,
            # 卡片在时间线里**只留一个 id 引用**：它的 name/input/output/ms 已经
            # 在 ``tool`` part 里存过一遍了，再抄一份会让 parts_json 直接涨一倍
            # （单条 output 上限就有 8KB，工具多的回合很可观）。读的时候按 id
            # join 回 ``tool`` part，见 ``parts_to_timeline``。
            "items": [_flow_ref(seg) for seg in timeline if isinstance(seg, dict) and seg],
        })
    return json.dumps(parts, ensure_ascii=False)


def _flow_ref(seg: dict[str, Any]) -> dict[str, Any]:
    """时间线里的一个片段 → 落库形态（工具卡只留 id）。"""
    if seg.get("type") == RUN_PART_TYPE:
        return {"type": RUN_PART_TYPE, "id": seg.get("id")}
    return dict(seg)


def parts_to_timeline(parts_json: str) -> list[dict[str, Any]]:
    """按**发生顺序**取出这一回合的可视片段：``{type:"text"|"tool", …}``。

    界面重放历史时必须按这个顺序画，才能还原"说一句 → 调工具 → 再说一句"。
    老数据没有 ``flow`` part，退化成 ``[整段正文, 工具卡…]`` —— 即改造前的
    行为，不会更差。
    """
    try:
        parts = json.loads(parts_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(parts, list):
        return []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == FLOW_PART_TYPE:
            items = p.get("items")
            if not isinstance(items, list):
                return []
            by_id = {r.get("id"): r for r in parts_to_runs(parts_json)}
            out: list[dict[str, Any]] = []
            for seg in items:
                if not isinstance(seg, dict) or not seg:
                    continue
                if seg.get("type") == RUN_PART_TYPE:
                    run = by_id.get(seg.get("id"))
                    if run is not None:      # 卡片正文在 tool part 里，按 id 取回
                        out.append({"type": RUN_PART_TYPE, **run})
                    continue                 # 取不回来就别塞个空壳糊弄界面
                out.append(dict(seg))
            return out
    # 旧数据回退：只有真的存在工具卡时才合成时间线（正文在前、卡片在后）。
    # 纯正文回合没有可"交替"的东西 → 返回空，前端走原画法，零变化。
    runs = parts_to_runs(parts_json)
    if not runs:
        return []
    out = []
    # 子代理回合的正文在 ``subtext`` part 里（``parts_to_text`` 看不到它），
    # 不补这一句的话那几行就是"空白气泡 + 一摞卡片"。
    sub = parts_to_sub(parts_json)
    legacy_text = parts_to_text(parts_json) or ((sub or {}).get("text") or "")
    if legacy_text:
        out.append({"type": "text", "text": legacy_text})
    out.extend({"type": RUN_PART_TYPE, **r} for r in runs)
    return out


def parts_to_runs(parts_json: str) -> list[dict[str, Any]]:
    """Extract the persisted tool-call records from a stored ``parts_json``."""
    try:
        parts = json.loads(parts_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(parts, list):
        return []
    out: list[dict[str, Any]] = []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == RUN_PART_TYPE:
            out.append({k: v for k, v in p.items() if k != "type"})
    return out


#: [T-subagent-log-persist] 子代理过程在 ``parts_json`` 里的两个类型标签。
#:
#: 一次子代理委派落成一条 assistant 行：``[{submeta}, {subtext}…, {tool}…]``。
#: 它的正文刻意**不写成 ``text`` part** —— ``parts_to_text``（界面正文 +
#: 模型上下文都靠它）只认 ``text``，于是子代理过程与它的工具卡一样：
#: 会话里看得见、能展开，但永远不进模型上下文。零迁移，和历史数据并存。
SUB_META_TYPE = "submeta"
SUB_TEXT_TYPE = "subtext"


def _sub_parts(
    speaker: dict[str, Any] | None,
    task: str,
    room_id: str,
    text: str,
    runs: list[dict[str, Any]] | None,
) -> str:
    parts: list[dict[str, Any]] = [{
        "type": SUB_META_TYPE,
        "speaker": speaker or {},
        "task": task or "",
        "roomId": room_id or "",
    }]
    if text:
        parts.append({"type": SUB_TEXT_TYPE, "text": text})
    for run in runs or []:
        if isinstance(run, dict) and run:
            parts.append({"type": RUN_PART_TYPE, **run})
    return json.dumps(parts, ensure_ascii=False)


def parts_to_sub(parts_json: str) -> dict[str, Any] | None:
    """子代理消息的身份与正文；不是子代理消息就返回 ``None``。"""
    try:
        parts = json.loads(parts_json or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(parts, list):
        return None
    meta: dict[str, Any] | None = None
    chunks: list[str] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        kind = p.get("type")
        if kind == SUB_META_TYPE and meta is None:
            meta = p
        elif kind == SUB_TEXT_TYPE:
            chunks.append(str(p.get("text") or ""))
    if meta is None:
        return None
    speaker = meta.get("speaker")
    return {
        "speaker": speaker if isinstance(speaker, dict) else {},
        "task": str(meta.get("task") or ""),
        "roomId": str(meta.get("roomId") or ""),
        "text": "".join(chunks),
    }


@dataclass(frozen=True)
class ChatSessionInfo:
    id: str
    title: str
    updatedAt: int
    lastMessage: str
    folderId: str | None = None


@dataclass(frozen=True)
class ChatMessageInfo:
    id: str
    role: str
    text: str
    createdAt: int
    #: [T-tool-cards-persist-and-fold] 这一回合调用过的工具（有序）。工具卡在
    #: 会话里要能一直看得见、能展开，所以随消息一起落库；它们**不进模型上下文**
    #: （见 ``parts_to_text`` 只认 text part）。
    runs: list[dict[str, Any]] | None = None
    #: [T-turn-timeline-order] 这一回合的**可视时间线**（有序：正文段与工具卡
    #: 交替）。界面重放历史按它来画，工具卡才会停在原来的位置，而不是全被
    #: 堆到会话底部。同样不进模型上下文。
    timeline: list[dict[str, Any]] | None = None
    #: [T-subagent-log-persist] 子代理消息：``{speaker, task, roomId, text}``。
    #: 正文在 ``subtext`` part 里（``text`` 字段因此是空的），所以同样不进上下文。
    sub: dict[str, Any] | None = None


async def ensure_db() -> None:
    """Initialise the shared database exactly once per process."""
    global _db_ready
    if _db_ready:
        return
    async with _db_lock:
        if _db_ready:
            return
        _engine = _get_db()
        await _engine.initialize()
        _db_ready = True


def _model_label() -> str:
    """Best-effort label of the active provider/model, without touching
    settings import cycles at module import time."""
    try:
        from ..settings.store import SettingsStore
        from ..settings.catalog import provider_label

        data = SettingsStore.get().load()
        pid = data.get("activeProviderId")
        conf = (data["providers"].get(pid) if pid else None) or {}
        model = conf.get("model") or ""
        if pid:
            label = str(conf.get("label") or "").strip()
            if not label:
                label = provider_label(str(conf.get("type") or pid))
            return f"{label}{(' · ' + model) if model else ''}"
    except Exception:  # pragma: no cover - never fail storage over cosmetics
        pass
    return ""


# -- sessions ----------------------------------------------------------------
async def list_sessions(folder_id: str | None | object = None) -> list[ChatSessionInfo]:
    """List all sessions, optionally filtered by ``folder_id``.

    Pass the sentinel value :data:`UNFILED` (``UNFILED``) to return only
    sessions that have no workspace; pass a real folder id to return only
    sessions in that workspace; pass ``None`` (default) for the unfiltered
    list.
    """
    await ensure_db()
    async with _get_db().session() as s:
        rows = await ChatDao(s).list_sessions_sorted()
        if folder_id is not None:
            target = None if folder_id is UNFILED else str(folder_id)
            rows = [r for r in rows if (r.folder_id or None) == target]
        return [
            ChatSessionInfo(
                id=r.id,
                title=r.title or TITLE_DEFAULT,
                updatedAt=r.updated_at,
                lastMessage=r.last_message or "",
                folderId=r.folder_id,
            )
            for r in rows
        ]


UNFILED: object = object()  # sentinel for "folder_id IS NULL"


async def create_session(
    model_label: str | None = None,
    folder_id: str | None = None,
) -> ChatSessionInfo:
    """Create an empty session and return its info.

    If ``folder_id`` is given the session is filed into that workspace;
    ``None`` (the default) leaves it ungrouped.
    """
    await ensure_db()
    sid = uuid.uuid4().hex
    now = _now_ms()
    entity = ChatSessionEntity(
        id=sid,
        title=TITLE_DEFAULT,
        model_id=model_label or _model_label() or "unset",
        created_at=now,
        updated_at=now,
        last_message="",
        folder_id=folder_id,
    )
    async with _get_db().session() as s:
        await ChatDao(s).insert_session(entity)
    return ChatSessionInfo(
        id=sid, title=TITLE_DEFAULT, updatedAt=now, lastMessage="",
        folderId=folder_id,
    )


async def get_session(session_id: str) -> ChatSessionInfo | None:
    await ensure_db()
    async with _get_db().session() as s:
        row = await ChatDao(s).get_session(session_id)
        if row is None:
            return None
        return ChatSessionInfo(
            id=row.id, title=row.title or TITLE_DEFAULT,
            updatedAt=row.updated_at, lastMessage=row.last_message or "",
            folderId=row.folder_id,
        )


async def delete_session(session_id: str) -> bool:
    await ensure_db()
    async with _get_db().session() as s:
        dao = ChatDao(s)
        if await dao.get_session(session_id) is None:
            return False
        await dao.delete_session(session_id)  # messages cascade
    drop_runtime(session_id)
    # 会话不存在了，它的循环防护滑动窗口也一并丢弃（避免长期累积）。
    from ..settings.chat_service import reset_session_guards

    reset_session_guards(session_id)
    return True


async def delete_message(session_id: str, message_id: str) -> bool:
    """删一条消息（气泡上的「删除」按钮）。运行时缓存一并失效，下一轮
    LLM 历史从库里重读，保证删掉的消息不再进上下文。"""
    await ensure_db()
    async with _get_db().session() as s:
        dao = ChatDao(s)
        deleted = await dao.delete_message(session_id, message_id)
    if deleted:
        drop_runtime(session_id)
    return deleted


async def clear_all_chat_data() -> dict[str, int]:
    """Wipe chat sessions, messages and compact markers — but KEEP workspaces.

    Deliberately touches ONLY conversation content. Two categories are treated
    as *configuration* and left completely alone:

    * providers / models / soul / memory / skills — they live outside the chat
      DAO entirely, so this function can't reach them;
    * **workspaces (folders) and their real-path bindings** — a workspace like
      ``AI智控 → E:\\code\\...`` is setup the user configured, not history.
      Deleting it used to break the very thing we rely on: the session's shell
      + file tools boot inside that bound folder. If clear-data removed the
      folder, the frontend's cached ``currentWorkspace`` then pointed at a
      dead id → "把会话放进工作区失败：工作空间不存在" and the agent lost sight
      of the project. So folders survive; only their sessions are emptied.

    Returns per-category counts so the UI/log can confirm what was removed.
    """
    await ensure_db()
    counts = {"sessions": 0, "messages": 0, "folders_kept": 0, "markers": 0}
    async with _get_db().session() as s:
        dao = ChatDao(s)
        sessions = await dao.list_sessions()
        counts["sessions"] = len(sessions)
        counts["folders_kept"] = len(await dao.list_folders())
        for sess in sessions:
            msgs = await dao.load_messages(sess.id)
            counts["messages"] += len(msgs)
            await dao.delete_compact_markers(sess.id)
            await dao.delete_messages(sess.id)
            await dao.delete_session(sess.id)
    counts["markers"] = counts["sessions"]  # markers cleared per session above

    # Drop in-process caches + loop guards so a fresh chat starts truly empty.
    global _RUNTIME
    for sid in list(_RUNTIME.keys()):
        drop_runtime(sid)
    _RUNTIME = {}
    try:
        from ..settings.chat_service import reset_session_guards

        for sess in sessions:
            reset_session_guards(sess.id)
    except Exception:  # pragma: no cover - guard cleanup is best-effort
        pass
    return counts


# -- runtime transcript cache ------------------------------------------------
def _cache_runtime(session_id: str, messages: list[LLMMessage]) -> None:
    _RUNTIME[session_id] = messages
    if len(_RUNTIME) > _MAX_RUNTIME_SESSIONS:
        # drop the least-recently stored session (dict keeps insertion order)
        _RUNTIME.pop(next(iter(_RUNTIME)))


def drop_runtime(session_id: str) -> None:
    _RUNTIME.pop(session_id, None)


async def load_runtime_history(session_id: str) -> list[LLMMessage]:
    """The mutable transcript the agent loop appends to, per session.

    Prefers the in-process cache; on a miss it rebuilds a plain-text history
    from the DB (merged so ``user``/``assistant`` strictly alternate).

    If the session has been compacted, the rebuild starts at the latest
    marker's boundary and is prefixed with that marker's summary — the folded
    originals stay in the DB (history/export still sees them) but no longer
    consume context window.
    """
    cached = _RUNTIME.get(session_id)
    if cached is not None:
        return cached
    rows = await load_raw_messages(session_id)
    marker = await latest_compact_marker(session_id)
    start = first_kept_index(rows, marker)

    messages: list[LLMMessage] = []
    if marker is not None and start > 0 and marker.summary.strip():
        # Injected as an assistant turn: providers reject a ``system`` role
        # inside the message list (Anthropic in particular).
        messages.append(LLMMessage(LLMMessage.Role.ASSISTANT, compact_summary_text(
            marker.summary
        )))
    for row in rows[start:]:
        role = (
            LLMMessage.Role.ASSISTANT
            if row.role == "assistant"
            else LLMMessage.Role.USER
        )
        text = parts_to_text(row.parts_json).strip()
        if not text:
            continue
        if messages and messages[-1].role == role:
            # merge adjacent same-role turns to keep user/assistant alternating
            prev = messages[-1]
            prev.content = (prev.content + "\n\n" + text).strip()
        else:
            messages.append(LLMMessage(role, text))
    _cache_runtime(session_id, messages)
    return messages


#: Wraps a compacted summary so the model can tell it apart from a real reply.
COMPACT_SUMMARY_PREFIX = "[以下是对本会话较早对话的压缩摘要,原文仍保留在历史记录中]\n\n"


def compact_summary_text(summary: str) -> str:
    """Render a stored summary as the injected transcript entry."""
    return f"{COMPACT_SUMMARY_PREFIX}{summary.strip()}"


# -- messages ----------------------------------------------------------------
async def load_raw_messages(session_id: str) -> list[MessageEntity]:
    """Persisted turns with their ids and sort order (compaction needs both)."""
    await ensure_db()
    async with _get_db().session() as s:
        return await ChatDao(s).load_messages(session_id)


async def latest_compact_marker(session_id: str) -> CompactMarkerEntity | None:
    await ensure_db()
    async with _get_db().session() as s:
        return await ChatDao(s).latest_compact_marker(session_id)


def first_kept_index(
    rows: list[MessageEntity], marker: CompactMarkerEntity | None
) -> int:
    """Index of the first message that is *not* folded into ``marker``."""
    if marker is None:
        return 0
    anchor = marker.last_compacted_message_id
    if anchor:
        for i, row in enumerate(rows):
            if row.id == anchor:
                return i + 1
    # Fallback: markers without a usable anchor fall back to created_at.
    for i, row in enumerate(rows):
        if row.created_at > marker.created_at:
            return i
    return len(rows)


async def load_messages(session_id: str) -> list[ChatMessageInfo]:
    await ensure_db()
    async with _get_db().session() as s:
        rows = await ChatDao(s).load_messages(session_id)
        return [
            ChatMessageInfo(
                id=r.id,
                role=r.role,
                text=parts_to_text(r.parts_json),
                createdAt=r.created_at,
                runs=parts_to_runs(r.parts_json) or None,
                timeline=parts_to_timeline(r.parts_json) or None,
                sub=parts_to_sub(r.parts_json),
            )
            for r in rows
        ]


async def append_sub_turn(
    session_id: str,
    *,
    speaker: dict[str, Any] | None,
    task: str,
    room_id: str,
    text: str,
    runs: list[dict[str, Any]] | None = None,
) -> None:
    """把子代理的一次委派过程（发言 + 它调用的工具）落库。

    [T-subagent-log-persist] 原先子代理过程只在推流帧里活着，刷新/切会话/
    重启后端之后就没了 —— 开着子代理时「工具调用切窗口就消失」的根因。
    这里落成一条 assistant 行，但正文放 ``subtext`` part：既能回放，又
    不会挤进模型上下文（历史重建只收 text part）。

    刻意**不更新**会话的 last_message / updatedAt：子代理那句话不是用户
    看到的答复，别让侧边栏预览变成它的自言自语。
    """
    await ensure_db()
    text = (text or "").strip()
    now = _now_ms()
    async with _get_db().session() as s:
        dao = ChatDao(s)
        if await dao.get_session(session_id) is None:
            return
        order = await dao.next_sort_order(session_id)
        await dao.insert_message(
            MessageEntity(
                id=uuid.uuid4().hex,
                session_id=session_id,
                role="assistant",
                parts_json=_sub_parts(speaker, task, room_id, text, runs),
                created_at=now,
                sort_order=order,
            )
        )


async def append_turn(
    session_id: str,
    role: str,
    text: str,
    *,
    runs: list[dict[str, Any]] | None = None,
    #: [T-turn-timeline-order] 有序可视片段（正文段与工具卡交替）。给了就按它
    #: 落库，界面重放历史才能还原工具卡原本的位置。
    timeline: list[dict[str, Any]] | None = None,
    model_label: str | None = None,
    token_usage: str | None = None,
    model_id: str | None = None,
    model_display_name: str | None = None,
    provider_type: str | None = None,
    provider_instance_id: str | None = None,
) -> None:
    """Persist one user/assistant text turn and refresh session metadata
    (auto-title from the first user message, last-message preview, ordering).

    [T-token-attribution-snapshot] The ``model_*`` / ``provider_*`` kwargs are
    the per-message attribution snapshot the Usage page reads. They are
    recorded AT WRITE TIME because ``sessions.model_id`` is a single mutable
    column: joining on it re-attributed a session's whole history to whichever
    model it currently pointed at. ``token_usage`` is the serialised
    :class:`LLMUsage` JSON — only rows where it is non-NULL are billed rows.

    [T-tool-cards-persist-and-fold] ``runs`` 是这一回合的工具调用记录（有序：
    ``{id, name, input, ok, output, ms}``），与正文一同写进 ``parts_json``。
    它们不会进模型上下文（``parts_to_text`` 只取 text part），只服务于界面上的
    工具卡 —— 刷新、切会话、重启后端之后卡片依然在、依然能展开看原文。
    """
    await ensure_db()
    text = text.strip()
    now = _now_ms()
    async with _get_db().session() as s:
        dao = ChatDao(s)
        session = await dao.get_session(session_id)
        if session is None:
            return
        order = await dao.next_sort_order(session_id)
        await dao.insert_message(
            MessageEntity(
                id=uuid.uuid4().hex,
                session_id=session_id,
                role=role,
                parts_json=(
                    _parts_with_runs(text, runs, timeline)
                    if (runs or timeline) else _text_parts(text)
                ),
                created_at=now,
                sort_order=order,
                token_usage=token_usage,
                model_id=model_id,
                model_display_name=model_display_name,
                provider_type=provider_type,
                provider_instance_id=provider_instance_id,
            )
        )
        label = model_label or session.model_id or "unset"
        await dao.update_session_model(session_id, label, updated_at=now)
        if role == "user":
            title = session.title
            if not title or title == TITLE_DEFAULT:
                await dao.update_session_title(
                    session_id, _one_line(text)[:_TITLE_LEN] or TITLE_DEFAULT, now
                )
        elif role == "assistant":
            await dao.update_last_message(
                session_id, _one_line(text)[:_PREVIEW_LEN] or None, now
            )


async def update_session_title(session_id: str, title: str) -> None:
    """Rename a session (kept for future rename affordance)."""
    await ensure_db()
    async with _get_db().session() as s:
        await ChatDao(s).update_session_title(
            session_id, _one_line(title)[:_TITLE_LEN] or TITLE_DEFAULT, _now_ms()
        )
