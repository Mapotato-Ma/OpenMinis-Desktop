"""[T-plugins-manager] 插件工具：把插件声明的 ``tools`` 注册成 agent 可调用的工具。

**这是「插件不只是通道」的关键那一环。** 通道插件给引擎接通一个 IM；工具插件
给 agent 加能力 —— 装一个插件就能多一个工具，不用改引擎代码、不用发版。

约定（足够简单，什么语言都能实现）：

    plugin.json 里写 tools[]
        ↓ 引擎按需起一次进程，cwd = 插件目录
    stdin  ← {"tool": "...", "args": {...}, "sessionId": "...", "pluginDir": "..."}
    stdout → 工具输出（纯文本或 JSON，原样作为工具结果）

插件必须**已安装且已启用**才会挂给 agent。启用是显式的：没装/没启用的插件
不会偷偷往模型 schema 里塞工具。

工具对外的名字是 ``<插件id>__<工具id>``（插件 id 里的 ``-``/``.`` 换成 ``_``）——
加前缀是为了不和内置工具撞名，也让模型一眼看出这是哪来的。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from ..agent.agent_runtime import ToolExecutor
from ..core.logging import get_logger
from ..data.model.agent_tool_definition import AgentToolDefinition, AgentToolParam
from ..tools.tool_execution_result import ToolExecutionResult
from . import store
from .manifest import (
    TOOL_OUTPUT_MAX_CHARS,
    ChannelManifest,
    PluginToolSpec,
)
from .process import resolve_executable

logger = get_logger(__name__)


def tool_full_id(plugin_id: str, tool_id: str) -> str:
    """``qq-bot`` + ``send`` → ``qq_bot__send``。

    只留字母数字下划线：Gemini 的函数名不接受 ``-``/``.``（各家 provider 里
    最严的那个说了算），所以统一换成下划线，别到运行时才炸。
    """
    head = _ident(plugin_id) or "plugin"
    tail = _ident(tool_id) or "tool"
    return f"{head}__{tail}"


def _ident(raw: str) -> str:
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in str(raw or ""))


def _coordinator_for(session_id: str):  # noqa: ANN202
    """这个会话的执行协调器（没有会话 id 时为 None —— 没有归属就没人能取消它）。"""
    if not session_id:
        return None
    try:
        from ..tools.shell_execute_tool import get_coordinator

        return get_coordinator()
    except Exception:  # pragma: no cover - 拿不到协调器不该拦住插件工具
        return None


def _new_group_kwargs() -> dict[str, Any]:
    """独立进程组：取消/超时时才能连子孙一起杀（R15）。"""
    try:
        from ..sandbox.persistent_shell import new_group_kwargs

        return new_group_kwargs()
    except Exception:  # pragma: no cover
        return {}


def _kill_plugin(proc: Any) -> None:
    """收掉插件进程**整棵树**（杀不掉就如实记一笔，不假装成功）。"""
    if proc is None or proc.returncode is not None:
        return
    try:
        from ..sandbox.persistent_shell import kill_process_tree

        kill_process_tree(proc.pid, proc)
        return
    except Exception:  # pragma: no cover - 退回单进程 kill
        logger.debug("kill_process_tree failed for plugin tool", exc_info=True)
    try:
        proc.kill()
    except (ProcessLookupError, OSError):  # pragma: no cover
        pass


def enabled_specs() -> list[tuple[ChannelManifest, PluginToolSpec]]:
    """所有「已安装 + 已启用」插件提供的工具。

    每次现读清单 —— 用户刚改完 plugin.json 或刚启用某个插件，下一轮对话就该
    看到新工具，不该还要重启引擎。
    """
    out: list[tuple[ChannelManifest, PluginToolSpec]] = []
    for manifest in store.list_installed():
        if not manifest.tools or not store.is_enabled(manifest.id):
            continue
        for spec in manifest.tools:
            out.append((manifest, spec))
    return out


def tool_ids() -> set[str]:
    """已启用的插件工具对外名字集合（给 enabled_tools 校验放行用）。"""
    return {tool_full_id(m.id, s.id) for m, s in enabled_specs()}


def catalog_entries() -> list[dict[str, Any]]:
    """给设置界面看的一行 —— 与 ``settings.catalog.TOOL_CATALOG`` 同形状。"""
    out: list[dict[str, Any]] = []
    for manifest, spec in enabled_specs():
        out.append({
            "id": tool_full_id(manifest.id, spec.id),
            "name": spec.label,
            "description": spec.description,
            "category": "Plugin",
            "pluginId": manifest.id,
            "pluginName": manifest.name,
        })
    return out


def definition_of(manifest: ChannelManifest, spec: PluginToolSpec) -> AgentToolDefinition:
    params: dict[str, AgentToolParam] = {}
    for key, raw in (spec.parameters or {}).items():
        params[key] = AgentToolParam(
            type=str(raw.get("type") or "string"),
            description=str(raw.get("description") or ""),
            enum_values=list(raw["enum"]) if raw.get("enum") else None,
        )
    desc = spec.description or f"{manifest.name} 提供的工具"
    return AgentToolDefinition(
        name=tool_full_id(manifest.id, spec.id),
        description=desc,
        parameters=params,
        required=[r for r in spec.required if r in params],
        property_ordering=list(params),
    )


def build_registry() -> dict[str, ToolExecutor]:
    """``{工具名: ToolExecutor}`` —— 由 ``catalog.build_tool_registry`` 合并。"""
    out: dict[str, ToolExecutor] = {}
    for manifest, spec in enabled_specs():
        name = tool_full_id(manifest.id, spec.id)
        out[name] = ToolExecutor(
            definition=definition_of(manifest, spec),
            executor=_make_executor(manifest, spec),
        )
    return out


def _make_executor(manifest: ChannelManifest, spec: PluginToolSpec):
    async def executor(args_json: str, session_id: str = "", **_kw: Any) -> ToolExecutionResult:
        return await run_tool(manifest, spec, args_json, session_id)

    return executor


async def run_tool(
    manifest: ChannelManifest,
    spec: PluginToolSpec,
    args_json: str,
    session_id: str = "",
) -> ToolExecutionResult:
    """起一次插件进程，把参数喂进去，收 stdout。"""
    base = (manifest.path or store.plugins_dir() / manifest.id).resolve()
    cwd = _resolve_cwd(base, spec.cwd)
    if cwd is None:
        return ToolExecutionResult(
            output=f"插件 {manifest.id} 的工作目录越界了：{spec.cwd}", success=False
        )
    exe = resolve_executable(spec.command[0] if spec.command else "")
    if exe is None:
        return ToolExecutionResult(
            output=f"找不到可执行文件：{spec.command[0] if spec.command else '(空)'}",
            success=False,
        )

    try:
        args = json.loads(args_json) if args_json else {}
    except (TypeError, ValueError):
        args = {"_raw": args_json}
    payload = json.dumps(
        {
            "tool": spec.id,
            "args": args if isinstance(args, dict) else {"value": args},
            "sessionId": session_id,
            "pluginDir": str(base),
        },
        ensure_ascii=False,
    )

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # 配置项注入成 OM_<KEY>，工具脚本能直接读（密钥不必写死在脚本里）
    for key, value in store.read_config(manifest.id).items():
        if key.startswith("_") or value in (None, ""):
            continue
        if isinstance(value, list):
            env[f"OM_{key.upper()}"] = ",".join(str(v) for v in value)
        elif not isinstance(value, dict):
            env[f"OM_{key.upper()}"] = str(value)

    # R15：插件进程要能被**会话级停止**收掉，也要在取消/超时时连子孙一起杀。
    # 改前这里既没登记给协调器，也没有取消分支 —— 按停止只是结束等待，插件进程
    # 留在后台继续跑（隔离探针：communicate 中取消、kill 调用 = 0）；超时也只
    # kill 父进程、不 wait 回收、不杀树（插件脚本往往再拉一个子进程干活）。
    coordinator = _coordinator_for(session_id)
    registered = False
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            *[str(a) for a in spec.command[1:]],
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_new_group_kwargs(),
        )
        if coordinator is not None:
            # 登记是**尽力而为**：协调器是鸭子类型（测试里常换成只记调用的假对象），
            # 少一个方法不该让插件工具本身跑不起来 —— 但登记失败要留痕（那条路就
            # 失去了"停止时被收掉"的能力）。
            try:
                coordinator.register_fallback(session_id, proc)
                registered = True
            except Exception:
                logger.debug("plugin process registration failed", exc_info=True)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(payload.encode("utf-8")), timeout=spec.timeout_sec
            )
        except asyncio.TimeoutError:
            _kill_plugin(proc)
            return ToolExecutionResult(
                output=f"工具 {spec.label} 超过 {spec.timeout_sec}s 没返回，已中止。",
                success=False,
                timed_out=True,
            )
        except asyncio.CancelledError:
            # 用户按了停止（或连接断了）：把整棵进程树收掉再让取消继续传播 ——
            # "取消"必须等于"进程真的没了"，不能只是不等它了。
            _kill_plugin(proc)
            raise
    except OSError as exc:
        return ToolExecutionResult(output=f"启动工具失败：{exc}", success=False)
    finally:
        if registered and coordinator is not None and proc is not None:
            try:
                coordinator.unregister_fallback(session_id, proc)
            except Exception:  # pragma: no cover - 注销失败不该盖住真正的结果
                logger.debug("plugin process unregister failed", exc_info=True)

    stdout = out.decode("utf-8", errors="replace").strip()
    stderr = err.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0 and not stdout:
        detail = stderr[-1500:] or f"退出码 {proc.returncode}"
        return ToolExecutionResult(output=f"工具 {spec.label} 失败：{detail}", success=False)
    if len(stdout) > TOOL_OUTPUT_MAX_CHARS:
        stdout = stdout[:TOOL_OUTPUT_MAX_CHARS] + "\n…（输出过长已截断）"
    return ToolExecutionResult(
        output=stdout or f"（{spec.label} 没有输出）",
        success=proc.returncode == 0,
        tool_title=spec.label,
    )


def _resolve_cwd(base: Path, cwd: str) -> Path | None:
    """工作目录必须落在插件目录内（相对路径以插件目录为基准）。"""
    rel = str(cwd or ".").strip()
    if rel in ("", "."):
        return base if base.is_dir() else None
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        return None
    return target if target.is_dir() else None


__all__ = [
    "tool_full_id",
    "enabled_specs",
    "tool_ids",
    "catalog_entries",
    "definition_of",
    "build_registry",
    "run_tool",
]
