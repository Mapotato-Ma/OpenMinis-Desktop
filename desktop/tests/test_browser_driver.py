"""浏览器工具：驱动接线 + SSRF 守卫（用户第 4 件，选型路线 2）。

这一套断言**不需要浏览器**（不能假设跑测试的机器有 Edge/Playwright）：
* SSRF 守则是纯函数，直接测；
* 工具接线用 monkeypatch 把驱动换成假的，测"成功/失败如实传递"；
* 再加一条**一致性护栏**：schema 里声明的 action 必须有对应实现分支 ——
  漏一个就是模型喊了它、工具却答"不认识"。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from openminis.tools import browser_use_tool as tool_mod
from openminis.tools.browser import driver

ROOT = Path(__file__).resolve().parents[2]


# ── SSRF 守卫 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
    ],
)
def test_cloud_metadata_is_blocked(url):
    ok, why = driver.url_is_allowed(url)
    assert not ok, "云元数据地址必须拦 —— agent 打过去等于把机器凭据递出去"
    assert why


@pytest.mark.parametrize(
    "url",
    [
        "https://www.baidu.com",
        "http://127.0.0.1:3000/",  # 本地 dev server：故意放行
        "http://192.168.1.3:8000/",  # 内网：故意放行
        "https://example.com/a?b=c",
    ],
)
def test_normal_urls_are_allowed(url):
    ok, why = driver.url_is_allowed(url)
    assert ok, f"{url} 被误拦了：{why}"


def test_weird_schemes_are_refused():
    ok, why = driver.url_is_allowed("javascript:alert(1)")
    assert not ok and why


# ── schema 与实现的一致性 ───────────────────────────────────────────────
def test_every_declared_action_has_an_implementation_branch():
    """schema 里声明的 action 必须都有人接。

    漏一个的症状很隐蔽：模型照着 schema 喊 `fetch`，工具却回"不认识的 action" ——
    用户只会觉得"这工具怎么这么难用"。
    """
    src = (ROOT / "src" / "openminis" / "tools" / "browser_use_tool.py").read_text(
        encoding="utf-8"
    )
    m = re.search(r"BROWSER_ACTIONS[^=]*=\s*\((.*?)\)", src, re.S)
    assert m, "找不到 BROWSER_ACTIONS"
    declared = re.findall(r'"([a-z_]+)"', m.group(1))
    assert len(declared) >= 20, f"只解析出 {len(declared)} 个 action，正则可能过时了"
    missing = [a for a in declared if a not in driver.SUPPORTED_ACTIONS]
    assert not missing, f"这些 action 声明了但没有实现分支：{missing}"


# ── 工具接线：成功/失败都要如实 ─────────────────────────────────────────
def _args(**kw):
    return json.dumps({"tool_title": "浏览器", **kw})


def test_tool_reports_success_only_when_the_driver_succeeds(monkeypatch):
    async def fake_run(_args, _session=None):
        return "已打开 https://example.com"

    monkeypatch.setattr(driver, "run", fake_run)
    r = asyncio.run(tool_mod.BrowserUseTool.execute(_args(action="navigate"), "s1"))
    assert r.success and "已打开" in r.output


def test_tool_reports_failure_when_the_engine_is_missing(monkeypatch):
    async def fake_run(_args, _session=None):
        raise driver.BrowserUnavailable("没装浏览器驱动")

    monkeypatch.setattr(driver, "run", fake_run)
    r = asyncio.run(tool_mod.BrowserUseTool.execute(_args(action="navigate"), "s1"))
    assert not r.success, "引擎起不来时报成功 —— 又会骗过 agent"
    assert "没装浏览器驱动" in r.output, "没把原因告诉模型/用户"


def test_tool_reports_failure_when_the_action_blows_up(monkeypatch):
    async def fake_run(_args, _session=None):
        raise RuntimeError("ref 0 不存在或已失效")

    monkeypatch.setattr(driver, "run", fake_run)
    r = asyncio.run(tool_mod.BrowserUseTool.execute(_args(action="click", ref=0), "s1"))
    assert not r.success and "ref 0" in r.output


# ── 描述文案：别再把模型劝退 ────────────────────────────────────────────
def test_the_description_no_longer_says_the_engine_is_missing():
    src = (ROOT / "src" / "openminis" / "tools" / "browser_use_tool.py").read_text(
        encoding="utf-8"
    )
    # 描述是给模型看的：上面还写着"引擎未移植"，模型就会主动绕开这个工具。
    body = src.split('parameters={', 1)[0]
    assert "not yet ported" not in body, "工具描述里还留着「引擎未移植」—— 会把模型劝退"
    assert "snapshot" in body, "描述里没告诉模型「先 snapshot 拿 ref」这个用法"
