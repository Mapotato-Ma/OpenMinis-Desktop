"""browser_use tool.

Ported from: src/android/app/src/main/java/com/openminis/app/tools/BrowserUseTool.kt
Original package: com.openminis.app.tools

PORT: 引擎在 ``tools/browser/driver.py``（自包含实现）。

选型结论是**驱动系统自带的 Edge/Chrome**，而不是打包 Playwright 自带的 Chromium：
后者会让安装包多背 **658MB**（2026-10-06 实测：下载 186.8MiB、装完 658MB），
前者只要 playwright 包本身的 137MB，而且浏览器内核跟着系统更新、永远最新。
真 Windows 上 ``channel="msedge"`` 已实测可用（Edge 156，取元素 6.9ms/83 个）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..data.model.agent_tool_definition import AgentToolDefinition, AgentToolParam
from .tool_execution_result import ToolExecutionResult

__all__ = ["BrowserUseTool", "BROWSER_ACTIONS"]


def _screenshot_dir(session_id: str) -> Optional[Path]:
    """截图默认目录：本会话工作区下的 ``.minis/shots/``。

    必须是**工作区内**的目录，否则 read_image（只在工作区里解析路径）读不回来。
    拿不到工作区就返回 None —— 那时退回驱动自带的默认值，宁可不改行为。

    用点目录是为了不污染用户的真实项目目录：工作区常常就是他的代码仓库，
    冒出一个 ``browser-shots/`` 会直接出现在 ``git status`` 里（独立复核建议）。
    """
    try:
        from .shell_execute_tool import get_coordinator

        raw = get_coordinator().cwd_for(session_id)
    except Exception:  # pragma: no cover - 协调器不可用就不改默认值
        return None
    # 空串要当"拿不到"：``Path("")`` 是 ``.``，会在**服务进程的工作目录**里
    # 建目录并返回相对路径，read_image 照样读不回来 —— 那就白改了。
    if not raw:
        return None
    try:
        target = Path(raw) / ".minis" / "shots"
        target.mkdir(parents=True, exist_ok=True)
        return target
    except Exception:  # pragma: no cover - 目录建不出来也别让工具炸
        return None


# Aligned with the Kotlin BrowserAction enum. Kept as a tuple so the order
# matches the enum declaration in the original.
BROWSER_ACTIONS: tuple[str, ...] = (
    "navigate",
    "screenshot",
    "click",
    "type",
    "get_text",
    "get_readable",
    "scroll",
    "scroll_and_collect",
    "find_elements",
    "get_page_info",
    "get_backbone",
    "execute_js",
    "fetch",
    "hover",
    "set_user_agent",
    "set_viewport",
    "get_cookies",
    "set_cookies",
    "wait_for_dom_stable",
    "new_tab",
    "close_tab",
    "list_tabs",
)


class BrowserUseTool:
    """Kotlin: ``object BrowserUseTool`` — schema-only stub.

    The schema is registered so the LLM can call ``browser_use``; the executor
    currently rejects every action with a clear "engine not ported" message.
    Once a Python browser driver lands, swap ``execute`` for a real dispatcher
    and the rest of the agent loop is unchanged.
    """

    NAME = "browser_use"

    @staticmethod
    def definition() -> AgentToolDefinition:
        return AgentToolDefinition(
            name=BrowserUseTool.NAME,
            description=(
                "Control a web browser with up to 3 tabs. Do NOT use this tool "
                "for minis:// action URLs — those are app deep links, use "
                "Markdown links in chat instead. The browser supports both web "
                "URLs and minis:// resource URLs. Use navigate to open URLs, "
                "screenshot to see the page (returns an image), click/type to "
                "interact with elements, get_text/get_readable to extract "
                "content, scroll to navigate long pages, find_elements to "
                "discover interactive elements, get_page_info for page "
                "metadata, get_backbone to get a structural overview of the "
                "page DOM as a simplified tree, fetch to download files using "
                "the page's session, and new_tab / close_tab / list_tabs to "
                "manage tabs. 引擎用系统自带的 Edge/Chrome，不需要下载浏览器。"
                "第一次操作某个页面之前先做一次 snapshot：它会给每个可交互元素一个"
                "整数 ref 编号，之后 click/type 用 ref 比写选择器稳。"
            ),
            parameters={
                "tool_title": AgentToolParam(
                    "string",
                    "A concise 5-10 word summary of what this tool call does, "
                    "shown to the user (e.g. 'Open Wikipedia homepage', 'Take "
                    "screenshot of current page'). Use the same language as "
                    "the user.",
                ),
                "action": AgentToolParam(
                    "string",
                    "The browser action to perform",
                    enum_values=list(BROWSER_ACTIONS),
                ),
                "url": AgentToolParam(
                    "string",
                    "URL to navigate to (for navigate action) or resource to "
                    "download (for fetch action)",
                ),
                "selector": AgentToolParam(
                    "string",
                    "CSS selector for targeting elements (click, type, "
                    "get_text, scroll, hover, find_elements). For scroll: "
                    "specify a scrollable container to scroll; if omitted, "
                    "auto-detects the best scrollable element.",
                ),
                "text": AgentToolParam(
                    "string", "Text to type (for type action)"
                ),
                "coordinate_x": AgentToolParam(
                    "integer", "X coordinate for click (alternative to selector)"
                ),
                "coordinate_y": AgentToolParam(
                    "integer", "Y coordinate for click (alternative to selector)"
                ),
                "direction": AgentToolParam(
                    "string", "Scroll direction", enum_values=["up", "down"]
                ),
                "amount": AgentToolParam(
                    "integer", "Scroll amount in pixels (default: 500)"
                ),
                "script": AgentToolParam(
                    "string",
                    "JavaScript code to execute (for execute_js action). The "
                    "script runs inside an async function wrapper — `await` "
                    "and top-level `return` are both supported.",
                ),
                "user_agent": AgentToolParam(
                    "string",
                    "User agent profile to switch to",
                    enum_values=["desktop_chrome", "mobile_chrome"],
                ),
                "max_depth": AgentToolParam(
                    "integer", "Maximum tree depth for get_backbone (default: 5)"
                ),
                "scroll_count": AgentToolParam(
                    "integer",
                    "Number of scroll steps for scroll_and_collect (default: 10, max: 20). "
                    "Each step scrolls by 'amount' pixels and waits for new content.",
                ),
                "item_selector": AgentToolParam(
                    "string",
                    "CSS selector for individual content items in "
                    "scroll_and_collect (e.g. 'article', "
                    "'[data-testid=\"tweet\"]'). If omitted, auto-detects "
                    "repeated elements.",
                ),
                "tab_id": AgentToolParam(
                    "integer",
                    "Target tab ID (optional, defaults to most recently used "
                    "tab). Use list_tabs to see available tabs.",
                ),
                "keywords": AgentToolParam(
                    "string",
                    "Filter cookies by name (for get_cookies). A "
                    "space-separated string or array of strings.",
                ),
                "fuzzy": AgentToolParam(
                    "boolean",
                    "Whether keyword matching is fuzzy (contains-all) or "
                    "exact-any (for get_cookies, default: true).",
                ),
                "cookies": AgentToolParam(
                    "string",
                    "For set_cookies: a JSON array of cookie objects to write. "
                    "Each object: {name, value, domain?, path?, secure?, "
                    "http_only?, expires?}",
                ),
                "timeout": AgentToolParam(
                    "integer",
                    "Timeout in seconds for wait_for_dom_stable (default: 10)",
                ),
                "viewport_width": AgentToolParam(
                    "integer",
                    "Viewport width in CSS pixels for set_viewport (e.g. 1920)",
                ),
                "viewport_height": AgentToolParam(
                    "integer",
                    "Viewport height in CSS pixels for set_viewport (e.g. 1080)",
                ),
                "reset": AgentToolParam(
                    "boolean",
                    "For set_viewport: when true, clear the session-level "
                    "viewport override and fall back to the global browser "
                    "setting.",
                ),
            },
            required=["tool_title", "action"],
            property_ordering=[
                "tool_title",
                "action",
                "tab_id",
                "url",
                "selector",
                "text",
                "coordinate_x",
                "coordinate_y",
                "direction",
                "amount",
                "scroll_count",
                "item_selector",
                "script",
                "user_agent",
                "max_depth",
                "keywords",
                "fuzzy",
                "cookies",
                "timeout",
                "viewport_width",
                "viewport_height",
                "reset",
            ],
        )

    @staticmethod
    async def execute(
        args_json: str,
        session_id: str,
        _line_callback=None,
    ) -> ToolExecutionResult:
        """真正的分发：交给 ``tools/browser/driver.py``。

        失败必须**如实返回失败** —— 第一版桩返回 success=True，模型拿到成功信号
        就不重试、不绕路，用户只看到绿色工具卡（2026-10-06 实测，查了两轮才发现
        它从来没执行过任何动作）。重试风暴有 repeat_guard 兜着，假成功会一直骗下去。
        """
        try:
            args = json.loads(args_json)
        except ValueError as e:
            return ToolExecutionResult(
                f"Error: invalid JSON args: {e}", False, tool_title=BrowserUseTool.NAME
            )

        tool_title = str(args.get("tool_title", BrowserUseTool.NAME))
        from .browser import driver

        # 截图默认落在**会话工作区**里，而不是 POSIX 的 ``/tmp``。真机上踩出
        # 来两个坑（2026-10-08）：
        # 1. Windows 上 ``Path("/tmp/minis-shots")`` 是 ``\tmp\minis-shots``（盘根），
        #    跟 shell 认的 ``%TEMP%`` 根本不是一回事；
        # 2. 更要命的是**读不回来** —— read_image 只在会话工作区内解析路径，
        #    截图落在外面就永远 "Cannot resolve path"。截图工具不能看等于白截。
        # 模型显式给了 path 就照它的来（它可能有意放到别处）。
        if str(args.get("action") or "") == "screenshot" and not str(
            args.get("path") or ""
        ).strip():
            default_dir = _screenshot_dir(session_id)
            if default_dir is not None:
                args["path"] = str(default_dir)

        try:
            output = await driver.run(args)
        except driver.BrowserUnavailable as e:
            # 引擎起不来：把"为什么、怎么修"原样给模型和用户，别一句笼统错误。
            return ToolExecutionResult(
                f"浏览器引擎不可用：{e}", False, tool_title=tool_title
            )
        except Exception as e:
            return ToolExecutionResult(
                f"浏览器动作失败：{e}", False, tool_title=tool_title
            )
        return ToolExecutionResult(output, True, tool_title=tool_title)
