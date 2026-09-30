"""访问闸门 [T-desktop-access-gate]。

这组用例断言的是**口子关着**，不是"功能能用" —— 所以每条都能反向自检：
把对应的那道闸去掉，它就红。

背景（完整推导见 `desktop/access_gate.py` 开头）：内核的本地服务原本对**任何**
本机客户端敞开 —— CORS 是 `allow_origins=["*"]` 且会把请求 Origin 原样回显，
访问闸门又以"设过密码"为第一条件（默认没设 → 完全不拦），而 `/ws` 是聊天通道。
桌面壳把同一个服务放到了用户自己的电脑上，那里住着浏览器和用户访问的每个网站，
于是"任意网页都能驱动本机这个手里有 shell 的 agent"。
"""

from __future__ import annotations

import asyncio

import pytest

from desktop.access_gate import (
    COOKIE_NAME,
    AccessGate,
    DesktopAccessGate,
    GateHolder,
    access_file_path,
    new_token,
    publish_access_url,
    read_access_url,
    token_for_port,
    with_token,
)

HOST = "127.0.0.1:8799"
TOKEN = "tok-abc"


@pytest.fixture
def gate() -> AccessGate:
    return AccessGate(TOKEN, "127.0.0.1", 8799)


# ---------------------------------------------------------------------------
# 判定逻辑
# ---------------------------------------------------------------------------
def test_no_token_is_denied(gate):
    """最要紧的一条：什么凭据都不带，打不了 /api/。"""
    d = gate.decide(path="/api/chats/sessions", host=HOST)
    assert d.kind == "deny" and d.status == 403


def test_wrong_token_is_denied(gate):
    assert gate.decide(path="/api/chats/sessions", host=HOST, cookie_values=["nope"]).kind == "deny"
    assert gate.decide(path="/api/chats/sessions", host=HOST, header_token="nope").kind == "deny"


def test_cookie_token_is_allowed(gate):
    d = gate.decide(path="/api/chats/sessions", host=HOST, cookie_values=[TOKEN])
    assert d.kind == "allow"


def test_header_token_is_allowed(gate):
    d = gate.decide(path="/api/chats/sessions", host=HOST, header_token=TOKEN)
    assert d.kind == "allow"


def test_query_token_redirects_and_sets_a_cookie(gate):
    """窗口地址上的 ?k= 只用来换 cookie —— 换完就该从地址里消失。"""
    d = gate.decide(path="/api/chats/sessions", query=f"k={TOKEN}&other=1", host=HOST)
    assert d.kind == "redirect" and d.status == 302
    assert gate.redirect_location("/api/chats/sessions", f"k={TOKEN}&other=1") == (
        "/api/chats/sessions?other=1"
    )
    cookie = gate.cookie_header().decode()
    assert f"{COOKIE_NAME}={TOKEN}" in cookie
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_query_token_is_exchanged_on_the_ui_path_too(gate):
    """窗口加载的是 `/_desktop/`（不是 /api/*）—— 那条路上也必须换到 cookie。

    只在受保护路径上换 cookie，窗口就永远拿不到它，之后每个 API 调用都被拒，
    **界面全白**。这条是端到端演示抓出来的洞，单元用例当时只喂了 /api/ 路径。
    """
    d = gate.decide(path="/_desktop/", query=f"k={TOKEN}", host=HOST)
    assert d.kind == "redirect", "界面路径上不换 cookie 的话窗口就废了"
    # 只是"没带令牌的界面路径"仍然放行（静态资源不含用户数据）
    assert gate.decide(path="/_desktop/", host=HOST).kind == "allow"


def test_websocket_with_a_query_token_is_allowed(gate):
    """WS 没法重定向 —— 令牌有效就该放行，别把 302 当成拒绝。"""
    assert gate.decide(path="/ws", query=f"k={TOKEN}", host=HOST).kind == "redirect"


def test_sec_fetch_site_blocks_same_site_requests(gate):
    """本机**另一个端口**上的页面 = 同站跨源（"站"不含端口）→ 也要拒。

    那种请求会带上 cookie（SameSite 按站算），而 `<img>` / 表单 GET **不带
    Origin**，所以只靠 Origin 闸挡不住。少了这道，攻击面就从"任意网页"收窄到
    "本机另一个 loopback 页面"—— 而本机跑着别的 dev server 是很常见的事。
    """
    for site in ("same-site", "cross-site"):
        d = gate.decide(
            path="/api/chats/sessions", host=HOST,
            cookie_values=[TOKEN], fetch_site=site,
        )
        assert d.kind == "deny", site
    # 自己人（页内 fetch）与"用户直接打开"（导航）都放行；头不存在时不判
    for site in ("same-origin", "none", None):
        assert gate.decide(
            path="/api/chats/sessions", host=HOST,
            cookie_values=[TOKEN], fetch_site=site,
        ).kind == "allow", site


def test_any_matching_cookie_counts_not_just_the_first(gate):
    """cookie 按域名存、**不分端口**：同机另一个页面能塞一个同名 cookie 进来。

    只认第一个的话，那一下就足够让界面整片 403 —— 白送一个拒绝服务。
    """
    assert gate.decide(
        path="/api/chats/sessions", host=HOST,
        cookie_values=["evil", TOKEN],
    ).kind == "allow"


def test_cross_site_origin_is_denied_even_with_a_valid_token(gate):
    """跨站请求一进来就死 —— 哪怕它手里有令牌。

    这条是"纵深"：将来令牌万一从别的地方漏了，跨站这条路仍然是封死的。
    """
    d = gate.decide(
        path="/api/chats/sessions", host=HOST, cookie_values=[TOKEN], origin="https://evil.example"
    )
    assert d.kind == "deny"


def test_host_outside_the_whitelist_is_denied(gate):
    """DNS rebinding：攻击者把自己的域名解析到 127.0.0.1，Host 会露出来。"""
    d = gate.decide(path="/api/chats/sessions", host="evil.example:8799", cookie_values=[TOKEN])
    assert d.kind == "deny"


def test_localhost_host_is_accepted(gate):
    """本机两种写法等价 —— 只认 127.0.0.1 的话，用 localhost 打开就 403。"""
    assert gate.decide(path="/api/health", host="localhost:8799").kind == "allow"
    assert gate.decide(path="/api/chats/sessions", host="localhost:8799", cookie_values=[TOKEN]).kind == "allow"


def test_health_is_exempt(gate):
    """`/api/health` 只回答"活着吗"，必须能被拿不到令牌的外部工具问到 ——
    CI 的启动探针、以及"端口被占时是不是我们自己的服务"都靠它。"""
    assert gate.decide(path="/api/health", host=HOST).kind == "allow"


def test_static_assets_are_not_gated(gate):
    """界面自己的 JS/CSS 不含用户数据，而且必须在换到 cookie **之前**就能加载。"""
    assert gate.decide(path="/_desktop/app.js", host=HOST).kind == "allow"
    assert gate.decide(path="/", host=HOST).kind == "allow"


def test_websocket_is_gated(gate):
    assert gate.decide(path="/ws", host=HOST).kind == "deny"
    assert gate.decide(path="/ws", host=HOST, cookie_values=[TOKEN]).kind == "allow"


# ---------------------------------------------------------------------------
# 中间件（直接喂 ASGI scope，最精确）
# ---------------------------------------------------------------------------
def _scope(kind="http", path="/api/chats/sessions", query="", headers=()):
    return {
        "type": kind,
        "path": path,
        "query_string": query.encode(),
        "headers": [(b"host", HOST.encode()), *headers],
    }


async def _drive(mw, scope):
    """跑一次中间件，返回 (收到的消息, 内层 app 是否被调用)。"""
    sent: list[dict] = []
    seen = {"inner": False}

    async def inner(_scope, _receive, _send):
        seen["inner"] = True

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await DesktopAccessGate(inner, GateHolder(AccessGate(TOKEN, "127.0.0.1", 8799)))(
        scope, receive, send
    )
    return sent, seen["inner"]


def test_middleware_denies_without_touching_the_app():
    sent, inner = asyncio.run(_drive(None, _scope()))
    assert sent[0]["status"] == 403
    assert inner is False, "被拒的请求不该进到应用里"


def test_middleware_lets_a_cookie_through():
    sent, inner = asyncio.run(
        _drive(None, _scope(headers=[(b"cookie", f"{COOKIE_NAME}={TOKEN}".encode())]))
    )
    assert inner is True and sent == []


def test_middleware_redirects_a_query_token():
    sent, inner = asyncio.run(_drive(None, _scope(query=f"k={TOKEN}")))
    assert sent[0]["status"] == 302
    headers = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
    assert headers["location"] == "/api/chats/sessions"
    assert f"{COOKIE_NAME}={TOKEN}" in headers["set-cookie"]
    assert inner is False, "换 cookie 这一步不该顺手把请求放进去"


def test_middleware_rejects_a_cross_site_websocket_before_accept():
    """WS 不受同源策略约束 —— 浏览器不挡跨站握手，所以必须由服务端挡。"""
    sent, inner = asyncio.run(
        _drive(
            None,
            _scope(
                kind="websocket",
                path="/ws",
                # **必须带有效令牌**：不然拒它的是令牌闸，这条用例就测不到 Origin
                # 了（反向自检正是这么发现第一版是"假绿"的）。
                headers=[
                    (b"origin", b"https://evil.example"),
                    (b"cookie", f"{COOKIE_NAME}={TOKEN}".encode()),
                ],
            ),
        )
    )
    assert inner is False
    assert sent and sent[0]["type"] == "websocket.close"


def test_middleware_lets_a_websocket_through_on_a_query_token():
    """WS 不能被 302 掉（握不了手），令牌有效就放行。"""
    sent, inner = asyncio.run(
        _drive(None, _scope(kind="websocket", path="/ws", query=f"k={TOKEN}"))
    )
    assert inner is True and sent == []


def test_middleware_passes_through_when_no_gate_is_installed():
    """闸门没装（holder 为空）时必须原样放行。

    内核 app 是进程级单例、中间件装上了卸载不掉 —— 没有这条"空持有者即放行"，
    一个用例装上闸门就会把同进程后面所有打 /api/* 的用例全变成 403。
    """
    sent: list[dict] = []
    seen = {"inner": False}

    async def inner(_scope, _receive, _send):
        seen["inner"] = True

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        DesktopAccessGate(inner, GateHolder(None))(_scope(), receive, send)
    )
    assert seen["inner"] is True and sent == []


# ---------------------------------------------------------------------------
# 装到真实 app 上（这条走完整栈，能顺带证明我们的中间件排在内核 CORS 之前）
# ---------------------------------------------------------------------------
def test_real_app_gate_and_no_cors_hole():
    import httpx

    from desktop.server_runner import build_app

    app = build_app(desktop_dir=None, ui_active=False, access_token=TOKEN, access_port=8799)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8799") as c:
            anon = await c.get("/api/chats/sessions")
            health = await c.get("/api/health")
            cross = await c.get(
                "/api/chats/sessions",
                # 带**有效令牌**再叠跨站 Origin：能拒它的只剩 Origin 闸。
                headers={
                    "Origin": "https://evil.example",
                    "x-minis-desktop-access": TOKEN,
                },
            )
            authed = await c.get(
                "/api/chats/sessions", headers={"x-minis-desktop-access": TOKEN}
            )
        return anon, health, cross, authed

    anon, health, cross, authed = asyncio.run(run())

    assert anon.status_code == 403
    assert health.status_code == 200
    assert cross.status_code == 403
    # 内核给我们回显 Origin 的那个头**一个都不该出现** —— 出现了就说明
    # 闸门排到了内核 CORS 后面（或者根本没生效），浏览器就能读到响应。
    assert "access-control-allow-origin" not in {k.lower() for k in cross.headers}
    assert authed.status_code in (200, 404)  # 404 = 内核那侧没有这个会话库，没关系
    assert authed.status_code != 403


# ---------------------------------------------------------------------------
# 访问地址的落盘（第二个实例 / CI 探针要用）
# ---------------------------------------------------------------------------
def test_publish_and_read_back(tmp_path):
    path = access_file_path(tmp_path)
    publish_access_url(path, "http://127.0.0.1:8799", TOKEN, 8799)

    assert read_access_url(path) == "http://127.0.0.1:8799"
    assert token_for_port(path, 8799) == TOKEN
    # 端口对不上就不认 —— 那是上一次运行留下的陈迹，拿错了只会得到一片 403。
    assert token_for_port(path, 1111) is None


def test_read_back_is_forgiving(tmp_path):
    path = access_file_path(tmp_path)
    assert read_access_url(path) is None  # 文件不存在
    path.write_text("{ not json", encoding="utf-8")
    assert read_access_url(path) is None and token_for_port(path, 8799) is None


def test_with_token_handles_an_existing_query():
    assert with_token("http://h:1/_desktop/", "t") == "http://h:1/_desktop/?k=t"
    assert with_token("http://h:1/x?y=2", "t") == "http://h:1/x?y=2&k=t"


def test_tokens_are_random():
    assert new_token() != new_token()
    assert len(new_token()) >= 32


def test_access_gate_refuses_an_empty_token():
    """空令牌 = 没有闸门。宁可当场炸，也别悄悄放行一切。"""
    with pytest.raises(ValueError):
        AccessGate("", "127.0.0.1", 8799)
