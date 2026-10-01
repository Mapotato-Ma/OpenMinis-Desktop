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
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI

from . import __version__
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount, Route

logger = logging.getLogger(__name__)

DESKTOP_MOUNT_PATH = "/_desktop"

#: 应用内更新的进度/结果（单实例，全局一份够了 —— 一次只跑一个更新）。
_update_state: dict[str, Any] = {"phase": "idle", "done": 0, "total": 0, "error": None}

#: 壳注册的"正常退出"回调。有它就走它（顺手收后端、关窗口）；没有就兜底硬退。
#: 为什么用注册而不是 import：窗口在 launcher 手里，而 api 模块不该反过来依赖它。
_restart_hook: dict[str, Callable[[], None] | None] = {"fn": None}


def set_restart_hook(fn: Callable[[], None] | None) -> None:
    """壳在窗口建好之后把"怎么正常退出"告诉这里（见 desktop/launcher.py）。"""
    _restart_hook["fn"] = fn
DESKTOP_UI_VERSION = "0.4.7"

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
        from .paths import app_root, data_root, is_frozen, module_origin, payload_root  # noqa: PLC0415

        payload = payload_root()
        info: dict[str, Any] = {
            "desktop": True,
            "uiActive": ui_active,
            "uiVersion": DESKTOP_UI_VERSION,
            "uiMount": DESKTOP_MOUNT_PATH if ui_active else None,
            "frozen": is_frozen(),
            "appRoot": str(app_root()),
            "dataRoot": str(data_root()),
            "assetsDir": str(desktop_dir) if desktop_dir else None,
            # 可覆盖载荷（"更新只换几 MB"的落脚点）。这三行是给**排查**用的：
            # 载荷没生效时表现是"改的东西没反应"，没有它们只能靠猜。
            "payloadDir": str(payload) if payload else None,
            "kernelFrom": module_origin("openminis"),
            "desktopFrom": module_origin("desktop"),
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

    async def _clear_data(request: Any) -> JSONResponse:  # noqa: ARG001
        """一键清空对话数据（会话/消息/分组/压缩标记），保留供应商等一切设置。

        排除「历史数据把模型带偏」这类怀疑时用：只动对话表，SettingsStore 里的
        供应商、模型、人设、记忆一律不碰。
        """
        from openminis.server import chat_store  # noqa: PLC0415

        try:
            counts = await chat_store.clear_all_chat_data()
        except Exception as exc:  # noqa: BLE001
            logger.exception("clear-data failed")
            return JSONResponse(
                {"ok": False, "error": str(exc)}, status_code=500, headers=_NO_STORE
            )
        logger.info("desktop clear-data: %s", counts)
        return JSONResponse({"ok": True, "cleared": counts}, headers=_NO_STORE)

    async def _logs_bundle(request: Any) -> Any:  # noqa: ARG001
        """打包全部日志成一个 zip 供下载 —— 排障时用户把它发回来即可。

        收集两处：内核 ``cache_dir/logs``（``minis.log*``，含本次新增的
        ``[repeat-diag]`` 行）与桌面壳 ``data_root/logs``（``desktop.log``）。
        """
        import io  # noqa: PLC0415
        import zipfile  # noqa: PLC0415

        from starlette.responses import Response  # noqa: PLC0415

        from .paths import data_root  # noqa: PLC0415

        roots: list[Path] = []
        try:
            from openminis.core.context import app_context  # noqa: PLC0415

            roots.append(app_context().cache_dir / "logs")
        except Exception:  # pragma: no cover
            pass
        roots.append(data_root() / "logs")

        buf = io.BytesIO()
        added = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            seen: set[str] = set()
            for root in roots:
                if not root.is_dir():
                    continue
                for p in sorted(root.glob("*")):
                    if not p.is_file():
                        continue
                    arc = f"{root.name}/{p.name}"
                    if arc in seen:
                        continue
                    seen.add(arc)
                    try:
                        zf.write(p, arcname=arc)
                        added += 1
                    except OSError:
                        continue
            if added == 0:
                zf.writestr("README.txt", "还没有任何日志文件。跑一轮对话后再下载。\n")
        data = buf.getvalue()
        return Response(
            content=data,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="openminis-logs.zip"',
                "Cache-Control": "no-store",
            },
        )

    async def _logs_where(request: Any) -> JSONResponse:  # noqa: ARG001
        """日志到底写在哪个目录 —— 界面上直接展示给用户。"""
        from .paths import data_root  # noqa: PLC0415

        out: dict[str, Any] = {}
        try:
            from openminis.core.context import app_context  # noqa: PLC0415

            out["kernel"] = str(app_context().cache_dir / "logs")
        except Exception:  # pragma: no cover
            out["kernel"] = None
        out["desktop"] = str(data_root() / "logs")
        return JSONResponse(out, headers=_NO_STORE)

    # ── 从 cc-switch 导入供应商 ──────────────────────────────────────────
    # 两条路由必须紧邻注册（GET 列清单、POST 取明文），并进 desktop_routes() 的
    # 顺序契约测试。清单里只有掩码；明文只在用户显式点「导入」的那一次返回。
    _NO_STORE = {"Cache-Control": "no-store"}

    async def _import_scan(request: Any) -> JSONResponse:  # noqa: ARG001
        from . import ccswitch_import  # noqa: PLC0415

        return JSONResponse(ccswitch_import.scan(), headers=_NO_STORE)

    async def _import_entries(request: Any) -> JSONResponse:
        from . import ccswitch_import  # noqa: PLC0415

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 客户端发什么都有可能
            body = {}
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list):
            return JSONResponse({"error": "ids 必须是数组"}, status_code=400, headers=_NO_STORE)
        # 只收字符串 id（实测真实库里 id 是 UUID 这类 TEXT），限长限量：
        # 别让这一个接口变成「整库导出」
        clean = [str(i).strip()[:128] for i in ids[:50] if isinstance(i, str) and str(i).strip()]
        return JSONResponse(ccswitch_import.entries(clean), headers=_NO_STORE)

    async def _zoom_capability(request: Any) -> JSONResponse:  # noqa: ARG001
        """缩放交给谁：WebView2 原生 ZoomFactor（引擎级）还是页面里的 CSS zoom。

        只报告**握到手的东西**，不声称支持 —— 真正的权威是 POST 的返回。
        """
        from . import native_zoom  # noqa: PLC0415

        return JSONResponse(native_zoom.capability(), headers=_NO_STORE)

    async def _zoom_set(request: Any) -> JSONResponse:
        """把缩放交给 WebView2。只有「设进去 + 读回来一致」才 ok=True。"""
        from . import native_zoom  # noqa: PLC0415

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 客户端发什么都有可能
            body = {}
        factor = body.get("factor") if isinstance(body, dict) else None
        # 脏参数是客户端错误（400）；「窗口不支持」是能力问题（200 + ok=false）。
        # 界面两条路都会回落 CSS，但分开能让日志一眼看出是谁的问题。
        if isinstance(factor, bool) or not isinstance(factor, (int, float)):
            return JSONResponse({"error": "factor 必须是数字"}, status_code=400, headers=_NO_STORE)
        return JSONResponse(native_zoom.set_zoom(factor), headers=_NO_STORE)

    async def _update_check(request: Any) -> JSONResponse:  # noqa: ARG001
        """检查有没有新版本 [T-in-app-update]。

        网络请求放线程里跑：这是同步阻塞的 HTTP，直接放在事件循环上会把整个后端
        卡住（连界面自己的轮询都会排队）。
        """
        import asyncio  # noqa: PLC0415

        from . import updater  # noqa: PLC0415

        try:
            result = await asyncio.to_thread(
                updater.plan,
                current_version=__version__,
                current_shell=updater.own_shell_id(),
                url=updater.MANIFEST_URL,
            )
        except updater.UpdateError as exc:
            return JSONResponse(
                {"ok": False, "current": __version__, "error": str(exc)},
                status_code=200,   # "检查不了"不是服务器错误，界面照样要能画
                headers=_NO_STORE,
            )
        info = result.info
        return JSONResponse(
            {
                "ok": True,
                "current": __version__,
                "available": result.kind != "none",
                "kind": result.kind,
                "reason": result.reason,
                "latest": info.version if info else None,
                "notes": info.notes if info else "",
                "pubDate": info.pub_date if info else "",
                "bytes": (result.asset.bytes if result.asset else 0),
            },
            headers=_NO_STORE,
        )

    async def _update_apply(request: Any) -> JSONResponse:  # noqa: ARG001
        """下载并安装（后台跑，界面轮询 `update/status` 看进度）。"""
        state = _update_state
        if state.get("phase") == "running":
            return JSONResponse({"ok": False, "error": "已经在更新了"}, headers=_NO_STORE)
        state.clear()
        state.update({"phase": "checking", "done": 0, "total": 0, "error": None})
        threading.Thread(target=_run_update, name="openminis-update", daemon=True).start()
        return JSONResponse({"ok": True, "phase": "checking"}, headers=_NO_STORE)

    async def _update_status(request: Any) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(dict(_update_state), headers=_NO_STORE)

    async def _update_restart(request: Any) -> JSONResponse:  # noqa: ARG001
        """重启以让新载荷生效。

        正在跑的进程没法换掉自己的代码（``.pyc`` 已进内存），所以只能"我先退出，
        让助手把我拉起来"。非 Windows 上不写助手脚本 —— 那里不是发布目标。
        """
        from . import updater  # noqa: PLC0415

        hook = _restart_hook["fn"]
        script = updater.schedule_relaunch()
        if hook is not None:
            # 有壳注册的正常退出路径就走它（会顺手收掉后端、关掉窗口）。
            threading.Timer(0.4, hook).start()
        else:
            # 兜底：没有壳的时候（无头/测试）直接退出进程，助手会把它拉起来。
            threading.Timer(0.6, lambda: os._exit(0)).start()

        return JSONResponse(
            {"ok": True, "relaunch": str(script) if script else None}, headers=_NO_STORE
        )

    def _run_update() -> None:
        """后台更新流程：探清单 → 下载 → 校验 → 换载荷。"""
        from . import updater  # noqa: PLC0415
        from .paths import payload_root  # noqa: PLC0415

        state = _update_state
        try:
            result = updater.plan(
                current_version=__version__,
                current_shell=updater.own_shell_id(),
                url=updater.MANIFEST_URL,
            )
            if result.kind == "none" or result.asset is None:
                state.update({"phase": "done", "note": result.reason})
                return
            if result.kind != "payload":
                # 换壳的更新需要替换 exe，这一步还没做（见 CHANGELOG 的"还没做"）。
                state.update({"phase": "manual", "note": result.reason})
                return

            state.update(
                {"phase": "downloading", "version": result.info.version,
                 "total": result.asset.bytes, "done": 0}
            )
            archive = updater.download(
                result.asset,
                updater.updates_dir(),
                on_progress=lambda done, total: state.update(
                    {"done": done, "total": total or result.asset.bytes}
                ),
            )
            state.update({"phase": "installing"})
            target = payload_root()
            if target is None:
                state.update({"phase": "manual", "note": "这个安装没有载荷目录，需要整包装"})
                return
            updater.install_payload(
                archive, target, expect_shell=result.info.shell_id or updater.own_shell_id()
            )
            updater.cleanup_old_downloads(keep=archive)
            state.update({"phase": "ready", "version": result.info.version})
        except updater.UpdateError as exc:
            state.update({"phase": "failed", "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 任何意外都要让界面看得见
            logger.exception("update failed")
            state.update({"phase": "failed", "error": f"{type(exc).__name__}: {exc}"})

    routes.extend(
        [
            # 壳层与界面之间那点小事：界面靠 /api/desktop/info 画窗口 chrome，
            # 而不用从 user agent 去猜。
            Route("/api/desktop/info", _info, methods=["GET"], include_in_schema=False),
            # 应用内更新（对标 CC Switch 那套）：探清单 / 下载安装 / 看进度 / 重启。
            Route("/api/desktop/update", _update_check, methods=["GET"], include_in_schema=False),
            Route("/api/desktop/update", _update_apply, methods=["POST"], include_in_schema=False),
            Route("/api/desktop/update/status", _update_status, methods=["GET"], include_in_schema=False),
            Route("/api/desktop/update/restart", _update_restart, methods=["POST"], include_in_schema=False),
            # /api/desktop/test-provider —— 这个服务商真的能应答吗？
            # 上游已有的 /api/settings/fetch-models 只探测 GET /models：
            # 它能在「模型 id 写错」时依然是绿的，所以这里发一次真正的最小
            # completion，让按钮报的就是对话会遇到的结果。两者都读**已保存**的
            # 配置，所以界面上点「测试连接」会先保存再测 —— 测的和看到的是同一份。
            Route("/api/desktop/test-provider", _test_provider, methods=["POST"], include_in_schema=False),
            Route("/api/desktop/chat-readiness", _readiness, methods=["GET"], include_in_schema=False),
            # /api/desktop/clear-data —— 一键清空对话数据（保留供应商/设置）。
            # 排除「历史把模型带偏」的嫌疑用，只动对话表。
            Route("/api/desktop/clear-data", _clear_data, methods=["POST"], include_in_schema=False),
            # /api/desktop/logs —— 打包全部日志下载 / 告知日志目录。排障回路。
            Route("/api/desktop/logs", _logs_bundle, methods=["GET"], include_in_schema=False),
            Route("/api/desktop/logs/where", _logs_where, methods=["GET"], include_in_schema=False),
            Route("/api/desktop/import/cc-switch", _import_scan, methods=["GET"], include_in_schema=False),
            Route("/api/desktop/import/cc-switch", _import_entries, methods=["POST"], include_in_schema=False),
            # /api/desktop/zoom —— 缩放落在哪儿。WebView2 的 ZoomFactor 是引擎级
            # 缩放（vh/100%/媒体查询自洽，文字按真实字号渲染），CSS zoom 是页面内
            # 缩放（视口单位要自己补偿）。界面开机问一次、每次改缩放 POST 一次，
            # 失败就自动回落 CSS —— 所以这里的返回必须诚实（见 native_zoom）。
            Route("/api/desktop/zoom", _zoom_capability, methods=["GET"], include_in_schema=False),
            Route("/api/desktop/zoom", _zoom_set, methods=["POST"], include_in_schema=False),
        ]
    )

    # ── 界面本体：重定向 / 首页 / 静态挂载（挂载必须最后） ───────────────
    if ui_active and desktop_dir is not None:
        index = desktop_dir / "index.html"

        async def _desktop_index(request: Any) -> HTMLResponse:  # noqa: ARG001
            # Starlette 把 Request 交给普通 Route 端点 —— 收下并忽略它，而不是
            # 把这些函数写成零参数。
            # 首页每次现读：它才几 KB，换来的是「改完刷新就能看到」。
            return HTMLResponse(
                stamp_assets(index.read_text(encoding="utf-8"), desktop_dir),
                headers={"Cache-Control": "no-store"},
            )

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


_ASSET_REF = re.compile(r'(?P<attr>src|href)="\./(?P<name>[^"?]+)"')


def stamp_assets(html: str, desktop_dir: Path) -> str:
    """把首页里的本地资源引用换成 ``./x.js?v=<mtime 毫秒>``。

    没有构建步骤就没有指纹文件名，而静态挂载只发 etag —— 浏览器于是按「启发式新鲜度」
    自己缓存。于是升级后会出现一种很难查的错配：**index.html 是新的、JS 还是缓存的旧版**
    （实测踩到：新首页引了 module 脚本，页面里跑的却是上一版 app.js，白排查一轮）。
    把 URL 本身换掉，这种错配就不可能发生 —— 也能顺带绕开任何中间层缓存。

    引用不存在的文件时原样保留：那是 404，应该照常暴露出来。
    """

    def repl(m: re.Match[str]) -> str:
        name = m.group("name")
        target = desktop_dir / name
        if not target.is_file():
            return m.group(0)
        stamp = int(target.stat().st_mtime * 1000)
        return f'{m.group("attr")}="./{name}?v={stamp}"'

    return _ASSET_REF.sub(repl, html)


class DesktopNoStore:
    """给 ``/_desktop/`` 下的响应补上 ``Cache-Control: no-store``。

    静态挂载只发 ``etag`` / ``last-modified``，**没有** ``Cache-Control``；浏览器于是
    按「启发式新鲜度」自己决定缓存多久（通常是文件年龄的 10%）。桌面应用里这意味着一件事：
    **升级后 WebView 会继续跑上一版的界面**。实测踩到过 —— 改完 app.js，页面里执行的
    还是旧的 ``applyTheme``，白排查一轮。这些文件都在本机磁盘上，省这点缓存没有意义。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith(
            DESKTOP_MOUNT_PATH
        ):
            await self.app(scope, receive, send)
            return

        async def send_no_store(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                if not any(k.lower() == b"cache-control" for k, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
            await send(message)

        await self.app(scope, receive, send_no_store)


def catch_all_index(app: FastAPI) -> int | None:
    """兜底路由在路由表里的位置；内核没注册就返回 ``None``。"""
    for i, route in enumerate(app.router.routes):
        if getattr(route, "path", None) == KERNEL_CATCH_ALL_PATH:
            return i
    return None


def ordering_problems(app: FastAPI, routes: list[Any] | None = None) -> list[str]:
    """装完之后能自查的**两条**顺序不变量（这个模块唯一会静默失败的地方）。

    1. 没有路由排在内核兜底路由之后 —— 否则永远不会被命中；
    2. 精确路径排在**会吞掉它的静态挂载**之前 —— ``Mount`` 匹配前缀下的一切，
       而 ``/_desktop/window-bootstrap.js`` 这种「磁盘上不存在、由处理器现算」
       的路由一旦排到 ``Mount("/_desktop", …)`` 后面，就会被静态挂载接走并 404。

    第 2 条以前只写在测试里，运行时不查（独立评审指出的：清单被重排不会有任何告警）。
    另外两处也按评审改了：`index()` 走 ``==`` 语义（依赖 Starlette 不给 Route 加
    ``__eq__``）→ 改成按对象身份建索引；查不到（不在表里）以前被 `continue` 吞掉
    → 现在它本身就是问题（装完却查不到，说明这次自查不可信）。
    """
    problems: list[str] = []
    watched = list(app.router.routes if routes is None else routes)

    limit = catch_all_index(app)
    if limit is None:
        # fail loud：认不出兜底就**不能**当成「全都安全」（内核换个注册方式就静默失效）
        problems.append(
            f"认不出内核兜底路由（{KERNEL_CATCH_ALL_PATH}）—— 顺序自查失效，请核对内核的注册方式"
        )
    else:
        pos = {id(r): i for i, r in enumerate(app.router.routes)}
        for route in watched:
            i = pos.get(id(route))
            path = str(getattr(route, "path", route))
            if i is None:
                problems.append(f"{path} 装完却不在路由表里 —— 这次自查不可信")
            elif i > limit:
                problems.append(f"{path} 排在兜底路由之后，永远不会被命中")

    for i, mount in enumerate(watched):
        if not isinstance(mount, Mount):
            continue
        prefix = str(getattr(mount, "path", ""))
        if not prefix:
            continue
        for j, other in enumerate(watched):
            # 只查**排在挂载之后**的精确路径：那才会被 Mount 接走。
            # （评审的原话写的是「Mount 之前没有同前缀的精确 Route」，方向反了 ——
            #  精确路径必须排在挂载之前才可达；照原话实现会让正常路径一直告警。）
            if j <= i or isinstance(other, Mount):
                continue
            path = str(getattr(other, "path", ""))
            if path.startswith(prefix):
                problems.append(f"精确路径 {path} 排在静态挂载 {prefix} 之后，会被它接走")

    return problems


def _add_middleware_anytime(app: FastAPI, middleware_class: Any, **kwargs: Any) -> None:
    """加一个中间件，**即使应用已经开始处理请求**。

    Starlette 的 ``add_middleware`` 在应用启动后会直接抛
    ``RuntimeError: Cannot add middleware after an application has started``。
    生产路径上 ``attach()`` 一定发生在 uvicorn 起来之前 —— 但内核的 ``app`` 是
    **进程级单例**，测试里它经常已经被别的用例启动过了。不处理的话，那次 attach
    会当场炸（或者更糟：被当成"装过了"而**静默失去中间件**）。

    做法就是把中间件栈重建一次：``user_middleware`` 已经加进去了，重建只是让它
    被用上。
    """
    started = getattr(app, "middleware_stack", None) is not None
    if started:
        app.middleware_stack = None  # 只是让 add_middleware 别拦我们
    app.add_middleware(middleware_class, **kwargs)
    if started:
        app.middleware_stack = app.build_middleware_stack()


def attach(
    app: FastAPI,
    *,
    desktop_dir: Path | None,
    ui_active: bool = True,
    access_token: str | None = None,
    access_host: str = "127.0.0.1",
    access_port: int = 0,
) -> bool:
    """把桌面壳装到内核 app 上；返回**界面是否真的挂上了**。

    返回值就是界面的 ``uiActive`` —— 不是调用方想要什么，而是实际结果。

    幂等：内核的 ``app`` 是模块级单例，同一个进程里第二次调用（重启后端、
    测试里重复建 app）不会再插一遍 —— 否则路由表里会出现重复项。
    """
    mounted = bool(ui_active) and desktop_assets_ready(desktop_dir)

    # --- 中间件：装一次，且**与路由的幂等判断分开** ---------------------------
    #
    # 顺序说明：`add_middleware` 是**头插**，后加的在外层。所以这里先落后加闸门
    # —— 闸门最终在最外层，排在内核 CORS **之前**，可以先拒后放（内核那个 CORS
    # 是 `allow_origins=["*"] + allow_credentials=True`，会把请求 Origin 原样回显）。
    from .access_gate import AccessGate, DesktopAccessGate, GateHolder  # noqa: PLC0415

    def _fresh_gate() -> AccessGate | None:
        return AccessGate(access_token, access_host, access_port) if access_token else None

    if not getattr(app.state, "desktop_middleware_ready", False):
        # **先把闸门造好再装**：若先装一个空 holder、稍后再赋令牌，中间就存在一段
        # 「装着闸门但放行一切」的窗口。生产路径上 attach() 在启动前，那一段不可达；
        # 但"应用已启动后再 attach"（测试、将来的热重载）是真实可达的（复核指出）。
        holder = GateHolder(_fresh_gate())
        _add_middleware_anytime(app, DesktopNoStore)
        _add_middleware_anytime(app, DesktopAccessGate, holder=holder)
        app.state.desktop_access_gate = holder
        app.state.desktop_middleware_ready = True
    else:
        holder = app.state.desktop_access_gate
        if access_token:
            # 中间件不可卸载（Starlette），所以换令牌 = 换 holder.gate。
            # 见 access_gate.GateHolder —— 那是为了同一进程里几百个用例不互相污染。
            holder.gate = AccessGate(access_token, access_host, access_port)

    if holder.gate is None:
        # 说大声点：这个组合（桌面界面 + 无鉴权）意味着**用户访问的任意网页**
        # 都能驱动本机这个手里有 shell 的 agent。见 access_gate 模块开头的推导。
        logger.warning(
            "桌面壳未提供访问令牌 —— 本地服务处于无鉴权状态，"
            "任何网页都能调用它的 /api/* 与 /ws"
        )

    if getattr(app.state, "desktop_routes_attached", False):
        # 幂等：第二次不改路由表，所以返回的必须是**首次**的实际结论。
        # （独立评审指出：这里以前返回的是本次重算的值 —— 「首次资源缺失(False)、
        #  资源就位后再调一次」会谎报 True，而表里根本没有界面挂载。）
        logger.debug("desktop routes already attached to this app — skipping")
        return bool(getattr(app.state, "desktop_ui_mounted", False))

    if ui_active and not mounted:
        logger.warning(
            "desktop UI assets missing at %s — falling back to upstream UI", desktop_dir
        )

    routes = desktop_routes(desktop_dir=desktop_dir, ui_active=mounted)
    # 倒着插，清单顺序即最终顺序（可读性：清单从上到下就是匹配优先级）。
    for route in reversed(routes):
        app.router.routes.insert(0, route)
    app.state.desktop_routes_attached = True
    app.state.desktop_ui_mounted = mounted

    problems = ordering_problems(app, routes)
    if problems:
        logger.warning("desktop route ordering problems: %s", "; ".join(problems))
    return mounted
