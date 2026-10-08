"""shell_execute tool — run commands in the session's persistent sandbox shell.

Ported from: src/android/app/src/main/java/com/openminis/app/tools/AgentTools.kt
             (shellExecuteDefinition + its execution path through
              ExecutionCoordinator / PersistentShell)
Original package: com.openminis.app.tools

Schema kept identical to the Kotlin definition (name ``shell_execute``, params
``tool_title``/``command``/``timeout``/``delay``). Execution semantics match the
Android pipeline: a shared per-session ``PersistentShell`` via
``ExecutionCoordinator.execute``, with TerminalSanitizer cleanup applied there.

# PORT: unlike the stateless file tools, shell_execute touches shared shell
# state, so this tool is stateful by necessity. ``execute`` is therefore
# ``async`` — the agent loop awaits tools uniformly.
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Optional

from ..core.logging import get_logger
from ..data.model.agent_tool_definition import AgentToolDefinition, AgentToolParam
from ..sandbox.execution_coordinator import ExecutionCoordinator
from .tool_execution_result import ToolExecutionResult

logger = get_logger(__name__)

__all__ = ["ShellExecuteTool"]

#: 命令里出现这些片段就认定是「生图」（技能脚本形式）。
_IMAGE_CMD_MARKERS = (
    "image_generation", "image_gen", "agnes-image", "agnes_image", "imagegen",
)

#: 生图命令的最小 timeout（秒）。实测 90–150 秒，留足余量。
_IMAGE_CMD_MIN_TIMEOUT = 300


def _looks_like_image_gen(command: str) -> bool:
    """这条命令是生图脚本吗（按命令文本判断，跟 repeat_guard 用同一组标记）。"""
    low = (command or "").lower()
    return any(marker in low for marker in _IMAGE_CMD_MARKERS)


class ShellExecuteTool:
    """Kotlin ``shell_execute`` tool definition + execution."""

    NAME = "shell_execute"

    def __init__(
        self,
        coordinator: Optional[ExecutionCoordinator] = None,
        get_env: Optional[Callable[[], dict[str, str]]] = None,
    ) -> None:
        """``get_env`` supplies per-call user env vars (T124a snapshot)."""
        self.coordinator = coordinator or _default_coordinator()
        self.get_env = get_env

    @staticmethod
    def definition() -> AgentToolDefinition:
        return AgentToolDefinition(
            name=ShellExecuteTool.NAME,
            description=(
                "Execute a command in an isolated Linux process (Alpine Linux via PRoot). "
                "The command runs via /bin/sh -c with stdout and stderr merged. "
                "Environment variables persist between commands in the same session "
                "(cd and export persist too)."
            ),
            parameters={
                "tool_title": AgentToolParam(
                    "string",
                    "A concise 5-10 word summary of what this command does, shown to the "
                    "user (e.g. 'List project files', 'Install Python dependencies'). "
                    "Use the same language as the user.",
                ),
                "command": AgentToolParam(
                    "string",
                    "The shell command to execute. Supports multi-line commands directly — "
                    "no special escaping needed. Keep under 1000 chars; for longer scripts, "
                    "write to a file with file_write first, then run it.",
                ),
                "timeout": AgentToolParam(
                    "integer",
                    "Timeout in seconds (default: 900). Use a larger value for long-running "
                    "commands like package installs.",
                ),
                "delay": AgentToolParam(
                    "integer",
                    "Delay in seconds before execution begins. The tool blocks the agent "
                    "flow during this wait WITHOUT occupying the shell.",
                ),
            },
            required=["tool_title", "command"],
            property_ordering=["tool_title", "command", "timeout", "delay"],
        )

    async def _one_shot_fallback(
        self, session_id: str, command: str, timeout: int,
        env_extra: Optional[dict[str, str]] = None,
    ) -> Optional[tuple[str, int]]:
        """持久 shell 失效时的一次性执行兜底（Popen + 工作线程等待）。

        返回 ``(output, exit_code)``；连兜底都失败（找不到 shell 等）返回
        ``None``，让上层继续走持久 shell 的失败提示。
        """
        import subprocess as _sp

        from .path_utils import workspace_root

        try:
            # 走**模块属性**而不是 from-import：这样测试（和任何调用方）能
            # monkeypatch 掉 persistent_shell.kill_process_tree 观察真实行为 ——
            # 复核指出这条改动原本一行断言都没有（撤掉后 57 个用例全绿）。
            from ..sandbox import persistent_shell as _ps

            from ..core.logging import get_logger

            spec = _ps.detect_shell_spec()
        except Exception:
            return None
        cwd = None
        coordinator = self.coordinator
        overrides = getattr(coordinator, "_cwd_overrides", None)
        if isinstance(overrides, dict):
            cwd = overrides.get(session_id) or str(workspace_root())
        run_env = None
        if env_extra:
            import os as _os

            run_env = {**_os.environ, **env_extra}
        if spec.name == "cmd.exe":
            argv = [spec.executable, "/d", "/s", "/c", command]
        else:
            argv = [spec.executable, "--noprofile", "--norc", "-c", command]
        limit = max(float(timeout), 1.0)
        try:
            # 用 Popen 而不是 subprocess.run：run() 整个跑在工作线程里，外层
            # 取消协程时**杀不动它**（这正是「按了停止，命令还在跑」的一条路径）。
            # Popen 拿得到 pid，登记到协调器后停止按钮就能把它一起收掉。
            proc = _sp.Popen(
                argv,
                stdout=_sp.PIPE,
                stderr=_sp.PIPE,
                text=True,
                cwd=cwd,
                env=run_env,
                errors="replace",
                **_ps.new_group_kwargs(),
            )
        except Exception as exc:
            logger.warning("one-shot shell fallback spawn failed: %s", exc)
            return None
        coordinator.register_fallback(session_id, proc)
        try:
            try:
                out, err = await asyncio.to_thread(proc.communicate, timeout=limit)
            except _sp.TimeoutExpired:
                _ps.kill_process_tree(proc.pid, proc)
                return f"[Command timed out after {int(limit)}s]", 124
            except asyncio.CancelledError:
                # 用户按了停止 —— 杀掉，别留孤儿进程在后台继续跑。
                _ps.kill_process_tree(proc.pid, proc)
                raise
            except Exception as exc:  # pragma: no cover - 兜底绝不能把异常抛上去
                _ps.kill_process_tree(proc.pid, proc)
                logger.warning("one-shot shell fallback failed: %s", exc)
                return None
        finally:
            coordinator.unregister_fallback(session_id, proc)
        result = (((out or "") + (err or "")).strip() or "(no output)", proc.returncode)
        logger.warning(
            "PersistentShell[%s] unusable (exit=-1); one-shot fallback ran "
            "the command (exit=%d)", session_id, result[1],
        )
        return result

    async def execute(
        self,
        args_json: str,
        session_id: str,
        line_callback: Optional[Callable[[str], None]] = None,
    ) -> ToolExecutionResult:
        """Kotlin ``execute(argsJson, sessionId)`` — async shell dispatch."""
        try:
            args = json.loads(args_json)
        except ValueError as exc:
            return ToolExecutionResult(f"Error: invalid JSON args: {exc}", True,
                                       tool_title=ShellExecuteTool.NAME)

        command = str(args.get("command", ""))
        tool_title = str(args.get("tool_title", ShellExecuteTool.NAME))
        if not command.strip():
            return ToolExecutionResult("Error: 'command' is required", True,
                                       tool_title=tool_title)

        # 把沙箱写法还原成本机路径再执行。
        #
        # 为什么必须做：出站给模型的内容里，机器路径被统一换成
        # ``/var/minis/workspace|data|home/…``（见 path_utils.scrub_machine_paths），
        # 模型照着这个形态拼命令是**必然**的 —— 而 Windows 上根本没有
        # ``/var/minis``，于是 ``cd /var/minis/data/skills/xxx`` 一律
        # "No such file or directory"。实测这就是技能脚本跑不起来的原因。
        from .path_utils import unscrub_sandbox_paths

        command = unscrub_sandbox_paths(command)

        # 沙箱守卫：异常删除 / 敏感信息（读密钥、外传凭据）先在这里拦下。
        # 拦下后记一条事件，用户在「沙箱」页可以手动放行。
        try:
            from ..sandbox.guard import check_shell_command

            # 先落成一个变量：下面问用户「要不要放行」时要用同一个 cwd。
            # （第一版直接写了个不存在的名字 cwd —— ruff 的 F821 本该在写盘前就拦下它。）
            cwd = self.coordinator.cwd_for(session_id)
            blocked = check_shell_command(command, session_id=session_id, cwd=cwd)
        except Exception:  # pragma: no cover - 守卫故障不该让 shell 整体失效
            logger.exception("sandbox guard failed")
            blocked = None
        if blocked is not None:
            # 不再一拦到底：先问用户（界面弹一条确认）。用户点头就**当场执行**，
            # 模型不用重发那条命令；拒绝/超时维持原来的拦截行为。
            blocked = await _ask_user_to_allow(
                blocked, command=command, session_id=session_id, cwd=cwd
            )
        if blocked is not None:
            return ToolExecutionResult(blocked, True, tool_title=tool_title)
        try:
            timeout = int(args.get("timeout", 900))
        except (TypeError, ValueError):
            timeout = 900
        # 生图命令（技能脚本）实测要 90–150 秒，有时候接近 3 分钟。模型经常随手
        # 给 timeout=120 —— 那是**必然**被杀：工具在 120 秒整中止，模型只能重跑
        # 一遍，白扔两分钟（现场就是这样把 QQ 群聊的 5 分钟被动窗口耗光的）。
        # 这是确定性的失败配置，直接抬到安全值并说明，而不是让模型自己去悟。
        timeout_note = ""
        if timeout < _IMAGE_CMD_MIN_TIMEOUT and _looks_like_image_gen(command):
            timeout_note = (
                f"[已自动把 timeout 从 {timeout}s 抬到 "
                f"{_IMAGE_CMD_MIN_TIMEOUT}s：生图实测 90–150 秒，"
                f"{timeout}s 必然被杀、只能重跑。]\n"
            )
            logger.info("raised shell timeout for image gen: %ss -> %ss",
                        timeout, _IMAGE_CMD_MIN_TIMEOUT)
            timeout = _IMAGE_CMD_MIN_TIMEOUT
        try:
            delay = int(args.get("delay", 0))
        except (TypeError, ValueError):
            delay = 0
        if delay > 0:
            await asyncio.sleep(delay)

        env = self.get_env() if self.get_env is not None else _load_env_extra()
        result = await self.coordinator.execute(
            session_id,
            command,
            timeout=max(float(timeout), 1.0),
            line_callback=line_callback,
            env_vars=env,
        )

        output = (timeout_note + (result.output or "(no output)")) if timeout_note else (
            result.output or "(no output)"
        )
        if result.exit_code == -1 and "[Write error" not in output:
            # 持久 shell 没跑成（app 的事件循环拓扑下偶发秒死，exit=-1）。
            # 兜底：用一次性 ``bash -c`` 在工作线程里同步执行 —— 不依赖
            # 持久 shell 的生命周期，命令必须真的跑起来。
            one_shot = await self._one_shot_fallback(session_id, command, timeout, env)
            if one_shot is not None:
                out_text, code = one_shot
                if code != 0:
                    out_text = (
                        f"$ {command}\n{out_text}\n"
                        f"[exit code: {code}]\n"
                        "请先分析上面的报错原因（命令不存在？依赖缺失？路径不对？），"
                        "修正命令或环境后再重试；连续失败 2 次就把问题如实报告用户，"
                        "不要改用无关工具（如 read_image）来回避。"
                    )
                return ToolExecutionResult(
                    out_text, code == 0, tool_title=tool_title,
                )
        if result.exit_code == 124:
            output += "\n[command timed out]"
        if result.exit_code != 0:
            # 失败必须把「跑的是什么、退出码、输出」完整交给模型，否则它
            # 拿到一句干瘪报错没法分析原因，只会乱试别的工具。
            output = (
                f"$ {command}\n{output}\n"
                f"[exit code: {result.exit_code}]\n"
                "请先分析上面的报错原因（命令不存在？依赖缺失？路径不对？），"
                "修正命令或环境后再重试；连续失败 2 次就把问题如实报告用户，"
                "不要改用无关工具（如 read_image）来回避。"
            )
        return ToolExecutionResult(
            output, result.exit_code == 0, tool_title=tool_title,
        )


_shared: Optional[ExecutionCoordinator] = None


def _load_env_extra() -> Optional[dict[str, str]]:
    """读取「环境变量」页配置的 ``sandbox.envExtra``（JSON KV）。

    ``ShellExecuteTool()`` 默认没人传 ``get_env``，导致用户在设置页配的
    环境变量（如 ``OPENAI_API_KEY``）根本进不了 shell —— 技能脚本里
    ``$OPENAI_API_KEY`` 永远是空。这里作为默认来源；显式注入仍优先。
    """
    try:
        from ..core.prefs import get_prefs

        raw = get_prefs().get_string("sandbox.envExtra") or ""
        if not raw.strip():
            return None
        import json as _json

        data = _json.loads(raw)
        if isinstance(data, dict):
            out = {str(k): str(v) for k, v in data.items() if str(v).strip()}
            return out or None
    except Exception:  # pragma: no cover - 配置损坏不该拖垮 shell
        logger.debug("envExtra unreadable", exc_info=True)
    return None


def _default_coordinator() -> ExecutionCoordinator:
    """Module-level shared coordinator — the shell cache is app-global."""
    global _shared
    if _shared is None:
        _shared = ExecutionCoordinator()
    return _shared


def get_coordinator() -> ExecutionCoordinator:
    """Access the shared coordinator (creating it if needed).

    Callers (the Web server) use this to register per-session sandbox roots
    before a chat turn starts, so filed sessions boot inside their workspace.
    """
    return _default_coordinator()


def install_coordinator(coordinator: ExecutionCoordinator) -> None:
    """Point the default tool at an externally-owned coordinator (server/CLI)."""
    global _shared
    _shared = coordinator


async def _ask_user_to_allow(
    blocked: str, *, command: str, session_id: str, cwd: str
) -> Optional[str]:
    """把拦截结果送进界面问一句。

    放行 → 返回 ``None``（调用方接着执行这条命令）；其余情况返回要回给模型的文案。
    只有接了客户端（桌面界面）才会问：纯 CLI / 没人在线时 ``confirm.ask()`` 直接
    返回 ``unavailable``，行为跟以前一模一样（硬拦），不会把 agent 挂住。
    """
    from ..sandbox import confirm
    from ..sandbox.guard import guard

    event = guard.last_event()
    if event is None or event.command != command:
        # 拿不到对应的事件（比如守卫在别处也记了一条）就别问，老老实实拦。
        return blocked
    decision = await confirm.ask(
        {
            "eventId": event.id,
            "family": event.family,
            "command": command,
            "cwd": cwd,
            "sessionId": session_id,
            "targets": list(event.targets),
            "reasons": list(event.reasons),
        }
    )
    if decision == "unavailable":
        return blocked
    if decision in ("once", "session", "always"):
        if decision != "once":
            # 本次会话 / 永久放行：写进白名单，下一同类命令不再问。
            guard.allow(event.id, decision)
        return None
    note = {
        "deny": "用户拒绝了这条命令，换一条安全的做法。",
        "timeout": "等用户确认超时，已按拒绝处理。",
    }.get(decision, "用户没有放行这条命令。")
    return f"{blocked}\n\n{note}"
