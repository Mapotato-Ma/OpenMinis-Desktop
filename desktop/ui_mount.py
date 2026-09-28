"""Attach the desktop UI to the upstream FastAPI app at runtime.

Why patch at runtime instead of editing the kernel: the whole point of this
project is to sit *on top of* OpenMinis without forking its internals. The
shell imports ``openminis.server.main.app`` untouched and then adds routes to
it. That keeps upstream merges trivial and makes it obvious which lines are
ours.

Route ordering is the one real subtlety, and it is now stated in exactly two
places instead of being spread across a handful of ``insert(0)`` calls:

1. **Everything must come before the kernel's catch-all.** The kernel ends with
   ``@app.get("/{full_path:path}")``; Starlette matches in registration order,
   so anything appended after it is dead code. Hence every desktop route is
   inserted at the front of ``app.router.routes``.
2. **Within the list, order matters too** — see ``desktop_routes()``. The
   precise paths must precede ``Mount("/_desktop", StaticFiles(...))``, or the
   mount swallows them (and the file they name is not on disk).

Adding an endpoint is therefore two things: add an entry to ``desktop_routes()``,
and nothing else. ``attach()`` installs the list and then verifies the invariant,
logging loudly if a kernel change ever makes one of our routes unreachable —
the old failure mode was silent (requests answered by the catch-all, or a JSON
404 where an API should be).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount, Route

logger = logging.getLogger(__name__)

DESKTOP_MOUNT_PATH = "/_desktop"
DESKTOP_UI_VERSION = "0.1.2"

#: 内核末尾注册的兜底路由。桌面路由必须**全部**排在它之前。
KERNEL_CATCH_ALL_PATH = "/{full_path:path}"


def desktop_assets_ready(desktop_dir: Path | None) -> bool:
    """桌面界面的产物在不在（``web/desktop/index.html``）。"""
    return bool(desktop_dir) and (desktop_dir / "index.html").is_file()  # type: ignore[union-attr]


def desktop_routes(*, desktop_dir: Path | None, ui_active: bool) -> list[Any]:
    """桌面壳的全部路由，**顺序即契约**。

    清单里精确路径在前、静态挂载在最后，原因见模块开头的第 2 条：
    ``Mount("/_desktop", ...)`` 会吃掉 ``/_desktop`` 下的**所有**路径，
    所以 ``/_desktop/window-bootstrap.js`` 这种「磁盘上并不存在、由处理器现算」
    的路由必须排在它前面。以前这条依赖靠调用顺序碰巧成立（每个 attach_*
    各自 insert(0)，谁最后调谁在最前），改一个字就可能静默失效。

    ``ui_active`` 为假时不注册界面相关路由（挂载 / 重定向 / 首页），
    但 ``/api/desktop/*`` 与 window bootstrap 仍然注册 —— 壳层在
    「没有界面产物」时也要能启动，并如实告诉界面自己没挂上。
    """
    routes: list[Any] = []

    # ── 精确路径：必须排在静态挂载之前 ─────────────────────────────────
    async def _bootstrap(request: Any) -> HTMLResponse:  # noqa: ARG001
        """告诉界面它在一个原生窗口里（给 <html> 加个 class）。

        发一个标记文件比把查询参数穿过 WebView 更便宜，而且同一套资源在窗口里
        和普通浏览器里都表现正确。
        """
        return HTMLResponse(
            "window.__OPENMINIS_DESKTOP__ = true;\n", media_type="application/javascript"
        )

    routes.append(
        Route(
            f"{DESKTOP_MOUNT_PATH}/window-bootstrap.js",
            _bootstrap,
            methods=["GET"],
            include_in_schema=False,
        )
    )

    async def _info(request: Any) -> JSONResponse:  # noqa: ARG001
        from .paths import app_root, data_root, is_frozen  # noqa: PLC0415

        info: dict[str, Any] = {
            "desktop": True,
            "uiActive": ui_active,
            "uiVersion": DESKTOP_UI_VERSION,
            "uiMount": DESKTOP_MOUNT_PATH if ui_active else None,
            "frozen": is_frozen(),
            "appRoot": str(app_root()),
            "dataRoot": str(data_root()),
            "assetsDir": str(desktop_dir) if desktop_dir else None,
        }
        try:
            from openminis.core.context import app_context  # noqa: PLC0415

            info["workspace"] = str(app_context().workspace_dir)
        except Exception:  # pragma: no cover
            info["workspace"] = None
        return JSONResponse(info)

    async def _test_provider(request: Any) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — malformed body is a client error
            return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
        pid = str((payload or {}).get("id") or "").strip()
        if not pid:
            return JSONResponse({"ok": False, "error": "缺少 id"}, status_code=400)
        from .provider_probe import probe_provider  # noqa: PLC0415

        return JSONResponse(await probe_provider(pid))

    async def _readiness(request: Any) -> JSONResponse:  # noqa: ARG001
        from .provider_probe import chat_readiness  # noqa: PLC0415

        return JSONResponse(await chat_readiness())

    routes.extend(
        [
            # 壳层与界面之间那点小事：界面靠 /api/desktop/info 画窗口 chrome，
            # 而不用从 user agent 去猜。
            Route("/api/desktop/info", _info, methods=["GET"], include_in_schema=False),
            # /api/desktop/test-provider —— 这个服务商真的能应答吗？
            # 上游已有的 /api/settings/fetch-models 只探测 GET /models：
            # 它能在「模型 id 写错」时依然是绿的，所以这里发一次真正的最小
            # completion，让按钮报的就是对话会遇到的结果。两者都读**已保存**的
            # 配置，所以界面上点「测试连接」会先保存再测 —— 测的和看到的是同一份。
            Route("/api/desktop/test-provider", _test_provider, methods=["POST"], include_in_schema=False),
            Route("/api/desktop/chat-readiness", _readiness, methods=["GET"], include_in_schema=False),
        ]
    )

    # ── 界面本体：重定向 / 首页 / 静态挂载（挂载必须最后） ───────────────
    if ui_active and desktop_dir is not None:
        index = desktop_dir / "index.html"

        async def _desktop_index(request: Any) -> FileResponse:  # noqa: ARG001
            # Starlette 把 Request 交给普通 Route 端点 —— 收下并忽略它，而不是
            # 把这些函数写成零参数。
            return FileResponse(index, headers={"Cache-Control": "no-store"})

        async def _root_redirect(request: Any) -> RedirectResponse:  # noqa: ARG001
            return RedirectResponse(f"{DESKTOP_MOUNT_PATH}/", status_code=307)

        routes.extend(
            [
                Route("/", _root_redirect, methods=["GET"], include_in_schema=False),
                Route(f"{DESKTOP_MOUNT_PATH}/", _desktop_index, methods=["GET"], include_in_schema=False),
                Mount(
                    DESKTOP_MOUNT_PATH,
                    app=StaticFiles(directory=str(desktop_dir), html=True),
                    name="openminis-desktop-ui",
                ),
            ]
        )

    return routes


def catch_all_index(app: FastAPI) -> int | None:
    """兜底路由在路由表里的位置；内核没注册就返回 ``None``。"""
    for i, route in enumerate(app.router.routes):
        if getattr(route, "path", None) == KERNEL_CATCH_ALL_PATH:
            return i
    return None


def shadowed_routes(app: FastAPI, routes: list[Any] | None = None) -> list[str]:
    """排在兜底路由**之后**的桌面路由 —— 它们永远不会被命中。

    这是这个模块唯一的失败模式，而且是静默的：请求被兜底吞掉（返回首页 HTML
    或 JSON 404）。所以 ``attach()`` 每次装完都自查一遍，测试里也断言是空的。
    """
    limit = catch_all_index(app)
    if limit is None:  # 内核哪天不留兜底路由了，那我们的路由反而都安全
        return []
    watched = app.router.routes if routes is None else routes
    out: list[str] = []
    for route in watched:
        try:
            if app.router.routes.index(route) > limit:
                out.append(str(getattr(route, "path", route)))
        except ValueError:  # 不在表里（已经装过了？）
            continue
    return out


def attach(app: FastAPI, *, desktop_dir: Path | None, ui_active: bool = True) -> bool:
    """把桌面壳装到内核 app 上；返回**界面是否真的挂上了**。

    返回值就是界面的 ``uiActive`` —— 不是调用方想要什么，而是实际结果。

    幂等：内核的 ``app`` 是模块级单例，同一个进程里第二次调用（重启后端、
    测试里重复建 app）不会再插一遍 —— 否则路由表里会出现重复项。
    """
    mounted = bool(ui_active) and desktop_assets_ready(desktop_dir)
    if getattr(app.state, "desktop_routes_attached", False):
        logger.debug("desktop routes already attached to this app — skipping")
        return mounted

    if ui_active and not mounted:
        logger.warning(
            "desktop UI assets missing at %s — falling back to upstream UI", desktop_dir
        )

    routes = desktop_routes(desktop_dir=desktop_dir, ui_active=mounted)
    # 倒着插，清单顺序即最终顺序（可读性：清单从上到下就是匹配优先级）。
    for route in reversed(routes):
        app.router.routes.insert(0, route)
    app.state.desktop_routes_attached = True

    shadowed = shadowed_routes(app, routes)
    if shadowed:
        logger.warning(
            "desktop routes registered after the kernel catch-all and therefore unreachable: %s",
            ", ".join(shadowed),
        )
    return mounted
