#!/usr/bin/env python3
"""Smoke-test the desktop shell without a GUI.

Runs anywhere (including headless CI) and asserts the things that are easy to
break and annoying to debug from a screenshot:

  1. the kernel imports and the desktop routes attach without error
  2. ``/api/health`` answers
  3. ``/`` redirects to the desktop UI, and the UI's three assets are served
  4. ``/api/desktop/info`` reports desktop mode
  5. a session can be created and listed over REST

It deliberately does *not* open a window — CI has no display, and the window
layer is pywebview's problem, not ours.

    python scripts/smoke_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Two environment fixes, both only observable on some platforms — which is
# exactly why this file runs in CI on windows-latest as well as locally.
#
# 1. Isolation. Some assertions below describe a fresh install ("no provider is
#    configured yet"), and a developer machine will usually have a real profile
#    with providers and sessions in it. MINIS_HOME is the kernel's own override
#    (see core/context.py) and wins on every platform; XDG_DATA_HOME is kept as
#    a belt-and-braces for code paths that read it directly.
# 2. Output encoding. Windows consoles default to cp1252, and a check detail
#    containing Chinese made print() raise UnicodeEncodeError — turning a
#    passing assertion into a crashed run. Force UTF-8 and degrade rather than
#    die on glyphs the terminal cannot render.
_SMOKE_HOME = Path(tempfile.mkdtemp(prefix="openminis-smoke-"))
os.environ["MINIS_HOME"] = str(_SMOKE_HOME)
os.environ["XDG_DATA_HOME"] = str(_SMOKE_HOME)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover — not a TextIOWrapper
        pass

from desktop.paths import desktop_web_dir  # noqa: E402
from desktop.server_runner import start_server  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def check_no_console_patch() -> None:
    """Prove the Windows console-window fix actually injects its flag.

    Runs *after* the server is shut down: it temporarily replaces
    ``subprocess.Popen.__init__`` with a recorder, and swapping that out from
    under a live server would be asking for trouble.
    """
    from desktop import no_console  # noqa: PLC0415

    probes = (None, 0, 0x00000200, 0x00000008, no_console.CREATE_NO_WINDOW)
    check(
        "merge_flags never drops the caller's own flags",
        all((v or 0) & no_console.merge_flags(v) == (v or 0) for v in probes),
        ", ".join(hex(no_console.merge_flags(v)) for v in probes),
    )
    check(
        "merge_flags always adds CREATE_NO_WINDOW",
        all(no_console.merge_flags(v) & no_console.CREATE_NO_WINDOW for v in probes),
    )
    if sys.platform != "win32":
        check("no-console patch is a no-op off Windows",
              no_console.install() == "skipped: not windows", no_console.install())
        return

    # The patch must reach real Popen calls, including the subclass route
    # asyncio takes (windows_utils.Popen calls super().__init__).
    real_init = subprocess.Popen.__init__
    seen: dict = {}

    def recorder(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        seen.update(kw)
        self.returncode = 0

    class _AsyncioStyle(subprocess.Popen):
        """Stands in for asyncio.windows_utils.Popen's calling convention."""

        def __init__(self, args, stdin=None, stdout=None, stderr=None, **kw):  # noqa: ANN001
            super().__init__(args, stdin=stdin, stdout=stdout, stderr=stderr, **kw)

    try:
        subprocess.Popen.__init__ = recorder
        status = no_console.install(force=True)
        subprocess.Popen(["true"])
        direct = seen.get("creationflags", 0)
        seen.clear()
        _AsyncioStyle(["true"])
        subclass = seen.get("creationflags", 0)
    finally:
        subprocess.Popen.__init__ = real_init

    check("no-console install reports success", status == "installed", status)
    check("Popen gets CREATE_NO_WINDOW", bool(direct & no_console.CREATE_NO_WINDOW), hex(direct))
    check(
        "asyncio-style subclass gets it too",
        bool(subclass & no_console.CREATE_NO_WINDOW),
        hex(subclass),
    )

    # And a genuine spawn with the flag set must still work — the flag hides a
    # window, it does not break stdio.
    real_status = no_console.install()
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('ok')"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, _err = proc.communicate(timeout=60)
    check(
        "a real child still runs with CREATE_NO_WINDOW applied",
        proc.returncode == 0 and b"ok" in out,
        f"rc={proc.returncode} out={out[:40]!r} status={real_status}",
    )


#: 访问闸门要的令牌，起完服务后填（见 :mod:`desktop.access_gate`）。
#:
#: 桌面壳的本地服务现在**要求令牌**：它挡的是"用户访问的任意网页都能跨域驱动
#: 本机那个手里有 shell 的 agent"。窗口靠 URL 上的 ``?k=`` 换 cookie，外部工具
#: （就是这里、以及 CI 的探针）走这个请求头。
AUTH: dict[str, str] = {}


def get(url: str, *, follow: bool = True, timeout: float = 10.0):
    """Return ``(status, body_bytes, headers)``; never raises for HTTP errors."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_a, **_kw):  # noqa: ANN002
            return None

    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    for _k, _v in AUTH.items():
        opener.addheaders.append((_k, _v))
    try:
        with opener.open(url, timeout=timeout) as resp:
            # Header names arrive in whatever case the server chose; normalise
            # so callers can ask for "Location" and get it either way.
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in (e.headers or {}).items()}


def main() -> int:
    desktop_dir = desktop_web_dir()
    check("web/desktop found", desktop_dir is not None, str(desktop_dir))
    if desktop_dir is None:
        return 1

    print("starting backend…")
    server = start_server(port=0, desktop_dir=desktop_dir, ui_active=True, log_level="warning")
    base = server.url
    AUTH["x-minis-desktop-access"] = server.access_token
    print(f"backend at {base}")

    try:
        status, body, _ = get(f"{base}/api/health")
        payload = json.loads(body or b"{}")
        check("GET /api/health -> 200", status == 200, f"status={status}")
        check("health payload ok", payload.get("status") == "ok", str(payload)[:120])

        # --- 访问闸门 -------------------------------------------------------
        # 这一组断言的是"口子关着"，不是"功能能用"。桌面壳的本地服务原本对**任何
        # 本机客户端**敞开（内核默认没设密码就完全不拦 + CORS 回显 Origin），
        # 而它背后是一个手里有 shell 的 agent —— 用户访问的任意网页都能驱动它。
        _saved = dict(AUTH)
        AUTH.clear()
        try:
            status, _, _ = get(f"{base}/api/chats/sessions")
            check("无令牌的 /api/* 被闸门拒绝", status == 403, f"status={status}")

            status, _, _ = get(f"{base}/api/health")
            check("豁免的 /api/health 仍可匿名问", status == 200, f"status={status}")

            req = urllib.request.Request(
                f"{base}/api/chats/sessions",
                headers={"Origin": "https://evil.example"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    cross = resp.status
            except urllib.error.HTTPError as e:
                cross = e.code
            check("跨站 Origin 被拒（DNS rebinding / 恶意网页那条路）", cross == 403, f"status={cross}")

            # 路径变体：闸门用**前缀**判断受保护路径，路由用**精确**匹配 ——
            # 两者若在某些写法上分歧，那就是"闸门放过去、路由却服务了它"的洞。
            #
            # 判据是**响应类型**而不是状态码：这些写法本来就会落到 SPA 兜底页
            # （返回 index.html，200）。之前我用"不是 2xx"来判，那条实际上在测
            # "web/dist 存不存在" —— 它缺席时给 503、存在时给 200，于是同一个
            # 检查会在两种环境下翻转。真正要测的是"没落到 API 处理函数"，
            # 而 API 一律回 application/json。
            # 判据是"**2xx 且 JSON**"，而不是"不是 2xx"、也不是"不是 JSON"：
            #   * 被闸门拒绝 → 403 + JSON（那是我自己的拒绝体，正是要的）
            #   * 落到 SPA 兜底 → 200 + HTML（这些写法本来就会落到那儿，无害）
            #   * 真被 API 服务了 → 200 + JSON  ← **只有这一种是洞**
            # 用"不是 2xx"会去测 web/dist 存不存在（它缺席给 503、存在给 200，
            # 同一个检查会翻转）；用"不是 JSON"会把拒绝也误判成洞。
            for variant in ("//api/chats/sessions", "/API/chats/sessions",
                            "/./api/chats/sessions", "/api/health/../chats/sessions"):
                status, body, headers = get(f"{base}{variant}")
                ctype = headers.get("content-type", "")
                served_by_api = 200 <= status < 300 and "application/json" in ctype
                check(f"路径变体 {variant} 不落到 API 处理函数", not served_by_api,
                      f"status={status} type={ctype!r} body={body[:60]!r}")

            # 拿 URL 上的令牌换 cookie：拿不到 cookie 就是 403，拿到了就该过。
            setup = f"{base}/api/chats/sessions?k={server.access_token}"
            status, _, headers = get(setup, follow=False)
            check("URL 令牌换 cookie（302 + Set-Cookie）",
                  status == 302 and "set-cookie" in headers, f"status={status} headers={list(headers)}")

            # 界面路径上也要换 —— 窗口加载的是 `/_desktop/`，只在 /api/* 上换的话
            # 窗口永远拿不到 cookie，之后每个 API 调用都被拒，**界面全白**。
            # （这一条是端到端演示抓出来的，单元用例当时只喂了 /api/ 路径。）
            status, _, headers = get(f"{base}/_desktop/?k={server.access_token}", follow=False)
            check("界面路径上也换 cookie（302 + Set-Cookie）",
                  status == 302 and "set-cookie" in headers, f"status={status}")
        finally:
            AUTH.update(_saved)

        status, _body, headers = get(f"{base}/", follow=False)
        check("GET / redirects to the desktop UI", status in (301, 302, 307, 308)
              and "_desktop" in headers.get("location", ""), f"{status} -> {headers.get('location')}")

        status, body, _ = get(f"{base}/_desktop/")
        check("GET /_desktop/ -> 200", status == 200, f"status={status}")
        check("desktop index served", b"OpenMinis Desktop" in body, f"{len(body)} bytes")

        for asset in ("app.js", "style.css"):
            status, body, _ = get(f"{base}/_desktop/{asset}")
            check(f"asset {asset} served", status == 200 and len(body) > 1000,
                  f"status={status} bytes={len(body)}")

        status, body, _ = get(f"{base}/api/desktop/info")
        info = json.loads(body or b"{}")
        check("GET /api/desktop/info -> desktop mode", status == 200 and info.get("desktop") is True)
        check("desktop ui active", info.get("uiActive") is True, str(info.get("uiMount")))

        status, body, _ = get(f"{base}/api/chats/sessions")
        check("GET /api/chats/sessions -> 200", status == 200, f"status={status}")

        req = urllib.request.Request(
            f"{base}/api/chats/sessions", data=b"{}", method="POST",
            headers={"Content-Type": "application/json", **AUTH},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            created = json.loads(resp.read() or b"{}")
        sid = created.get("id") or (created.get("session") or {}).get("id")
        check("POST /api/chats/sessions creates a session", bool(sid), str(sid)[:40])

        status, body, _ = get(f"{base}/api/chats/sessions")
        sessions = json.loads(body or b"{}").get("sessions") or []
        check("new session is listed", any(s.get("id") == sid for s in sessions), f"{len(sessions)} session(s)")

        # The upstream mobile UI must still be reachable, since --upstream-ui
        # is a supported flag and web/dist ships in the bundle.
        status, _body, _ = get(f"{base}/_desktop/window-bootstrap.js")
        check("window bootstrap served", status == 200, f"status={status}")

        status, body, _ = get(f"{base}/api/desktop/chat-readiness")
        ready = json.loads(body or b"{}")
        check("GET /api/desktop/chat-readiness -> 200", status == 200, f"status={status}")
        # A fresh profile has no active provider, so this must report *not*
        # ready — that is exactly the state the settings pane has to explain.
        check(
            "readiness reports not-ready with no provider configured",
            ready.get("ready") is False and ready.get("reason") == "no_active_provider",
            str(ready),
        )

        req = urllib.request.Request(
            f"{base}/api/desktop/test-provider", data=b'{"id":"nope"}', method="POST",
            headers={"Content-Type": "application/json", **AUTH},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            probe = json.loads(resp.read() or b"{}")
        # A probe must never 500: unknown instance is a normal answer.
        check(
            "POST /api/desktop/test-provider answers for an unknown id",
            resp.status == 200 and probe.get("ok") is False,
            str(probe)[:80],
        )

        req = urllib.request.Request(
            f"{base}/api/desktop/test-provider", data=b"not json", method="POST",
            headers={"Content-Type": "application/json", **AUTH},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                bad = (resp.status, json.loads(resp.read() or b"{}"))
        except urllib.error.HTTPError as e:  # 400 is the expected shape
            bad = (e.code, json.loads(e.read() or b"{}"))
        check("test-provider rejects a malformed body with 400",
              bad[0] == 400 and bad[1].get("ok") is False, str(bad)[:80])
    finally:
        server.shutdown()
        shutil.rmtree(_SMOKE_HOME, ignore_errors=True)

    # Server is down — safe to swap subprocess.Popen out from under the process.
    check_no_console_patch()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
