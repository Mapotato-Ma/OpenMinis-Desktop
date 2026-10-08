"""死控件与竞态护栏（2026-10-08 审计修的那一批）。

每条都对应一个用户能碰到的具体故障，而且都是**静默**的 —— 不报错、不提示，
只是"点了没反应"或"界面显示的和实际的不一样"，所以必须靠测试钉住：

* 插件启用/停用发了后端不认的动作名 → 每次点都 404（前端词表与后端契约不一致）
* 命令面板里的「面板：代码/变更」指向根本不存在的面板 → 左栏整块空白，像界面坏了
* 快速切换会话 → A 的响应后到，把 B 的消息区覆盖成 A 的（标题是 B、消息是 A）
* 文件树三处并发调用 → 旧根的响应覆盖新树
* WS 断开时裸改 state.streaming → 发送键停在"停止"图标，点下去却是发送
* 市场/知识库的"只加载一次"标记在**请求前**置位 → 一次失败就永远停在"加载失败"，只能重启
* 顶栏危险模式芯片是 <button>、有 hover 反馈，却没有任何点击监听
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent.parent / "web" / "desktop"
APP = (WEB / "app.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    m = re.search(rf"^(?:async\s+)?function {name}\(", src, re.M)
    assert m, f"找不到函数 {name}"
    if name == "switchTab":
        end = src.index("\n}\n", m.start())
        return src[m.start():end]
    end = src.index("\n}\n", m.start())
    return src[m.start():end]


def test_plugin_toggle_uses_the_backend_vocabulary():
    """后端 plugin_action 只认 start/stop/restart（plugins_api.py）。"""
    m = re.search(r"/plugins/\$\{encodeURIComponent\(p\.id\)\}/\$\{p\.enabled \? '(\w+)' : '(\w+)'\}", APP)
    assert m, "找不到插件启停的调用"
    assert (m.group(1), m.group(2)) == ("stop", "start"), (
        f"发的还是 {m.group(1)}/{m.group(2)} —— 后端不认，必然 404"
    )


def test_switch_tab_ignores_unknown_panels():
    body = _fn(APP, "switchTab")
    assert re.search(r'\.tab-panel\[data-panel="\$\{name\}"\]', body) and "return" in body, (
        "switchTab 对不存在的面板名没有护栏 —— 会把所有面板取消激活，左栏空白"
    )


def test_command_palette_has_no_entries_for_missing_panels():
    panels = set(re.findall(r'class="tab-panel[^"]*" data-panel="(\w+)"', HTML))
    assert panels, "index.html 里一个 tab-panel 都没找到"
    called = set(re.findall(r"switchTab\('(\w+)'\)", APP))
    stale = {c for c in called if c not in panels}
    assert not stale, f"命令面板指向了不存在的面板：{sorted(stale)}"


def test_switching_sessions_checks_the_response_still_belongs():
    body = _fn(APP, "selectSession")
    guard = body.find("state.sessionId !== id")
    render = body.find("renderMessages(")
    assert guard != -1, "切会话没有归属校验"
    assert -1 < guard < render, "归属校验必须在 await 之后、渲染之前"


def test_file_tree_load_checks_ownership():
    body = _fn(APP, "loadTree")
    assert "state.treeSeq" in body and re.search(r"state\.treeSeq !== seq", body), (
        "文件树没有归属校验：启动/刷新/切工作区三处并发时旧响应会覆盖新树"
    )


def test_ws_close_goes_through_set_streaming():
    """发送键的图标由 updateComposerButton 决定，裸改状态会让图标与行为不一致。"""
    m = re.search(r"sock\.onclose\s*=\s*\(ev\)\s*=>\s*\{(.*?)\n  \};", APP, re.S)
    assert m, "找不到 onclose"
    body = m.group(1)
    assert "setStreaming(false)" in body, "onclose 没走统一入口"
    assert not re.search(r"^\s*state\.streaming\s*=", body, re.M), "onclose 里还在裸改 state.streaming"


def test_pane_load_flags_are_not_set_before_the_request():
    """标记只能在加载成功时置位；置在请求前 = 一次失败永久卡死。"""
    body = _fn(APP, "switchSettingsPane")
    for flag in ("settings.knowledgeLoaded = true", "settings.marketplaceLoaded = true"):
        assert flag not in body, f"{flag} 出现在面板切换里 —— 应在加载函数内部成功后才置位"
    for fn, flag in (("loadKnowledge", "settings.knowledgeLoaded"),
                     ("loadMarketplace", "settings.marketplaceLoaded")):
        src = _fn(APP, fn)
        assert f"{flag} = true" in src, f"{fn} 成功时没有置位"
        assert f"{flag} = false" in src, f"{fn} 失败时没有清标记，无法重试"


def test_danger_chip_is_clickable():
    assert re.search(r'id="dangerChip"', HTML)
    assert re.search(r"\$\('dangerChip'\)[^\n]*addEventListener\('click'", APP), (
        "顶栏那个危险模式芯片是 <button>、有 hover 反馈，却没人监听"
    )
