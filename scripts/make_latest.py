"""产出发给应用的更新清单 ``latest.json``（跟着 release 一起发）。

## 这是个什么形状、抄的是谁

抄的是 CC Switch（Tauri）那一套：应用去拉一个**固定地址**的清单，比版本，下载，
校验，替换自己，重启。

    清单地址   https://github.com/<owner>/<repo>/releases/latest/download/latest.json
               ↑ `releases/latest` 永远指向最新那个 release，所以应用里不用写版本号，
                 也不吃 GitHub API 的限流（它是 release 资产，不是 API）。

## 为什么清单里同时有"小更新"和"整包"

第 2 步把内核与界面拆成了 exe 旁边的 ``payload/``（见 ``desktop/paths.py`` 与
``scripts/make_payload.py``）。于是：

* ``payload`` —— **只换载荷**，2~3 MB，**不碰 exe**，所以不需要替换正在运行的文件，
  重启即可生效。天天变的东西（内核、界面）都走这条。
* ``full`` —— 壳或运行时变了才需要（``shellId`` 对不上时）。

``shellId`` 是"载荷能不能用"的判据：``desktop/build_id.py`` 里那份壳指纹。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO), str(REPO / "src")]

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # pragma: no cover
        pass

#: 发布资产的固定名字。CI 与本地构建都产这些名字，改这里要一起改 workflow。
PAYLOAD_ASSET = "OpenMinisDesktop-update.zip"
FULL_ASSET = "OpenMinisDesktop-portable.zip"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_entry(dist: Path, name: str, tag: str, owner_repo: str) -> dict | None:
    path = dist / name
    if not path.is_file():
        return None
    return {
        "url": f"https://github.com/{owner_repo}/releases/download/{tag}/{name}",
        "sha256": sha256_of(path),
        "bytes": path.stat().st_size,
    }


def build(
    *,
    dist: Path,
    tag: str,
    owner_repo: str,
    notes: str = "",
    out: Path | None = None,
) -> dict:
    from desktop import __version__ as app_version  # noqa: PLC0415
    from desktop.build_id import SHELL_ID  # noqa: PLC0415

    manifest: dict = {
        "format": 1,
        "version": app_version,
        #: **壳指纹**：应用拿它和 exe 里那份（desktop/build_id.py）比对，决定
        #: "只换载荷"还是"下整包"。用版本号当判据是错的 —— 版本号每次都变，
        #: 那样每次更新都会退化成 39MB 的整包。
        "shellId": SHELL_ID,
        #: 前端展示用（它自己算不了）。
        "pubDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "notes": notes,
        "assets": {},
    }
    for key, name in (("payload", PAYLOAD_ASSET), ("full", FULL_ASSET)):
        entry = asset_entry(dist, name, tag, owner_repo)
        if entry is not None:
            manifest["assets"][key] = entry

    if "full" not in manifest["assets"]:
        # 没有整包 = 壳更新时用户会无处可去。宁可让流水线红，也别发一个"只能小更新"
        # 的清单出去。
        raise SystemExit(f"缺少 {FULL_ASSET}：清单必须带整包")

    target = out or (dist / "latest.json")
    target.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="产出更新清单 latest.json")
    parser.add_argument("--dist", type=Path, default=REPO / "dist", help="构建产物目录")
    parser.add_argument("--tag", required=True, help="release tag，形如 v0.4.6")
    parser.add_argument("--repo", default="Mapotato-Ma/OpenMinis-Desktop", help="owner/repo")
    parser.add_argument("--notes", default="", help="更新说明（界面里会显示）")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    manifest = build(
        dist=args.dist.resolve(),
        tag=args.tag,
        owner_repo=args.repo,
        notes=args.notes,
        out=args.out.resolve() if args.out else None,
    )
    print(f"更新清单 {args.out or (args.dist / 'latest.json')}")
    print(f"  version {manifest['version']} / shellId {manifest['shellId']}")
    for key, entry in manifest["assets"].items():
        print(f"  {key:8} {entry['bytes'] / 1048576:7.2f} MB  {entry['sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
