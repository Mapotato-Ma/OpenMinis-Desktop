"""凭据的两条承诺：日志里没有令牌（R18）、配置文件只有本用户可读（R17）。

产品口径（2026-10-11 拍板）：API Key 仍**明文**存在 settings.json（文档明说），
但要收紧它的权限、且绝不让它出现在给别人的东西里（日志、诊断包、界面回显）。
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path

import pytest

from desktop import redact


# ---------------------------------------------------------------------------
# R18 · 日志脱敏
# ---------------------------------------------------------------------------
def test_tokens_are_redacted_in_urls_and_headers():
    text = (
        "ready — http://127.0.0.1:8765/?k=SEKRET-TOKEN&x=1\n"
        "GET /_desktop/?token=ANOTHER\n"
        "headers: {'x-minis-access': 'HEADER-TOKEN'}\n"
    )
    out = redact.redact_text(text)

    for secret in ("SEKRET-TOKEN", "ANOTHER", "HEADER-TOKEN"):
        assert secret not in out, out
    assert out.count(redact.REDACTED) == 3
    # 不是凭据的部分一字不动
    assert "http://127.0.0.1:8765/" in out and "x=1" in out


def test_normal_log_lines_are_untouched():
    text = "backend ready on http://127.0.0.1:8765 / update failed: 校验不过"
    assert redact.redact_text(text) == text


def test_the_log_filter_rewrites_msg_and_args():
    handler = logging.Handler()
    handler.addFilter(redact.RedactionFilter())
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "url=%s", ("http://x/?k=TOKEN",), None
    )
    handler.filter(record)
    assert "TOKEN" not in record.getMessage()
    assert redact.REDACTED in record.getMessage()


def test_the_diagnostics_zip_redacts_older_logs(monkeypatch, tmp_path):
    """过滤器只管"以后"写下的日志；诊断包要连**以前**那份也一起兜住。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from desktop import ui_mount

    monkeypatch.setattr("desktop.paths.data_root", lambda: tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    (logs / "desktop.log").write_text(
        "ready — http://127.0.0.1:8765/?k=OLD-TOKEN\n", encoding="utf-8"
    )
    app = FastAPI()
    ui_mount.attach(app, desktop_dir=None, ui_active=False)
    with TestClient(app) as c:
        resp = c.get("/api/desktop/logs")
    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist()).decode("utf-8", "replace")
    assert "OLD-TOKEN" not in blob, "诊断包里还能看到访问令牌"
    assert redact.REDACTED in blob


def test_the_headless_ready_line_carries_no_token(tmp_path, monkeypatch, caplog):
    """headless 那条路以前会把**带令牌的完整地址**打进日志。"""
    import argparse

    from desktop import app as desktop_app
    from desktop import launcher

    monkeypatch.setattr(desktop_app, "start_server", _fake_server)
    monkeypatch.setattr(desktop_app, "_park", lambda server: 0)
    plan = launcher.BootPlan(
        host="127.0.0.1", port=8765, desktop_dir=None, ui_active=False, existing=None
    )
    args = argparse.Namespace(no_window=True, browser=False, debug=False)

    with caplog.at_level(logging.INFO, logger="openminis.desktop"):
        assert desktop_app._present_headless(plan, args) == 0

    assert "SEKRET" not in caplog.text, caplog.text
    assert "127.0.0.1:8765" in caplog.text


def _fake_server(**kwargs):  # noqa: ANN003, ANN202
    from desktop.server_runner import DesktopServer

    return DesktopServer(
        host="127.0.0.1", port=8765, access_token="SEKRET",  # noqa: S106 - 测试用
        _server=object(), _thread=_FakeThread(),
    )


class _FakeThread:
    def join(self, timeout: float | None = None) -> None:
        return None

    def is_alive(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# R17 · 权限收紧
# ---------------------------------------------------------------------------
def test_tighten_calls_icacls_with_owner_only(monkeypatch, tmp_path):
    from desktop import secrets_acl

    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stderr = ""

    def _run(argv, **kw):  # noqa: ANN001, ANN003, ANN202
        calls.append(list(argv))
        return _Proc()

    target = tmp_path / "settings.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(secrets_acl.sys, "platform", "win32")
    monkeypatch.setattr(secrets_acl.subprocess, "run", _run)
    monkeypatch.setenv("USERNAME", "Mapotato")

    note = secrets_acl.tighten(target)

    assert note.startswith("ok:"), note
    argv = " ".join(calls[0])
    assert "icacls" in argv and "/inheritance:r" in argv
    assert "Mapotato:(F)" in argv, argv


def test_tighten_reports_failure_honestly(monkeypatch, tmp_path):
    """改不动就如实说改不动 —— 不许假装"已加密/已收紧"。"""
    from desktop import secrets_acl

    class _Proc:
        returncode = 5
        stderr = "Access is denied"

    target = tmp_path / "settings.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(secrets_acl.sys, "platform", "win32")
    monkeypatch.setattr(secrets_acl.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setenv("USERNAME", "Mapotato")

    assert secrets_acl.tighten(target).startswith("failed:")


def test_tighten_skips_off_windows(monkeypatch, tmp_path):
    from desktop import secrets_acl

    target = tmp_path / "settings.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(secrets_acl.sys, "platform", "linux")

    assert secrets_acl.tighten(target).startswith("skipped:")


# ---------------------------------------------------------------------------
# 界面/接口不回显明文密钥
# ---------------------------------------------------------------------------
def test_the_settings_api_never_echoes_the_key(tmp_path, monkeypatch):
    """界面只需要知道"有没有配"（hasKey），永远不需要明文把它带回来。"""
    from fastapi.testclient import TestClient

    from openminis.server.main import app
    from openminis.settings.store import SettingsStore

    store = SettingsStore(path=tmp_path / "settings.json")
    monkeypatch.setattr(SettingsStore, "get", classmethod(lambda cls: store))
    secret = "sk-super-secret-123"
    with TestClient(app) as c:
        r = c.put("/api/settings", json={"providers": [
            {"id": "gw", "type": "openAI", "apiKey": secret, "model": "", "baseUrl": ""}
        ]})
        assert r.status_code == 200, r.text
        assert secret not in r.text, "保存的响应里回显了密钥"
        body = c.get("/api/settings")
        assert secret not in body.text, "读取设置的响应里回显了密钥"
        assert "revision" in body.json(), "没有给出配置指纹（R11 的乐观并发要用它）"
