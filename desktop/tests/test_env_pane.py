"""环境变量设置页：**界面这一层**的契约。

内核侧早就齐了（值存在 ``sandbox.envExtra``、沙箱每次执行前整份注入、技能脚本里
``$KEY`` 直接可用；``skills_api`` 还有 ``GET/PUT /api/skills/env``）—— 缺的一直是
入口，用户只能手改 ``settings.json``。这个用例钉住"入口接对了"，因为这类接线
**不会报错，只会静默地什么都不发生**（点了保存却没存进去、切到面板一片空白）。
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent.parent / "web" / "desktop"
APP = (WEB / "app.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CSS = (WEB / "style.css").read_text(encoding="utf-8")


def test_settings_has_an_entry_that_switches_to_the_env_pane():
    assert 'data-pane="env"' in HTML, "设置侧栏里没有「环境变量」入口"
    assert re.search(r'<section class="settings-pane" data-pane="env">', HTML), "没有这个面板"
    assert "环境变量" in HTML
    assert re.search(r"if \(name === 'env'\) loadEnvVars\(\);", APP), (
        "切到该面板时没触发加载 —— 打开会是一片空白"
    )


def test_pane_talks_to_the_kernel_contract():
    """读 /skills/env、存回 /skills/env —— 路径写错不会报错，只会永远空着。"""
    assert re.search(r"await api\('/skills/env'\)", APP), "没读 /skills/env"
    assert re.search(r"api\('/skills/env',\s*\{\s*\n?\s*method: 'PUT'", APP), (
        "保存没走 PUT /skills/env"
    )
    # 后端是**整份覆盖**语义，前端就必须整体提交（而不是只发一条）
    assert "values: values || {}" in APP


def test_add_button_is_bound_and_keys_are_validated():
    # 绑定写法是 `{ const b = $('btnEnvAdd'); if (b) b.addEventListener(...) }`
    assert re.search(
        r"const b = \$\('btnEnvAdd'\);[^\n]*addEventListener\('click', envAdd\)", APP
    ), "「添加」按钮没接上"
    assert r"/^[A-Za-z_][A-Za-z0-9_]*$/" in APP, (
        "没有校验变量名 —— 塞进去一个 `1BAD-NAME` 在 shell 里根本不是变量"
    )


def test_reveal_state_survives_a_rerender():
    """打码/明文的开关必须存在 settings 上。

    第一版存进行对象里，而重画会重建每一行 —— 点「显示」闪一下又变回打码。
    """
    assert "settings.envReveal[key]" in APP
    assert re.search(r"reveal: !!\(settings\.envReveal", APP)


def test_rows_have_styles():
    for cls in (".env-row", ".env-key", ".env-val", ".env-add"):
        assert cls in CSS, f"缺样式 {cls}"
