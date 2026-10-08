"""把「这条命令被沙箱拦了，要不要放行」送到界面去问，等用户回答。

守卫跑在**内核**里，而人在**界面**上 —— 中间隔着 WebSocket。这里只放一个 hook：
服务端把 asker 注册进来（`server/main.py` 里一行），shell 工具 `await ask(...)` 即可。

没注册 asker（纯 CLI、或没有客户端连着）时返回 ``unavailable``，
工具侧照旧硬拦 —— 行为与加这个功能之前**完全一致**，不会把 agent 永久挂住。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

#: 用户不回答时的兜底：按拒绝处理，别让 agent 无限等下去。
TIMEOUT_SECONDS = 180

_asker: Optional[Callable[[dict[str, Any]], Awaitable[str]]] = None


def set_asker(fn: Optional[Callable[[dict[str, Any]], Awaitable[str]]]) -> None:
    """注册问话实现（服务端调用）。传 None 可以退回「没人可问」。"""
    global _asker
    _asker = fn


def has_asker() -> bool:
    return _asker is not None


async def ask(payload: dict[str, Any]) -> str:
    """问用户。返回 once / session / always / deny / timeout / unavailable。"""
    fn = _asker
    if fn is None:
        return "unavailable"
    try:
        return await asyncio.wait_for(fn(payload), timeout=TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return "timeout"
    except Exception:  # pragma: no cover - 问不出去就按「没人可问」处理
        return "unavailable"
