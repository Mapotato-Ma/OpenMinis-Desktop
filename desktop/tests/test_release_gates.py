"""发布门禁与可复现性（R19）。

这几条都不是"读文档确认一下"，而是**让流水线配置本身受检**：以前"等两边都绿再发版"
靠人记着看，而 `continue-on-error: true` + `exit 0` 会让"窗口模式崩了"照样发出去。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
BUILD = ROOT / ".github" / "workflows" / "build-windows.yml"
VERIFY = ROOT / ".github" / "workflows" / "verify.yml"


def _build() -> dict:
    return yaml.safe_load(BUILD.read_text(encoding="utf-8"))


def _verify() -> dict:
    return yaml.safe_load(VERIFY.read_text(encoding="utf-8"))


def test_release_waits_for_the_check_suite():
    """打包/发版必须**依赖** verify —— 不是靠人记着看两个 workflow 都绿。"""
    jobs = _build()["jobs"]
    assert "verify" in jobs, "build 流水线里没有 verify job"
    assert jobs["verify"]["uses"] == "./.github/workflows/verify.yml", (
        "verify 不是复用那条流水线（两处各写一遍迟早漂移）"
    )
    needs = jobs["portable"].get("needs")
    assert needs == "verify" or (isinstance(needs, list) and "verify" in needs), needs


def test_verify_workflow_is_reusable():
    on = _verify().get("on") or _verify().get(True)  # YAML 1.1 把 `on` 解析成 True
    assert "workflow_call" in on, "verify.yml 不能被复用（缺 workflow_call）"


def test_the_windowed_probe_cannot_silently_swallow_a_crash():
    """窗口模式崩了必须红。

    改前那一步是 `continue-on-error: true` + 结尾 `exit 0`：日志里喊一声
    "窗口模式崩了"，流水线照旧绿、release 照旧发。
    """
    steps = _build()["jobs"]["portable"]["steps"]
    gate = [s for s in steps if "Gate" in str(s.get("name", ""))]
    assert gate, "没有「窗口探针的判定」这一步"
    gate_body = str(gate[0].get("run", ""))
    assert "exit 1" in gate_body, "崩了也不失败 —— 门禁是假的"
    assert "crash" in gate_body

    probe = [s for s in steps if "windowed" in str(s.get("name", "")).lower()]
    assert probe, "窗口探针那一步不见了"
    probe_body = str(probe[0].get("run", ""))
    assert "windowed-probe.json" in probe_body, "探针没有把结论落盘给判定那一步看"
    assert "exit 0" in probe_body, "探针自己不该决定红绿（转交判定那一步）"


def test_the_build_installs_from_the_lockfile():
    """依赖按 uv.lock 装 —— 改前是 `pip install -e .` + 两条浮动的 `>=`，
    于是"壳没变、依赖变了"会被判成可以只换载荷。"""
    steps = _build()["jobs"]["portable"]["steps"]
    install = next(s for s in steps if "Install dependencies" in str(s.get("name", "")))
    body = str(install["run"])
    assert "uv sync --frozen" in body, "没有按锁文件安装"
    assert "pip install -e ." not in body

    payload = next(s for s in steps if "payload" in str(s.get("name", "")).lower())
    assert "uv run python" in str(payload["run"]), "打包没用锁定环境的解释器"


def test_the_shell_id_covers_dependencies_and_the_runtime_manifest():
    """壳指纹要算依赖：依赖变了就该整包（而不是发一个装不起来的"小更新"）。"""
    from scripts import shell_id

    assert "pyproject.toml" in shell_id.SHELL_INPUTS
    assert "uv.lock" in shell_id.SHELL_INPUTS
    assert shell_id.compute(), "算不出指纹"


def test_runtime_manifest_records_what_was_actually_installed(tmp_path):
    out = tmp_path / "runtime.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_runtime_manifest.py"),
         "--out", str(out)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert payload["python"].startswith("3."), payload
    assert payload["deps"].get("fastapi"), "直接依赖里没记到 fastapi"
    assert all(isinstance(v, str) and v for v in payload["deps"].values())
