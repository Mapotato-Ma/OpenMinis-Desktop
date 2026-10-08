"""输入区那颗按钮：生成中=停止、空闲=发送；生成中再发消息要**排队而不是丢掉**。

用户原话（2026-10-06）：
  「停止AI回答的按钮不要在右上角，发出消息之后发送按钮就变成停止。
    再输入发送消息就变成排队，不要发不出去。」

改前：右上角 `#btnStop`，而生成中按发送 → `sendNow()` 只 toast 一句
「上一条还在处理中」就 return —— **打好的字直接丢在地上**。

静态断言一律先剥 JS 注释（同一个坑踩过两次：注释里提到关键字，真代码删掉测试照样绿）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "web" / "desktop" / "app.js"
INDEX_HTML = ROOT / "web" / "desktop" / "index.html"


def _no_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"//[^\n]*", "", code)


def _app() -> str:
    return _no_comments(APP_JS.read_text(encoding="utf-8"))


def _fn(name: str) -> str:
    src = _app()
    m = re.search(r"function " + name + r"\([^)]*\)\s*\{(.*?)\n\}", src, re.S)
    assert m, f"app.js 里找不到 {name}() —— 结构变了，先看这个测试还认不认"
    return m.group(1)


def _case(label: str) -> str:
    # 收尾必须钉 **case 层级**的 break（6 个空格缩进）—— 第一版写 `(.*?)break;`，
    # 被 case 内层 `if (state.termBusy) { … break; }` 提前截断，于是拿到的
    # 半个分支里当然找不到 flushQueue()。
    m = re.search(r"case '" + label + r"':(.*?)\n      break;", _app(), re.S)
    assert m, f"app.js 里找不到 case '{label}':"
    return m.group(1)


def test_a_message_typed_while_generating_is_queued_not_dropped():
    # 真正的发送逻辑在 async function send() 里；sendNow() 只是它的一个包装
    # （第一版写成 _fn("sendNow") —— 那个函数体只有 72 字，当然找不到入队代码）。
    body = _fn("send")
    assert re.search(r"state\.queue\.push", body), (
        "生成中再发消息没有进队列 —— 又回到「发不出去」了"
    )
    assert "上一条还在处理中" not in _app(), (
        "旧的阻断式提示还在 —— 它会吞掉用户输入的整条消息"
    )


def test_the_queue_is_drained_when_a_turn_finishes():
    assert "flushQueue()" in _case("done"), (
        "这一轮结束了却没去取排队的消息 —— 队列会永远堆着"
    )
    assert "flushQueue()" in _case("error"), (
        "这一轮失败了就没收队列 —— 排队的内容被困住（静默吞消息是最糟的失败方式）"
    )


def test_flush_actually_sends_through_send_turn():
    body = _fn("flushQueue")
    assert re.search(r"sendTurn\(", body), "出队后没走统一的发送函数"
    assert re.search(r"state\.streaming", body), "没检查当前是否还在生成，可能并发发出两条"


def test_the_send_button_becomes_stop_while_generating():
    body = _fn("updateComposerButton")
    assert re.search(r"""\$\(['"]btnSend['"]\)""", body), "没有去改输入区那颗按钮"
    assert re.search(r"name.{0,40}(square|arrow-up)", body) or "setAttribute" in body, (
        "按钮图标不随生成状态切换"
    )
    assert re.search(r"state\.streaming", body), "没有按生成状态分支"
    # 光有函数不算数 —— 还得有人调它。第一版只断言了函数体，
    # 把 setStreaming 里的调用删掉测试照样绿（反向自检当场抓到）。
    assert re.search(r"updateComposerButton\(\)", _fn("setStreaming")), (
        "改了生成状态却不更新那颗按钮 —— 它会一直显示「发送」"
    )


def test_the_top_right_stop_button_is_gone():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "btnStop" not in html, (
        "右上角那颗「停止」还在 —— 用户要求停止按钮不要放在右上角"
    )
    assert "btnStop" not in _app(), "app.js 里还留着 btnStop 的引用（会拿到 null）"
