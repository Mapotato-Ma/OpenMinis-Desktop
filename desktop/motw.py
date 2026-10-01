"""Windows 的「网络来源标记」（MOTW / Mark of the Web）会让 .NET 拒绝加载 DLL。

## 现场

v0.4.6 的便携版第一次发到一台新电脑上，用户双击 exe：SmartScreen 弹一次，点「仍要
运行」，然后**什么都没有**（任务管理器里也没有）。日志里是：

    File "webview\\platforms\\winforms.py", line 17, in <module>
        import clr
    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize
      from ...\\_internal\\pythonnet\\runtime\\Python.Runtime.dll

## 机制

Windows 会给「从网络下载来的文件」写一个 ``Zone.Identifier`` 备用数据流（ADS），
而 **Windows 自带的解压工具会把这个流传染给解压出来的每一个文件**。.NET 看见它
就不加载那个程序集（``HRESULT: 0x80131515``，即「不支持操作」），于是 pythonnet
起不来 → pywebview 建不出窗口 → ``webview.start()`` 抛异常 → 退出码 1。

用户端看到的是「双击没反应」，因为窗口版没有控制台。

## 为什么 CI 发现不了

CI 上的文件都是**本机构建**的（没有标记），而真实用户的文件**永远**来自浏览器下载
解压（一定有标记）。这是自动化流程与真实用户之间一个结构性的盲区 —— 所以
``.github/workflows/build-windows.yml`` 里现在会**人为给产物打上标记**再启动一次，
把这条盲区补上。

## 为什么不偷偷解除

删除 MOTW 是攻击者的常用手法（MITRE T1553.005），静默做容易招杀软误报，也不尊重
用户。所以默认**弹窗问一句**（:mod:`desktop.fatal` 的 ``MessageBoxW``，不依赖
WebView2/pythonnet，这条路上一定弹得出来），用户点是才动手。
``OPENMINIS_MOTW=unblock`` 供 CI 无人值守使用，``ignore`` 完全跳过。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from . import fatal

logger = logging.getLogger(__name__)

#: NTFS 备用数据流的名字。
STREAM = ":Zone.Identifier"

#: prompt（默认，弹窗问）| unblock（直接解除，CI 用）| ignore（不查）
ENV = "OPENMINIS_MOTW"

#: 扫描上限 —— 纯保险丝，正常安装目录里一千多个文件。
SCAN_LIMIT = 20000

MANUAL_HINT = (
    '也可以在 PowerShell 里手动解除：\n'
    '  Get-ChildItem "<解压出来的文件夹>" -Recurse -File | Unblock-File'
)


def _supported() -> bool:
    """MOTW 是 Windows/NTFS 的概念；别的平台直接跳过（测试会替换它）。"""
    return os.name == "nt"


def install_root() -> Path | None:
    """要检查的安装目录。**只在打包版里做** —— 源码运行时 ``sys.executable``
    是 python.exe，它的旁边是整个解释器安装目录，不该去动。"""
    if not getattr(sys, "frozen", False):
        return None
    try:
        return Path(sys.executable).resolve().parent
    except Exception:  # pragma: no cover
        return None


def is_marked(path: Path | str) -> bool:
    """这个文件带没带网络的「锁定」标记。

    用打开备用数据流来判断：``CreateFileW`` 认识 ``文件:流`` 这种写法，而
    ``os.path.exists``（走 ``GetFileAttributesW``）不认。
    """
    if not _supported():
        return False
    try:
        with open(f"{path}{STREAM}", "rb") as fh:
            return bool(fh.read(4096).strip())
    except OSError:
        return False


def find_marked(root: Path, *, limit: int = SCAN_LIMIT) -> list[Path]:
    """扫出安装目录里所有带标记的文件。"""
    if not _supported() or not root.is_dir():
        return []
    marked: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            candidate = Path(dirpath) / name
            if is_marked(candidate):
                marked.append(candidate)
                if len(marked) >= limit:
                    logger.warning("MOTW 扫描到达上限 %d，不再继续", limit)
                    return marked
    return marked


def unblock(paths: list[Path]) -> int:
    """解除这些文件的锁定，返回真正成功的个数。"""
    done = 0
    for path in paths:
        try:
            os.remove(f"{path}{STREAM}")
            done += 1
        except OSError:
            # 删除失败就退一步：把流清空。空流同样不再算「来自互联网」。
            try:
                with open(f"{path}{STREAM}", "wb"):
                    pass
                done += 1
            except OSError:
                logger.debug("解除锁定失败：%s", path, exc_info=True)
    return done


def _ask(count: int, root: Path) -> bool:
    """问用户一句。弹不出来（非 Windows / 无桌面）时按「否」处理 —— 绝不静默改文件。"""
    text = (
        "这些文件是从网上下载并解压出来的，Windows 给它们打上了「来自其他计算机」的标记。\n"
        ".NET 会拒绝加载带这个标记的 DLL，结果是窗口起不来 —— 看起来就是双击没反应。\n\n"
        f"是否解除锁定并继续启动？\n\n  位置：{root}\n  带标记的文件：{count} 个\n\n"
        "选「否」将退出。" + MANUAL_HINT
    )
    # yes_no=True 是**必须的**：漏了它，弹出来的是「只有一个确定」的框，
    # 返回值是 "ok" 而不是 "yes"，于是用户点了确定却被当成「拒绝」→ 直接退出。
    # （真机上就是这么翻车的：用户看到只有「确定」的弹窗，点了之后应用还是起不来。）
    answer = fatal.message_box(
        text, title="OpenMinis Desktop — 需要解除文件锁定", yes_no=True
    )
    if answer is None:
        logger.error("需要解除文件锁定，但这里弹不出对话框（%d 个文件）", count)
        return False
    return answer == "yes"


def _running_exe() -> Path | None:
    """正在运行的那个 exe。它自己带不带标记都无所谓 —— 它已经跑起来了；而且它的文件
    被占用（改不动），去改它又正是杀软爱盯的动作（删除 MOTW，MITRE T1553.005）。"""
    if not getattr(sys, "frozen", False):
        return None
    try:
        return Path(sys.executable).resolve()
    except Exception:  # pragma: no cover
        return None


def guard(root: Path | None = None, *, ask: bool | None = None) -> str:
    """在**加载 .NET 之前**处理 MOTW。返回发生了什么：

    ``clean`` 没有标记 / ``unblocked`` 已解除 / ``declined`` 用户拒绝（调用方该退出）/
    ``skipped`` 不适用或已按配置跳过 / ``unfixable`` 标记还在，但解除失败。
    """
    if not _supported():
        return "skipped"
    mode = os.environ.get(ENV, "prompt").strip().lower()
    if mode == "ignore":
        return "skipped"

    root = root if root is not None else install_root()
    if root is None or not root.is_dir():
        return "skipped"

    marked = [p for p in find_marked(root) if p != _running_exe()]
    if not marked:
        return "clean"

    if mode == "unblock":
        consent = True
    elif ask is not None:
        consent = ask
    else:
        consent = _ask(len(marked), root)

    if not consent:
        logger.warning("用户拒绝解除 %d 个文件的锁定，窗口后端将无法加载", len(marked))
        return "declined"

    done = unblock(marked)
    if done < len(marked):
        logger.warning("只解除了 %d/%d 个文件的锁定", done, len(marked))
        return "unfixable" if done == 0 else "unblocked"

    logger.info("已解除 %d 个文件的锁定（这些文件来自网络下载）", done)
    return "unblocked"
