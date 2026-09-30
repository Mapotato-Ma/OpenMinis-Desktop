"""可覆盖载荷（payload/）—— "更新只换几 MB"的机制 [T-payload-overlay]。

载荷是 exe 旁边的一个 ``payload/`` 目录：它被插到 ``sys.path`` **最前面**，
于是盖过 exe 里冻住的那一份内核与界面。这样更新一个已经装好的实例只需要换掉
这一个目录（实测 **2.33 MB**），而不是重发 **37.9 MB** 的整包 —— 内核的 ``.pyc``
本来是被打进 exe 里的（``packaging/OpenMinisDesktop.spec`` 的 ``PYZ`` → ``PKG``）。

覆盖机制只取决于两件事：``app_root()`` 怎么算、``sys.path`` 的先后。都验得到，
不需要 Windows 也不需要 PyInstaller。**import 优先级必须走子进程验** ——
进程内 ``sys.modules`` 已经缓存了 ``openminis``，改 ``sys.path`` 是换不掉的
（那正是这个机制最容易"看起来对了其实没生效"的地方）。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from desktop import paths

REPO = Path(__file__).resolve().parent.parent.parent

#: 在子进程里伪造一个 PyInstaller 布局，然后报告内核到底从哪儿加载。
_PROBE = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {repo!r})
    sys.frozen = True
    sys.executable = {exe!r}
    sys._MEIPASS = {internal!r}
    sys.path.append({internal!r})          # 真实 frozen 进程里 bootloader 会这么干
    from desktop import paths
    print("payload_root", paths.payload_root())
    paths.install_payload_path()
    import openminis
    print("origin", openminis.__file__)
    print("ui", paths.desktop_web_dir())
    """
)


def _make_install(tmp_path: Path, *, with_payload: bool) -> tuple[Path, Path]:
    """造一个假安装目录：(exe, _internal)。"""
    app = tmp_path / "OpenMinisDesktop"
    internal = app / "_internal"
    (internal / "openminis").mkdir(parents=True)
    (internal / "openminis" / "__init__.py").write_text('MARK = "bundle"\n')
    if with_payload:
        pkg = app / "payload" / "openminis"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text('MARK = "payload"\n')
        ui = app / "payload" / "web" / "desktop"
        ui.mkdir(parents=True)
        (ui / "index.html").write_text("PAYLOAD-UI")
    return app, internal


def _probe(tmp_path: Path, *, with_payload: bool) -> dict[str, str]:
    app, internal = _make_install(tmp_path, with_payload=with_payload)
    script = _PROBE.format(
        repo=str(REPO), exe=str(app / "OpenMinisDesktop.exe"), internal=str(internal)
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    return dict(line.split(" ", 1) for line in out.stdout.strip().splitlines() if " " in line)


def test_payload_shadows_the_bundled_kernel(tmp_path):
    """有 payload/ 时，内核与界面都必须从载荷加载 —— 这是整个机制的核心。"""
    got = _probe(tmp_path, with_payload=True)
    assert "payload" in got["payload_root"]
    assert str(tmp_path) in got["origin"], got
    assert "/payload/" in got["origin"].replace("\\", "/"), got
    assert "/payload/" in got["ui"].replace("\\", "/"), got


def test_without_payload_nothing_is_injected(tmp_path):
    """没有 payload/ 时，一行都不插手 —— **老安装包不会被这个机制弄坏**。

    注意这里**不能**断言"回落到 _internal"：仓库被 `pip install -e .` 装过之后，
    `src/` 本身就在 import 路径上，内核会从源码树解析（本地没装可编辑包时又是
    另一种结果）。那种断言是在测环境，不是在测代码。所以分两层：

    * 子进程里断言"**不是**从载荷加载"（这条与环境无关）；
    * 进程内断言"没往 sys.path 里塞任何东西"（这条是确定的 —— 落回 bundle 的
      真实路径就是"我们什么都不做"）。
    """
    got = _probe(tmp_path, with_payload=False)
    assert got["payload_root"] == "None"
    assert "/payload/" not in got["origin"].replace("\\", "/"), got


def test_install_payload_path_is_a_noop_without_a_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(tmp_path / "x.exe"), raising=False)
    before = list(sys.path)
    assert paths.install_payload_path() is None
    assert sys.path == before, "没有载荷时不该动 sys.path"


def test_payload_root_ignores_a_file_with_that_name(tmp_path, monkeypatch):
    """``payload`` 得是个**目录**：有人丢个同名文件在那儿也不该改变行为。"""
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(tmp_path / "x.exe"), raising=False)
    (tmp_path / "payload").write_text("not a dir")
    assert paths.payload_root() is None


def test_install_payload_path_puts_it_first_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(tmp_path / "x.exe"), raising=False)
    payload = tmp_path / "payload"
    (payload / "openminis").mkdir(parents=True)

    assert paths.install_payload_path() == payload
    assert sys.path[0] == str(payload)
    # 再调一次不该把重复项堆进 sys.path（attach/启动路径会调多次）
    assert paths.install_payload_path() == payload
    assert sys.path.count(str(payload)) == 1
    sys.path.remove(str(payload))


def test_module_origin_reports_real_file():
    assert paths.module_origin("desktop.paths", ).endswith("paths.py")
    assert paths.module_origin("no.such.module.xyz") is None


@pytest.mark.parametrize("name", ["openminis", "desktop"])
def test_bootstrap_modules_are_not_in_the_payload(name):
    """载荷里**不该有** ``desktop/`` —— 能决定"去哪儿找载荷"的代码不能由载荷提供。

    半个桌面包能从载荷覆盖、半个不能，那种版本错配比"壳改完要重发 exe"更难查。
    这条钉住 ``scripts/make_payload.py`` 的 ``PAYLOAD_TREES``。
    """
    from scripts.make_payload import PAYLOAD_TREES  # noqa: PLC0415

    sources = [src for src, _dst in PAYLOAD_TREES]
    assert "desktop" not in sources
    assert "src/openminis" in sources and "web/desktop" in sources
