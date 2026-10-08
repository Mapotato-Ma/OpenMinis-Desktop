"""整包替换（换壳更新）的契约测试。

这里能测的是**决策与产物**：解出来的目录对不对、该拒绝的拒绝（包不对 / 空间不够）、
助教脚本里那几步有没有按顺序写进去、有没有出现"删安装目录"这种致命命令。
真正的替换动作（改名正在用的目录 / 退出后接管）只在 Windows 上跑得起来 ——
那部分靠真机验证，别在这里假装测过。
"""
from __future__ import annotations

import sys
import types
import zipfile
from pathlib import Path

import pytest

from desktop import updater


def _make_zip(tmp_path: Path, *, top_level: bool = True, exe: bool = True,
              payload: bool = True) -> Path:
    """造一个"便携包"形态的 zip（CI 用 Compress-Archive 打出来的就是这个形状）。"""
    archive = tmp_path / "OpenMinisDesktop-portable.zip"
    prefix = "OpenMinisDesktop/" if top_level else ""
    with zipfile.ZipFile(archive, "w") as zf:
        if exe:
            zf.writestr(prefix + "OpenMinisDesktop.exe", "MZ fake")
        if payload:
            zf.writestr(prefix + "payload/payload.json", '{"appVersion": "0.5.0"}')
        zf.writestr(prefix + "_internal/base_library.zip", "fake")
    return archive


@pytest.fixture()
def install_dir(tmp_path: Path) -> Path:
    d = tmp_path / "OpenMinisDesktop"
    d.mkdir()
    (d / "OpenMinisDesktop.exe").write_text("old exe", encoding="utf-8")
    return d


def test_install_root_is_none_when_not_frozen():
    """源码运行没有"安装目录"这个概念 —— 换掉 cwd 只会把仓库搞坏。"""
    assert updater.install_root() is None


def test_install_root_follows_the_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "OpenMinisDesktop" / "app.exe"))
    assert updater.install_root() == (tmp_path / "OpenMinisDesktop").resolve()


def test_installs_staging_and_schedules_swap(monkeypatch, tmp_path, install_dir):
    calls: list[dict] = []
    monkeypatch.setattr(
        updater, "schedule_swap",
        lambda **kw: (calls.append(kw), tmp_path / "helper.cmd")[1],
    )
    script = updater.install_full_package(_make_zip(tmp_path), install_dir=install_dir)

    assert script == tmp_path / "helper.cmd"
    assert len(calls) == 1
    kw = calls[0]
    assert kw["install_dir"] == install_dir
    # zip 里是顶层目录 OpenMinisDesktop/ —— 就位后它要变成**安装目录本身**
    assert kw["staged"].name == "OpenMinisDesktop"
    assert kw["staged"].parent.name.startswith("_openminis-update-")
    assert (kw["staged"] / "OpenMinisDesktop.exe").is_file(), "解出来的东西不对"
    assert (kw["staged"] / "payload" / "payload.json").is_file()
    # 暂存在**同一个父目录**：跨盘 move 会退化成慢拷贝
    assert kw["staged"].parent.parent == install_dir.parent


def test_accepts_a_zip_without_the_top_level_folder(monkeypatch, tmp_path, install_dir):
    """有人手工重打包（没套一层目录）也得能用 —— 按目录里的 exe 判断。"""
    monkeypatch.setattr(updater, "schedule_swap", lambda **kw: kw["staged"])
    staged = updater.install_full_package(
        _make_zip(tmp_path, top_level=False), install_dir=install_dir)
    assert staged.name.startswith("_openminis-update-")
    assert (staged / "OpenMinisDesktop.exe").is_file()


def test_refuses_a_zip_without_the_exe(monkeypatch, tmp_path, install_dir):
    monkeypatch.setattr(updater, "schedule_swap", lambda **kw: pytest.fail("不该走到安排助手"))
    with pytest.raises(updater.UpdateError, match="便携包"):
        updater.install_full_package(_make_zip(tmp_path, exe=False), install_dir=install_dir)
    assert not list(install_dir.parent.glob("_openminis-update-*")), "拒绝后要清掉暂存"


def test_refuses_a_zip_without_the_payload_manifest(monkeypatch, tmp_path, install_dir):
    monkeypatch.setattr(updater, "schedule_swap", lambda **kw: pytest.fail("不该走到安排助手"))
    with pytest.raises(updater.UpdateError, match="payload.json"):
        updater.install_full_package(_make_zip(tmp_path, payload=False), install_dir=install_dir)


def test_refuses_when_the_disk_is_too_small(monkeypatch, tmp_path, install_dir):
    """整包解开要 ~3 倍 zip 的空间 —— 宁可提前说清楚，也别解到一半失败。"""
    monkeypatch.setattr(updater.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=1024))
    monkeypatch.setattr(updater, "schedule_swap", lambda **kw: pytest.fail("空间不够还动手"))
    with pytest.raises(updater.UpdateError, match="腾点空间"):
        updater.install_full_package(_make_zip(tmp_path), install_dir=install_dir)


def test_cleans_up_leftovers_from_previous_attempts(monkeypatch, tmp_path, install_dir):
    old = install_dir.with_name(install_dir.name + ".old-20261008-120000")
    stale = install_dir.parent / "_openminis-update-999"
    for d in (old, stale):
        d.mkdir()
        (d / "junk.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(updater, "schedule_swap", lambda **kw: tmp_path / "helper.cmd")

    updater.install_full_package(_make_zip(tmp_path), install_dir=install_dir)

    assert not old.exists() and not stale.exists(), "上次的残留没收拾"
    assert install_dir.is_dir(), "收拾残留时不许碰到安装目录本身"


def _script() -> str:
    return updater.swap_script_text(
        pid=4321, install_dir=Path(r"F:\app\OpenMinisDesktop"),
        staged=Path(r"F:\app\_openminis-update-99\OpenMinisDesktop"),
        exe_name="OpenMinisDesktop.exe", stamp="20261008-213000",
    )


def test_swap_script_waits_for_the_old_process_first():
    text = _script()
    wait = text.index("PID eq 4321")
    move_old = text.index('move "%INSTALL%" "%OLD%"')
    assert wait < move_old, "必须先等旧进程退出，否则文件还锁着"
    assert "GEQ 90" in text and "放弃替换" in text, "等超时要**放弃**，不能硬来（半替换更糟）"


def test_swap_script_order_and_rollback():
    text = _script()
    i_old = text.index('move "%INSTALL%" "%OLD%"')
    i_new = text.index('move "%NEW%" "%INSTALL%"')
    i_rollback = text.index('move "%OLD%" "%INSTALL%"')
    i_start = text.index('start "" "%INSTALL%\\%EXE%"')
    assert i_old < i_new < i_start, "顺序必须是 让位 → 就位 → 启动"
    assert i_new < i_rollback, "回滚要在新目录就位失败之后"
    assert "openminis-swap.log" in text, "助手自删后就查不到 —— 必须留日志"


def test_swap_script_never_deletes_the_install_directory_itself():
    """最容易写错、后果最严重的一行：清理只能删 `.old-*`，绝不能删安装目录。"""
    text = _script()
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("rmdir") or stripped.startswith("del "):
            assert "%install%" not in stripped or "%old%" in stripped, (
                f"清理命令指向了安装目录本身：{line}")
    assert "del \"%~f0\"" in text, "助手脚本最后要自删"
