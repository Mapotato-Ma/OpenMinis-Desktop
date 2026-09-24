"""Give a windowed build somewhere to write.

A PyInstaller ``console=False`` executable on Windows has **no console
attached**, and Python's ``sys.stdout`` / ``sys.stderr`` come back as ``None``.
That is fatal here rather than cosmetic:

* the kernel's ``setup_logging()`` builds a ``rich`` handler that writes to
  stdout, and a plain ``logging.StreamHandler(sys.stderr)`` when rich is
  missing — both blow up with ``AttributeError: 'NoneType' object has no
  attribute 'write'`` the moment anything is logged;
* uvicorn configures its own handlers against ``ext://sys.stderr``.

The failure mode is nasty because it happens *during startup*, so the process
dies before it can bind a port, and a windowed build shows the user nothing at
all. CI caught exactly this: the exe built fine and then never became healthy.

So: point the missing streams at a log file under the user data dir. A GUI app
logging to a file is the right behaviour anyway — it is also the only way to
diagnose a field failure, since there is no console to read.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

LOG_NAME = "desktop.log"


def log_path() -> Path:
    """Where a console-less build writes its logs."""
    from .paths import data_root  # noqa: PLC0415

    return data_root() / "logs" / LOG_NAME


def _usable(stream: object) -> bool:
    return stream is not None and callable(getattr(stream, "write", None))


def ensure_console_streams() -> Path | None:
    """Replace missing std streams with a log file.

    Returns the log path when a substitution happened, ``None`` when the
    process already had real streams (a console build, or any non-Windows run).
    """
    if _usable(sys.stdout) and _usable(sys.stderr):
        return None

    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Line-buffered so a crash still leaves the last lines on disk.
        stream = open(path, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115
    except Exception:
        # Last resort: a file handle we cannot create must not be the thing
        # that stops the app from starting.
        stream = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        path = None  # type: ignore[assignment]

    if not _usable(sys.stdout):
        sys.stdout = stream  # type: ignore[assignment]
    if not _usable(sys.stderr):
        sys.stderr = stream  # type: ignore[assignment]
    if not _usable(sys.stdin):
        try:
            sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: SIM115
        except Exception:  # pragma: no cover
            pass

    return path
