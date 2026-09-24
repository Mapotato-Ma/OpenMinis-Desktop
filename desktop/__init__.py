"""OpenMinis Desktop — native window shell around the OpenMinis kernel.

This package turns the Python port's ``FastAPI`` backend into a real desktop
application: a native OS window (WebView2 on Windows, WebKit on macOS, GTK/Qt
on Linux) instead of a browser tab, plus a desktop-first UI.

Nothing here changes the upstream kernel — the shell imports
``openminis.server.main.app`` as-is and only *adds* routes and a window.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
