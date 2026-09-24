#!/usr/bin/env python3
"""Frozen-app entry point.

PyInstaller needs a single top-level script, and pointing it straight at
``desktop/app.py`` would leave the ``desktop`` package unresolvable inside the
bundle. This shim fixes the import path for both layouts — source checkout and
``_MEIPASS`` — and then hands over to the real entry point.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from desktop.stdio import ensure_console_streams  # noqa: E402

# Must run before anything imports rich/uvicorn/logging: a windowed build has
# no console, and the first log call would otherwise kill startup.
_LOG_PATH = ensure_console_streams()

from desktop.app import main  # noqa: E402

if __name__ == "__main__":
    os.environ.setdefault("OPENMINIS_DESKTOP", "1")
    if _LOG_PATH is not None:
        print(f"[openminis] no console attached — logging to {_LOG_PATH}", flush=True)
    raise SystemExit(main())
