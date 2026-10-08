"""沙箱确认的界面契约：内核在等界面的回答，界面不回就是**白等 180 秒**。

用户第 2 件：「沙箱拦截不要直接硬拦，执行命令时增加一个工具来请求用户确认。
用户确认执行就不要拦了。」

内核侧会把拦截包成 `guard_confirm` 帧送出来、然后 await 回答（180 秒超时按拒绝处理）。
所以这条链路断掉的症状**不是报错，而是每次拦截都卡三分钟** —— 沉默的坏法必须有护栏。
静态断言一律先剥 JS 注释（同一个坑踩过三次：注释里提到关键字，真代码删掉测试照样绿）。
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


def _js() -> str:
    return _no_comments(APP_JS.read_text(encoding="utf-8"))


def _fn(name: str) -> str:
    m = re.search(r"function " + name + r"\([^)]*\)\s*\{(.*?)\n\}", _js(), re.S)
    assert m, f"app.js 里找不到 {name}() —— 结构变了，先看这个测试还认不认"
    return m.group(1)


def test_the_confirm_frame_is_dispatched_and_drawn():
    src = _js()
    m = re.search(r"case 'guard_confirm':(.*?)break;", src, re.S)
    assert m, "handleFrame 不再处理 guard_confirm —— 内核会一直等到超时"
    assert "showGuardConfirm" in m.group(1), "收到了确认帧却没有交给弹层去画"


def test_the_answer_carries_the_request_id_and_a_decision():
    body = _fn("answerGuardConfirm")
    assert "guard_confirm_answer" in body, "回答帧的类型名变了"
    assert "requestId" in body, "回答没带 requestId —— 内核认不出这是哪一条"
    assert "decision" in body, "回答没带决定"


def test_all_four_decisions_are_offered():
    body = _fn("showGuardConfirm")
    # 四个选项是作为参数传给 mk(label, decision) 的，不是字面调用 —— 第一版断言
    # 写成 answerGuardConfirm('once') 直接找不到（护栏自己写错了断言）。
    for decision in ("'deny'", "'once'", "'session'", "'always'"):
        assert re.search(r"mk\([^)]*" + re.escape(decision) + r"[^)]*\)", body), (
            f"弹层里少了 {decision} 这个选项"
        )


def test_escape_answers_deny_instead_of_leaving_the_kernel_waiting():
    src = _js()
    assert re.search(r"state\.pendingConfirm\)\s*\{\s*answerGuardConfirm\('deny'\)", src), (
        "Esc 不会把待回答的确认判为拒绝 —— 界面看着干净了，内核还在等 180 秒"
    )


def test_the_pending_confirm_is_cleared_after_answering():
    body = _fn("answerGuardConfirm")
    assert re.search(r"pendingConfirm\s*=\s*''", body), (
        "回答后没清掉 pendingConfirm —— 后续 Esc 会重复发帧"
    )


def test_danger_mode_switch_exists_and_posts_to_the_api():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "guardModeDanger" in html, "沙箱页里没有危险模式开关"
    assert "dangerChip" in html, "顶栏没有危险模式的常驻警示（开了危险模式却看不见）"
    body = _fn("toggleGuardMode")
    assert re.search(r"['\"]/guard/mode['\"]", body), "开关没有调 /guard/mode 接口"
    assert re.search(r"danger\s*\?\s*'danger'\s*:\s*'normal'", body), "开关没有正确映射两种模式"


def test_danger_mode_is_reflected_on_load():
    """进「沙箱」页时要问一次内核当前模式，不能只靠本地状态猜。"""
    assert re.search(r"loadGuardMode\(\)", _js()), "没有任何地方去读当前守卫模式"
    body = _fn("setGuardModeUi")
    assert "dangerChip" in body and "guardModeDanger" in body
