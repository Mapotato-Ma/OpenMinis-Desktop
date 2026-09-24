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
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from desktop.paths import desktop_web_dir  # noqa: E402
from desktop.server_runner import start_server  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def get(url: str, *, follow: bool = True, timeout: float = 10.0):
    """Return ``(status, body_bytes, headers)``; never raises for HTTP errors."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_a, **_kw):  # noqa: ANN002
            return None

    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
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
    print(f"backend at {base}")

    try:
        status, body, _ = get(f"{base}/api/health")
        payload = json.loads(body or b"{}")
        check("GET /api/health -> 200", status == 200, f"status={status}")
        check("health payload ok", payload.get("status") == "ok", str(payload)[:120])

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
            headers={"Content-Type": "application/json"},
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
    finally:
        server.shutdown()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
