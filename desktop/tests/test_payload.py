"""可覆盖载荷（payload/）—— "更新只换几 MB"的机制 [T-payload-overlay]。

载荷是 exe 旁边的一个 ``payload/`` 目录：内核与界面从它**优先**加载，于是更新一个
已经装好的实例只需要换掉这一个目录（实测 **2.33 MB**），而不是重发 **37.9 MB** 的
整包 —— 内核的 ``.pyc`` 本来是被打进 exe 里的（``packaging/OpenMinisDesktop.spec``
的 ``PYZ`` → ``PKG``）。

## 这里的实验模型必须"和现实一样苛刻"

覆盖机制最容易骗过自己：本地用 ``sys.path`` 模拟 bundle 会**全绿**，而打包版根本不
生效。区别在于 —— **PyInstaller 冻出来的应用里，bundle 内的模块是由 ``sys.meta_path``
上的冻结导入器提供的，而 meta_path 的查找器一律排在 ``sys.path`` 之前。**
所以下面这个探针**特意装了一个假的冻结导入器**，把那个优先关系复现出来。
（第一版没装，测试绿了，真正的机制却是错的。）

另外，**import 优先级必须走子进程验**：进程内 ``sys.modules`` 已经缓存了
``openminis``，改 ``sys.path`` / ``sys.meta_path`` 都换不掉。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from desktop import paths

REPO = Path(__file__).resolve().parent.parent.parent

#: 在子进程里伪造一个 PyInstaller 布局（含 meta_path 上的冻结导入器），
#: 然后报告内核到底从哪儿加载。
_PROBE = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {repo!r})
    sys.frozen = True
    sys.executable = {exe!r}
    sys._MEIPASS = {internal!r}
    sys.path.append({internal!r})          # 真实 frozen 进程里 bootloader 会这么干

    import importlib.util as _m
    from pathlib import Path as _P

    class _FrozenFinder:
        \"\"\"模拟 PyInstaller 的冻结导入器：在 sys.meta_path 上直接提供 bundle 里的包。

        这就是"用 sys.path 模拟 bundle"会漏掉的那一环 —— 真实的冻结导入器**排在
        sys.path 之前**，所以只往 sys.path 插载荷是盖不住它的。
        \"\"\"
        def __init__(self, root):
            self.root = root
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "openminis":
                return None
            base = self.root / "openminis"
            if not base.is_dir():
                return None
            return _m.spec_from_file_location(
                fullname, base / "__init__.py",
                submodule_search_locations=[str(base)])

    sys.meta_path.insert(0, _FrozenFinder(_P({internal!r})))

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
    """有 payload/ 时，内核与界面都必须从载荷加载 —— 这是整个机制的核心。

    注意断言的是"**在冻结导入器也认领它**"的前提下仍然从载荷加载。
    """
    got = _probe(tmp_path, with_payload=True)
    assert "payload" in got["payload_root"]
    assert str(tmp_path) in got["origin"], got
    assert "/payload/" in got["origin"].replace("\\", "/"), got
    assert "/payload/" in got["ui"].replace("\\", "/"), got


def test_without_payload_it_falls_back_to_the_bundle(tmp_path):
    """没有 payload/ 时一切照旧：回落到 bundle 里的那一份（老安装包不会被弄坏）。"""
    got = _probe(tmp_path, with_payload=False)
    assert got["payload_root"] == "None"
    assert "/payload/" not in got["origin"].replace("\\", "/"), got
    assert "_internal" in got["origin"].replace("\\", "/"), got


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

    try:
        assert paths.install_payload_path() == payload
        assert sys.path[0] == str(payload)
        # 认领内核的那个查找器要占住 meta_path 最前面的一格
        assert isinstance(sys.meta_path[0], paths.PayloadFinder)
        assert sys.meta_path[0].root == payload
        # 再调一次不该重复插（启动路径 + attach 会调多次）
        assert paths.install_payload_path() == payload
        assert sys.path.count(str(payload)) == 1
        assert sum(isinstance(f, paths.PayloadFinder) for f in sys.meta_path) == 1
    finally:
        sys.path.remove(str(payload))
        for finder in [f for f in sys.meta_path if isinstance(f, paths.PayloadFinder)]:
            sys.meta_path.remove(finder)


def test_install_payload_path_is_a_noop_without_a_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(tmp_path / "x.exe"), raising=False)
    before = list(sys.path)
    assert paths.install_payload_path() is None
    assert sys.path == before, "没有载荷时不该动 sys.path"
    assert not any(isinstance(f, paths.PayloadFinder) for f in sys.meta_path)


def test_payload_finder_does_not_claim_the_shell(tmp_path):
    """查找器**不认领** ``desktop`` —— 壳永远来自 exe。"""
    finder = paths.PayloadFinder(tmp_path)
    (tmp_path / "openminis").mkdir()
    (tmp_path / "openminis" / "__init__.py").write_text("")
    (tmp_path / "desktop").mkdir()
    (tmp_path / "desktop" / "__init__.py").write_text("")

    assert finder.find_spec("desktop") is None
    assert finder.find_spec("desktop.app") is None
    assert finder.find_spec("openminis.server.main") is None  # 子模块交给常规机制
    assert finder.find_spec("openminis") is not None


def test_module_origin_reports_real_file():
    assert paths.module_origin("desktop.paths").endswith("paths.py")
    assert paths.module_origin("no.such.module.xyz") is None


def test_payload_manifest_matches_the_builder():
    """钉住 ``scripts/make_payload.py`` 的载荷清单：不含 ``desktop``，含内核与界面。"""
    from scripts.make_payload import PAYLOAD_TREES  # noqa: PLC0415

    sources = [src for src, _dst in PAYLOAD_TREES]
    assert "desktop" not in sources
    assert "src/openminis" in sources and "web/desktop" in sources
    # 与 paths.PAYLOAD_PACKAGES 必须说同一件事：载荷只负责内核这一个包
    assert paths.PAYLOAD_PACKAGES == ("openminis",)
