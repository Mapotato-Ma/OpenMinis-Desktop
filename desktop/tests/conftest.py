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
