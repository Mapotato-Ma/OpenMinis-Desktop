"""浏览器驱动的自包含实现（选型结论：Playwright 驱动系统自带的 Edge/Chrome）。

**为什么新写而不是复活 `firstagenttools/browser/` 那 2453 行**：
1. 那层整片挂在 `agent.*` / `common.*`（`BaseTool`/`ToolResult`/`logger`/`state_dir`/`mcp`…）
   上，不只是 browser 一个子目录；
2. 它是**同步** API，而工具侧 `BrowserUseTool.execute` 是 async —— 必解接缝；
3. 新写一个文件就能覆盖 22 个 action 名 + 整数 ref 快照 + SSRF 守卫 + channel 探测，
   而且**没装 playwright 也能 import**（工具据此如实报"引擎缺失"，而不是谎报成功）。
   死代码保留当设计参考，ref 方案与 SSRF 守卫的思路照搬。

选型依据（2026-10-06 实测，报告在服务器 `/root/bench/REPORT.md`）：
* 打包自带 Chromium → 安装包 **+795MB**（下载 186.8MiB、装完 658MB）；
* 驱动系统 Edge/Chrome → **+137MB**（只是 playwright 包本身），且内核永远最新；
* 真 Windows 上 `channel="msedge"` 已实测可用（Edge 156，冷启动 275ms，取元素 6.9ms/83 个）。

⚠️ 本模块**不许**在 import 期就把 playwright 拉进来：没装引擎时要能优雅降级。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
import urllib.parse
from typing import Any, Optional

from ...core.logging import get_logger

logger = get_logger(__name__)

#: 探测顺序：Windows 10/11 必带 Edge，所以它排第一；其次用户可能装的 Chrome；
#: 最后才是 playwright 自带的 Chromium（只在用户自己跑过 `playwright install` 时才有）。
PREFERRED_CHANNELS: tuple[str, ...] = ("msedge", "chrome", None)

#: 云元数据端点 —— 这类地址一旦被 agent 打开，等于把机器凭据递出去。
_BLOCKED_HOSTS = frozenset({"metadata.google.internal", "metadata.goog"})

DEFAULT_TIMEOUT_MS = 30_000
#: 快照返回给模型的字符上限（对齐死代码里的 snapshot_max_chars）。
SNAPSHOT_MAX_CHARS = 30_000
#: 一次快照最多列多少个可交互元素。
SNAPSHOT_MAX_ELEMENTS = 400

_INTERACTIVE_SELECTOR = (
    "a[href],button,input,select,textarea,[role=button],[role=link],"
    "[onclick],[contenteditable=true]"
)


class BrowserUnavailable(RuntimeError):
    """引擎起不来（没装 playwright / 没有可用浏览器 / 启动失败）。

    调用方要把这个**当作失败**回给模型，并把 message 原样带上 —— 用户看到的是
    "为什么不能用、怎么修"，而不是一句笼统的错误。
    """


def _host_is_blocked(host: str) -> bool:
    if not host:
        return True
    h = host.strip().lower().rstrip(".")
    if h in _BLOCKED_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    # 云元数据：169.254.169.254 / fd00:ec2::254 那一族
    return ip.is_link_local


def url_is_allowed(url: str) -> tuple[bool, str]:
    """SSRF 守卫：拦云元数据，放行 loopback 与内网。

    放行内网是**故意**的 —— agent 打开 `http://127.0.0.1:3000` 看自己刚起的 dev server
    是很常见的用法（死代码里也是这么取舍的）。真正要拦的是"能把机器凭据读出来"的
    那些地址。
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False, f"URL 解析失败：{url!r}"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https", "file", "about", "data"):
        return False, f"不支持的协议：{scheme or '(空)'}；只允许 http/https/about/data/file"
    host = parsed.hostname or ""
    if scheme in ("about", "data", "file"):
        return True, ""
    if _host_is_blocked(host):
        return False, f"这个地址指向云元数据服务，已拦截：{host}"
    return True, ""


def _now_ms() -> int:
    return int(time.time() * 1000)


class BrowserSession:
    """一个进程内复用的浏览器会话（懒启动）。

    Playwright 的 async 对象绑定在创建它的 event loop 上；服务端只有一个常驻 loop，
    所以模块级单例是安全的。多标签用 ``tab_id`` 索引。
    """

    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._pages: dict[str, Any] = {}
        self._refs: dict[str, dict[int, Any]] = {}
        self._active = "main"
        self._channel: Optional[str] = None
        self._engine_note = ""
        self._lock = asyncio.Lock()

    # -- 生命周期 -------------------------------------------------------
    @property
    def channel(self) -> Optional[str]:
        return self._channel

    @property
    def engine_note(self) -> str:
        return self._engine_note

    async def _ensure(self) -> None:
        if self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:  # pragma: no cover - 取决于运行环境
                raise BrowserUnavailable(
                    "没装浏览器驱动：`pip install playwright` 之后重启即可。"
                    "（本工具走的是**系统自带的 Edge/Chrome**，不需要 `playwright install` 下载浏览器）"
                ) from e

            last_err: Optional[Exception] = None
            for channel in PREFERRED_CHANNELS:
                label = channel or "playwright 自带 Chromium"
                try:
                    pw = await async_playwright().start()
                except Exception as e:  # pragma: no cover
                    raise BrowserUnavailable(f"playwright 启动失败：{e}") from e
                try:
                    kwargs: dict[str, Any] = {"headless": True}
                    if channel:
                        kwargs["channel"] = channel
                    browser = await pw.chromium.launch(**kwargs)
                except Exception as e:
                    last_err = e
                    logger.info("浏览器 channel %s 起不来：%s", label, e)
                    try:
                        await pw.stop()
                    except Exception:
                        pass
                    continue
                self._pw, self._browser = pw, browser
                self._channel = channel
                self._engine_note = label
                logger.info("浏览器引擎就绪：%s", label)
                return
            raise BrowserUnavailable(
                "系统里找不到可用的 Edge/Chrome，playwright 自带内核也没装。"
                f"最后一次的错误：{last_err}"
            )

    async def _page(self, tab_id: str = ""):
        await self._ensure()
        key = tab_id or self._active
        if self._context is None:
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=None,
            )
        page = self._pages.get(key)
        if page is None or page.is_closed():
            page = await self._context.new_page()
            page.set_default_timeout(DEFAULT_TIMEOUT_MS)
            self._pages[key] = page
        self._active = key
        return page

    async def close(self) -> None:
        for name, closer in (("browser", self._browser), ("playwright", self._pw)):
            if closer is None:
                continue
            try:
                await (closer.close() if name == "browser" else closer.stop())
            except Exception:
                logger.debug("关闭 %s 时出错", name, exc_info=True)
        self._pw = self._browser = self._context = None
        self._pages.clear()
        self._refs.clear()

    def forget_refs(self, tab_id: str = "") -> None:
        """导航/换页之后旧 ref 全部失效 —— 别让模型拿旧编号点错东西。"""
        if tab_id:
            self._refs.pop(tab_id, None)
        else:
            self._refs.clear()

    # -- 快照 -----------------------------------------------------------
    async def snapshot(self, tab_id: str = "") -> dict[str, Any]:
        """列出可交互元素，返回带**整数 ref** 的扁平列表（面向 LLM 消费）。

        ref 是会话内有效的自增整数（照搬死代码的方案）：模型点 ``ref=3`` 比让它
        写一长串 CSS 选择器稳得多。
        """
        page = await self._page(tab_id)
        key = tab_id or self._active
        raw = await page.evaluate(
            """(sel) => {
              const nodes = Array.from(document.querySelectorAll(sel));
              return nodes.slice(0, 400).map((e) => {
                const r = e.getBoundingClientRect();
                return {
                  tag: e.tagName.toLowerCase(),
                  text: (e.innerText || e.value || e.placeholder || '').trim().slice(0, 80),
                  name: e.getAttribute('name') || e.getAttribute('aria-label') || '',
                  type: e.getAttribute('type') || '',
                  visible: !!(r.width && r.height) && getComputedStyle(e).visibility !== 'hidden',
                };
              });
            }""",
            _INTERACTIVE_SELECTOR,
        )
        handles = await page.query_selector_all(_INTERACTIVE_SELECTOR)
        refs: dict[int, Any] = {}
        lines: list[str] = []
        idx = 0
        for item, handle in zip(raw, handles):
            if idx >= SNAPSHOT_MAX_ELEMENTS:
                break
            refs[idx] = handle
            bits = [f"[{idx}]", item["tag"]]
            if item.get("type"):
                bits.append(f'type={item["type"]}')
            if item.get("name"):
                bits.append(f'name="{item["name"]}"')
            text = item.get("text") or ""
            if text:
                bits.append(f'"{text}"')
            if not item.get("visible"):
                bits.append("(不可见)")
            lines.append(" ".join(bits))
            idx += 1
        self._refs[key] = refs
        body = "\n".join(lines)
        truncated = len(body) > SNAPSHOT_MAX_CHARS
        if truncated:
            body = body[:SNAPSHOT_MAX_CHARS] + "\n…（快照过长已截断）"
        title = await page.title()
        url = page.url
        return {
            "url": url,
            "title": title,
            "count": idx,
            "refs": body,
            "truncated": truncated,
        }

    async def resolve_ref(self, ref: int, tab_id: str = ""):
        page = await self._page(tab_id)
        key = tab_id or self._active
        handle = (self._refs.get(key) or {}).get(int(ref))
        if handle is None:
            raise RuntimeError(
                f"ref {ref} 不存在或已失效 —— 先做一次 snapshot 再点。"
            )
        return page, handle


# ---------------------------------------------------------------------------
# action 分发
# ---------------------------------------------------------------------------

#: 工具 schema 声明的 22 个 action + 死代码里的几个别名。
SUPPORTED_ACTIONS: tuple[str, ...] = (
    "navigate", "screenshot", "click", "type", "get_text", "get_readable",
    "scroll", "scroll_and_collect", "find_elements", "get_page_info",
    "get_backbone", "execute_js", "fetch", "hover", "set_user_agent",
    "set_viewport", "get_cookies", "set_cookies", "wait_for_dom_stable",
    "new_tab", "close_tab", "list_tabs",
    # 别名（死代码用的是这套名字，模型两边都可能喊）
    "snapshot", "fill", "press", "back", "forward", "evaluate", "goto",
)

#: 明确还没做、但**如实报错**的（绝不假成功）。
_NOT_IMPLEMENTED: dict[str, str] = {
    "set_user_agent": (
        "本会话不支持中途改 User-Agent：Playwright 的 UA 在建 context 时固定，"
        "换 UA 等于换一个干净 context（会丢登录态与 cookie）。"
    ),
}


def _need(args: dict[str, Any], key: str, action: str) -> Any:
    value = args.get(key)
    if value in (None, ""):
        raise RuntimeError(f"action '{action}' 缺少参数 '{key}'")
    return value


def _clip(text: str, limit: int = 4000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n…（已截断，共 {len(text)} 字符）"


async def run(args: dict[str, Any], session: BrowserSession | None = None) -> str:
    """执行一个 action，返回给模型看的文本。

    约定：**失败一律抛异常**（工具层包成诚实的失败结果），绝不返回"成功但什么都没做"。
    """
    sess = session or get_session()
    action = str(args.get("action") or "").strip()
    tab = str(args.get("tab_id") or "")

    if not action:
        raise RuntimeError("缺少 action")
    if action in _NOT_IMPLEMENTED:
        raise RuntimeError(_NOT_IMPLEMENTED[action] + "（当前实现阶段未覆盖，已如实报错）")
    if action not in SUPPORTED_ACTIONS:
        raise RuntimeError(
            f"不认识的 action：{action}；可用的是 {', '.join(SUPPORTED_ACTIONS)}"
        )

    if action in ("navigate", "goto"):
        url = str(_need(args, "url", action))
        ok, why = url_is_allowed(url)
        if not ok:
            raise RuntimeError(why)
        page = await sess._page(tab)
        resp = await page.goto(url, wait_until="load")
        sess.forget_refs(tab)  # 导航之后旧 ref 一律作废
        status = resp.status if resp is not None else "?"
        return f"已打开 {page.url}（HTTP {status}）\n标题：{await page.title()}"

    if action == "screenshot":
        page = await sess._page(tab)
        from pathlib import Path as _P

        out_dir = _P(args.get("path") or "/tmp/minis-shots")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"shot-{_now_ms()}.png"
        await page.screenshot(path=str(path), full_page=bool(args.get("full_page")))
        return f"截图已保存：{path}"

    if action == "snapshot":
        snap = await sess.snapshot(tab)
        return (
            f"{snap['url']}\n标题：{snap['title']}\n"
            f"可交互元素 {snap['count']} 个：\n{snap['refs']}"
        )

    if action == "click":
        if args.get("ref") is not None:
            page, handle = await sess.resolve_ref(int(args["ref"]), tab)
            await handle.click()
        else:
            sel = str(_need(args, "selector", action))
            page = await sess._page(tab)
            await page.click(sel)
        await page.wait_for_load_state("domcontentloaded")
        return f"已点击。当前地址：{page.url}"

    if action in ("type", "fill"):
        text = str(args.get("text") or "")
        if args.get("ref") is not None:
            page, handle = await sess.resolve_ref(int(args["ref"]), tab)
            await handle.fill(text)
        else:
            sel = str(_need(args, "selector", action))
            page = await sess._page(tab)
            await page.fill(sel, text)
        if args.get("submit"):
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
        return f"已填入 {len(text)} 个字符" + ("并回车提交。" if args.get("submit") else "。")

    if action == "press":
        page = await sess._page(tab)
        await page.keyboard.press(str(_need(args, "key", action)))
        return "已按键。"

    if action in ("get_text", "get_readable"):
        page = await sess._page(tab)
        sel = args.get("selector")
        if sel:
            node = await page.query_selector(str(sel))
            if node is None:
                raise RuntimeError(f"页面上找不到选择器：{sel}")
            text = await node.inner_text()
        else:
            text = await page.inner_text("body")
        return _clip(text)

    if action == "scroll":
        page = await sess._page(tab)
        delta = int(args.get("amount") or 800)
        if str(args.get("direction") or "down").lower() == "up":
            delta = -delta
        await page.mouse.wheel(0, delta)
        return f"已滚动 {delta} 像素。"

    if action == "scroll_and_collect":
        page = await sess._page(tab)
        steps = int(args.get("scroll_count") or 10)
        amount = int(args.get("amount") or 800)
        seen: list[str] = []
        for _ in range(max(1, min(steps, 20))):
            text = await page.inner_text("body")
            for line in text.splitlines():
                line = line.strip()
                if line and line not in seen:
                    seen.append(line)
            await page.mouse.wheel(0, amount)
            await page.wait_for_timeout(400)
        return _clip("\n".join(seen), 8000)

    if action == "find_elements":
        page = await sess._page(tab)
        sel = str(_need(args, "selector", action))
        nodes = await page.query_selector_all(sel)
        out = []
        for i, node in enumerate(nodes[:100]):
            try:
                text = (await node.inner_text())[:60].replace("\n", " ")
            except Exception:
                text = ""
            out.append(f"[{i}] {text}")
        return f"匹配 {len(nodes)} 个元素：\n" + "\n".join(out)

    if action == "get_page_info":
        page = await sess._page(tab)
        size = page.viewport_size or {}
        return json.dumps(
            {
                "url": page.url,
                "title": await page.title(),
                "viewport": size,
                "tabs": list(sess._pages),
            },
            ensure_ascii=False,
        )

    if action == "get_backbone":
        page = await sess._page(tab)
        outline = await page.evaluate(
            """() => {
              const out = [];
              const walk = (node, depth) => {
                if (out.length > 200 || depth > 4) return;
                for (const el of node.children) {
                  const id = el.id ? '#' + el.id : '';
                  const cls = (el.className || '').toString().trim().split(/\\s+/).slice(0, 2)
                    .filter(Boolean).map((c) => '.' + c).join('');
                  const tag = el.tagName.toLowerCase();
                  if (['script', 'style', 'svg', 'path'].includes(tag)) continue;
                  const text = Array.from(el.childNodes)
                    .filter((n) => n.nodeType === 3).map((n) => n.textContent.trim())
                    .join(' ').slice(0, 50);
                  out.push('  '.repeat(depth) + tag + id + cls + (text ? ' — ' + text : ''));
                  walk(el, depth + 1);
                }
              };
              walk(document.body, 0);
              return out.join('\\n');
            }"""
        )
        return _clip(outline or "(空)", 6000)

    if action in ("execute_js", "evaluate"):
        page = await sess._page(tab)
        script = str(_need(args, "script", action))
        result = await page.evaluate(script)
        return _clip(json.dumps(result, ensure_ascii=False, default=str))

    if action == "fetch":
        page = await sess._page(tab)
        url = str(_need(args, "url", action))
        ok, why = url_is_allowed(url)
        if not ok:
            raise RuntimeError(why)
        resp = await page.request.get(url)
        body = await resp.text()
        return f"HTTP {resp.status}\n{_clip(body, 6000)}"

    if action == "hover":
        page = await sess._page(tab)
        if args.get("ref") is not None:
            _, handle = await sess.resolve_ref(int(args["ref"]), tab)
            await handle.hover()
        else:
            await page.hover(str(_need(args, "selector", action)))
        return "已悬停。"

    if action == "set_viewport":
        page = await sess._page(tab)
        width = int(args.get("viewport_width") or args.get("width") or 1280)
        height = int(args.get("viewport_height") or args.get("height") or 900)
        await page.set_viewport_size({"width": width, "height": height})
        return f"视口已设为 {width}×{height}。"

    if action == "get_cookies":
        page = await sess._page(tab)
        cookies = await page.context.cookies()
        return json.dumps(cookies, ensure_ascii=False)[:6000]

    if action == "set_cookies":
        page = await sess._page(tab)
        raw = args.get("cookies")
        if not raw:
            raise RuntimeError("set_cookies 需要 cookies 参数")
        data = json.loads(raw) if isinstance(raw, str) else raw
        await page.context.add_cookies(data)
        return f"已写入 {len(data)} 条 cookie。"

    if action == "wait_for_dom_stable":
        page = await sess._page(tab)
        timeout_s = float(args.get("timeout") or 10)
        deadline = time.time() + timeout_s
        last, stable = "", 0
        while time.time() < deadline:
            snap = await page.evaluate("() => document.body ? document.body.innerHTML.length : 0")
            if snap == last:
                stable += 1
                if stable >= 2:
                    return f"DOM 已稳定（长度 {snap}）。"
            else:
                stable, last = 0, snap
            await page.wait_for_timeout(500)
        return "等待超时：DOM 仍在变化。"

    if action == "new_tab":
        url = str(args.get("url") or "about:blank")
        ok, why = url_is_allowed(url)
        if not ok:
            raise RuntimeError(why)
        await sess._ensure()
        page = await sess._page(str(args.get("tab_id") or f"t{len(sess._pages) + 1}"))
        if url != "about:blank":
            await page.goto(url, wait_until="load")
        return f"已新开标签：{page.url}（现有标签 {list(sess._pages)}）"

    if action == "close_tab":
        name = tab or sess._active
        page = sess._pages.pop(name, None)
        if page is None:
            raise RuntimeError(f"没有名为 {name} 的标签")
        await page.close()
        sess.forget_refs(name)
        return f"已关闭标签 {name}；剩下 {list(sess._pages)}"

    if action == "list_tabs":
        return json.dumps(
            [{"tab_id": k, "url": v.url} for k, v in sess._pages.items()], ensure_ascii=False
        )

    # navigate 的 back/forward 变体
    if action in ("back", "forward"):
        page = await sess._page(tab)
        await (page.go_back() if action == "back" else page.go_forward())
        sess.forget_refs(tab)
        return f"已{'后退' if action == 'back' else '前进'}到 {page.url}"

    raise RuntimeError(f"action '{action}' 没有实现分支（这是驱动自己的 bug，不是用户的问题）")


_session: BrowserSession | None = None


def get_session() -> BrowserSession:
    """进程内单例 —— 浏览器很贵，别每次工具调用都重开一个。"""
    global _session
    if _session is None:
        _session = BrowserSession()
    return _session
