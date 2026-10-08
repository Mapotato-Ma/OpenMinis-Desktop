"""实时上下文状态（第 6 件）+ 手动压缩（第 5 件）。

用户原话：
　　「增加压缩会话的功能」「增加实时展示上下文状态的功能」。

两件是一件事的两半：**看不见占用就不知道何时该压**。所以测试也放一起：
* 内核把「这次请求的上下文多大 / 模型窗口多大」报出来；
* 界面上把它显示成「上下文 12.3k / 128k · 10%」，超 70% 变警示色；
* 压缩有手动入口（预览 → 确认 → 压 → 把摘要贴回对话）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "web" / "desktop" / "app.js"
MAIN_PY = ROOT / "src" / "openminis" / "server" / "main.py"
API_PY = ROOT / "src" / "openminis" / "server" / "chat_api.py"
MODEL_PY = ROOT / "src" / "openminis" / "data" / "model" / "__init__.py"


def _no_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"//[^\n]*", "", code)


def _js_fn(name: str) -> str:
    src = _no_comments(APP_JS.read_text(encoding="utf-8"))
    m = re.search(r"function " + name + r"\([^)]*\)\s*\{(.*?)\n\}", src, re.S)
    assert m, f"app.js 里找不到 {name}()"
    return m.group(1)


# ── 内核：上下文口径 ────────────────────────────────────────────────────
class _Usage:
    def __init__(self, i: int, read: int = 0, write: int = 0) -> None:
        self.input_tokens = i
        self.cache_read_input_tokens = read
        self.cache_creation_input_tokens = write


def test_openai_style_prompt_tokens_are_not_double_counted():
    """OpenAI 口径（含 DeepSeek）：input 已经是总量，缓存命中是它的子集。

    第一版若把所有字段相加，DeepSeek/OpenAI 用户的上下文会被算成两倍。
    """
    from openminis.server.main import _context_tokens_from_usage

    assert _context_tokens_from_usage(_Usage(1000, 800)) == 1000


def test_anthropic_style_adds_cache_to_input():
    """Anthropic 口径：input 只是没命中缓存的那部分，总量要把缓存读写加上。"""
    from openminis.server.main import _context_tokens_from_usage

    assert _context_tokens_from_usage(_Usage(50, 20000, 300)) == 20350


def test_the_window_helper_knows_common_models():
    from openminis.data.model import context_window_for

    assert context_window_for("deepseek-flash") == 128_000
    assert context_window_for("claude-sonnet-5") > 0
    # 用户自填的模型名（中转/自建网关）也要给个合理数字，不能是 0
    assert context_window_for("my-gateway-model") > 0


def test_the_usage_frame_reports_context_state():
    src = MAIN_PY.read_text(encoding="utf-8")
    m = re.search(r'"type": "usage",(.*?)\}\)', src, re.S)
    assert m, "找不到 usage 帧"
    body = m.group(1)
    assert '"contextTokens"' in body, "用量帧没报当前上下文大小"
    assert '"contextWindow"' in body, "用量帧没报模型窗口上限"


# ── 界面：显示 ──────────────────────────────────────────────────────────
def test_usage_frame_goes_through_the_context_renderer():
    src = _no_comments(APP_JS.read_text(encoding="utf-8"))
    m = re.search(r"case 'usage':(.*?)break;", src, re.S)
    assert m and "renderContextUsage" in m.group(1), (
        "用量帧没接到上下文显示上 —— 用户还是只看到累计花费"
    )


def test_the_context_display_shows_used_over_window_and_warns():
    body = _js_fn("renderContextUsage")
    assert "contextTokens" in body and "contextWindow" in body, "没读那两个字段"
    assert re.search(r"ctx-warn", body), "接近上限时没有警示态，用户看不出来"
    assert re.search(r"/\s*win\s*\)", body), "没算百分比 —— 光看两个数字看不出紧张程度"


# ── 压缩：接口 + 入口 ───────────────────────────────────────────────────
def test_the_compact_api_supports_preview_and_run():
    src = API_PY.read_text(encoding="utf-8")
    assert re.search(r'@router\.get\("/sessions/\{session_id\}/compact"\)', src), "缺压缩预览接口"
    assert re.search(r'@router\.post\("/sessions/\{session_id\}/compact"\)', src), "缺手动压缩接口"
    assert "turns_since_last_compact" in src, "预览没报「距上次压缩攒了多少轮」"
    assert "compact_session" in src, "没有真的调压缩"


def test_compact_confirm_previews_before_running():
    body = _js_fn("showCompactConfirm")
    assert "/compact`" in body, "没先读预览"
    assert "turnsSinceCompact" in body, "确认框里没告诉用户攒了多少轮"
    assert "runCompact" in body, "确认后没真的压"


def test_compact_result_is_shown_back_in_the_transcript():
    body = _js_fn("runCompact")
    assert "/compact`" in body and "POST" in body, "没发压缩请求"
    assert "appendCompactNote" in body, "压完没把摘要贴回对话 —— 用户看不到发生了什么事"


def test_compact_button_is_wired():
    src = _no_comments(APP_JS.read_text(encoding="utf-8"))
    html = (ROOT / "web" / "desktop" / "index.html").read_text(encoding="utf-8")
    assert "btnCompact" in html, "聊天标题栏里没有压缩按钮"
    assert re.search(r"\$\('btnCompact'\)\.addEventListener\('click'", src), "按钮没绑定"
