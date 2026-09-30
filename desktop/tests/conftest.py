"""桌面壳自身的测试（与上游 `tests/` 分开：这里的代码是桌面版新增的）。

隔离方式与 `tests/conftest.py` 保持一致 —— ``context.app_context()`` 是进程级
单例，不隔离就会落到用户真实的 ``~/openminis``。
"""

from __future__ import annotations

import pytest

from openminis.core import context


@pytest.fixture(autouse=True)
def isolated_app_context(tmp_path, monkeypatch):
    home = tmp_path / "minis-home"
    home.mkdir()
    monkeypatch.setenv("MINIS_HOME", str(home))
    context.set_app_context(context.AppContext(data_dir=home, cache_dir=tmp_path / "minis-cache"))
    yield
    context.reset_app_context()


@pytest.fixture(autouse=True)
def close_desktop_gate_afterwards():
    """每个用例跑完都把访问闸门放行。

    内核 app 是**进程级单例**，而 pytest 把 `tests/` 与 `desktop/tests/` 跑在同一个
    进程里。装了闸门之后中间件卸载不掉，所以只要有一个用例 `attach()` 带上令牌，
    同进程后面所有打 `/api/*` 的用例都会 403。这里统一在用例结束后清掉
    （`GateHolder.gate = None` 即放行，见 desktop/access_gate.py）。
    """
    yield
    from openminis.server.main import app  # noqa: PLC0415

    holder = getattr(app.state, "desktop_access_gate", None)
    if holder is not None:
        holder.gate = None
