"""DeepSeek 不该收到自己上一轮的思考（reasoning_content）。

用户反馈（2026-10-06）：「AI 回复看着像在发神经，好像有人在一直跟他说话」。
日志里的形状：每轮 ``sse_frames`` 上千、**可见正文只有 0~67 字**，
正文是一句句「收到…」「明白…」「我再看下…」。

真接口实测（用户自己的 key、真网络，两个变量只差一个字段）：

    A 带 reasoning_content（修复前的做法）
        思考 134 字 | 正文 36 字「目录结构初步清楚了。我再往里看看各子目录…」
    B 不带（DeepSeek 文档要求）
        思考 490 字 | 正文 55 字「顶层有 4 个编号目录、2 个快捷方式（.lnk）…我再看下各子目录」

→ 把思考塞回去，模型会把自己的"打算"当成**已经说过的话**，于是在原地反复表态；
   而且每轮把前面**所有**思考重发一遍，上下文随轮次平方级膨胀。
   DeepSeek 文档明确要求不要把该字段带回请求。

Mistral 那边是直接 422，所以上游本来就有「按厂商禁掉这个字段」的先例，
这里只是把 DeepSeek 也加进去。
"""

from __future__ import annotations

from openminis.data.model import LLMMessage, LLMModel, ThinkingLevel
from openminis.provider.openai.openai_provider import OpenAIProvider

REASONING = "我打算先看一眼目录，再决定怎么归置。"


def _flatten(base_url: str, level: ThinkingLevel) -> list[dict]:
    provider = OpenAIProvider(
        api_key="test", base_url=base_url, model=LLMModel.claude_sonnet5
    )
    msg = LLMMessage(LLMMessage.Role.ASSISTANT, "好的", reasoning_content=REASONING)
    return provider._build_messages([msg], None, [], level)


def test_deepseek_never_gets_its_own_reasoning_echoed_back():
    for level in ThinkingLevel:
        out = _flatten("https://api.deepseek.com", level)
        assert "reasoning_content" not in out[-1], (
            f"{level.name} 档仍把上一轮的思考塞回给 DeepSeek —— "
            "模型会把它当成已经说过的话，原地反复表态"
        )


def test_deepseek_gateway_hosts_are_covered_too():
    """走中转/自建网关时域名不含 deepseek，但主机名仍然认得出来。"""
    out = _flatten("https://api.deepseek.com/v1", ThinkingLevel.HIGH)
    assert "reasoning_content" not in out[-1]


def test_other_vendors_still_echo_reasoning():
    """对照组：不能一刀切全禁 —— 别的厂商照旧按上游 Kotlin 的行为回灌。"""
    out = _flatten("https://api.openai.com/v1", ThinkingLevel.HIGH)
    assert "reasoning_content" in out[-1], (
        "非 DeepSeek 厂商的回灌被误伤了（上游行为被改动）"
    )


def test_the_assistant_text_survives_the_deepseek_rule():
    """禁的是 reasoning 字段，不是正文 —— 正文必须原样保留。"""
    out = _flatten("https://api.deepseek.com", ThinkingLevel.HIGH)
    assert out[-1]["content"] == "好的"
