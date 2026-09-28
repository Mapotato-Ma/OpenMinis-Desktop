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
