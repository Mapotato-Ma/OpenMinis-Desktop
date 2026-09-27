"""Stop console children from flashing a window on Windows.

A PyInstaller ``console=False`` build is a **GUI-subsystem** process: it has no
console attached. When such a process starts a console child without
``CREATE_NO_WINDOW``, Windows allocates a *brand new* console for that child —
and on Windows 11 the new-console case is handed to Windows Terminal, so the
user sees a black window with a tab bar, titled after the interpreter
(``C:\\Program Files\\Git\\bin\\bash`` in the original report).

That is what made every single chat message pop a window: the agent ran
``shell_execute``, which spawns the shell via ``subprocess.Popen`` with no
``creationflags`` (``tools/firstagenttools/bash/bash.py``). The kernel *does*
get this right for Chrome — ``chrome_launcher.py`` passes
``CREATE_NO_WINDOW`` — it just never applied it to the shell it runs agent
commands in. ``search_files`` (grep/rg via ``subprocess.run``) has the same
gap, so the patch is installed globally rather than per call site.

Consistent with the rest of this shell, this is applied at runtime instead of
editing ``src/`` — the upstream tree stays byte-identical, so merging upstream
stays trivial and it is obvious which behaviour is ours.

Note ``CREATE_NO_WINDOW`` does **not** break stdio: the child still gets a
console (just without a window) and the kernel already drives it over pipes
with ``stdin=DEVNULL``. Only patched when we truly have no console — launched
from a terminal, children inherit that console and no window appears anyway.
"""

from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

#: winbase.h — runs a console app without a console *window*.
CREATE_NO_WINDOW = 0x08000000

#: ``creationflags`` is the 14th positional parameter of Popen (index 13 once
#: the command itself occupies 0). Nobody passes it positionally, but if a call
#: ever did we must not silently rewrite argv.
_MAX_SAFE_POSITIONAL_ARGS = 13

#: Escape hatch. CREATE_NO_WINDOW also clears the child's console *handle*
#: (not merely its window), and a rare Windows tool that insists on a real
#: console can misbehave because of it. Setting this to 1 restores the old
#: behaviour without a rebuild.
DISABLE_ENV = "OPENMINIS_DESKTOP_SHOW_CONSOLE"

_installed = False


def _has_console() -> bool:
    """True when this process already owns a console."""
    try:
        import ctypes  # noqa: PLC0415

        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:  # pragma: no cover — no ctypes/windll is not fatal
        return True


def has_console() -> bool | None:
    """Public probe used by --diagnose; ``None`` off Windows."""
    if sys.platform != "win32":
        return None
    return _has_console()


def merge_flags(existing: int | None) -> int:
    """OR ``CREATE_NO_WINDOW`` into whatever the caller already asked for.

    Pure, and deliberately public: it is the one piece of this module whose
    behaviour can be asserted on every platform, so the test suite does.
    """
    return int(existing or 0) | CREATE_NO_WINDOW


def install(force: bool = False) -> str:
    """Patch ``subprocess.Popen`` so children never open a console window.

    Returns a short human-readable status string (also logged by the caller).
    ``force`` skips the platform/console guards so a test can exercise the
    patch anywhere — the flag arithmetic itself is not platform-specific.
    """
    global _installed
    if not force:
        import os  # noqa: PLC0415

        if os.environ.get(DISABLE_ENV):
            return f"disabled by {DISABLE_ENV}"
        if sys.platform != "win32":
            return "skipped: not windows"
        if _installed:
            return "already installed"
        if _has_console():
            # A console build: children inherit our console, so no window is
            # created and hiding it would only make debugging harder.
            return "skipped: process has a console"

    original_init = subprocess.Popen.__init__

    def patched_init(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if len(args) <= _MAX_SAFE_POSITIONAL_ARGS:
            # OR rather than assign: the caller may have asked for something
            # (chrome_launcher already sets CREATE_NO_WINDOW, others may set
            # CREATE_NEW_PROCESS_GROUP) and we must not drop it.
            kwargs["creationflags"] = merge_flags(kwargs.get("creationflags"))
        return original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched_init
    _installed = True
    logger.info("[no-console] subprocess.Popen patched with CREATE_NO_WINDOW")
    return "installed"
