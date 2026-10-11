#!/usr/bin/env python3
"""生成一份**可复现的运行时清单**（R19）：

    python scripts/make_runtime_manifest.py --out dist/OpenMinisDesktop/payload/runtime.json

为什么要它：壳指纹（``desktop/build_id.py``）改前只看壳源码 —— 于是"壳没变、依赖
升级了"会被判成"可以只换载荷"，用户装完起不来。现在壳指纹把 ``pyproject.toml`` 与
``uv.lock`` 也算进去（见 ``scripts/shell_id.py``），这份清单则是**打包当时实际装了什么**
的现场记录：出问题时不用猜"那个版本的包里到底是什么"。

判据是"信息如实"而不是"好看"：装不到的包就写 ``<unknown>``，web/dist 不在就留空。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 直接依赖（pyproject 里那几条）—— 逐个记**实际装到的版本**。
_DIRECT = (
    "pydantic", "httpx", "anyio", "sqlalchemy", "aiosqlite", "python-dotenv",
    "pyyaml", "keyring", "cryptography", "typer", "rich", "textual", "fastapi",
    "uvicorn", "websockets", "python-multipart", "sse-starlette", "pillow",
    "pywebview", "pyinstaller",
)


def _version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "<unknown>"


def _web_dist_id() -> str:
    """web/dist 的指纹（界面也是载荷的一部分，得能对上号）。"""
    dist = ROOT / "web" / "dist"
    if not dist.is_dir():
        return ""
    digest = hashlib.sha256()
    for path in sorted(dist.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(dist).as_posix().encode("utf-8"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


def build_manifest() -> dict:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": sys.platform,
        "deps": {name: _version(name) for name in _DIRECT},
        "webDist": _web_dist_id(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成运行时清单")
    parser.add_argument("--out", required=True, help="写到哪（例如 payload/runtime.json）")
    args = parser.parse_args(argv)

    payload = build_manifest()
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"运行时清单写好了：{target}")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
