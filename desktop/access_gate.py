"""桌面壳的本地服务访问闸门 [T-desktop-access-gate]。

## 为什么需要它（这是架构问题，不是"漏了个参数"）

内核原本是给**手机端**用的 —— 那时候唯一的客户端是 App 自己的 WebView，
服务绑在 loopback 上，而 loopback 上只有自己。桌面壳把同一个服务放到了
**用户自己的电脑**上，那里同时住着浏览器、别的程序、以及用户访问的每一个网站。
部署形态变了，威胁模型没跟着变。具体三条，都已逐条核实：

* 内核 CORS 是 ``allow_origins=["*"] + allow_credentials=True``，Starlette 会把
  请求的 Origin **原样回显**（``cors.py:163,176``）→ 任意网页都能**读到**本地 API
  的响应（会话内容、配置……）。
* 内核的访问闸门以 ``has_password()`` 为第一条件，**没设过密码就完全不拦**（默认态）。
* 全内核没有任何 Host / Origin 校验，而 ``/ws`` 是聊天通道 —— WebSocket
  **不受同源策略约束**，浏览器不挡跨站握手。

合起来：用户访问的任意网页，都能驱动这台机器上那个手里有 ``shell_execute``
的 agent。所以这道闸放在**壳这一层**，不改内核 —— ``attach()`` 里加的中间件是
**头插**的（``add_middleware`` 插到最前 = 最外层），天然排在内核 CORS 之前，
可以先拒后放。也不该去改内核的 CORS：再加一个 CORSMiddleware 会让
``Access-Control-Allow-Origin`` 发两遍，浏览器直接拒绝整个响应。

## 三道闸，互相独立

1. **Host** —— 只认自己的 loopback 地址。挡 DNS rebinding：攻击者把自己的域名
   解析到 127.0.0.1，请求就带着他的 Host / Origin 进来。
2. **Origin** —— 带了 Origin 且不是自己人，一律拒。挡跨站读取（含 WS 握手）。
3. **令牌** —— 每次启动随机生成，随窗口地址 ``?k=`` 交给 WebView，换成一个
   ``HttpOnly + SameSite=Strict`` 的 cookie。

第 3 条选 cookie 而不是"让前端到处带 header"是**刻意的**，两个理由：

* 同源请求浏览器自动带上 → **前端零改动**（所有 fetch / WebSocket 都不必动）；
* 跨站请求浏览器**不会**带 SameSite=Strict 的 cookie → 这正是我们要的语义，
  而且比"前端记得带 header"可靠得多（前端漏一处就是一个洞）。
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlencode

logger = logging.getLogger(__name__)

#: 窗口地址上的令牌参数名。窗口只能接手一个 URL，所以令牌只能这么递进去。
TOKEN_QUERY = "k"

#: 换到 cookie 之后就不再需要它了 —— HttpOnly 让页面脚本读不到，
#: SameSite=Strict 让它不随跨站请求发出。
COOKIE_NAME = "openminis_desktop_access"

#: 给命令行/脚本用的等价入口（CI 的启动探针走这里）。
HEADER_NAME = "x-minis-desktop-access"

#: 不需要令牌的路径。
#:
#: ``/api/health`` 只回答"活着吗"，**没有任何会话/文件内容**，但必须在拿不到
#: 令牌时也能问 —— CI 的启动探针、以及「端口被占时是不是我们自己的服务」都靠它。
#:
#: 注意它的响应里带 ``data_dir`` / ``workspace`` 绝对路径（会暴露本机用户名与
#: 目录结构）。那是**本机**信息泄漏（同机的其它进程本来也看得到），不是网页那条路：
#: 跨域读它会被上面的 Origin 闸拦掉（Origin 闸排在豁免判断**之前**，豁免不能当
#: 外泄通道）。复核要求把措辞写准，别让下一个人以为这条豁免完全无副作用。
EXEMPT_PATHS = ("/api/health",)

#: 需要令牌的路径。静态资源**故意不在内**：它们是界面自己的 JS/CSS，不含用户
#: 数据，而且必须在换到 cookie **之前**就能加载（splash 页就是先来的）。
PROTECTED_PATHS = ("/ws",)
PROTECTED_PREFIXES = ("/api/",)


def new_token() -> str:
    """每次进程启动生成一次。"""
    return secrets.token_urlsafe(32)


class Decision(NamedTuple):
    """一次判定的结果。``kind`` 只有三种，测试直接盯这三个字。"""

    kind: str  # "allow" | "redirect" | "deny"
    reason: str
    status: int = 200

    @property
    def allowed(self) -> bool:
        return self.kind != "deny"


class AccessGate:
    """判定逻辑（纯函数式，不碰 ASGI）—— 中间件只是它的壳。"""

    def __init__(self, token: str, host: str = "127.0.0.1", port: int = 0) -> None:
        if not token:
            raise ValueError("AccessGate 需要一个非空令牌")
        self.token = token
        self.host = host
        self.port = port
        #: ``Host`` 头的白名单。本机两种写法等价，都要认，否则用户用
        #: ``localhost`` 打开窗口就会 403。
        authorities = {f"{host}:{port}"}
        if host in {"127.0.0.1", "localhost"}:
            authorities |= {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.authorities = {a.lower() for a in authorities}
        self.origins = {f"http://{a}" for a in self.authorities}

    # -- 判定 ---------------------------------------------------------------
    def decide(
        self,
        *,
        path: str,
        query: str = "",
        host: str | None = None,
        origin: str | None = None,
        cookie_values: list[str] | None = None,
        header_token: str | None = None,
        fetch_site: str | None = None,
    ) -> Decision:
        # 闸 1：Host。缺 Host 不判（HTTP/1.0 客户端可能没有；浏览器一定有）。
        if host and host.lower() not in self.authorities:
            return Decision("deny", f"host 不在白名单：{host!r}", 403)

        # 闸 2：Origin。跨站请求一进来就死在这，连令牌都不用看 ——
        # 这样即使令牌将来出了别的岔子，跨站这条路也是封死的。
        if origin and origin.lower() not in self.origins:
            return Decision("deny", f"跨站 origin：{origin!r}", 403)

        # 闸 2b：Sec-Fetch-Site —— 浏览器自己声明这次请求的站内外关系。
        # **只认 same-origin / none，`same-site` 也要拒**：因为"站"不含端口，
        # 本机**另一个端口**上的页面与本服务是同站，那种请求会带上 cookie，
        # 而它未必带 Origin（`<img>`、表单 GET 就不带）。少了这道，攻击面就从
        # "任意网页"收窄到"本机另一个 loopback 页面"—— 而本机跑着别的 dev server
        # 是很常见的事（复核指出）。头不存在时不判：老浏览器与非浏览器客户端
        # 还是得过令牌闸。
        if fetch_site and fetch_site.lower() not in {"same-origin", "none"}:
            return Decision("deny", f"Sec-Fetch-Site={fetch_site!r}", 403)

        if path in EXEMPT_PATHS:
            return Decision("allow", "豁免路径（无用户数据）")

        # 闸 3：令牌。三个来源都认 —— cookie/header 是"已认证"的常规请求，
        # URL 上的 `?k=` 是窗口刚打开那一下。
        if self.token in (cookie_values or []) or header_token == self.token:
            return Decision("allow", "令牌有效")

        if parse_qs(query).get(TOKEN_QUERY, [""])[0] == self.token:
            # **任何路径**都要换 cookie，不只是受保护的路径。窗口加载的是
            # `/_desktop/`（界面本身，不属于 /api/*）—— 只在 /api/* 上换的话，
            # 窗口永远拿不到 cookie，之后每个 API 调用都被拒，**界面全白**。
            # （单元用例当时只喂了 /api/ 路径，是端到端演示把这个洞照出来的。）
            return Decision("redirect", "setup：用 URL 令牌换 cookie", 302)

        if not self._protected(path):
            return Decision("allow", "非受保护路径")

        return Decision("deny", "缺少或无效的访问令牌", 403)

    @staticmethod
    def _protected(path: str) -> bool:
        return path in PROTECTED_PATHS or path.startswith(PROTECTED_PREFIXES)

    # -- 中间件要的两样东西 -------------------------------------------------
    def cookie_header(self) -> bytes:
        return (
            f"{COOKIE_NAME}={self.token}; Path=/; HttpOnly; SameSite=Strict"
        ).encode("latin-1")

    def redirect_location(self, path: str, query: str) -> str:
        """把 ``k`` 摘掉的同一个地址（其余参数原样保留）。"""
        rest = {k: v for k, v in parse_qs(query).items() if k != TOKEN_QUERY}
        tail = urlencode(rest, doseq=True) if rest else ""
        return f"{path}?{tail}" if tail else path


class GateHolder:
    """可替换的闸门持有者。

    **为什么需要这一层**：内核 app 是**进程级单例**，而 ``attach()`` 在同一进程里
    会被调用多次（测试反复建 app、重启后端）。Starlette 装上去的中间件**卸载不掉**，
    所以中间件手里拿的必须是这个 holder 而不是闸门本身 —— 换令牌 = 换
    ``holder.gate``，测试要临时让它放行 = 置 ``None``。

    没有这一层的话，「测试里第一次 attach 装上了闸门」会一直生效，把同一进程里
    后面所有打 ``/api/*`` 的测试全变成 403 —— 一次改动炸掉几百个用例。
    """

    __slots__ = ("gate",)

    def __init__(self, gate: AccessGate | None = None) -> None:
        self.gate = gate


class DesktopAccessGate:
    """ASGI 中间件。``attach()`` 里装，天然排在内核 CORS 之前。"""

    def __init__(self, app: Any, holder: GateHolder) -> None:
        self.app = app
        self.holder = holder

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        gate = self.holder.gate
        kind = scope.get("type")
        if gate is None or kind not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in (scope.get("headers") or [])
        }
        verdict = gate.decide(
            path=scope.get("path", ""),
            query=(scope.get("query_string") or b"").decode("latin-1"),
            host=headers.get("host"),
            origin=headers.get("origin"),
            cookie_values=_cookie_values(headers.get("cookie")),
            header_token=headers.get(HEADER_NAME),
            fetch_site=headers.get("sec-fetch-site"),
        )

        if verdict.kind == "allow":
            await self.app(scope, receive, send)
            return

        if verdict.kind == "redirect":
            if kind == "http":
                location = gate.redirect_location(
                    scope.get("path", ""), (scope.get("query_string") or b"").decode("latin-1")
                )
                await send(
                    {
                        "type": "http.response.start",
                        "status": 302,
                        "headers": [
                            (b"location", location.encode("latin-1")),
                            (b"set-cookie", gate.cookie_header()),
                            (b"cache-control", b"no-store"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
            # WS 握不了手就没法重定向；令牌本身有效，放行。
            await self.app(scope, receive, send)
            return

        logger.warning(
            "desktop access gate denied %s %s — %s",
            kind,
            scope.get("path", ""),
            verdict.reason,
        )
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 4403})
            return
        await _send_json(send, verdict.status, {"detail": verdict.reason})


def _cookie_values(raw: str | None) -> list[str]:
    """取**全部**同名 cookie 的值。

    不能只看第一个：cookie 是按域名存的、**不分端口**，所以本机另一个端口上的
    页面用 ``document.cookie`` 就能往同一个域名塞一个同名 cookie（还能用更长的
    ``Path=/api`` 让它排在前面）。只认第一个的话，那一下就足够让界面整片 403
    —— 白送一个拒绝服务。这里改成"任何一个匹配即算通过"。（复核指出。）
    """
    if not raw:
        return []
    out: list[str] = []
    for chunk in raw.split(";"):
        name, _, value = chunk.strip().partition("=")
        if name == COOKIE_NAME:
            out.append(value)
    return out


async def _send_json(send: Any, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# 访问地址的发布
# ---------------------------------------------------------------------------
#: 令牌要能被**第二个实例**拿到（端口被占时它会复用第一个实例的服务，
#: 没有令牌的话那个窗口的界面会整片 403），也要能被 CI 的启动探针拿到。
#: 落在用户数据目录（``~/openminis``）—— 第二个实例解析出的是同一个路径。
ACCESS_FILE_NAME = ".desktop-access.json"


def access_file_path(data_root: Path) -> Path:
    return data_root / ACCESS_FILE_NAME


def publish_access_url(path: Path, url: str, token: str, port: int) -> None:
    """把带令牌的地址落盘。失败只记日志 —— 不能因为写不了它就不让用户开窗口。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"url": url, "token": token, "port": port}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("访问地址落盘失败：%s", path, exc_info=True)


def read_access_url(path: Path) -> str | None:
    """读回带令牌的地址；读不到/不合法就返回 None（调用方退回自己的令牌）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = data.get("url")
    return url if isinstance(url, str) and url else None


def token_for_port(path: Path, port: int) -> str | None:
    """按端口取回落盘的令牌。

    **端口对不上就不认** —— 那多半是上一次运行留下的陈迹，拿错了令牌只会得到
    一片 403，不如明确地取不到。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if int(data.get("port") or 0) != port:
        return None
    token = data.get("token")
    return token if isinstance(token, str) and token else None


def published_token(port: int) -> str | None:
    """``token_for_port`` 的便利版：自己解析数据目录，失败一律返回 None。"""
    from .paths import data_root  # noqa: PLC0415

    try:
        root = data_root()
    except Exception:  # pragma: no cover - 解析不出来就没得读
        return None
    return token_for_port(access_file_path(root), port)


def with_token(url: str, token: str) -> str:
    """给地址挂上 ``?k=``。已经是带参数的地址也能正确处理。"""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{TOKEN_QUERY}={token}"
