"""顶部那颗「工作目录」芯片必须跟着左侧的工作区走。

用户实测（2026-10-06）：左侧文件面板选了「桌面 · F:\\桌面」，
点中间那颗芯片打开的却是 ``C:\\Users\\…\\openminis\\workspace``。

两个原因，都在前端：

1. ``#wsPath``（芯片上的字）**全前端没人更新过** —— 永远显示字面量 "workspace"；
2. ``openWorkspace()`` 读的是 ``state.info.workspace``，那是**内核默认沙盒**
   （``<data>/workspace``），跟左侧选中的项目目录根本不是一回事。

⚠️ 两条断言都必须钉**真正的赋值/取值**，不能只断言"函数体里出现某个词"：
第一版写成 ``"wsPath" in body``，把赋值那一行删掉照样绿（``const chipLabel =
$('wsPath')`` 还在）。同理 ``openWorkspace`` 里 ``currentWorkspace`` 出现在
``find()`` 里，光看它在不在也拦不住退回默认沙盒。
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[2] / "web" / "desktop" / "app.js"


def _no_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"//[^\n]*", "", code)


def _fn(name: str) -> str:
    src = _no_comments(APP_JS.read_text(encoding="utf-8"))
    m = re.search(r"function " + name + r"\([^)]*\)\s*\{(.*?)\n\}", src, re.S)
    assert m, f"app.js 里找不到 {name}() —— 结构变了，先看这个测试还认不认"
    return m.group(1)


def test_the_chip_label_follows_the_current_workspace():
    body = _fn("renderWorkspacePicker")
    assert re.search(r"""\$\(['"]wsPath['"]\)[\s\S]{0,120}?\.textContent\s*=""", body), (
        "芯片标签没有被赋值 —— 会永远显示字面量 workspace，"
        "用户看不出 agent 到底在哪个目录里干活"
    )
    assert re.search(r"""\$\(['"]wsChip['"]\)[\s\S]{0,120}?\.title\s*=""", body), (
        "芯片的 title 没跟着走，鼠标悬停看到的还是旧信息"
    )


def test_the_chip_opens_the_selected_workspace_not_the_default_sandbox():
    body = _fn("openWorkspace")
    assert re.search(r"currentWorkspace", body) and re.search(r"\.path", body), (
        "不再去查当前工作区的路径了"
    )
    assert re.search(r"\bp\s*=\s*bound\s*\|\|", body), (
        "打开的目标不再优先用绑定的工作区路径 —— 退回内核默认沙盒了"
    )
    assert re.search(r"state\.info", body), "默认沙盒的兜底没了 —— 没绑工作区时会打不开"
