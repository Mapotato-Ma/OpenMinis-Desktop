"""更新事务：一次更新只有一个拥有者，且它只认自己的目录 [R01–R05]。

改前那套是"一串散在各调用点的字符串状态"：``ui_mount._update_state`` 是个全局
字典，apply 只拒绝 ``phase == "running"``（而流程里**从来没赋过** running），
下载名 / ``payload.new`` / ``payload.old`` / 按 PID 的暂存目录 / 助手脚本全部复用
—— 于是连点两次"下载并安装"、或者开两个窗口各点一次，就会有两个线程往同一批
文件上写、后一个还会把前一个的暂存目录清掉（R03）。

这里把它收成一个对象：**一个 job id + 一个状态机 + 一份不可变快照 + 一次单飞**。

    idle → checking → downloading → staged → committing → done
                                      │           │
                                      │           └── 进程退出，助手接力（见 updater）
                                      ├── manual（源码运行等"只能手动"的情形）
                                      └── failed

三条不变量（每条都有护栏测试盯着）：

1. **单飞**：``begin()`` 在已有未终结 job 时抛 :class:`UpdateBusy` 并附上快照，
   而不是"再开一个线程试试"。跨进程的权威是落盘的 ``updates/txn.json``。
2. **准备与提交分离**：``run()`` 只下载 / 解压 / 校验 / 写 journal，**绝不 spawn
   助手**；``commit()`` 才是"用户点了重启"的那一下，也是唯一启动助手的地方（R02）。
3. **破坏性动作只碰本次事务的目录**：每个删除 / 改名之前都过
   :func:`updater.assert_owned` —— R01 那个"把安装目录的父目录当清理目录"的形态
   从此不可能发生。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable

from . import updater

logger = logging.getLogger(__name__)

#: 状态机的全部相位。**字符串是字符串，但互斥不靠它** —— 靠 job 本身（见 `begin`）。
PHASE_IDLE = "idle"
PHASE_CHECKING = "checking"
PHASE_DOWNLOADING = "downloading"
PHASE_STAGED = "staged"
PHASE_COMMITTING = "committing"
PHASE_DONE = "done"
PHASE_MANUAL = "manual"
PHASE_FAILED = "failed"

#: 终态：到了这里就不会再自己动，下一次更新可以开始。
TERMINAL_PHASES = frozenset({PHASE_DONE, PHASE_MANUAL, PHASE_FAILED})

JOURNAL_FILE = "txn.json"


@dataclass(frozen=True)
class Job:
    """更新任务的一次**不可变**快照（界面/日志拿到的都是它）。"""

    id: str = ""
    phase: str = PHASE_IDLE
    kind: str = "none"          # none | payload | full
    version: str = ""
    running_version: str = ""
    pending_version: str = ""
    done: int = 0
    total: int = 0
    error: str = ""
    note: str = ""
    staged: str = ""
    target: str = ""
    staging_root: str = ""
    helper: str = ""
    install_dir: str = ""
    started_at: int = 0
    updated_at: int = 0

    @property
    def busy(self) -> bool:
        """还有一个没走完的 job 吗（`idle` 不算，终态不算）。"""
        return (
            bool(self.id)
            and self.phase not in TERMINAL_PHASES
            and self.phase != PHASE_IDLE
        )

    @property
    def awaiting_restart(self) -> bool:
        """准备好、只等用户点重启。"""
        return self.phase == PHASE_STAGED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> Job:
        if not isinstance(raw, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})


class UpdateBusy(RuntimeError):
    """已经有一个更新任务在跑 —— 带上它的快照，界面照着画就行（不再开第二个）。"""

    def __init__(self, job: Job) -> None:
        super().__init__("已经有一个更新任务在进行中")
        self.job = job


class UpdateTransaction:
    """一个进程里唯一的更新拥有者（``ui_mount`` 只造一个）。"""

    def __init__(
        self,
        *,
        install_dir: Path | None = None,
        payload_dir: Path | None = None,
        manifest_url: str | None = None,
        spawn_helper: Callable[..., Path] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._lock = threading.RLock()
        self._job = Job()
        self._install_dir = install_dir
        self._payload_dir = payload_dir
        self._manifest_url = manifest_url or updater.MANIFEST_URL
        self._spawn_helper = spawn_helper or updater.schedule_swap
        self._now = now

    # -- 只读 -------------------------------------------------------------
    def snapshot(self) -> Job:
        with self._lock:
            return self._job

    @property
    def install_dir(self) -> Path | None:
        """安装目录（改前 = 装着 exe 的那一层）。源码运行时是 None。"""
        return self._install_dir if self._install_dir is not None else updater.install_root()

    @property
    def payload_dir(self) -> Path | None:
        """可覆盖载荷目录（``payload/``）。懒解析：测试里换掉路径函数也生效。"""
        if self._payload_dir is not None:
            return self._payload_dir
        return updater.payload_root()

    def journal_path(self) -> Path:
        return updater.updates_dir() / JOURNAL_FILE

    def running_version(self) -> str:
        """**正在跑**的那个版本的载荷（不是"装了哪个"）。"""
        return updater.installed_payload_version() or updater.shell_version()

    # -- 单飞 -------------------------------------------------------------
    def begin(self) -> Job:
        """占住这次更新。已有未终结 job → 抛 :class:`UpdateBusy`（附快照）。

        跨进程的权威是 journal：另一个窗口的进程点了同一个按钮时，它看到的
        ``txn.json`` 里已经有一个在跑的 job，于是也走同一个"拒绝 + 回报"。
        """
        with self._lock:
            self._job = self._reconcile_from_journal(self._job)
            if self._job.busy:
                raise UpdateBusy(self._job)
            job = Job(
                id=self._new_job_id(),
                phase=PHASE_CHECKING,
                running_version=self.running_version(),
                install_dir=str(self.install_dir or ""),
                started_at=int(self._now() * 1000),
                updated_at=int(self._now() * 1000),
            )
            self._job = job
            self._write_journal(job)
            return job

    def start(self) -> Job:
        """``begin()`` + ``run()``：同步跑完一次更新（测试与简单调用方用）。"""
        self.begin()
        self.run()
        return self.snapshot()

    # -- 准备（慢活；跑在后台线程里）--------------------------------------
    def run(self) -> None:
        try:
            self._prepare()
        except updater.UpdateError as exc:
            self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - 任何意外都要让界面看得见
            logger.exception("update failed")
            self._fail(f"{type(exc).__name__}: {exc}")

    def _prepare(self) -> None:
        result = updater.plan(
            current_version=updater.own_version(),
            current_shell=updater.own_shell_id(),
            url=self._manifest_url,
        )
        if result.kind == "none" or result.asset is None:
            self._update(phase=PHASE_DONE, note=result.reason, kind="none")
            self._note_state("检查过了：已是最新")
            return

        if result.kind == "full":
            if self.install_dir is None:
                self._update(phase=PHASE_MANUAL, kind="full",
                             note="这是源码运行、不是安装版，整包只能手动换")
                self._note_state("这次更新换壳了，但当前不是安装版 —— 需要手动换整包")
                return
            install_dir = self.install_dir
            payload_dir = self.payload_dir
        else:
            if self.payload_dir is None:
                self._update(phase=PHASE_MANUAL, kind="payload",
                             note="这个安装没有载荷目录，需要整包装")
                self._note_state("这次更新没有可用的载荷目录 —— 需要手动换整包")
                return
            install_dir = self.install_dir or self.payload_dir.parent
            payload_dir = self.payload_dir

        self._update(
            phase=PHASE_DOWNLOADING, kind=result.kind, version=result.info.version,
            total=result.asset.bytes, done=0, note="正在下载",
        )
        archive = updater.download(
            result.asset,
            self._download_dir(),
            on_progress=self._on_progress(result.asset.bytes),
        )
        self._update(note="正在解压校验", done=result.asset.bytes)

        job_id = self.snapshot().id
        if result.kind == "full":
            staged = updater.stage_full_package(
                archive, install_dir=install_dir, job_id=job_id
            )
            staging_root = updater.staging_root_for(install_dir, job_id)
            target = install_dir
            pending = result.info.version
        else:
            assert payload_dir is not None  # 上面已经拦过 None
            staged = updater.stage_payload(
                archive, payload_dir,
                expect_shell=result.info.shell_id or updater.own_shell_id(),
                job_id=job_id,
            )
            staging_root = staged
            target = payload_dir
            pending = updater.staged_payload_version(staged) or result.info.version

        # 破坏性动作之前的最后一关：这三个路径必须都属于本次事务（R01）。
        allowed = (install_dir, target, staged, staging_root)
        updater.assert_owned(staged, allowed=allowed, what="暂存目录",
                             forbidden=self._forbidden())
        updater.assert_owned(staging_root, allowed=allowed, what="事务暂存根",
                             forbidden=self._forbidden())
        updater.assert_owned(target, allowed=allowed, what="替换目标",
                             forbidden=self._forbidden())

        updater.cleanup_old_downloads(keep=archive)
        updater.cleanup_old_helpers()
        self._update(
            phase=PHASE_STAGED, staged=str(staged), target=str(target),
            staging_root=str(staging_root), pending_version=pending,
            install_dir=str(install_dir), note="",
        )
        self._note_state(
            f"{pending or result.info.version} 已准备好，点「重启并完成替换」生效",
            phase=PHASE_STAGED,
        )

    # -- 提交（用户点重启的那一下）----------------------------------------
    def commit(self) -> Job:
        """启动**唯一**助手，然后把宿主交给它。只有 ``staged`` 允许调用。"""
        with self._lock:
            job = self._job
            if job.phase != PHASE_STAGED or not job.staged:
                raise updater.UpdateError("现在没有待启用的更新")
            target = Path(job.target)
            staged = Path(job.staged)
            staging_root = Path(job.staging_root or job.staged)
            install_dir = self.install_dir or (
                target.parent if job.kind == "payload" else None
            )
            if install_dir is None:
                raise updater.UpdateError("找不到安装目录，无法完成替换")
            if not staged.is_dir():
                # 暂存目录消失了（用户清理了磁盘 / 上一次事务的 recover 清过）。
                # 绝不能拿着一个不存在的路径去改安装 —— 助手会把目标改名之后
                # 拿不到新的那份，用户就剩一个坏安装。
                self._fail("暂存目录不见了，这次更新请重新下载安装")
                raise updater.UpdateError("暂存目录不见了，请重新下载再装一次")

            allowed = (Path(install_dir), target, staged, staging_root)
            for path, what in ((staged, "暂存目录"), (staging_root, "事务暂存根"),
                               (target, "替换目标")):
                updater.assert_owned(path, allowed=allowed, what=what,
                                     forbidden=self._forbidden())

            # 旧标记留着的话，助手会以为"新版已经起来了"（它只看文件在不在）。
            updater.clear_boot_ok()
            self._update(phase=PHASE_COMMITTING, note="正在退出并完成替换")
            # journal 先写 committing、再 spawn：万一这时断电，下次启动 recover()
            # 至少知道"有个助手可能在外面跑"，而不是当成"没走完"把暂存删掉。
            self._note_state(
                f"正在完成 {job.pending_version or job.version} 的替换",
                phase=PHASE_COMMITTING,
            )
            helper = self._spawn_helper(
                target=target,
                staged=staged,
                staging_root=staging_root,
                install_dir=Path(install_dir),
            )
            self._update(helper=str(helper))
            return self._job

    # -- 启动恢复 ---------------------------------------------------------
    def recover(self) -> Job | None:
        """启动时给上一次事务收尾。返回那个 job（没有 journal 就 None）。

        三种遗留：

        * ``committing`` + 新版留下了启动标记 → **成了**：可以删让位目录；
        * ``committing`` + 没有标记 → **没确认**：旧版本原样保留，如实写进界面能看到
          的那句话里，并说明它在哪（R04）；
        * 其它相位（checking/downloading/staged）→ 上次没走完就退出了：清掉**本次
          事务自己的**暂存，现有安装一个字节都没动过。
        """
        job = Job.from_dict(self._read_journal())
        if not job.id:
            return None
        if job.phase == PHASE_COMMITTING:
            self._recover_commit(job)
        else:
            for raw in (job.staged, job.staging_root):
                path = Path(raw) if raw else None
                if path is not None and self._leftover_ok(path):
                    shutil.rmtree(path, ignore_errors=True)
            self._note_state("上次更新没走完就退出了 —— 已清理暂存，现有安装没有改动")
        self._clear_journal()
        with self._lock:
            self._job = Job(phase=PHASE_IDLE, running_version=self.running_version())
            return job

    def _recover_commit(self, job: Job) -> None:
        marker = updater.read_boot_ok()
        confirmed = bool(job.id) and str(marker.get("jobId") or "") == job.id
        target = Path(job.target) if job.target else None
        if confirmed and target is not None:
            for path in updater.retired_dirs(target):
                shutil.rmtree(path, ignore_errors=True)
            updater.clear_boot_ok()
            self._note_state(
                f"已更新到 {job.pending_version or job.version}（新版本已启动）"
            )
            return
        where = ""
        if target is not None:
            retired = updater.retired_dirs(target)
            where = str(retired[0]) if retired else ""
        self._note_state(
            "上次更新没有确认成功：新版本没报告启动成功"
            + (f"，旧版本仍保留在 {where}" if where else "")
        )

    # -- 内部 -------------------------------------------------------------
    def _new_job_id(self) -> str:
        with self._lock:
            seq = getattr(self, "_seq", 0) + 1
            self._seq = seq
        return updater.safe_job_id(f"{os.getpid()}-{int(self._now() * 1000)}-{seq}")

    def _download_dir(self) -> Path:
        return updater.updates_dir() / self.snapshot().id

    def _on_progress(self, total: int) -> Callable[[int, int], None]:
        def report(done: int, seen_total: int) -> None:
            self._update(done=done, total=seen_total or total)

        return report

    def _forbidden(self) -> tuple[Path, ...]:
        """"再往上一步就是用户别的东西"的目录 —— 换成它自己就得拒绝。"""
        out: list[Path] = []
        install_dir = self.install_dir
        payload_dir = self.payload_dir
        if install_dir is not None:
            out.append(install_dir.parent)
        if payload_dir is not None:
            out.append(payload_dir.parent)
        return tuple(out)

    def _leftover_ok(self, path: Path) -> bool:
        """这看起来像**我们自己这次事务**留下的目录吗（删之前的形状检查）？"""
        name = path.name
        install_dir = self.install_dir
        payload_dir = self.payload_dir
        if install_dir is not None:
            if name.startswith("_openminis-update-") and path.parent == install_dir.parent:
                return True
            if name.startswith(f"{install_dir.name}.new-") and path.parent == install_dir:
                return True
        if payload_dir is not None:
            if name.startswith(f"{payload_dir.name}.new-") and path.parent == payload_dir.parent:
                return True
        return False

    def _update(self, **fields: Any) -> Job:
        """在锁里改相位，并把新快照落 journal。"""
        with self._lock:
            data = self._job.to_dict()
            data.update(fields)
            data["updated_at"] = int(self._now() * 1000)
            self._job = Job.from_dict(data)
            if self._job.phase != PHASE_IDLE:
                self._write_journal(self._job)
            return self._job

    def _fail(self, message: str) -> None:
        self._update(phase=PHASE_FAILED, error=message, note="")
        self._note_state(f"上次更新失败：{message}")
        logger.warning("update failed: %s", message)

    def _write_journal(self, job: Job) -> None:
        try:
            self.journal_path().write_text(
                json.dumps(job.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:  # pragma: no cover - 写不进去只影响跨进程单飞
            logger.debug("update journal write failed", exc_info=True)

    def _read_journal(self) -> dict[str, Any]:
        try:
            data = json.loads(self.journal_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _clear_journal(self) -> None:
        try:
            self.journal_path().unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            logger.debug("update journal clear failed", exc_info=True)

    def _note_state(self, summary: str, *, phase: str = "") -> None:
        """把"人话摘要"落盘。带上 jobId 与（提交中的）相位：

        * ``mark_boot_ok`` 靠 jobId 把"这次启动"对上一次切换；
        * ``phase=committing`` 是"只有这一小段时间才写启动标记"的判据 ——
          平时不产生任何文件。
        """
        state: dict[str, Any] = {
            "summary": summary,
            "jobId": self.snapshot().id,
            "version": self.snapshot().pending_version or self.snapshot().version,
            "at": int(self._now() * 1000),
        }
        if phase:
            state["phase"] = phase
        updater.save_state(state)

    def _reconcile_from_journal(self, current: Job) -> Job:
        """本进程没在跑，但盘上还有一个没终结的 job → 认它（另一个窗口开的）。"""
        if current.busy:
            return current
        other = Job.from_dict(self._read_journal())
        if other.busy:
            return other
        return current
