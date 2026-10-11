"""整包替换（换壳更新）的契约测试。

这里能测的是**决策与产物**：解出来的目录对不对、该拒绝的拒绝（包不对 / 平铺包 /
空间不够）、助手脚本里那几步有没有按顺序写进去、有没有出现"删安装目录"这种致命命令。
真正的替换动作（改名正在用的目录 / 退出后接管）只在 Windows 上跑得起来 ——
那部分靠 ``scripts/update_drill.py`` 真机演练，别在这里假装测过。
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


def test_stages_the_package_without_touching_anything(monkeypatch, tmp_path, install_dir):
    """准备阶段：只解到旁边、校验，**不安排助手、不动安装目录**（R02/R05）。

    改前这里是一边解压一边就把助手叫起来了：助手开始等当前进程退出（上限 90 秒），
    而界面还在让用户慢慢决定 —— 等超了它自删，界面却还说"重启即完成替换"。
    """
    monkeypatch.setattr(updater, "schedule_swap",
                        lambda **kw: pytest.fail("准备阶段不该起助手"))
    staged = updater.stage_full_package(
        _make_zip(tmp_path), install_dir=install_dir, job_id="42-1"
    )

    # zip 里是顶层目录 OpenMinisDesktop/ —— 就位后它要变成**安装目录本身**
    assert staged.name == "OpenMinisDesktop"
    assert staged.parent.name == "_openminis-update-42-1", "暂存目录要按 job 命名"
    assert (staged / "OpenMinisDesktop.exe").is_file(), "解出来的东西不对"
    assert (staged / "payload" / "payload.json").is_file()
    # 暂存在**同一个父目录**：跨盘 move 会退化成慢拷贝
    assert staged.parent.parent == install_dir.parent
    # 安装目录一个字节都没动
    assert (install_dir / "OpenMinisDesktop.exe").read_text(encoding="utf-8") == "old exe"
    assert not install_dir.with_name("OpenMinisDesktop.old-*").exists()


def test_refuses_a_zip_without_the_top_level_folder(monkeypatch, tmp_path, install_dir):
    """平铺整包（没有顶层目录）**拒绝** —— R01 那条清理越界就是从这个形态来的。

    改前：这种包被接受，``staged`` 恰好等于暂存根，而调度助手时拿 ``staged.parent``
    当"清理目录" → 那条分支下它正是**安装目录的父目录**，助手对着一整层目录
    ``rmdir /s /q``。猜错一层目录的代价太大，所以现在不猜：直接拒绝。
    """
    monkeypatch.setattr(updater, "schedule_swap", lambda **kw: pytest.fail("不该走到起助手"))
    sibling = install_dir.parent / "用户的其它东西.txt"
    sibling.write_text("别删我", encoding="utf-8")

    with pytest.raises(updater.UpdateError, match="顶层目录"):
        updater.stage_full_package(
            _make_zip(tmp_path, top_level=False), install_dir=install_dir, job_id="1"
        )
    assert sibling.is_file(), "拒绝一个包的过程中不能碰安装目录的兄弟文件"
    assert install_dir.is_dir()
    assert not list(install_dir.parent.glob("_openminis-update-*")), "拒绝后要清掉暂存"


def test_refuses_a_zip_without_the_exe(monkeypatch, tmp_path, install_dir):
    monkeypatch.setattr(updater, "schedule_swap", lambda **kw: pytest.fail("不该走到安排助手"))
    with pytest.raises(updater.UpdateError, match="便携包"):
        updater.stage_full_package(
            _make_zip(tmp_path, exe=False), install_dir=install_dir, job_id="1"
        )
    assert not list(install_dir.parent.glob("_openminis-update-*")), "拒绝后要清掉暂存"


def test_refuses_a_zip_without_the_payload_manifest(monkeypatch, tmp_path, install_dir):
    monkeypatch.setattr(updater, "schedule_swap", lambda **kw: pytest.fail("不该走到安排助手"))
    with pytest.raises(updater.UpdateError, match="payload.json"):
        updater.stage_full_package(
            _make_zip(tmp_path, payload=False), install_dir=install_dir, job_id="1"
        )


def test_refuses_when_the_disk_is_too_small(monkeypatch, tmp_path, install_dir):
    """整包解开要 ~3 倍 zip 的空间 —— 宁可提前说清楚，也别解到一半失败。"""
    monkeypatch.setattr(updater.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=1024))
    monkeypatch.setattr(updater, "schedule_swap", lambda **kw: pytest.fail("空间不够还动手"))
    with pytest.raises(updater.UpdateError, match="腾点空间"):
        updater.stage_full_package(_make_zip(tmp_path), install_dir=install_dir, job_id="1")


def test_cleans_up_leftovers_from_previous_attempts(monkeypatch, tmp_path, install_dir):
    old = install_dir.with_name(install_dir.name + ".old-20261008-120000")
    stale = install_dir.parent / "_openminis-update-999"
    for d in (old, stale):
        d.mkdir()
        (d / "junk.txt").write_text("x", encoding="utf-8")

    staged = updater.stage_full_package(_make_zip(tmp_path), install_dir=install_dir,
                                       job_id="7")

    assert not old.exists() and not stale.exists(), "上次的残留没收拾"
    assert install_dir.is_dir(), "收拾残留时不许碰到安装目录本身"
    assert staged.is_dir(), "本次事务自己的暂存不能被自己清掉"


def _script() -> str:
    return updater.swap_script_text()


def test_swap_script_is_pure_ascii_and_takes_paths_from_the_environment():
    """真机演练（2026-10-08）：把中文安装路径写进 ANSI 的 .cmd，`move` 连试 15 次
    全失败（cmd 按控制台码页解释文件字节）；改成环境变量传参后一次成功。
    这条钉住的就是那个性质：脚本本身不许出现非 ASCII，路径只许以 %OM_UPD_*% 出现。
    """
    text = _script()
    assert all(ord(ch) < 128 for ch in text), "脚本混进了非 ASCII"
    assert chr(92) * 2 not in text, "多了个反斜杠会让路径变成 C:\\a\\\\b"
    assert "OM_UPD_TARGET" in text and "OM_UPD_NEW" in text, "路径没走环境变量"
    assert updater.helper_env(**{updater._ENV_TARGET: r"F:\桌面\07-代码\app"})[
        updater._ENV_TARGET] == r"F:\桌面\07-代码\app"


def test_swap_script_waits_for_the_old_process_first():
    text = _script()
    wait = text.index('PID eq %OM_UPD_PID%')
    move_old = text.index('move "%TARGET%" "%OLD%"')
    assert wait < move_old, "必须先等旧进程退出，否则文件还锁着"
    assert "GEQ %OM_UPD_WAITMAX%" in text and "giving up" in text, (
        "等超时要**放弃**，不能硬来（半替换更糟）")


def test_swap_script_order_and_rollback():
    text = _script()
    i_old = text.index('move "%TARGET%" "%OLD%"')
    i_new = text.index('move "%NEW%" "%TARGET%"')
    i_rollback = text.index('move "%OLD%" "%TARGET%"')
    i_start = text.index('start "" "%APP%\\%EXE%"')
    assert i_old < i_new < i_start, "顺序必须是 让位 → 就位 → 启动"
    assert i_new < i_rollback, "回滚要在新目录就位失败之后"
    assert "openminis-swap.log" in text, "助手自删后就查不到 —— 必须留日志"


def test_swap_script_never_deletes_the_install_directory_itself():
    """最容易写错、后果最严重的一行：清理只能删 `.old-*` 与事务暂存根。"""
    text = _script()
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("rmdir") or stripped.startswith("del "):
            assert "%old%" in stripped or "%om_upd_staging%" in stripped, (
                f"清理命令指向了别的东西：{line}")
            assert "%om_upd_target%" not in stripped, (
                f"清理命令指向了要替换的目标（安装目录或载荷）：{line}")
    # 脚本**不自删**：`del "%~f0"` 会让 cmd 读不到下一行、打印
    # "The batch file cannot be found." 并以退出码 1 结束（自删之后的命令全不执行，
    # 实测）。临时脚本改由 updater.cleanup_old_helpers() 在下次更新时清。
    assert 'del "%~f0"' not in text
    assert text.rstrip().endswith("exit /b 0"), "最后要有一句干净的 exit（退出码 0）"


def test_old_helper_scripts_get_cleaned_by_the_app(monkeypatch, tmp_path):
    """临时脚本与日志由应用清（代替那段会破坏退出码的自删）。"""
    import os
    import time as _time

    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
    fresh = tmp_path / "openminis-swap-123.cmd"
    stale = tmp_path / "openminis-swap-456.cmd"
    log = tmp_path / "openminis-swap.log"
    for path in (fresh, stale, log):
        path.write_text("x", encoding="utf-8")
    old = _time.time() - 90000
    os.utime(stale, (old, old))
    os.utime(log, (old, old))

    updater.cleanup_old_helpers()

    assert fresh.is_file(), "刚写下的那份会被正在跑的助手用着，不能删"
    assert not stale.exists() and not log.exists()


def test_swap_script_waits_for_the_new_build_before_dropping_the_old_one():
    """R04：发过 start 不等于新版能用 —— 删旧版必须等新版的启动标记。"""
    text = _script()
    i_start = text.index('start "" "%APP%\\%EXE%"')
    i_await = text.index(":awaitok")
    i_clean = text.index('rmdir /s /q "%OLD%"')
    assert i_start < i_await < i_clean, "顺序必须是 启动 → 等启动标记 → 才删旧版"
    assert "%OM_UPD_BOOTOK%" in text and "GEQ %OM_UPD_BOOTMAX%" in text, (
        "没有等启动标记，或者没有等待上限")
    assert "keeping the old copy" in text, "等不到时要**保留**旧版（否则用户没得回滚）"


def test_swap_script_does_not_use_timeout_for_sleeping():
    """**不能**用 ``timeout /t`` 当 sleep：助手是 stdin=DEVNULL 起来的。

    ``timeout`` 在 stdin 被重定向时会立刻报 "Input redirection is not supported"
    并返回 —— 于是等旧进程退出那个 90 次的循环会在毫秒级空转完，"等超时放弃"
    变成"立刻放弃"，助手什么都没干就自删了（现象就是"点了重启，窗口没了"）。
    ``ping -n N 127.0.0.1`` 不依赖控制台，是这里唯一可靠的 sleep。
    """
    text = _script()
    # 只看**命令**行：注释里提到 timeout 是在解释为什么不能用它。
    commands = [line.strip().lower() for line in text.splitlines()
                if line.strip() and not line.strip().lower().startswith("rem")]
    assert not [c for c in commands if c.startswith("timeout")], (
        "timeout 在无控制台的助手进程里等不了 —— 循环会瞬间空转完"
    )
    assert text.count("ping -n") >= 3, "等待点不止一处，别漏"


def test_helper_scripts_go_out_in_the_ansi_codepage_on_windows(monkeypatch):
    """cmd.exe 按 ANSI 读 .cmd。

    助手脚本里带**安装路径**，而用户完全可能装在 `F:\\桌面\\07-代码\\...` 下 ——
    写成 UTF-8 会被读成乱码路径，`move` / `start` 全部指空。失败还很隐蔽：
    脚本"跑完"了，只是什么都没动。
    """
    monkeypatch.setattr(updater.sys, "platform", "win32")
    assert updater.script_encoding() == "mbcs"


def test_helper_scripts_use_utf8_off_windows(monkeypatch):
    """非 Windows 上没有 cmd，助手脚本用 utf-8 就够。

    **必须显式固定平台**：写成裸断言的话，这条测试在 windows-latest 上必红
    （那边 script_encoding() 返回 mbcs）—— CI 当场抓到过一次。
    """
    monkeypatch.setattr(updater.sys, "platform", "linux")
    assert updater.script_encoding() == "utf-8"


def test_write_helper_script_falls_back_when_the_codepage_cannot_encode(monkeypatch, tmp_path):
    """码页编不出来的字（万一）也不能把脚本写坏：退回 utf-8 并留下完整内容。"""
    monkeypatch.setattr(updater, "script_encoding", lambda: "ascii")
    target = tmp_path / "helper.cmd"
    updater.write_helper_script(target, "echo 中文路径 F:\\桌面")
    assert "中文路径 F:\\桌面" in target.read_text(encoding="utf-8")


def test_helper_script_is_written_with_crlf(tmp_path):
    """cmd.exe 解析 .cmd 要 CRLF —— 纯 LF 的文件整个跑不起来。

    真机演练（2026-10-08）：LF 的脚本 `cmd /c` 返回 1、日志一个字都没写，
    看着像"脚本没跑"，其实是行尾。这条把它钉死。
    """
    target = tmp_path / "helper.cmd"
    updater.write_helper_script(target, "line1\nline2\n")
    raw = target.read_bytes()
    assert b"line1\r\nline2\r\n" in raw, f"没有写成 CRLF：{raw[:40]!r}"
    assert b"\n\n" not in raw
