"""算"壳指纹"：载荷能不能只换载荷，取决于壳有没有真的变。

## 为什么不能拿版本号当判据

第一版把 ``shellVersion`` 设成发布版本号，结果**每次更新都被判成"换壳了"** ——
因为版本号每个 release 都会涨，于是"小更新"这条路上永远走不到，每次都下 39MB。
版本号变了不等于壳变了。

## 判据是什么

``SHELL_ID`` = 对**壳的源码**取哈希（``desktop/`` 下所有 .py + ``desktop_main.py``
+ PyInstaller 的 spec + Python 主次版本）。它只在壳**真的改了一行**时才变。

* 载荷包和更新清单里都带这个值；
* 应用那边也带一份（冻在 exe 里，见 ``desktop/build_id.py``）；
* 两者相等 → 只换载荷（2~3 MB）；不等 → 老老实实下整包。

这比"每次发版都换壳"精确得多：只改内核或界面时，壳的哈希一字不变，小更新照走。

## 为什么还要 CI 校验

``build_id.py`` 是**签进仓库的常量**，改了壳却忘了更新它，这个机制就会骗人
（拿旧壳去配新载荷）。所以 CI 里跑 ``--check``：算出来的和签进去的对不上，
流水线红。忘了就红，比"悄悄发错"强。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: 参与哈希的文件/目录。**不含** build_id.py 自己（它装着结果，会自我循环），
#: 也不含测试（改测试不该让用户的更新变大）。
SHELL_INPUTS = ("desktop", "desktop_main.py", "packaging/OpenMinisDesktop.spec")
SKIP_NAMES = {"build_id.py"}
SKIP_DIRS = {"__pycache__", "tests", "assets"}


def _iter_shell_files() -> list[Path]:
    out: list[Path] = []
    for entry in SHELL_INPUTS:
        path = REPO / entry
        if path.is_file():
            out.append(path)
        elif path.is_dir():
            for item in sorted(path.rglob("*")):
                if not item.is_file() or item.suffix not in {".py", ".spec"}:
                    continue
                if item.name in SKIP_NAMES:
                    continue
                if SKIP_DIRS & set(item.parts):
                    continue
                out.append(item)
    return sorted(out, key=lambda p: p.relative_to(REPO).as_posix())


def normalise(data: bytes) -> bytes:
    """把行尾统一成 LF 再算哈希。

    Windows 上 git 默认按 **CRLF** 检出，Linux/macOS 上是 LF —— 那是**检出方式**的
    差别，不是壳变了。不归一化的话，同一个提交在两个平台上会算出两个壳指纹，而载荷
    恰恰是 Windows 上用的（CI 实测：Verify 在 windows-latest 上因此红过一次）。

    顺手也就容忍了用户本地编辑器改行尾的情况。
    """
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def compute() -> str:
    digest = hashlib.sha256()
    # Python 主次版本也算进去：换解释器（3.12 → 3.13）就是换壳。
    digest.update(f"py{sys.version_info.major}.{sys.version_info.minor}\0".encode())
    for path in _iter_shell_files():
        rel = path.relative_to(REPO).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(normalise(path.read_bytes())).digest())
    return digest.hexdigest()[:16]


def read_pinned() -> str:
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(
        "_shell_build_id", REPO / "desktop" / "build_id.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover
        raise SystemExit("读不到 desktop/build_id.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.SHELL_ID)


def main() -> int:
    parser = argparse.ArgumentParser(description="壳指纹")
    parser.add_argument("--write", action="store_true", help="把算出来的写回 build_id.py")
    parser.add_argument("--show", "--print", dest="show", action="store_true", help="只打印")
    args = parser.parse_args()

    actual = compute()
    if args.show:
        print(actual)
        return 0

    if args.write:
        path = REPO / "desktop" / "build_id.py"
        path.write_text(
            '"""壳指纹 —— **由 scripts/shell_id.py 生成，别手改**。\n\n'
            "壳（desktop/ + 入口 + spec + Python 版本）的内容哈希。载荷只对**同一个壳**\n"
            "有效，应用拿它和更新清单里的值比对，决定「只换载荷」还是「下整包」。\n"
            "改了壳就跑 `python scripts/shell_id.py --write`，CI 会校验它没跑偏。\n"
            '"""\n\n'
            f'SHELL_ID = "{actual}"\n',
            encoding="utf-8",
        )
        print(f"已写入 SHELL_ID = {actual}")
        return 0
    pinned = read_pinned()
    if actual != pinned:
        print(f"壳指纹对不上：算出来 {actual}，签在 desktop/build_id.py 里的是 {pinned}")
        print("改了壳的话跑一下：python scripts/shell_id.py --write")
        return 1
    print(f"壳指纹一致：{actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
