"""Attach the desktop UI to the upstream FastAPI app at runtime.

Why patch at runtime instead of editing the kernel: the whole point of this
project is to sit *on top of* OpenMinis without forking its internals. The
shell imports ``openminis.server.main.app`` untouched and then adds routes to
it. That keeps upstream merges trivial and makes it obvious which lines are
ours.

Route ordering is the one real subtlety. The kernel registers a catch-all
``@app.get("/{full_path:path}")`` that answers unknown paths with either a
JSON 404 (for ``api/`` / ``ws``) or the SPA index. Anything appended *after*
it is dead code, so every route we add is **inserted at the front** of
``app.router.routes``.
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
DESKTOP_UI_VERSION = "0.1.0"


def _insert_front(app: FastAPI, route: Any) -> None:
    app.router.routes.insert(0, route)


def attach_desktop_ui(app: FastAPI, desktop_dir: Path) -> bool:
    """Mount ``desktop_dir`` at ``/_desktop`` and make ``/`` point at it.

    Returns ``False`` (and logs) when the assets are missing, so the shell can
    still start and fall back to the upstream mobile UI.
    """
    index = desktop_dir / "index.html"
    if not index.is_file():
        logger.warning("desktop UI assets missing at %s — falling back to upstream UI", desktop_dir)
        return False

    _insert_front(
        app,
        Mount(
            DESKTOP_MOUNT_PATH,
            app=StaticFiles(directory=str(desktop_dir), html=True),
            name="openminis-desktop-ui",
        ),
    )

    # Starlette hands plain ``Route`` endpoints a ``Request`` — accept and
    # ignore it rather than making these functions take no arguments.
    async def _desktop_index(request: Any) -> FileResponse:  # noqa: ARG001
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    async def _root_redirect(request: Any) -> RedirectResponse:  # noqa: ARG001
        return RedirectResponse(f"{DESKTOP_MOUNT_PATH}/", status_code=307)

    # Both go in front of the kernel's SPA catch-all. Order between them is
    # irrelevant — they match different paths.
    _insert_front(app, Route("/", _root_redirect, methods=["GET"], include_in_schema=False))
    _insert_front(
        app,
        Route(f"{DESKTOP_MOUNT_PATH}/", _desktop_index, methods=["GET"], include_in_schema=False),
    )
    return True


def attach_desktop_api(app: FastAPI, *, desktop_dir: Path | None, ui_active: bool) -> None:
    """A tiny ``/api/desktop/*`` surface for the shell and the UI to talk to.

    The UI uses ``/api/desktop/info`` to render the window chrome (workspace
    path, version, shell flavour) without guessing from the user agent.
    """

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

    _insert_front(
        app,
        Route("/api/desktop/info", _info, methods=["GET"], include_in_schema=False),
    )


def attach_window_bootstrap(app: FastAPI) -> None:
    """Tell the UI it is inside a native window (adds a class to ``<html>``).

    Serving a marker file is cheaper than threading a query parameter through
    the WebView, and it means the same assets behave correctly whether opened
    in a window or a plain browser.
    """

    async def _bootstrap(request: Any) -> HTMLResponse:  # noqa: ARG001
        return HTMLResponse("window.__OPENMINIS_DESKTOP__ = true;\n", media_type="application/javascript")

    _insert_front(
        app,
        Route("/_desktop/window-bootstrap.js", _bootstrap, methods=["GET"], include_in_schema=False),
    )
