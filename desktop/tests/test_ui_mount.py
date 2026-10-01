"""桌面路由挂载的契约测试。

这里锁的是 `desktop/ui_mount.py` 唯一的失败模式：**路由排在内核兜底路由之后**。
它是静默的 —— 请求被兜底吞掉（返回首页 HTML 或 JSON 404），没有任何报错。
曾经这条规则靠「每个 attach_* 各自 insert(0)」的调用顺序碰巧成立，
而 `/_desktop/window-bootstrap.js` 这种「磁盘上没有、由处理器现算」的路由，
一旦排到 `Mount("/_desktop", StaticFiles(...))` 后面就会被静态挂载接走并 404。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.routing import Mount, Route

from desktop import ui_mount
from desktop.ui_mount import (
    DESKTOP_MOUNT_PATH,
    KERNEL_CATCH_ALL_PATH,
    attach,
    catch_all_index,
    ordering_problems,
)


def kernel_like_app() -> FastAPI:
    """一个形状与内核一致的最小 app：末尾一条兜底路由，后面注册什么都不命中。"""
    app = FastAPI()

    @app.get("/api/health")
    async def _health():  # noqa: ANN202
        return {"ok": True}

    @app.get(KERNEL_CATCH_ALL_PATH)
    async def _spa(full_path: str):  # noqa: ANN202, ARG001
        return {"detail": "spa fallback"}

    return app


@pytest.fixture()
def ui_dir(tmp_path: Path) -> Path:
    d = tmp_path / "web-desktop"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>桌面界面</title>", encoding="utf-8")
    (d / "app.js").write_text("// desktop ui", encoding="utf-8")
    return d


def paths(app: FastAPI) -> list[str]:
    return [str(getattr(r, "path", r)) for r in app.router.routes]


# ── 契约 1：全部桌面路由都在兜底路由之前 ────────────────────────────────
def test_every_desktop_route_lands_before_the_kernel_catch_all(ui_dir: Path):
    app = kernel_like_app()
    mounted = attach(app, desktop_dir=ui_dir, ui_active=True)

    assert mounted is True
    assert ordering_problems(app) == [], f"顺序不变量被破坏: {ordering_problems(app)}"
    # 该有的都在
    for expected in (
        f"{DESKTOP_MOUNT_PATH}/window-bootstrap.js",
        "/api/desktop/info",
        "/api/desktop/test-provider",
        "/api/desktop/chat-readiness",
        "/api/desktop/clear-data",
        "/api/desktop/logs",
        "/api/desktop/logs/where",
        "/api/desktop/logs/save",
        "/",
        f"{DESKTOP_MOUNT_PATH}/",
    ):
        assert expected in paths(app), f"缺路由 {expected}"


def test_catch_all_is_still_found_and_still_last(ui_dir: Path):
    app = kernel_like_app()
    attach(app, desktop_dir=ui_dir, ui_active=True)
    idx = catch_all_index(app)
    assert idx is not None, "内核兜底路由不见了 —— 那我们的路由反而都安全，但要显式知道"
    assert idx == len(app.router.routes) - 1, "兜底路由不再在末尾，顺序契约需要重新审视"


# ── 契约 2：精确路径必须排在静态挂载之前 ────────────────────────────────
def test_bootstrap_wins_over_the_static_mount(ui_dir: Path):
    """`/_desktop/window-bootstrap.js` 在磁盘上不存在，靠处理器现算。

    排到 Mount 后面就会被 StaticFiles 接走 → 404 → 界面以为自己在普通浏览器里。
    """
    app = kernel_like_app()
    attach(app, desktop_dir=ui_dir, ui_active=True)
    routes = app.router.routes
    boot = next(i for i, r in enumerate(routes) if getattr(r, "path", None).endswith("window-bootstrap.js"))
    mount = next(i for i, r in enumerate(routes) if isinstance(r, Mount) and r.path == DESKTOP_MOUNT_PATH)
    assert boot < mount


def test_mount_is_last_of_the_desktop_routes(ui_dir: Path):
    app = kernel_like_app()
    routes = ui_mount.desktop_routes(desktop_dir=ui_dir, ui_active=True)
    assert isinstance(routes[-1], Mount), "静态挂载应当是清单的最后一项"
    assert all(isinstance(r, Route) for r in routes[:-1])


# ── 契约 3：界面产物缺失时优雅降级，且如实汇报 ──────────────────────────
def test_missing_assets_keeps_the_apis_but_not_the_ui(tmp_path: Path):
    app = kernel_like_app()
    mounted = attach(app, desktop_dir=tmp_path / "nope", ui_active=True)

    assert mounted is False, "没有 index.html 就不该声称界面挂上了"
    got = paths(app)
    # 界面相关的不注册
    assert f"{DESKTOP_MOUNT_PATH}/" not in got
    assert "/" not in got
    assert not any(isinstance(r, Mount) for r in app.router.routes)
    # 但壳层仍然能启动，并且接口照旧
    assert "/api/desktop/info" in got
    assert f"{DESKTOP_MOUNT_PATH}/window-bootstrap.js" in got
    assert ordering_problems(app) == []


def test_desktop_dir_none_is_a_valid_state():
    app = kernel_like_app()
    assert attach(app, desktop_dir=None, ui_active=True) is False
    assert "/api/desktop/info" in paths(app)


def test_attach_is_idempotent(ui_dir: Path):
    """内核的 app 是模块级单例 —— 装两次不该出现两套路由。"""
    app = kernel_like_app()
    assert attach(app, desktop_dir=ui_dir, ui_active=True) is True
    before = paths(app)
    assert attach(app, desktop_dir=ui_dir, ui_active=True) is True

    assert paths(app) == before, "第二次 attach 不该改变路由表"
    assert paths(app).count("/api/desktop/info") == 1


# ── 契约 4：自检真的能发现被吞掉的路由 ──────────────────────────────────
def test_shadowed_detection_catches_a_route_after_the_catch_all(ui_dir: Path):
    app = kernel_like_app()
    attach(app, desktop_dir=ui_dir, ui_active=True)

    async def _late():  # noqa: ANN202
        return {"ok": True}

    late = Route("/api/desktop/late", _late, methods=["GET"])
    app.router.routes.append(late)          # 手工制造「被吞掉」的场景

    late_problems = ordering_problems(app, [late])
    assert any("/api/desktop/late" in pr for pr in late_problems), late_problems
    assert any("/api/desktop/late" in pr for pr in ordering_problems(app)), "不传清单也要能查出来"


# ── 端到端：真起 app，走 HTTP 看谁应答 ──────────────────────────────────
def test_bootstrap_is_served_as_javascript_not_the_spa(ui_dir: Path):
    from fastapi.testclient import TestClient

    app = kernel_like_app()
    attach(app, desktop_dir=ui_dir, ui_active=True)
    with TestClient(app) as client:
        r = client.get(f"{DESKTOP_MOUNT_PATH}/window-bootstrap.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]
        assert "__OPENMINIS_DESKTOP__" in r.text

        # 静态资源仍由挂载提供
        assert client.get(f"{DESKTOP_MOUNT_PATH}/app.js").status_code == 200
        # 未知路径落回内核兜底（证明我们没把兜底挤掉）
        assert client.get("/not-a-desktop-route").json() == {"detail": "spa fallback"}
        # / 重定向到桌面界面
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == f"{DESKTOP_MOUNT_PATH}/"


def test_info_reports_what_actually_got_mounted(ui_dir: Path, tmp_path: Path):
    from fastapi.testclient import TestClient

    app = kernel_like_app()
    attach(app, desktop_dir=tmp_path / "gone", ui_active=True)   # 想要界面，但产物不在
    with TestClient(app) as client:
        info = client.get("/api/desktop/info").json()
    assert info["desktop"] is True
    assert info["uiActive"] is False, "汇报的必须是实际结果，不是调用方的愿望"
    assert info["uiMount"] is None


# ── 契约 5：运行时的第二条顺序不变量（精确路径 vs 静态挂载） ─────────────
def test_runtime_check_catches_a_route_ordered_after_its_mount(ui_dir: Path):
    """这条以前只在测试里断言，运行时不查 —— 清单被重排不会有任何告警。

    这里手工把顺序倒过来，验证自查能抓到（独立评审指出来的缺口）。
    """
    app = kernel_like_app()
    attach(app, desktop_dir=ui_dir, ui_active=True)

    boot = next(r for r in app.router.routes
                if str(getattr(r, "path", "")).endswith("window-bootstrap.js"))
    mount = next(r for r in app.router.routes
                 if isinstance(r, Mount) and r.path == DESKTOP_MOUNT_PATH)

    problems = ordering_problems(app, [mount, boot])
    assert any("window-bootstrap.js" in pr and DESKTOP_MOUNT_PATH in pr for pr in problems), problems


def test_runtime_check_fails_loud_when_the_catch_all_disappears():
    """内核哪天换了注册方式，自查不能静默失效（fail loud，不是 fail open）。"""
    app = FastAPI()

    @app.get("/api/health")
    async def _health():  # noqa: ANN202
        return {"ok": True}

    assert catch_all_index(app) is None
    problems = ordering_problems(app, [])
    assert any("认不出内核兜底路由" in pr for pr in problems), problems


def test_second_attach_reports_the_first_result(tmp_path: Path):
    """幂等分支返回的必须是**首次**的实际结论。

    回归自一次独立评审：以前它返回本次重算的值 —— 首次「想要界面但资源缺失」
    返回 False，资源就位后再调一次会谎报 True，而路由表里根本没有界面挂载。
    """
    app = kernel_like_app()
    assert attach(app, desktop_dir=tmp_path / "nope", ui_active=True) is False

    d = tmp_path / "nope"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html>", encoding="utf-8")
    assert attach(app, desktop_dir=d, ui_active=True) is False, "被幂等跳过，就不该说界面挂上了"
    assert f"{DESKTOP_MOUNT_PATH}/" not in paths(app)


# ── 契约 6：桌面静态资源禁缓存 ───────────────────────────────────────────
def test_desktop_assets_are_not_cacheable(ui_dir: Path):
    """静态挂载只发 etag，浏览器会按启发式新鲜度自己缓存 —— 升级后 WebView 跑旧界面。

    实测踩到：改完 app.js，页面里执行的还是上一版的函数，白排查一轮。
    """
    from fastapi.testclient import TestClient  # noqa: PLC0415

    app = kernel_like_app()
    attach(app, desktop_dir=ui_dir, ui_active=True)
    with TestClient(app) as client:
        # 夹具里只有这两个文件；`/_desktop/` 走的是目录 → index.html 那条路径
        for path in ("app.js", ""):
            r = client.get(f"{DESKTOP_MOUNT_PATH}/{path}")
            assert r.status_code == 200, path
            assert r.headers.get("cache-control") == "no-store", f"{path}: {dict(r.headers)}"


def test_no_store_does_not_leak_to_other_routes(ui_dir: Path):
    """只对 /_desktop/ 生效 —— 内核自己的接口不该被我们改掉缓存策略。"""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    app = kernel_like_app()
    attach(app, desktop_dir=ui_dir, ui_active=True)
    with TestClient(app) as client:
        r = client.get("/api/desktop/info")
        assert r.status_code == 200
        assert r.headers.get("cache-control") != "no-store"


# ── 契约 7：首页的资源引用带指纹 ─────────────────────────────────────────
def test_index_assets_are_stamped(ui_dir: Path, tmp_path: Path):
    """新 HTML + 旧 JS 的错配必须不可能发生（实测踩到过一次，白排查一轮）。"""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    # 夹具里的首页太简陋（没有任何资源引用），这里造一个像真首页的
    (ui_dir / "index.html").write_text(
        '<!doctype html><script src="./app.js"></script>', encoding="utf-8"
    )
    app = kernel_like_app()
    attach(app, desktop_dir=ui_dir, ui_active=True)
    with TestClient(app) as client:
        r = client.get("/_desktop/")
        assert r.status_code == 200, r.status_code
        assert "./app.js?v=" in r.text, r.text[:200]


def test_missing_asset_reference_is_left_alone(tmp_path: Path):
    """引用不存在的文件时不要伪造指纹 —— 那是个 404，应该照常暴露。"""
    d = tmp_path / "ui"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (d / "app.js").write_text("// x", encoding="utf-8")

    stamped = ui_mount.stamp_assets(
        '<script src="./app.js"></script><link href="./nope.css" rel="stylesheet">', d
    )
    assert "./app.js?v=" in stamped
    assert "./nope.css" in stamped and "nope.css?v=" not in stamped


def test_stamp_changes_when_the_file_changes(ui_dir: Path):
    """文件改了，指纹就得跟着变 —— 否则等于没指纹。"""
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    before = ui_mount.stamp_assets('<script src="./app.js"></script>', ui_dir)
    time.sleep(0.01)
    os.utime(ui_dir / "app.js", (time.time() + 5, time.time() + 5))
    after = ui_mount.stamp_assets('<script src="./app.js"></script>', ui_dir)
    assert before != after, f"{before} == {after}"


# ── 日志包：桌面壳里必须落到磁盘上 ──────────────────────────────────────
def test_logs_save_writes_the_zip_and_reports_the_path(ui_dir):
    """桌面壳里走这条：后端自己写盘，把路径回报给界面。

    为什么不靠页面自己下载：窗口是 WebView2，pywebview 没有接管下载事件，
    页面里 blob + <a download> 存不存、存到哪都不由我们说了算 ——
    真机上的表现是「提示说已开始下载，然后什么都没有」，而那句提示还是我们自己写的。
    """
    import pathlib as _pathlib
    import zipfile as _zipfile

    from fastapi.testclient import TestClient

    app = kernel_like_app()
    attach(app, desktop_dir=ui_dir, ui_active=True)
    with TestClient(app) as client:
        r = client.post("/api/desktop/logs/save")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["path"]
    path = _pathlib.Path(body["path"])
    assert path.exists() and path.suffix == ".zip", "没真的写到磁盘上"
    assert body["bytes"] == path.stat().st_size
    with _zipfile.ZipFile(path) as zf:
        assert zf.namelist(), "空的压缩包对排障没用"


def test_the_saved_zip_is_exactly_what_the_download_endpoint_serves(ui_dir):
    """两条路必须是**同一份内容**（同一个构造器）。

    否则用户发回来的和界面上说的可能不是一回事，排障就白做了。
    """
    import pathlib as _pathlib

    from fastapi.testclient import TestClient

    app = kernel_like_app()
    attach(app, desktop_dir=ui_dir, ui_active=True)
    with TestClient(app) as client:
        saved = client.post("/api/desktop/logs/save").json()
        got = client.get("/api/desktop/logs")
    assert got.status_code == 200
    assert got.content == _pathlib.Path(saved["path"]).read_bytes()
    # 而且下一次打包不能把上一次那个包再包进去（否则越滚越大）
    with TestClient(app) as client:
        again = client.post("/api/desktop/logs/save").json()
    import zipfile as _zipfile

    with _zipfile.ZipFile(again["path"]) as zf:
        names = zf.namelist()
    assert not [n for n in names if "openminis-logs-" in n], f"把历史日志包又包进去了: {names}"
