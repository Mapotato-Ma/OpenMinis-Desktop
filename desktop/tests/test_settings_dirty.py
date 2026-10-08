"""「未保存」必须覆盖用户真的改过的东西 —— 否则界面会静默吞掉用户输入。

这个文件存在的理由（2026-10-08 审计发现两个真事故）：

1. **刚填的 API Key 不算未保存**。保存按钮的显隐、关闭设置时的「有未保存的改动」
   提示，都只看 ``dirty.any``；而 ``dirty.any`` 只算了 providers/slots/agent/identity。
   于是"只填了密钥"的用户看不到保存按钮、关面板也没有任何提示 —— **密钥就没了**。
2. **人格根本不进草稿**。人格那 5 个字段走 DOM 直读 + ``/system/soul`` 这条独立的路，
   不在 ``SettingsModel`` 里；用户改完人格顺手点顶部的「保存」（那个按钮只管草稿），
   人格改动**静默消失**，而且关闭护栏（``sdirty()``）也不会拦。

这两条都是"界面看着在正常工作"的静默故障，所以用静态护栏钉住。
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent.parent / "web" / "desktop"
APP = (WEB / "app.js").read_text(encoding="utf-8")
MODEL = (WEB / "settings-model.js").read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    """取一个函数/箭头函数的函数体（到下一行的顶格 ``}`` 为止）。"""
    m = re.search(rf"^(?:async\s+)?function {name}\(", src, re.M)
    assert m, f"找不到函数 {name}"
    end = src.index("\n}\n", m.start())
    return src[m.start():end]


# ── 密钥：刚敲进去的必须算未保存 ──────────────────────────────────────────

def test_typed_api_key_counts_as_dirty():
    # toPayload 是对象字面量里的方法（不是 function 声明），所以这里直接匹配源码：
    # 两条都只会在 toPayload 里出现一次。
    assert re.search(r"keys:\s*Object\.keys\(state\.keys", MODEL), "dirty 里没有 keys 这一项"
    assert re.search(r"dirty\.any\s*=\s*dirty\.models\s*\|\|\s*dirty\.agent\s*\|\|\s*dirty\.identity\s*\|\|\s*dirty\.keys", MODEL), (
        "keys 没有并进 dirty.any —— 保存按钮的显隐看的是 any"
    )


# ── 人格：进未保存态，并且跟着全局保存一起提交 ────────────────────────────

def test_soul_dirtiness_is_computed_against_the_loaded_baseline():
    assert re.search(r"settings\.soulServer\s*=\s*soulForm\(\)", APP), (
        "loadSoul 没有留下基线，无法判断人格改没改"
    )
    body = _fn(APP, "soulDirty")
    for field in ("name", "emoji", "style", "lang", "body"):
        assert f"f.{field} !== s.{field}" in body, f"人格字段 {field} 没参与比较"


def test_sdirty_reports_soul_edits():
    body = _fn(APP, "sdirty")
    assert "soulDirty()" in body, "sdirty() 没有合并人格的未保存态"
    assert re.search(r"d\.any\s*=\s*true", body), "合并了灵魂但没把 any 抬起来"


def test_global_save_also_commits_soul():
    """用户眼里只有一个「保存」按钮 —— 它必须把人格也存了。"""
    body = _fn(APP, "saveSettings")
    assert re.search(r"soulDirty\(\)", body), "全局保存没管人格"
    assert re.search(r"saveSoul\(\{\s*silent:\s*true\s*\}\)", body), (
        "全局保存没有真正调用 saveSoul"
    )


def test_every_soul_field_marks_the_form_dirty():
    assert re.search(
        r"for \(const id of \['soulName',\s*'soulEmoji',\s*'soulStyle',\s*'soulLang',\s*'soulBody'\]\)",
        APP,
    ), "人格字段没有统一挂监听（原来只有 soulBody 有个数字符的监听）"
    assert APP.count("node.addEventListener('input', updateDirtyUI)") >= 1
    assert APP.count("node.addEventListener('change', updateDirtyUI)") >= 1, (
        "wa-select 走 change 事件，只挂 input 的话改语言不会亮保存按钮"
    )
