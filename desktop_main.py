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

# 可覆盖载荷（打包版里是 exe 旁边的 ``payload/``）要排在 **sys.path 最前面**：
# 它压过 exe 里冻住的那一份内核，这就是"更新只换几 MB"的机制。必须在任何
# ``openminis`` / ``desktop`` 业务模块之前 —— 详见 desktop/paths.py。
from desktop.paths import install_payload_path  # noqa: E402

_PAYLOAD = install_payload_path()

from desktop.stdio import ensure_console_streams  # noqa: E402

# 最早的一次打点：此刻还没 import 任何重型依赖。它和"进程创建时刻"的差就是
# bootloader 解包 + 解释器启动 —— 办公电脑上最可疑的那一段。
from desktop.startup_trace import mark as _trace_mark  # noqa: E402

_trace_mark("python")

# Must run before anything imports rich/uvicorn/logging: a windowed build has
# no console, and the first log call would otherwise kill startup.
_LOG_PATH = ensure_console_streams()

from desktop.fatal import install as install_fatal_guard  # noqa: E402
from desktop.fatal import report_fatal  # noqa: E402

# 任何没人接的异常都要留下证据并让用户看见 —— 无控制台的窗口版最糟的失败模式是
# 「双击没反应」。见 desktop/fatal.py。
install_fatal_guard()

from desktop.app import main  # noqa: E402

if __name__ == "__main__":
    os.environ.setdefault("OPENMINIS_DESKTOP", "1")
    if _LOG_PATH is not None:
        print(f"[openminis] no console attached — logging to {_LOG_PATH}", flush=True)
    try:
        _code = main()
    except SystemExit:
        raise
    except BaseException as _exc:  # noqa: BLE001 - 兜底就是它的职责
        report_fatal(
            _exc,
            context="应用启动失败（在窗口出现之前就退出了）。",
            hint="把上面这段和日志文件发给开发者即可定位。",
        )
        _code = 1
    raise SystemExit(_code)
