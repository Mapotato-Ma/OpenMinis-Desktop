"""`provider_probe` 与真实对话路径的契约。

探针的承诺是：**它给出的结论就是对话会遇到的结论**（上游的
`/api/settings/fetch-models` 只探测 `GET /models`，模型 ID 写错时它依然是绿的）。
这个承诺靠两件事维持，而两件事都会随内核升级静默漂移：

1. 探针走内核的 `build_provider`，而不是自己发 HTTP；
2. 探针的调用形状（位置参数、系统提示位、max_tokens 位）与内核接口一致。

漂移的症状恰好是「探针看着还能用，但结论不再代表对话」——
也就是它存在的理由被自己破坏，且没有任何东西会报警。这里把它钉住。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from desktop import provider_probe
from desktop.provider_probe import PROBE_MAX_TOKENS, chat_readiness, probe_provider

from openminis.provider.llm_provider import LLMProvider
from openminis.settings import chat_service
from openminis.settings.catalog import ENGINE_READY, engine_for
from openminis.settings.store import SettingsStore

READY_TYPE = next(iter(sorted(ENGINE_READY)))          # 内核里真有引擎的协议之一
UNPORTED_TYPE = next((t for t in ("gemini", "google", "qwen") if engine_for(t) not in ENGINE_READY), "gemini")


def save_settings(**over: Any) -> dict[str, Any]:
    """把一份设置写进（被隔离的）store —— 与真实读取路径一致。"""
    store = SettingsStore.get()
    data = store.load()
    data.update(over)
    store.save(data)
    return data


def provider_conf(**over: Any) -> dict[str, Any]:
    conf = {"type": READY_TYPE, "apiKey": "sk-test", "model": "some-model", "baseUrl": "http://127.0.0.1:9/v1"}
    conf.update(over)
    return conf


@pytest.fixture()
def record_build(monkeypatch):
    """记录 build_provider 的调用，并返回一个假的 provider（不碰网络、不装 SDK）。"""
    calls: list[tuple[Any, Any]] = []
    sent: list[tuple[Any, Any, Any]] = []

    class FakeProvider:
        async def send_message(self, messages, system_prompt, max_tokens):  # noqa: ANN001
            sent.append((messages, system_prompt, max_tokens))
            return SimpleNamespace(text="pong", usage={"total_tokens": 3})

    fake = FakeProvider()

    def _build(provider_id, conf):  # noqa: ANN001
        calls.append((provider_id, conf))
        return fake

    monkeypatch.setattr(chat_service, "build_provider", _build)
    return SimpleNamespace(calls=calls, sent=sent, fake=fake)


# ── 契约 1：探针确实走内核的构建路径 ────────────────────────────────────
async def test_probe_goes_through_the_kernel_builder(record_build):
    save_settings(providers={"p1": provider_conf()}, activeProviderId="p1")

    out = await probe_provider("p1")

    assert out["ok"] is True and out["reply"] == "pong"
    assert record_build.calls, "探针没有调用 build_provider —— 它开始自己发请求了吗？"
    assert record_build.calls[0][0] == "p1"
    assert record_build.calls[0][1]["type"] == READY_TYPE


async def test_probe_uses_the_same_call_shape_as_chat(record_build):
    """位置参数、系统提示位、max_tokens 位 —— 与对话调用一致。"""
    save_settings(providers={"p1": provider_conf()}, activeProviderId="p1")
    await probe_provider("p1")

    assert len(record_build.sent) == 1
    messages, system_prompt, max_tokens = record_build.sent[0]
    assert [getattr(m, "text", None) or m for m in messages], "探针得发一条真消息"
    assert system_prompt is None
    assert max_tokens == PROBE_MAX_TOKENS


def test_probe_call_shape_is_still_legal_for_the_kernel_interface():
    """内核接口若改了参数形状，这里先红 —— 而不是等运行时变成一句 setup 报错。"""
    sig = inspect.signature(LLMProvider.send_message)
    params = list(sig.parameters)
    assert params[1:4] == ["messages", "system_prompt", "max_tokens"], params
    # 探针只传三个位置参数，且不传任何关键字参数
    sig.bind(None, [], None, PROBE_MAX_TOKENS)   # 不抛 = 调用形状仍然合法


# ── 契约 2：setup 阶段的结论直接用内核原话 ──────────────────────────────
async def test_probe_surfaces_the_kernel_setup_error_verbatim():
    save_settings(providers={"p1": provider_conf(type=UNPORTED_TYPE)}, activeProviderId="p1")

    out = await probe_provider("p1")

    assert out["ok"] is False and out["stage"] == "setup"
    with pytest.raises(chat_service.ChatSetupError) as excinfo:
        chat_service.build_provider("p1", {"type": UNPORTED_TYPE, "apiKey": "sk-test"})
    assert str(excinfo.value) in out["error"], "探针应当把内核的原话透出来，而不是自己编一套说法"


# ── 契约 3：readiness 不许假绿（说 ready 就一定过得了 setup） ─────────────
async def test_readiness_never_says_ready_when_setup_would_fail(monkeypatch):
    """`chat_readiness()` 的 ready 与 `build_provider` 的成败必须一致。

    它自己不建 provider（那要装 SDK、连网络），只跑内核那几条守卫 ——
    这里把构造那一步换掉，只验守卫逻辑。
    """
    monkeypatch.setattr(chat_service, "_create_provider", lambda *a, **k: SimpleNamespace())

    cases = [
        ("没有当前服务商", dict(providers={"p1": provider_conf()}, activeProviderId="")),
        ("当前服务商不存在", dict(providers={"p1": provider_conf()}, activeProviderId="ghost")),
        ("没有 API Key", dict(providers={"p1": provider_conf(apiKey="")}, activeProviderId="p1")),
        ("没有模型 ID", dict(providers={"p1": provider_conf(model="")}, activeProviderId="p1")),
        ("引擎未移植", dict(providers={"p1": provider_conf(type=UNPORTED_TYPE)}, activeProviderId="p1")),
        ("一切正常", dict(providers={"p1": provider_conf()}, activeProviderId="p1")),
    ]

    for name, over in cases:
        data = save_settings(**over)
        ready = await chat_readiness()
        if not ready["ready"]:
            assert ready.get("reason"), f"{name}: 说不 ready 就得给出机器可读的原因"
            assert ready.get("message"), f"{name}: 也要给人看得懂的一句话"
            continue
        # 说 ready 的，内核那边必须真的能建出 provider
        conf = data["providers"][ready["providerId"]]
        try:
            chat_service.build_provider(ready["providerId"], conf)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"{name}: readiness 说 ready，build_provider 却抛了 {type(exc).__name__}: {exc}")


async def test_readiness_and_chat_agree_on_the_missing_key_wording():
    """同一个事实不该有两种说法 —— 用户会在界面上先看到 readiness 的那句。"""
    save_settings(providers={"p1": provider_conf(apiKey="")}, activeProviderId="p1")

    ready = await chat_readiness()
    assert ready["ready"] is False and ready["reason"] == "no_api_key"
    with pytest.raises(chat_service.ChatSetupError) as excinfo:
        chat_service.build_provider("p1", provider_conf(apiKey=""))
    assert "API Key" in ready["message"] and "API Key" in str(excinfo.value)


# ── 契约 4：探针永远返回 dict，永远不抛 ─────────────────────────────────
async def test_probe_never_raises_on_hostile_input():
    save_settings(providers={"p1": "这不是一个 dict"}, activeProviderId="p1")

    for pid in ("p1", "不存在", ""):
        out = await probe_provider(pid)
        assert isinstance(out, dict)
        assert out["ok"] is False
        assert out.get("error")


# ── 404 的提示必须点出「实例类型选错了」这条 ─────────────────────────────
#
# 真机上踩过：DeepSeek 的实例类型选了 Anthropic → 请求发到
# https://api.deepseek.com/v1/messages（DeepSeek 只有 OpenAI 那套接口）→ 404。
# 而「拉取模型列表」走的是 OpenAI 风格的 GET /models，**类型选错时它照样成功** ——
# 所以用户很容易把那次成功当成"地址和密钥都没问题"的证据，然后一直怀疑 Base URL。


def _http_error(status: int) -> BaseException:
    exc = RuntimeError(f"ProviderError: Provider error: HTTP {status}")
    exc.status_code = status  # type: ignore[attr-defined]
    return exc


def test_the_404_hint_names_the_type_mismatch():
    conf = {"type": "anthropic", "baseUrl": "https://api.deepseek.com",
            "apiKey": "sk", "model": "deepseek-flash"}
    out = provider_probe._diagnose(_http_error(404), conf, "deepseek-flash")
    assert "OpenAI" in out["hint"], "没告诉用户该把类型改成 OpenAI"
    assert "/v1/messages" in out["hint"], "没说明 Anthropic 协议会打到哪个路径"
    assert "拉取模型列表" in out["hint"], "没提醒那个按钮在类型选错时也会成功"


def test_the_404_hint_stays_quiet_when_the_type_matches_the_url():
    """地址本来就是 Anthropic 的，就别再拿类型去烦用户。"""
    conf = {"type": "anthropic", "baseUrl": "https://api.anthropic.com",
            "apiKey": "sk", "model": "claude-x"}
    out = provider_probe._diagnose(_http_error(404), conf, "claude-x")
    assert "实例类型" not in out["hint"]


def test_the_404_hint_still_leads_with_url_and_model():
    """类型那条是**补充**，不能把原有的两条挤掉。"""
    conf = {"type": "openAI", "baseUrl": "https://api.deepseek.com",
            "apiKey": "sk", "model": "nope"}
    out = provider_probe._diagnose(_http_error(404), conf, "nope")
    assert "https://api.deepseek.com" in out["hint"]
    assert "nope" in out["hint"]


async def test_an_empty_model_id_gets_a_clean_message(record_build):
    """没填模型 ID 时，别把内核那句 AttributeError 甩给用户。

    复现时撞到过：空模型会让内核的 _model_for 去取一个本移植里不存在的默认模型，
    报 ``type object 'LLMModel' has no attribute 'gpt_4o_mini'`` —— 用户看不懂，
    也完全指不到"你没填模型"这件事上。
    """
    save_settings(providers={"p1": provider_conf(model="")}, activeProviderId="p1")

    out = await probe_provider("p1")

    assert out["ok"] is False
    assert "模型 ID" in out["error"]
    assert "gpt_4o_mini" not in str(out.get("error")), "又漏出内核那句原始报错了"
    assert not record_build.calls, "没填模型就不该去建 provider（也就不会发请求）"
