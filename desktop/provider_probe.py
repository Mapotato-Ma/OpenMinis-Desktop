"""Real end-to-end connectivity probe for a configured model provider.

The kernel already ships ``POST /api/settings/fetch-models``, but that only
probes ``GET /models`` on the Base URL — it proves the URL and the key are
reachable, and nothing about the *model id*. A user who mistypes the model
name still gets a green light, then a red chat.

This module sends an actual minimal completion through the same
``build_provider`` path the chat uses, so the answer it gives is the answer
the chat will give. Lives in ``desktop/`` on purpose: the upstream kernel is
GPL-3.0 and we keep ``src/`` byte-identical to it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

#: A real call costs a few tokens; keep it as small as a completion can be.
PROBE_MAX_TOKENS = 16
PROBE_TIMEOUT_S = 45.0


def _status_of(exc: BaseException) -> int | None:
    """Dig an HTTP status out of whatever the provider layer raised."""
    for attr in ("status_code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    resp = getattr(exc, "response", None)
    val = getattr(resp, "status_code", None)
    if isinstance(val, int):
        return val
    return None


def _diagnose(exc: BaseException, conf: dict[str, Any], model: str) -> dict[str, Any]:
    """Turn a raw exception into something the user can act on.

    The provider layer spans httpx, the vendor SDK wrappers and the kernel's
    own setup errors, and they do not share an error vocabulary — so this
    classifies on status code first, then on the message text.
    """
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    status = _status_of(exc)
    base_url = str(conf.get("baseUrl") or "").strip() or "(厂商默认)"
    ptype = str(conf.get("type") or "")

    if status in (401, 403) or any(
        k in low for k in ("unauthorized", "invalid api key", "invalid_api_key",
                           "authentication", "permission", "api key")
    ):
        return {
            "error": text,
            "hint": f"密钥被拒（HTTP {status or '401'}）。检查 API Key 是否完整、"
                    f"是否有该模型的权限，以及它是否属于 {ptype} 协议的服务商。",
        }
    if status == 404 or "not found" in low or "no such model" in low:
        return {
            "error": text,
            "hint": f"HTTP 404。要么 Base URL 路径不对（当前 {base_url}），"
                    f"要么模型 ID「{model}」在该服务商不存在 —— "
                    f"可以先点「拉取模型列表」看它到底提供哪些。",
        }
    if status == 429 or "rate limit" in low or "quota" in low:
        return {
            "error": text,
            "hint": "被限流或额度用尽。稍后再试，或换一个服务商实例。",
        }
    if status and 500 <= status < 600:
        return {"error": text, "hint": f"服务商返回 HTTP {status}，是对方的问题。"}
    if any(k in low for k in ("connect", "getaddrinfo", "name or service",
                              "nodename", "sslerror", "certificate", "timeout")):
        return {
            "error": text,
            "hint": f"连不上 Base URL（当前 {base_url}）。检查地址有没有写错、"
                    f"本机网络能否访问它。",
        }
    return {"error": text, "hint": "调用失败。可以先用「拉取模型列表」确认地址和密钥通不通。"}


async def probe_provider(provider_id: str) -> dict[str, Any]:
    """Send ``ping`` to the stored config of ``provider_id`` and report back.

    Always returns a dict (never raises) — the frontend renders the outcome
    inline, and an HTTP 500 would just be a worse version of the same message.
    """
    from openminis.data.model import LLMMessage
    from openminis.settings.chat_service import ChatSetupError, build_provider
    from openminis.settings.store import SettingsStore

    store = SettingsStore.get()
    data = store.load()
    conf = (data.get("providers") or {}).get(provider_id)
    if not isinstance(conf, dict):
        return {"ok": False, "error": f"服务商实例不存在: {provider_id}",
                "hint": "先保存设置，再回来测试。"}

    model = str(conf.get("model") or "").strip()

    # Anything wrong with the *config* (missing key, engine not ported) fails
    # here rather than on the wire, and the message is already user-facing.
    t0 = time.monotonic()
    try:
        provider = build_provider(provider_id, conf)
    except ChatSetupError as e:
        return {"ok": False, "stage": "setup", "error": str(e)}
    except Exception as e:  # noqa: BLE001 — a probe must never 500
        return {"ok": False, "stage": "setup", "error": f"{type(e).__name__}: {e}"}

    messages = [LLMMessage(LLMMessage.Role.USER, "ping")]
    try:
        resp = await asyncio.wait_for(
            provider.send_message(messages, None, PROBE_MAX_TOKENS),
            timeout=PROBE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return {
            "ok": False, "stage": "call",
            "error": f"请求超时（{PROBE_TIMEOUT_S:.0f} 秒）",
            "hint": "服务商没有在超时内回应。可能是地址不可达，或该模型首字延迟极高。",
            "ms": int((time.monotonic() - t0) * 1000),
        }
    except Exception as e:  # noqa: BLE001
        out: dict[str, Any] = {"ok": False, "stage": "call", "model": model}
        out.update(_diagnose(e, conf, model))
        out["ms"] = int((time.monotonic() - t0) * 1000)
        return out

    text = (getattr(resp, "text", "") or "").strip()
    usage = getattr(resp, "usage", None)
    return {
        "ok": True,
        "stage": "done",
        "model": model,
        "reply": text[:120],
        "usage": (usage if isinstance(usage, dict) else None),
        "ms": int((time.monotonic() - t0) * 1000),
    }


async def chat_readiness() -> dict[str, Any]:
    """Whether a chat turn would get past setup right now.

    Mirrors the two guards at the top of ``chat_service`` so the UI can warn
    *before* the user types a message and eats the same error again.
    """
    from openminis.settings.store import SettingsStore

    store = SettingsStore.get()
    data = store.load()
    pid = data.get("activeProviderId")
    if not pid:
        return {"ready": False, "reason": "no_active_provider",
                "message": "还没有指定「当前对话」用哪个服务商。"}
    conf = (data.get("providers") or {}).get(pid)
    if not isinstance(conf, dict):
        return {"ready": False, "reason": "missing_provider",
                "message": f"当前服务商 {pid} 的配置不存在。"}
    if not str(conf.get("apiKey") or "").strip():
        return {"ready": False, "reason": "no_api_key",
                "message": "当前服务商没有填 API Key。"}
    if not str(conf.get("model") or "").strip():
        return {"ready": False, "reason": "no_model",
                "message": "当前服务商没有填模型 ID。"}
    return {
        "ready": True, "providerId": pid,
        "label": conf.get("label") or conf.get("type") or pid,
        "model": conf.get("model"),
    }
