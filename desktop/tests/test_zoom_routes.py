"""``/api/desktop/zoom`` 的口径测试。

界面靠这个接口决定「缩放归谁」，所以口径必须稳定：
  * GET 只报告握到手的东西（不许声称支持）；
  * POST 里「不是数字」是客户端错误（400），「窗口给不了」是能力问题（200 + ok=false）；
  * 两条都不会抛 —— 它在启动路径上，抛了界面就哑了。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from desktop import native_zoom, ui_mount
from desktop.ui_mount import KERNEL_CATCH_ALL_PATH, attach


@pytest.fixture(autouse=True)
def _clean():
    native_zoom.reset_for_tests()
    yield
    native_zoom.reset_for_tests()


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    ui = tmp_path / "web-desktop"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    app = FastAPI()

    @app.get(KERNEL_CATCH_ALL_PATH)
    async def _spa(full_path: str):  # noqa: ANN202, ARG001
        return {"detail": "spa"}

    attach(app, desktop_dir=ui, ui_active=True)
    return TestClient(app)


def test_capability_is_honest_without_a_window(client: TestClient):
    res = client.get("/api/desktop/zoom")
    assert res.status_code == 200
    body = res.json()
    assert body["handle"] is False
    assert body["reason"], "没有控件时必须给出原因，否则界面只能显示“不可用”"
    assert res.headers.get("cache-control") == "no-store", "能力快照不许被缓存"


def test_bad_factor_is_a_client_error(client: TestClient):
    for bad in ("nope", None, [1.4], True):
        res = client.post("/api/desktop/zoom", json={"factor": bad})
        assert res.status_code == 400, f"{bad!r} 应当 400"
        assert "必须是数字" in res.json()["error"]


def test_unusable_window_is_200_with_ok_false(client: TestClient):
    res = client.post("/api/desktop/zoom", json={"factor": 1.44})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert body["reason"]


def test_broken_body_does_not_blow_up(client: TestClient):
    res = client.post("/api/desktop/zoom", content=b"not json",
                      headers={"content-type": "application/json"})
    assert res.status_code == 400
