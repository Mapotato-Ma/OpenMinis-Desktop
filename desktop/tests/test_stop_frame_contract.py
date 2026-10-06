"""停止帧的契约：**前端**必须带 session_id。

为什么专门为"前端发了什么"写一条测试：
内核那边早就有测试（``test_stop_cancels_running_turn``），而且它显式传了
``{"sessionId": "s1"}`` —— 于是"客户端到底发不发这个字段"根本没人测。
2026-10-06 用户实测「停止按钮从来没生效过」，坏掉的正好是这半边，
而内核那半边的测试一直是绿的。

静态检查比"再测一遍内核"更能钉住它：改的是 app.js，不是内核。
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[2] / "web" / "desktop" / "app.js"


def _source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _stop_turn_body() -> str:
    m = re.search(r"function stopTurn\(\)\s*\{(.*?)\n\}", _source(), re.S)
    assert m, "app.js 里找不到 stopTurn() —— 结构变了，先看这个测试还认不认"
    return m.group(1)


def _strip_js_comments(code: str) -> str:
    """去掉 // 与 /* */ 注释。

    必须去 —— 第一版忘了去，于是把 ``session_id`` 从真代码里删掉、
    只有上方注释里还留着它，测试照样绿（反向自检当场抓到的假绿）。
    """
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"//[^\n]*", "", code)


def test_the_stop_frame_carries_the_session_id():
    body = _strip_js_comments(_stop_turn_body())
    call = re.search(r"wsSend\(\{(.*?)\}\)", body, re.S)
    assert call, "stopTurn 里找不到 wsSend 调用 —— 结构变了，先看这个测试还认不认"
    assert "type: 'stop'" in call.group(1), "这不再是停止帧了"
    assert "session_id" in call.group(1), (
        "停止帧没带 session_id —— 内核按 _RUNNING_CHATS[sid] 找要取消的那一轮，"
        "sid 为空就什么都取消不了，却照样回 stopped:true（真机 bug，2026-10-06）"
    )


def test_the_send_button_while_streaming_goes_through_stop_turn():
    """生成过程中按发送键 = 停止。它必须走同一个 stopTurn()，别各写一份。"""
    src = _strip_js_comments(_source())
    assert re.search(r"if \(state\.streaming\) \{ stopTurn\(\); return; \}", src), (
        "发送键在生成中的分支不再调用 stopTurn() —— 停止逻辑被复制了一份？"
    )


def test_the_done_handler_does_not_fake_a_stop():
    """服务端说 stopped:false 时不能显示成"已停止"。"""
    src = _strip_js_comments(_source())
    assert "stopped === false" in src, (
        "done 分支不再区分 stopped 真假 —— 会把『这一轮早就结束了』"
        "显示成『你停掉了它』"
    )
