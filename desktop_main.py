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

from desktop.app import main  # noqa: E402

if __name__ == "__main__":
    os.environ.setdefault("OPENMINIS_DESKTOP", "1")
    raise SystemExit(main())
