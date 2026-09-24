# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the OpenMinis Desktop windowed app.

Sibling of ``packaging/OpenMinis.spec`` (which builds the headless web
server). Differences that matter:

* **Entry point** is ``desktop_main.py`` — it adds the window shell on top of
  the same kernel.
* **``web/desktop`` is bundled.** The desktop UI is plain HTML/CSS/JS with no
  build step, so unlike ``web/dist`` it is always present.
* **pywebview and its .NET bridge are collected whole.** WebView2 is reached
  through ``clr``/``pythonnet``, which loads assemblies by reflection — static
  analysis misses most of it, and a missing ``WebView2Loader.dll`` shows up as
  an opaque COM error at runtime rather than an ImportError.
* **``console=False``** by default so it behaves like a real GUI app; set
  ``OPENMINIS_CONSOLE=1`` when you want the log console for debugging.

Shape is chosen with ``OPENMINIS_ONEFILE=1`` (onedir otherwise).
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent
PKG = ROOT / "src" / "openminis"

# ---------------------------------------------------------------------------
# bundled data
# ---------------------------------------------------------------------------
datas: list[tuple[str, str]] = []
binaries: list[tuple[str, str]] = []
hiddenimports: list[str] = []


def add_tree(src: Path, dest: str) -> None:
    """Bundle every file under ``src`` into ``dest`` (skipping source maps)."""
    if not src.is_dir():
        return
    for p in sorted(src.rglob("*")):
        if p.is_file() and p.suffix != ".map" and "__pycache__" not in p.parts:
            datas.append((str(p), str(Path(dest) / p.relative_to(src).parent)))


# The desktop UI is the whole point of this build — bundle it first.
add_tree(ROOT / "web" / "desktop", "web/desktop")

# The upstream mobile UI is optional here (the desktop UI is the default
# surface) but costs little and lets `--upstream-ui` work in the frozen build.
add_tree(ROOT / "web" / "dist", "web/dist")

# Package data: SKILL.md bundles, the knowledge wiki, plugin manifests …
for p in sorted(PKG.rglob("*")):
    if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts:
        datas.append((str(p), str(Path("openminis") / p.relative_to(PKG).parent)))

# ---------------------------------------------------------------------------
# pywebview + its native bridges
# ---------------------------------------------------------------------------
# collect_all swallows nothing: if the package is absent (Linux CI without the
# GUI extra) we simply skip it and the app falls back to a browser tab.
for _pkg in ("webview", "clr_loader", "pythonnet", "clr"):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception as exc:  # pragma: no cover - optional dependency
        print(f"[spec] skipping optional package {_pkg}: {exc}")

# Explicit backends: pywebview picks a platform module by string at runtime.
hiddenimports += [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "webview.platforms.gtk",
    "webview.platforms.qt",
    "webview.platforms.cocoa",
    "desktop",
    "desktop.app",
    "desktop.paths",
    "desktop.server_runner",
    "desktop.ui_mount",
    "desktop.window",
]

# ---------------------------------------------------------------------------
# kernel dependencies that are imported dynamically
# ---------------------------------------------------------------------------
hiddenimports += [
    # sqlite+aiosqlite driver is imported inside SQLAlchemy at engine-creation
    "aiosqlite",
    # read_image resizes/re-encodes via Pillow, and Pillow loads its format
    # plugins lazily.
    "PIL",
    "PIL.Image",
    "PIL.ImageFile",
    "PIL.JpegImagePlugin",
    "PIL.PngImagePlugin",
    # uvicorn resolves loop / protocol / lifespan classes by name.
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

# Plugin channel drivers are imported via import_module(); the static scan
# cannot see them, and the failure mode is "QQ bot silently missing".
for _driver in sorted((PKG / "plugins" / "drivers").glob("*.py")):
    if _driver.stem != "__init__":
        hiddenimports.append("openminis.plugins.drivers." + _driver.stem)

# ---------------------------------------------------------------------------
# trimming
# ---------------------------------------------------------------------------
# tkinter is dead weight in a WebView app, and the numerical stack is never
# imported by the kernel.
excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "IPython",
    "pytest",
    "_pytest",
    "pytest_asyncio",
]

onefile = os.environ.get("OPENMINIS_ONEFILE") == "1"
console = os.environ.get("OPENMINIS_CONSOLE") == "1"
icon = ROOT / "desktop" / "assets" / "icon.ico"

a = Analysis(
    [str(ROOT / "desktop_main.py")],
    pathex=[str(ROOT), str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe_common = dict(
    name="OpenMinisDesktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=console,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon) if icon.is_file() else None,
)

if onefile:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], runtime_tmpdir=None, **exe_common)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_common)
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="OpenMinisDesktop",
    )
