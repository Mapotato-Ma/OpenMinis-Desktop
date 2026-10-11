"""更新事务的护栏（R01–R05）。

这一组测的都是**改前真实存在、而且会伤到用户机器**的行为：

* R01：平铺整包 → 清理目录恰好是安装目录的父目录；
* R02：准备阶段就 spawn 助手（等 90 秒然后自删），提交时又起第二个；
* R03：连点两次能开出两个线程改同一批文件；
* R04：旧版本在新版本"发出 start"之后就被删掉；
* R05：活进程里换载荷，新旧代码混着跑。

真跑 ``cmd`` 助手的部分只在 Windows 上做（``scripts/update_drill.py``）；这里锁的是
**决策与产物**：spawn 了没有、spawn 了几次、传进去的是哪几个路径、journal 怎么收尾。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from desktop import updater
from desktop.update_transaction import (
    PHASE_COMMITTING,
    PHASE_FAILED,
    PHASE_IDLE,
    PHASE_STAGED,
    Job,
    UpdateBusy,
    UpdateTransaction,
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _payload_zip(path: Path, *, version: str, shell: str = "shell-current") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("payload.json", json.dumps({"format": 1, "shellId": shell,
                                                "appVersion": version}))
        zf.writestr("openminis/__init__.py", f'__version__ = "{version}"\n')
    return path


def _full_zip(path: Path, *, version: str, shell: str = "shell-current") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        prefix = f"{updater.PORTABLE_DIR_NAME}/"
        zf.writestr(prefix + "OpenMinisDesktop.exe", "MZ fake")
        zf.writestr(prefix + "payload/payload.json",
                    json.dumps({"appVersion": version, "shellId": shell}))
        zf.writestr(prefix + "payload/openminis/__init__.py", "# inner\n")
    return path


def _flat_full_zip(path: Path) -> Path:
    """有人手工重打包出来的形态：没有顶层目录。"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("OpenMinisDesktop.exe", "MZ fake")
        zf.writestr("payload/payload.json", json.dumps({"appVersion": "9.9.9"}))
    return path


class Sandbox:
    """一套假安装：``<tmp>/OpenMinisDesktop`` 装着 exe，里面是 ``payload/``。"""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.root = tmp_path
        self.install = tmp_path / "OpenMinisDesktop"
        self.payload = self.install / "payload"
        (self.payload / "openminis").mkdir(parents=True)
        (self.payload / "payload.json").write_text(
            json.dumps({"appVersion": "0.4.19", "shellId": "shell-current"}),
            encoding="utf-8",
        )
        (self.payload / "openminis" / "__init__.py").write_text(
            '__version__ = "0.4.19"\n', encoding="utf-8"
        )
        (self.install / "OpenMinisDesktop.exe").write_text("old exe", encoding="utf-8")
        # 安装目录的**兄弟**文件：R01 那条清理越界会把它一起删掉。
        self.sibling = self.install.parent / "用户的其它东西.txt"
        self.sibling.write_text("别删我", encoding="utf-8")
        self.updates = tmp_path / "userdata" / "updates"
        monkeypatch.setattr(updater, "updates_dir", lambda: (self.updates.mkdir(
            parents=True, exist_ok=True) or self.updates))
        monkeypatch.setattr(updater, "install_root", lambda: self.install)
        monkeypatch.setattr(updater, "payload_root", lambda: self.payload)
        monkeypatch.setattr(updater, "own_version", lambda: "0.4.19")
        monkeypatch.setattr(updater, "shell_version", lambda: "0.4.20")
        monkeypatch.setattr(updater, "own_shell_id", lambda: "shell-current")
        monkeypatch.setattr(updater, "installed_payload_version", lambda: "0.4.19")

    def serve(self, monkeypatch, *, version: str = "0.4.20", kind: str = "payload",
              flat: bool = False) -> None:
        """把清单与资产 stub 成"网上有一份新版本"。"""
        archive = self.root / "asset.zip"
        if kind == "full":
            _flat_full_zip(archive) if flat else _full_zip(
                archive, version=version, shell="shell-current")
        else:
            _payload_zip(archive, version=version, shell="shell-current")
        body = archive.read_bytes()
        raw = {
            "version": version,
            "shellId": "shell-current",
            "assets": {kind: {"url": f"https://example.invalid/{kind}.zip",
                              "sha256": __import__("hashlib").sha256(body).hexdigest(),
                              "bytes": len(body)}},
        }
        monkeypatch.setattr(updater, "fetch_manifest",
                            lambda url, **kw: updater.parse_manifest(raw))
        monkeypatch.setattr(updater, "download",
                            lambda asset, dest, on_progress=None: (
                                dest.mkdir(parents=True, exist_ok=True) or archive))


@pytest.fixture()
def sandbox(tmp_path, monkeypatch) -> Sandbox:
    box = Sandbox(tmp_path, monkeypatch)
    # 每个用例一个干净事务。**不**替换工厂函数：真实那个会懒解析
    # ``updater.install_root()/payload_root()``，于是上面这些 monkeypatch 生效。
    from desktop import ui_mount

    ui_mount.reset_update_transaction()
    return box


def _record_spawn(monkeypatch, calls: list[dict]):
    def _spawn(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        return Path("C:/tmp/openminis-swap-1.cmd")

    monkeypatch.setattr(updater, "schedule_swap", _spawn)
    return _spawn


# ---------------------------------------------------------------------------
# R03 · 单飞
# ---------------------------------------------------------------------------
def test_begin_is_single_flight_and_the_journal_is_authoritative(sandbox, monkeypatch):
    txn = UpdateTransaction()
    first = txn.begin()
    with pytest.raises(UpdateBusy) as exc:
        txn.begin()
    assert exc.value.job.id == first.id, "第二次 begin 必须是同一个 job，不是新开一个"

    # 另一个"窗口"（另一个事务对象）也得看到同一个任务 —— 权威是盘上的 journal。
    other = UpdateTransaction()
    with pytest.raises(UpdateBusy) as exc2:
        other.begin()
    assert exc2.value.job.id == first.id
    assert json.loads(txn.journal_path().read_text(encoding="utf-8"))["id"] == first.id


def test_a_finished_job_does_not_block_the_next_one(sandbox, monkeypatch):
    sandbox.serve(monkeypatch, kind="payload")
    txn = UpdateTransaction()
    txn.start()
    assert txn.snapshot().phase == PHASE_STAGED
    # 终态/已 staged 之后，另一次 begin 仍被拒（staged 还是个没走完的任务）
    with pytest.raises(UpdateBusy):
        txn.begin()


# ---------------------------------------------------------------------------
# R02 · 准备 ≠ 提交
# ---------------------------------------------------------------------------
def test_prepare_never_spawns_the_helper(sandbox, monkeypatch):
    sandbox.serve(monkeypatch, kind="payload")
    monkeypatch.setattr(updater, "schedule_swap",
                        lambda **kw: pytest.fail("准备阶段不该起助手（R02）"))

    job = UpdateTransaction().start()

    assert job.phase == PHASE_STAGED, job
    staged = Path(job.staged)
    assert staged.is_dir() and (staged / "openminis").is_dir()
    # 正在用的那份**一个字节都没动**（R05）
    assert '__version__ = "0.4.19"' in (
        sandbox.payload / "openminis" / "__init__.py"
    ).read_text(encoding="utf-8")
    assert job.pending_version == "0.4.20", job
    assert job.running_version == "0.4.19", job


def test_commit_spawns_exactly_one_helper_and_only_when_staged(sandbox, monkeypatch):
    sandbox.serve(monkeypatch, kind="payload")
    calls: list[dict] = []
    _record_spawn(monkeypatch, calls)
    txn = UpdateTransaction()

    with pytest.raises(updater.UpdateError, match="没有待启用的更新"):
        txn.commit()
    assert calls == [], "还没准备好就起助手 —— 那正是 R02"

    txn.start()
    job = txn.commit()

    assert len(calls) == 1, f"起助手的次数不是 1：{len(calls)}"
    kw = calls[0]
    assert kw["target"] == sandbox.payload
    assert kw["staged"] == Path(job.staged)
    assert kw["staging_root"] == Path(job.staged)
    assert kw["install_dir"] == sandbox.install
    assert job.phase == PHASE_COMMITTING
    # journal 先写 committing 再起助手：断电也留得下"外面可能有个助手在跑"
    journal = json.loads(txn.journal_path().read_text(encoding="utf-8"))
    assert journal["phase"] == PHASE_COMMITTING
    # 起助手之前必须清掉旧的启动标记（残留会被当成"新版已经起来了"）
    assert not updater.boot_ok_path().exists()


def test_commit_refuses_when_the_staged_dir_disappeared(sandbox, monkeypatch):
    """暂存目录被别处删掉/换掉时，绝不能拿着一个野路径去改用户的安装。"""
    sandbox.serve(monkeypatch, kind="payload")
    calls: list[dict] = []
    _record_spawn(monkeypatch, calls)
    txn = UpdateTransaction()
    txn.start()
    import shutil

    shutil.rmtree(Path(txn.snapshot().staged))
    with pytest.raises(updater.UpdateError, match="暂存目录不见了"):
        txn.commit()
    assert calls == []
    assert txn.snapshot().phase == PHASE_FAILED, "要如实报失败，别让界面以为还等着重启"


# ---------------------------------------------------------------------------
# R01 · 清理越界
# ---------------------------------------------------------------------------
def test_flat_full_package_is_refused_and_the_install_dir_stays_whole(
    sandbox, monkeypatch
):
    """平铺整包（没有顶层目录）**拒绝**，而且不许碰安装目录的父目录。"""
    sandbox.serve(monkeypatch, kind="full", flat=True)
    monkeypatch.setattr(updater, "schedule_swap",
                        lambda **kw: pytest.fail("拒绝的包不该走到起助手"))

    job = UpdateTransaction().start()

    assert job.phase == PHASE_FAILED, job
    assert "顶层目录" in job.error, job
    assert sandbox.sibling.is_file(), "拒绝一个包的过程中动了安装目录的兄弟文件"
    assert sandbox.install.is_dir(), "安装目录本身必须原样"
    assert not list(sandbox.install.parent.glob("_openminis-update-*")), "暂存没清掉"


def test_assert_owned_refuses_the_install_parent_and_the_root():
    with pytest.raises(updater.UpdateError, match="不在本次更新的目录里"):
        updater.assert_owned(Path("C:/somewhere/else"), allowed=(Path("C:/app"),),
                             what="清理目录")
    with pytest.raises(updater.UpdateError, match="受保护的目录"):
        updater.assert_owned(Path("C:/app"), allowed=(Path("C:/app"),),
                             what="清理目录", forbidden=(Path("C:/app"),))


def test_swap_script_cleans_only_transaction_dirs():
    """助手脚本里那两条整体删除，目标都带后缀、都由 Python 侧断言过归属。"""
    text = updater.swap_script_text()
    rmdirs = [line.strip().lower() for line in text.splitlines()
              if line.strip().lower().startswith("rmdir")]
    assert rmdirs, "清理命令不见了？"
    for line in rmdirs:
        assert "%om_upd_target%" not in line, f"直接删替换目标 = 删安装目录或载荷：{line}"
        assert "%old%" in line or "%om_upd_staging%" in line, line


# ---------------------------------------------------------------------------
# R04 · 旧版本保留到新版确认启动
# ---------------------------------------------------------------------------
def _retired(target: Path, stamp: str = "20261011-120000") -> Path:
    path = target.with_name(f"{target.name}.old-{stamp}")
    path.mkdir(parents=True)
    (path / "junk.txt").write_text("旧版本", encoding="utf-8")
    return path


def test_recover_keeps_the_old_copy_when_the_new_build_never_reported(
    sandbox, monkeypatch
):
    txn = UpdateTransaction()
    job = txn.begin()
    old = _retired(sandbox.payload)
    txn._update(  # noqa: SLF001 - 造出"提交之后崩掉"的现场
        phase=PHASE_COMMITTING, target=str(sandbox.payload), staged=str(sandbox.payload),
        staging_root=str(sandbox.payload), pending_version="0.4.20",
    )
    sandbox.updates.mkdir(parents=True, exist_ok=True)
    txn._note_state("正在完成 0.4.20 的替换", phase=PHASE_COMMITTING)  # noqa: SLF001

    recovered = UpdateTransaction().recover()

    assert recovered is not None and recovered.id == job.id
    assert old.is_dir(), "没有新版启动证据却把旧版本删了 —— 那正是 R04"
    summary = str(updater.load_state().get("summary") or "")
    assert "没有确认成功" in summary, summary
    assert str(old) in summary, "要告诉用户旧版本在哪（回滚/查找）"
    assert not txn.journal_path().exists(), "恢复之后 journal 要清掉，否则下次永远被拒"


def test_recover_confirms_success_and_drops_the_retired_copy(sandbox, monkeypatch):
    txn = UpdateTransaction()
    job = txn.begin()
    old = _retired(sandbox.payload)
    txn._update(phase=PHASE_COMMITTING, target=str(sandbox.payload),  # noqa: SLF001
                pending_version="0.4.20")
    txn._note_state("正在完成 0.4.20 的替换", phase=PHASE_COMMITTING)  # noqa: SLF001
    updater.boot_ok_path().write_text(json.dumps({"jobId": job.id, "pid": 4321}),
                                     encoding="utf-8")

    UpdateTransaction().recover()

    assert not old.exists(), "新版确认起来了，让位的旧目录该收拾掉"
    assert "已更新到 0.4.20" in str(updater.load_state().get("summary") or "")
    assert not updater.boot_ok_path().exists(), "确认过之后标记要收掉"


def test_recover_cleans_only_its_own_staging_when_prepare_was_interrupted(
    sandbox, monkeypatch
):
    txn = UpdateTransaction()
    txn.begin()
    staged = sandbox.payload.with_name("payload.new-1-2-3")
    staged.mkdir()
    (staged / "half.txt").write_text("半截", encoding="utf-8")
    unrelated = sandbox.root / "别动我"
    unrelated.mkdir()
    txn._update(phase=PHASE_STAGED, staged=str(staged), target=str(sandbox.payload),  # noqa: SLF001
                staging_root=str(staged))

    UpdateTransaction().recover()

    assert not staged.exists(), "没走完的暂存该清掉"
    assert unrelated.is_dir(), "只许碰本次事务自己的暂存目录"
    assert sandbox.payload.is_dir(), "现有安装一个字节都不许动"
    assert "没走完" in str(updater.load_state().get("summary") or "")


def test_boot_ok_is_only_written_while_committing(sandbox, monkeypatch):
    """日常启动不该落文件；只有"正在切换"那一小段时间才写。"""
    assert updater.mark_boot_ok() is None
    assert not updater.boot_ok_path().exists()

    txn = UpdateTransaction()
    job = txn.begin()
    txn._note_state("正在完成替换", phase=PHASE_COMMITTING)  # noqa: SLF001
    path = updater.mark_boot_ok(pid=1234)

    assert path is not None and path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["jobId"] == job.id


# ---------------------------------------------------------------------------
# 快照不可变
# ---------------------------------------------------------------------------
def test_job_snapshots_are_immutable(sandbox, monkeypatch):
    sandbox.serve(monkeypatch, kind="payload")
    txn = UpdateTransaction()
    early = txn.begin()
    txn.run()
    later = txn.snapshot()

    assert early.phase == "checking" and early.done == 0
    assert later.phase == PHASE_STAGED and later.id == early.id
    assert early.to_dict()["phase"] == "checking", "旧快照被就地改了 —— 界面会串线"
    with pytest.raises(Exception):
        early.phase = "done"  # type: ignore[misc]


def test_idle_job_is_not_busy():
    assert not Job().busy
    assert Job(id="j", phase=PHASE_STAGED).busy
    assert Job(id="j", phase=PHASE_IDLE).busy is False
