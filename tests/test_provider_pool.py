"""R14：provider 的连接池**由缓存拥有**，回合只借用。

改前的形状是"谁拿到谁关"：``_run_chat`` 与 ``run_subagent`` 的 finally 里各自
``aclose()`` 它们借来的 provider —— 而那个 provider 是 ``_PROVIDER_CACHE`` 里的
共享实例（同一个配置指纹复用同一个 HTTP 池）。于是：

* 同时跑的两轮里，一轮结束会把另一轮在途的池关掉（隔离探针：A aclose 之后
  B 借到的 client 已经 ``is_closed=True``）；
* 顺序的两轮也各自重建池，"缓存"只剩省一次 provider 构造，池子白缓存了。
"""
from __future__ import annotations

import inspect

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from openminis.core import context

    monkeypatch.setenv("MINIS_HOME", str(tmp_path))
    context.set_app_context(context.AppContext(data_dir=tmp_path, cache_dir=tmp_path))
    yield tmp_path
    context._context = None  # type: ignore[attr-defined]


def test_a_shared_provider_is_reused_across_borrows(home):
    """同一个配置指纹 → 同一个实例（池子活着，缓存才有意义）。"""
    from openminis.settings.chat_service import (
        build_provider,
        close_provider_cache,
        reset_provider_cache,
    )

    reset_provider_cache()
    conf = {"id": "gw", "type": "openAI", "apiKey": "sk-1", "baseUrl": "",
            "model": "gpt-4o-mini"}
    first = build_provider("gw", conf)
    assert build_provider("gw", conf) is first
    # 借用人（回合）不该关它 —— 这里模拟"用完不关"，池子必须还在
    assert getattr(first._client, "is_closed", True) is False or first._client is None  # noqa: SLF001
    close_provider_cache  # noqa: B018 - 文档性引用，真正关闭在下面那条用例里测
    reset_provider_cache()


async def test_only_the_cache_closes_the_pool(home):
    """关池子的动作只在缓存这一侧：关掉之后连接是闭的、再借是新的实例。"""
    from openminis.settings.chat_service import (
        build_provider,
        close_provider_cache,
        reset_provider_cache,
    )

    reset_provider_cache()
    conf = {"id": "gw", "type": "openAI", "apiKey": "sk-1", "baseUrl": "",
            "model": "gpt-4o-mini"}
    provider = build_provider("gw", conf)
    client = provider._client_instance()          # noqa: SLF001 - 真的开一个池
    assert client.is_closed is False

    await close_provider_cache()

    assert client.is_closed is True, "缓存关闭时没有把池关掉"
    assert build_provider("gw", conf) is not provider, "关掉之后还从缓存里拿旧实例"


async def test_retired_providers_are_closed_too(home):
    """被设置变更换下来的 provider 也要在关机时关掉（否则池子只等 GC）。"""
    from openminis.settings.chat_service import (
        build_provider,
        close_provider_cache,
        reset_provider_cache,
    )

    reset_provider_cache()
    conf = {"id": "gw", "type": "openAI", "apiKey": "sk-1", "baseUrl": "",
            "model": "gpt-4o-mini"}
    provider = build_provider("gw", conf)
    client = provider._client_instance()          # noqa: SLF001

    reset_provider_cache()                        # 设置变了 → 换下来
    assert client.is_closed is False, "换下来不等于立刻关（那要 await，调用方是同步的）"

    await close_provider_cache()

    assert client.is_closed is True, "被换下来的池子没人关"


def test_turn_paths_never_close_the_shared_provider():
    """回归护栏：回合路径里不许再出现 ``aclose``。

    这是 R14 的**行为**所在 —— 只要有人在回合结尾写回一句 ``await provider.aclose()``，
    上面那几条用例都还是绿的（它们测的是缓存），但"共享池被单轮关掉"就回来了。
    """
    from openminis.agent import subagents
    from openminis.server import main as server_main

    for fn in (server_main._run_chat, subagents.run_subagent, subagents.plan_subagent):
        # 去掉注释再看：那几段注释正是在解释"为什么不许在这里关"（会提到 aclose）。
        code = "\n".join(
            line for line in inspect.getsource(fn).splitlines()
            if not line.strip().startswith("#")
        )
        assert "aclose" not in code, (
            f"{fn.__name__} 又在回合结束时关共享 provider 了（R14）"
        )
