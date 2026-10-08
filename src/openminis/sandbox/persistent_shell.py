"""Long-lived shell process — execute commands and detect completion via marker.

Ported from: src/android/app/src/main/java/com/openminis/app/sandbox/PersistentShell.kt
Original package: com.openminis.app.sandbox

The Android original spawns one ``proot``-wrapped Alpine shell per session and
keeps it alive across turns, so ``cd``/``export``/installed packages persist.
The Python port keeps that shape: one child process per session, commands fed
through stdin, completion detected by a unique marker echoed after each command
(``__MINIS_DONE_<marker>_EXIT_<code>__``).

# PORT: platform differences vs. Android
- ``Process``/``BufferedWriter`` -> ``asyncio.create_subprocess_exec`` with
  ``stdin=PIPE`` (write via ``drain()``). No JNI/proot layer here: the child is
  a plain POSIX shell (or ``cmd.exe`` on Windows) rooted at the session
  workspace, which is the closest cross-platform equivalent of the sandbox.
- ``suspendCancellableCoroutine`` -> ``asyncio.Future`` resolved by the reader
  task; ``withTimeoutOrNull`` -> ``asyncio.wait_for`` (timeout yields exit 124,
  matching coreutils' timeout convention, and leaves the shell alive).
- stderr is merged into stdout (2>&1) exactly as the release Android build does,
  so ``death_tail()`` captures proot's last words the same way.
- ``@Volatile`` fields are plain attributes guarded by the event loop's
  single-threaded execution model.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from openminis.core.context import app_context
from openminis.core.logging import get_logger

__all__ = ["PersistentShell", "ShellSpec", "detect_shell_spec", "kill_process_tree", "new_group_kwargs"]

logger = get_logger(__name__)

#: Windows ``CREATE_NEW_PROCESS_GROUP`` —— 让 shell 独占一个进程组，
#: ``taskkill /T`` 才有东西可连坐。
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def new_group_kwargs() -> dict[str, Any]:
    """spawn 参数：把 shell 放进**独立进程组**，中断时才能连子孙一起杀。

    POSIX 上如果不开新会话，``os.getpgid(pid)`` 就是我们自己的组 ——
    ``killpg`` 会把自己也杀掉（这不是理论风险，是最容易写出来的 bug）。
    """
    if os.name == "nt":
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(pid: int, proc: Any = None) -> str:
    """同步杀掉 ``pid`` 的整棵进程树，返回一句人话（给日志/工具结果用）。

    **为什么必须连子孙一起杀**：真正在干活的是 shell 底下的 curl / python /
    node。``proc.kill()`` 只杀 shell 本身，它们会变成孤儿继续跑 —— 用户按了
    「停止」，命令却还在后台烧 CPU、还在往外发请求。这是「停止停不下来」的
    另一半根因（2026-10-08 用户实测：按停止后工具仍在继续执行）。

    **为什么不发 Ctrl-C**：这个 shell 的 stdin 是**管道**不是 tty，往里写
    ``\\x03`` 构不成任何信号；Windows 上 ``terminate()`` 也只杀直接子进程。
    """
    if os.name == "nt":
        try:
            import subprocess as _sp

            _sp.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
            return f"已终止进程树 (pid={pid})"
        except Exception as exc:  # pragma: no cover - 杀不掉就退回单进程
            logger.debug("taskkill failed: %s", exc)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return f"已终止进程组 (pid={pid})"
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug("killpg failed (%s), falling back to proc.kill", exc)
    if proc is not None:
        try:
            proc.kill()
            return f"已终止进程 (pid={pid})"
        except Exception as exc:  # pragma: no cover
            logger.debug("proc.kill failed: %s", exc)
    return f"无法终止进程 (pid={pid})"

# [T-android-shell-death-diagnosability] Death-capture windows. The head must
# comfortably hold proot's error line(s) printed BEFORE the multi-KB talloc leak
# dump; the tail shows how the output ended.
OUTPUT_HEAD_MAX = 1024
OUTPUT_TAIL_MAX = 2048

# Default command timeout: 600 s (Kotlin default ``timeout: Long = 600_000L``).
DEFAULT_COMMAND_TIMEOUT = 600.0

_MARKER_TEMPLATE = "__MINIS_DONE_{marker}_EXIT_{code}__"

# Any marker line, regardless of which command owns it. Used to drop markers
# belonging to commands that are no longer pending (timed out / cancelled) so
# their late arrival cannot pollute the NEXT command's output — the marker is
# protocol chatter, never user-visible output.
_STRAY_MARKER_RE = re.compile(r"__MINIS_DONE_[0-9a-f]{8}_EXIT_(\d+)__")


# ---------------------------------------------------------------------------
# ShellSpec — per-platform shell selection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShellSpec:
    """How to spawn and instrument a shell on this platform.

    # PORT: Android always gets ash (Alpine) under proot. Python has to pick a
    # shell per platform, and the marker-echo syntax differs between POSIX
    # (``$?``) and Windows ``cmd`` (``%ERRORLEVEL%``). Executable discovery is
    # POSIX-first so a Windows box with Git Bash still gets a bash-compatible
    # shell — the agent's generated commands assume POSIX syntax.
    """

    executable: str
    args: tuple[str, ...]
    marker_echo_template: str
    name: str

    def wrap(self, command: str, marker: str) -> str:
        """Return the command + marker echo, ready to be written to stdin."""
        echo = self.marker_echo_template.format(marker=marker)
        return f"{command}\n{echo}\n"

    def marker_pattern(self, marker: str) -> re.Pattern[str]:
        escaped = re.escape(marker)
        return re.compile(rf"__MINIS_DONE_{escaped}_EXIT_(\d+)__")


def detect_shell_spec() -> ShellSpec:
    """Pick the best available shell for this host.

    POSIX bash/sh is strongly preferred — the model emits POSIX commands
    (``ls -la``, ``pip install``, ``grep``). Falling back to ``cmd.exe`` on
    Windows keeps the port runnable but most agent commands will need
    translation; the coordinator reports which shell is active so callers
    can surface it.
    """
    if sys.platform != "win32":
        for exe in ("/bin/bash", "/bin/sh"):
            if Path(exe).exists():
                return ShellSpec(exe, ("--noprofile", "--norc", "-s"),
                                 'echo "__MINIS_DONE_{marker}_EXIT_$?__"', exe)
        return ShellSpec("/bin/sh", ("-s",),
                         'echo "__MINIS_DONE_{marker}_EXIT_$?__"', "/bin/sh")

    # Windows: prefer any bash we can find (Git Bash, MSYS2, WSL-adjacent).
    # 注意 ``C:\Windows\System32\bash.exe`` 是 WSL 的占位程序 —— 没装 WSL 时
    # 它只会打印「请到 Microsoft Store 安装…」然后退出（exit -1），整个持久
    # shell 就此报废。必须排除 Windows 目录下的 bash，宁可用 cmd.exe。
    import glob

    windir = os.environ.get("WINDIR", r"C:\Windows").lower()
    home = os.environ.get("USERPROFILE", "")
    candidates = [
        shutil.which("bash"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        # WorkBuddy 自带的 PortableGit（应用环境里最常见的真 bash）
        *glob.glob(os.path.join(
            home, ".workbuddy", "binaries", "PortableGit",
            "versions", "*", "usr", "bin", "bash.exe")),
        *glob.glob(os.path.join(
            home, "scoop", "apps", "git", "current", "bin", "bash.exe")),
    ]
    for cand in candidates:
        if (
            cand
            and Path(cand).exists()
            and windir not in str(Path(cand).resolve()).lower()
        ):
            return ShellSpec(cand, ("--noprofile", "--norc", "-s"),
                             'echo "__MINIS_DONE_{marker}_EXIT_$?__"', "bash")

    # Last resort: cmd.exe. ``%ERRORLEVEL%`` must be read on the SAME logical
    # line as the command, so the marker echo is chained with ``&``.
    return ShellSpec(
        os.environ.get("COMSPEC", "cmd.exe"),
        ("/Q", "/K", "@echo off"),
        "echo __MINIS_DONE_{marker}_EXIT_%ERRORLEVEL%__",
        "cmd.exe",
    )


# ---------------------------------------------------------------------------
# Pending command bookkeeping
# ---------------------------------------------------------------------------

@dataclass
class _CommandCallback:
    """Kotlin ``CommandCallback`` — one in-flight command at a time."""

    marker: str
    line_callback: Optional[Callable[[str], None]] = None
    lines: list[str] = field(default_factory=list)
    future: asyncio.Future[tuple[str, int]] = field(default=None)  # type: ignore[assignment]

    @property
    def output(self) -> str:
        return "\n".join(self.lines)


# ---------------------------------------------------------------------------
# PersistentShell
# ---------------------------------------------------------------------------

class PersistentShell:
    """One long-lived shell per session, mirroring ``PersistentShell`` (Kotlin)."""

    def __init__(
        self,
        session_id: str,
        session_bind_mounts: Optional[dict[str, str]] = None,
        spec: Optional[ShellSpec] = None,
        cwd: Optional[Path] = None,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        """Kotlin: ``PersistentShell(context, sessionId, sessionBindMounts)``.

        # PORT: ``context`` (Android) -> ``AppContext.external_files_dir``;
        ``sessionBindMounts`` is kept verbatim — ``debug_bind_mount`` reads it
        back for diagnostics even though proot is not in play here.
        """
        self.session_id = session_id
        self.session_bind_mounts: dict[str, str] = dict(session_bind_mounts or {})
        self.spec = spec or detect_shell_spec()

        workspace = Path(cwd) if cwd else app_context().external_files_dir / session_id
        workspace.mkdir(parents=True, exist_ok=True)
        self.cwd = workspace

        merged_env = dict(os.environ)
        merged_env.setdefault("MINIS_SESSION_ID", session_id)
        if env:
            merged_env.update(env)
        self._env = merged_env

        self._process: Optional[asyncio.subprocess.Process] = None
        self._is_starting = False
        self._pending: Optional[_CommandCallback] = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()
        # PORT: set by the reader task when stdout hits EOF. Needed because
        # ``Process.returncode`` is filled in asynchronously by the event loop
        # (especially on Windows' Proactor), so a shell that just exited still
        # reports ``returncode is None`` for a few ms — enough for
        # ``ensure_started()`` to wrongly believe it is alive and hand the
        # caller a dead stdin.
        self._dead = True

        # Death diagnostics: rolling head + tail of everything the shell printed.
        self._output_head: list[str] = []
        self._output_head_len = 0
        self._output_tail: list[str] = []
        self._output_tail_len = 0
        self._output_total = 0
        self._last_exit_code: Optional[int] = None
        # Names injected by the previous ``apply_environment`` call, so the next
        # call can ``unset`` whatever disappeared from the new snapshot.
        self._applied_env_keys: set[str] = set()

    # --- lifecycle --------------------------------------------------------
    @property
    def is_alive(self) -> bool:
        """Kotlin: ``val isAlive: Boolean get() = process?.isAlive == true``.

        # PORT: also requires ``not self._dead`` — see the ``_dead`` comment in
        # ``__init__``. Without it a just-exited shell passes this check.
        """
        return (
            not self._dead
            and self._process is not None
            and self._process.returncode is None
        )

    @property
    def shell_name(self) -> str:
        return self.spec.name

    def debug_bind_mount(self, linux_path: str) -> Optional[str]:
        """[diag] Read back the mount this shell was started with (frozen at boot)."""
        return self.session_bind_mounts.get(linux_path)

    async def ensure_started(self) -> None:
        """Start the shell if it is not running yet (idempotent, concurrency-safe)."""
        if self.is_alive:
            return
        async with self._lock:
            if self.is_alive:
                return
            if self._is_starting:
                # Another coroutine is mid-spawn; wait until it settles.
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    if self.is_alive or not self._is_starting:
                        return
                return
            self._is_starting = True
            try:
                await self._start_process()
            finally:
                self._is_starting = False

    async def _start_process(self) -> None:
        """Spawn the child shell and kick off the stdout reader task."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.spec.executable,
                *self.spec.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # Release-build parity: stderr is merged into stdout so the
                # death tail captures proot's (or the shell's) last words.
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.cwd),
                env=self._env,
                # 独立进程组：中断时要能连子孙一起杀（见 kill_process_tree）。
                **new_group_kwargs(),
            )
        except (OSError, FileNotFoundError) as exc:
            logger.error("PersistentShell[%s]: failed to spawn %s: %s",
                         self.session_id, self.spec.executable, exc)
            self._process = None
            return

        self._process = proc
        self._dead = False
        self._reader_task = asyncio.create_task(
            self._read_loop(proc), name=f"minis-shell-{self.session_id}"
        )
        logger.info("PersistentShell[%s]: started %s in %s",
                    self.session_id, self.spec.name, self.cwd)

    async def _drain_banner(self) -> None:
        """Deprecated: the reader task owns stdout, so nothing may read it here.

        # PORT: the naive port read the first 4 KiB here to swallow cmd.exe's
        # copyright banner. That raced with ``_read_loop`` for the same pipe and
        # stole real command output on Windows. The banner now lands in the
        # death-tail buffer instead — harmless, and it keeps one reader.
        """
        return

    # --- output capture ---------------------------------------------------
    def _append_tail(self, text: str) -> None:
        """[T-android-shell-death-diagnosability] Rolling head + tail buffers."""
        self._output_total += len(text)
        if self._output_head_len < OUTPUT_HEAD_MAX:
            self._output_head.append(text)
            self._output_head_len += len(text)
        self._output_tail.append(text)
        self._output_tail_len += len(text)
        while self._output_tail_len > OUTPUT_TAIL_MAX and len(self._output_tail) > 1:
            self._output_tail_len -= len(self._output_tail.pop(0))

    def _append_raw(self, text: str) -> None:
        for line in text.splitlines():
            if line:
                self._append_tail(line)

    def death_tail(self) -> str:
        """Last words of a dead shell — head + ``...`` + tail, like Kotlin."""
        head = "\n".join(self._output_head)
        tail = "\n".join(self._output_tail)
        if self._output_total > OUTPUT_HEAD_MAX + OUTPUT_TAIL_MAX:
            return f"{head}\n...[{self._output_total} chars total]...\n{tail}"
        if head and tail and head != tail:
            return f"{head}\n{tail}"
        return tail or head

    # --- reader -----------------------------------------------------------
    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        """Kotlin ``readLoop(p)``: feed lines to the pending command, or to tail."""
        assert proc.stdout is not None
        try:
            while True:
                try:
                    raw = await proc.stdout.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    continue
                if not raw:
                    break
                line = raw.decode(errors="replace").rstrip("\r\n")
                self._append_tail(line)

                pending = self._pending
                if pending is None:
                    continue

                code = self._parse_exit_code(line, pending.marker) if pending else -1
                if code >= 0:
                    self._pending = None
                    self._last_exit_code = code
                    if not pending.future.done():
                        pending.future.set_result((pending.output, code))
                    continue

                # A marker line that is not ours belongs to a command the
                # caller already gave up on (timeout/cancel). Drop it —
                # otherwise the abandoned command's exit marker shows up as
                # the first line of the next command's output.
                if _STRAY_MARKER_RE.search(line):
                    continue

                pending.lines.append(line)
                if pending.line_callback is not None:
                    try:
                        pending.line_callback(line)
                    except Exception as exc:  # callback must never kill the reader
                        logger.warning("PersistentShell[%s]: line callback raised: %s",
                                       self.session_id, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("PersistentShell[%s]: read loop died: %s", self.session_id, exc)
        finally:
            # stdout hit EOF — the shell is gone. Mark it dead BEFORE resolving
            # the in-flight command so ``is_alive`` stops lying immediately
            # (see the ``_dead`` note in ``__init__``).
            self._dead = True
            # [T-android-shell-death-diagnosability] Capture the real exit code
            # so "[Shell not running]" can say WHY (e.g. proot exit=1).
            try:
                self._last_exit_code = await asyncio.wait_for(proc.wait(), 2.0)
            except (asyncio.TimeoutError, Exception):
                pass

            pending = self._pending
            if pending is not None and not pending.future.done():
                self._pending = None
                pending.future.set_result((pending.output, -1))

    def _parse_exit_code(self, text: str, marker: str) -> int:
        """Pattern: ``__MINIS_DONE_<marker>_EXIT_<code>__``. -1 when absent."""
        match = self.spec.marker_pattern(marker).search(text)
        if match is None:
            return -1
        try:
            return int(match.group(1))
        except ValueError:
            return -1

    # --- execute ----------------------------------------------------------
    async def execute_command(
        self,
        command: str,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        line_callback: Optional[Callable[[str], None]] = None,
    ) -> tuple[str, int]:
        """Execute a command and wait for completion.

        Wraps the command with a unique marker to detect output boundaries::

            {command}
            echo "__MINIS_DONE_{marker}_EXIT_$?__"

        Returns ``(output, exit_code)``. Mirrors Kotlin's contract including the
        "[Shell not running]" diagnostic on a dead shell and exit ``124`` on
        timeout (the shell itself is left alive — a hung command must not force
        a cold restart of the session's environment).
        """
        await self.ensure_started()

        proc = self._process
        if proc is None or proc.stdin is None or not self.is_alive:
            exit_code = self._last_exit_code
            tail = self.death_tail()
            detail = "[Shell not running]"
            if exit_code is not None:
                detail += f" shell exit={exit_code}"
            if tail:
                detail += f"\n{tail[:300]}"
            return detail, -1

        marker = uuid.uuid4().hex[:8]
        wrapped = self.spec.wrap(command, marker)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[str, int]] = loop.create_future()
        cb = _CommandCallback(marker=marker, line_callback=line_callback, future=future)
        self._pending = cb

        try:
            proc.stdin.write(wrapped.encode("utf-8", errors="replace"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            self._pending = None
            return f"[Write error: {exc}]", -1

        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            # 超时不能只丢下等待 —— 那条命令**还在 shell 里跑**，它接下来的输出
            # 会掺进下一条命令的回显里（现场症状：模型看到两段输出拼在一起，
            # 以为命令写错了，然后换个写法再撞一次）。所以超时也把进程树收掉，
            # 下一条命令从干净的 shell 重新开始。
            note = self.interrupt_now()
            logger.warning("PersistentShell[%s]: command timed out after %ss; %s",
                           self.session_id, int(timeout), note)
            return f"[Command timed out after {int(timeout)}s]", 124
        except asyncio.CancelledError:
            # 用户按了「停止」：把这条工具调用**真的杀掉**，而不是只丢掉等待。
            note = self.interrupt_now()
            logger.info("PersistentShell[%s]: command cancelled by caller; %s",
                        self.session_id, note)
            raise

    def interrupt_now(self) -> str:
        """同步杀掉当前命令（整棵进程树），并把这个 shell 标记为已死。

        同步是**故意的**：调用点位于取消/超时处理里，那里任何 ``await`` 都可能
        被第二次取消打断，于是「停止」又退化成「只丢下等待」。Windows 上
        ``taskkill`` 要几百毫秒，这点阻塞换「真的停住」是划算的。

        杀掉之后 shell 就没了 —— 下一条命令经 ``ensure_started()`` 自动重建
        （约 1 秒 + 环境变量重注入），比留着一个脏 shell 强。
        """
        proc = self._process
        self._dead = True
        self._pending = None
        self._process = None
        if proc is None or proc.returncode is not None:
            return "shell 已经停了"
        return kill_process_tree(proc.pid, proc)


    # --- environment ------------------------------------------------------
    async def apply_environment(
        self,
        env_vars: dict[str, str],
        previous_keys: Optional[set[str]] = None,
    ) -> None:
        """Apply environment variables to the running shell.

        The shell is long-lived and reused across commands, so a stale ``export``
        from a previous turn lingers until something overwrites it. Names present
        in ``previous_keys`` but absent from ``env_vars`` are unset first, giving
        whole-snapshot semantics (matching iOS, which gets a fresh ``/bin/sh``
        per command). An empty ``previous_keys`` preserves the original overlay
        behaviour for system broadcasts (TZ, proxy).
        """
        await self.ensure_started()
        stale = (previous_keys or self._applied_env_keys) - set(env_vars)
        if stale:
            await self.execute_command("unset " + " ".join(sorted(stale)))
        if env_vars:
            exports = " ".join(f"{k}={_shell_quote(v)}" for k, v in env_vars.items())
            await self.execute_command(f"export {exports}")
        # Track what we injected regardless of whether the caller passed keys,
        # so the next call can clean up after this one.
        self._applied_env_keys = set(env_vars)

    # --- teardown ---------------------------------------------------------
    async def stop(self) -> None:
        """Terminate the shell (Kotlin ``stop()``)."""
        self._dead = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None

        proc = self._process
        self._process = None
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        # 连子孙一起杀：只 terminate() 会留下一堆孤儿 curl/python（见
        # kill_process_tree 的说明）。
        kill_process_tree(proc.pid, proc)
        try:
            await asyncio.wait_for(proc.wait(), 3.0)
        except asyncio.TimeoutError:
            logger.debug("PersistentShell[%s]: not reaped in 3s after kill",
                         self.session_id)


def _shell_quote(value: str) -> str:
    """POSIX single-quote a value for ``export`` (cmd.exe tolerates it loosely)."""
    return "'" + value.replace("'", "'\\''") + "'"
