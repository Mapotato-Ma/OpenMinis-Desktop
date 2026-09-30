"""组装可覆盖载荷（``payload/``）—— 就是"更新只换几 MB"的那个包。

## 为什么要拆

现在内核的 ``.pyc`` 是被打进 ``OpenMinisDesktop.exe`` 里的（见
``packaging/OpenMinisDesktop.spec`` 里 ``PYZ`` → ``PKG (CArchive)`` 那两步），
所以**改内核一行也要重发整个 37MB 的 exe**。而天天变的东西其实很小：

    src/openminis   2.8 MB（源码）      ← 内核
    web/desktop     1.9 MB              ← 界面
    ------------------------------------
    压缩后          ~1.7 MB            ← 而整包 37.9 MB

``desktop/paths.py`` 的 ``payload_root()`` 让 exe 旁边这个 ``payload/`` 目录
排在 ``sys.path`` 最前面，于是它盖过 bundle 里冻住的那一份。**壳与运行时留在
exe 里，内核与界面走载荷** —— 更新一个装到用户机器上的实例，只需要换这一个目录。

## 为什么发源码 + 预编译字节码（而不是只发一种）

* 只发源码：更新后**第一次**启动要多花约 0.19 秒现编译（实测），而且只在安装
  目录可写时才会缓存下来 —— Program Files 那种只读安装就每次启动都付。
* 用 ``--invalidation-mode checked-hash``（PEP 552）编译：``.pyc`` 按源码**哈希**
  校验而不是 mtime，所以**打包再解包、时间戳全变，字节码照样被复用**（实测过：
  默认模式会失效重编译，checked-hash 不会）。留源码是为了 traceback 里还有行号。
  另外它**仍然会发现源码内容变了** —— 这对更新很重要：万一只换了一半的文件，
  不会静默跑旧字节码。

  （踩过的坑记一笔：别去读 ``.pyc`` 头里的 flag 位来判断模式 —— 本机 3.12 上
  ``checked-hash`` 编出来的 flag 读出来是 ``0b11``，看着像 unchecked。**以行为为准**：
  改源码内容能被发现、改 mtime 不受影响，就是 checked-hash。我差点照 flag 编号
  把它"修"成错的。）
* 两者一起发：压缩后 1.7 MB vs 1.1 MB。多这 0.6 MB 换"更新后第一眼就是快的"
  和排障时看得见的栈，值。

用法：

    python scripts/make_payload.py --out dist/OpenMinisDesktop/payload
    python scripts/make_payload.py --out build/payload --zip build/payload.zip
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

#: 载荷里放什么（相对仓库根）。**刻意不含 ``desktop/``**：壳里有引导模块
#: （``paths`` / ``stdio`` / ``startup_trace``），它们必须由 exe 提供 ——
#: 能决定"去哪里找载荷"的代码不能由载荷自己给。半个桌面包能从载荷覆盖、半个不能，
#: 那种版本错配比"壳改完要重发 exe"更难查，所以这里一刀切：壳整包留在 exe。
PAYLOAD_TREES = (
    ("src/openminis", "openminis"),
    ("web/desktop", "web/desktop"),
)

PAYLOAD_MANIFEST = "payload.json"


def _iter_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _copy_tree(src: Path, dst: Path) -> None:
    """连同包数据一起拷（skill 包、knowledge wiki、插件清单都在里面）。

    ``__pycache__`` / ``*.pyc`` 不拷 —— 由我们统一用 checked-hash 重编，
    免得把开发机上 mtime 校验的旧字节码带出去（那种解压后就失效了）。
    """
    for path in _iter_files(src):
        rel = path.relative_to(src)
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _compile(package: Path) -> None:
    """用 checked-hash 模式编译：解压后 mtime 变了也不会失效。"""
    subprocess.run(
        [
            sys.executable,
            "-m",
            "compileall",
            "-q",
            "--invalidation-mode",
            "checked-hash",
            str(package),
        ],
        check=True,
    )


def _tree_digest(root: Path) -> tuple[str, int, int]:
    """(treeSha256, 文件数, 字节数)。按相对路径排序 → 同样的内容得同样的值。"""
    digest = hashlib.sha256()
    count = 0
    total = 0
    for path in _iter_files(root):
        if path.name == PAYLOAD_MANIFEST:
            continue
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
        count += 1
        total += len(data)
    return digest.hexdigest(), count, total


def build(out: Path, *, zip_path: Path | None = None) -> dict:
    import openminis  # noqa: PLC0415

    from desktop import __version__ as shell_version  # noqa: PLC0415

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for rel_src, rel_dst in PAYLOAD_TREES:
        src = ROOT / rel_src
        if not src.is_dir():
            raise SystemExit(f"找不到 {src}")
        _copy_tree(src, out / rel_dst)

    _compile(out / "openminis")

    digest, count, total = _tree_digest(out)
    manifest = {
        "format": 1,
        "kernelVersion": openminis.__version__,
        #: 载荷只对**同一个壳**有效 —— 壳在 exe 里，换壳要重装。更新器拿它比对。
        "shellVersion": shell_version,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "builtAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": count,
        "bytes": total,
        "treeSha256": digest,
    }
    (out / PAYLOAD_MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if zip_path is not None:
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in _iter_files(out):
                if path.name == PAYLOAD_MANIFEST:
                    continue
                zf.write(path, path.relative_to(out).as_posix())
            # 清单放最后：解压方可以先读完整个包再校验
            zf.write(out / PAYLOAD_MANIFEST, PAYLOAD_MANIFEST)

    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="组装可覆盖载荷 payload/")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "build" / "payload",
        help="载荷目录（打包版用 dist/OpenMinisDesktop/payload）",
    )
    parser.add_argument("--zip", type=Path, default=None, help="顺便产出更新包 zip")
    args = parser.parse_args()

    manifest = build(args.out.resolve(), zip_path=args.zip.resolve() if args.zip else None)

    size = sum(p.stat().st_size for p in _iter_files(args.out))
    print(f"载荷目录 {args.out}")
    print(f"  {manifest['files']} 个文件，{size / 1048576:.2f} MB（未压缩）")
    print(f"  kernel {manifest['kernelVersion']} / shell {manifest['shellVersion']}"
          f" / py{manifest['python']}")
    print(f"  treeSha256 {manifest['treeSha256'][:16]}…")
    if args.zip:
        print(f"  更新包 {args.zip}（{args.zip.stat().st_size / 1048576:.2f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
