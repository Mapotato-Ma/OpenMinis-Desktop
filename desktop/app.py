"""Entry point for the OpenMinis desktop application.

    python desktop/app.py              # native window
    python desktop/app.py --browser    # serve + open a browser tab
    python desktop/app.py --port 9000  # pin the backend port

Sequence (window mode): create the window **first** with a self-contained splash
page, boot the kernel behind it on a background thread, then ``load_url`` to the
real UI — see ``desktop/launcher.py`` for why.  ``OPENMINIS_NO_SPLASH=1`` restores
the older serial order (start the server, then open the window).

``--no-window`` and ``--browser`` keep that serial order and change nothing else:
CI's startup probe runs ``--no-window``. If pywebview is unavailable we degrade to
a browser tab rather than exiting with a traceback — the backend is the valuable
part and it works either way.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import webbrowser
from pathlib import Path

# Allow running straight from a checkout: ``python desktop/app.py``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from desktop import __version__, motw, paths  # noqa: E402

# 与 desktop_main.py 同一件事（直接跑这个文件时引导脚本不在场）。幂等。
paths.install_payload_path()

from desktop import startup_trace as trace  # noqa: E402
from desktop.launcher import (  # noqa: E402
    BootPlan,
    SplashUnsupported,
    existing_instance_token,
    run_serial_window,
    run_window_first,
    splash_enabled,
)
from desktop.server_runner import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_PORT,
    DesktopServer,
    port_in_use,
    start_server,
)

logger = logging.getLogger("openminis.desktop")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="openminis-desktop",
        description="OpenMinis Desktop — native window around the OpenMinis agent kernel.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            f"backend port. 不给 = 优先 {DEFAULT_PORT}，被占就用一个空闲端口；"
            "**显式给了** = 就是它，被占用时直接报错（不静默换端口）"
        ),
    )
    parser.add_argument("--browser", action="store_true", help="open a browser tab instead of a native window")
    parser.add_argument("--no-window", action="store_true", help="start the backend only (headless / server mode)")
    parser.add_argument("--width", type=int, default=1440, help="initial window width")
    parser.add_argument("--height", type=int, default=900, help="initial window height")
    parser.add_argument("--debug", action="store_true", help="enable WebView devtools and verbose logs")
    parser.add_argument(
        "--upstream-ui",
        action="store_true",
        help="serve the upstream mobile UI at / instead of the desktop UI",
    )
    return parser.parse_args(argv)


def _configure_logging(debug: bool) -> None:
    # 时间戳不能省：用户把 desktop.log 发过来时，"第几行"没有意义，"几秒"才是证据。
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _reuse_running(host: str, port: int) -> str | None:
    """If an OpenMinis backend already listens on ``host:port``, reuse it.

    Launching the app twice is a normal thing to do (double-clicked icon, a
    pinned taskbar entry plus a shortcut). Spinning up a second kernel on a
    random port would give the user two windows with two divergent session
    databases, so we point the new window at the existing server instead.

    R07：判据是**握手**，不是 ``/api/health`` 返回 200 —— 任何 HTTP 服务都可能是
    200，端口恰好被别的程序占着时会被误认成本产品的后端。单宿主之后这条路基本走不到
    （第二个启动在拿到锁之前就交接掉了），留着是为了"锁还在、但名片指向的宿主刚重启"
    这类边角。
    """
    if not port_in_use(host, port):
        return None
    from desktop import instance  # noqa: PLC0415

    record = instance.read_host()
    if not record or int(record.get("port") or 0) != int(port):
        return None
    if not instance.probe_host(record):
        return None
    return f"http://{host}:{port}"



def main(argv: list[str] | None = None) -> int:
    # A frozen windowed build starts with sys.stdout/stderr set to None; the
    # kernel logs during startup, so this has to happen first.
    from desktop.stdio import ensure_console_streams  # noqa: PLC0415

    # Also before anything can spawn a child: a GUI-subsystem build has no
    # console, and Windows would give every shell the agent runs its own
    # console window (one black box per message).
    from desktop.no_console import install as _install_no_console  # noqa: PLC0415

    _no_console_status = _install_no_console()

    log_file = ensure_console_streams()

    args = _parse_args(argv)
    _configure_logging(args.debug)
    # 日志脱敏兜底（R18）：访问令牌是鉴权凭据，不能进日志、更不能随诊断包发出去。
    from desktop import redact as _redact  # noqa: PLC0415

    _redact.install()
    # 配置文件的权限收紧（R17）：API Key 仍是明文（文档已披露），但这份文件不该被
    # 同机其它用户读走。失败只记一笔 —— 权限收紧不该拦住启动。
    try:
        from desktop import secrets_acl  # noqa: PLC0415

        for note in secrets_acl.tighten_data_root():
            logger.info("secrets ACL: %s", note)
    except Exception:  # pragma: no cover
        logger.debug("secrets ACL step failed", exc_info=True)
    if log_file is not None:
        logger.info("no console attached — logs are in %s", log_file)
    # Worth a line in the log: when this is "skipped: process has a console"
    # the user is running a console build, and a stray child window (if any)
    # has a different cause.
    logger.info("child console windows: %s", _no_console_status)
    trace.mark("imports")

    # 单宿主契约（R06）：第一个启动的进程拿锁当宿主；后来者不建 backend、不建窗口，
    # 只把"再开一个窗口"交给宿主然后自己退出。改前是每个启动都开一个进程、复用第一个
    # 的 HTTP —— 关掉第一个窗口就把后端带走了。
    from desktop import instance  # noqa: PLC0415

    host_lock = instance.HostLock()
    if not host_lock.acquire():
        record = instance.read_host()
        if record and instance.probe_host(record):
            handed = instance.request_window()
            logger.info(
                "OpenMinis Desktop 已经在运行（pid %s）—— %s",
                record.get("pid"),
                "已请它再开一个窗口" if handed else "但它没有回应开窗请求",
            )
            return 0 if handed else 1
        logger.error(
            "单实例锁被占用，但对端不是本应用的宿主（端口/握手对不上）——"
            "先关掉那个进程再启动。"
        )
        return 1
    # 进程退出（正常或异常）都要把锁放掉：留着的话下次启动会被自己挡住。
    # 操作系统在进程死亡时也会释放，这里只是让"正常退出"这条路更明确。
    import atexit  # noqa: PLC0415

    atexit.register(host_lock.release)

    # 上一次更新的事务收尾：如果上一回是"提交到一半就退出了/没确认成"，就在这里
    # 拿到结论（写成界面能看的那句话），并把**本次事务自己的**残留清掉 —— 现有
    # 安装一个字节都不动。失败不拦启动：它只影响"上次更新成没成"的说明。
    try:
        from desktop.ui_mount import update_transaction  # noqa: PLC0415

        recovered = update_transaction().recover()
        if recovered is not None:
            logger.info("previous update transaction recovered: %s (%s)",
                        recovered.id, recovered.phase)
    except Exception:  # pragma: no cover - 恢复失败只影响说明
        logger.debug("update transaction recovery failed", exc_info=True)

    desktop_dir = paths.desktop_web_dir()
    if desktop_dir is None:
        logger.warning("web/desktop not found — the desktop UI will not be available")
    ui_active = desktop_dir is not None and not args.upstream_ui

    # Single instance: never start a second kernel on the same port.
    existing = _reuse_running(args.host, args.port)
    if existing is not None:
        logger.info("reusing the running backend at %s", existing)

    plan = BootPlan(
        host=args.host,
        port=args.port,
        # 显式给的端口被占 → 报错；不给 → 优先 8765、被占换空闲（R07 的两种语义）
        explicit_port=args.port is not None,
        desktop_dir=desktop_dir,
        ui_active=ui_active,
        existing=existing,
        debug=args.debug,
    )

    if args.no_window or args.browser or not _window_available():
        return _present_headless(plan, args)

    storage = paths.data_root() / "webview"
    app_root = paths.app_root()

    # 加载 .NET 之前最后一道关：从网上下载解压出来的文件带着 MOTW，.NET 会拒绝加载
    # 这些 DLL，pythonnet 起不来，窗口就永远建不出来。必须赶在 `import webview`
    # 之前（下面那条路径里才第一次 import 它）。见 desktop/motw.py。
    motw_state = motw.guard()
    if motw_state in {"declined", "unfixable"}:
        logger.error(
            "文件带着「来自网络」的标记，不解除的话窗口后端加载不了（%s）\n%s",
            motw_state,
            motw.MANUAL_HINT,
        )
        return 1

    # Window first: the user gets a window in the time it takes to unpack the
    # exe and start WebView2, instead of waiting for the whole kernel as well.
    if splash_enabled():
        try:
            logger.info("showing the window first — the kernel boots behind it")
            return run_window_first(
                plan=plan, args=args, storage_path=storage, app_root=app_root
            )
        except SplashUnsupported as exc:
            logger.warning(
                "window-first startup unavailable (%s) — using the serial order", exc
            )

    return run_serial_window(plan=plan, args=args, storage_path=storage, app_root=app_root)


def _window_available() -> bool:
    from desktop.window import pywebview_available  # noqa: PLC0415

    return pywebview_available()


def _park(server: DesktopServer | None) -> int:
    """Idle until Ctrl+C, then stop the backend. Used by --no-window/--browser."""
    import time  # noqa: PLC0415

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0
    finally:
        if server is not None:
            server.shutdown()


def _present_headless(plan: BootPlan, args: argparse.Namespace) -> int:
    """无窗口路径：只跑后端，或者再开一个浏览器标签页。

    行为与原 ``_present`` 的这两个分支逐字一致 —— CI 的启动探活走的就是
    ``--no-window``，它同时也是量启动耗时的那条路径。
    """
    if plan.existing is not None:
        server: DesktopServer | None = None
        url = plan.url_for(plan.existing, existing_instance_token(plan.existing))
    else:
        try:
            server = start_server(
                host=plan.host,
                port=plan.port,
                desktop_dir=plan.desktop_dir,
                ui_active=plan.ui_active,
                explicit_port=plan.explicit_port,
                announce_host=True,
                log_level="debug" if plan.debug else "info",
            )
        except Exception as exc:
            logger.error("could not start the OpenMinis backend: %s", exc)
            return 1
        url = plan.url_for(server.url, server.access_token)

    # **不带令牌**（R18）：这个地址会被截进日志、也会随诊断包发出去。令牌是鉴权凭据，
    # 谁读到谁能驱动本机这个手里有 shell 的 agent —— 日志里只留"后端在哪个端口"。
    logger.info("OpenMinis Desktop ready — %s", url.split("?")[0])
    trace.report(version=__version__)

    if args.no_window:
        # 同样不带令牌（R18）：这个地址会进日志、随诊断包发出去。
        logger.info("headless mode: backend running at %s (Ctrl+C to stop)",
                    url.split("?")[0])
        return _park(server)

    if not args.browser:
        logger.warning(
            "pywebview is not installed — falling back to a browser tab.\n"
            "  install it with:  pip install \"pywebview>=5.0\""
        )
    webbrowser.open(url)
    return _park(server)


if __name__ == "__main__":
    os.environ.setdefault("OPENMINIS_DESKTOP", "1")
    raise SystemExit(main())
