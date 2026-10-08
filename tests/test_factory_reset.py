"""还原出厂：清掉一切**运行期产生**的数据，保留用户配置。

判定标准（很强，不看"调用过什么函数"）：
* 造一批"跑出来的"文件 + 一批"配置"文件 → 还原 → **前者消失、后者还在**；
* 内置技能会自动装回来（技能库清空之后由 ``ensure_installed`` 补齐）；
* 报告逐项列出删了什么（界面要显示给用户看，不能只回一句"成功"）。
"""

from __future__ import annotations

import json

import pytest

from openminis.core.context import app_context


def _seed_runtime_data() -> None:
    """造一批运行数据 + 一批配置，用来验证只删前者。"""
    data = app_context().data_dir
    ctx = app_context()
    (data / "memory" / "daily").mkdir(parents=True, exist_ok=True)
    (data / "memory" / "daily" / "2026-01-01.md").write_text("日志", encoding="utf-8")
    (data / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    (data / "memory" / "long-term" / "LONG_TERM.md").write_text("长期", encoding="utf-8")
    (data / "memory" / "SOUL.md").write_text("人设：简洁", encoding="utf-8")
    (data / "knowledge").mkdir(parents=True, exist_ok=True)
    (data / "knowledge" / "note.md").write_text("知识", encoding="utf-8")
    (data / "scheduled").mkdir(parents=True, exist_ok=True)
    (data / "scheduled" / "tasks.json").write_text("[]", encoding="utf-8")
    (data / "guard_events.json").write_text("[]", encoding="utf-8")
    (data / "guard_allowlist.json").write_text("{}", encoding="utf-8")
    (data / "guard_mode.json").write_text(json.dumps({"mode": "danger"}), encoding="utf-8")
    (data / "settings.json").write_text("{}", encoding="utf-8")
    (ctx.cache_dir / "logs").mkdir(parents=True, exist_ok=True)
    (ctx.cache_dir / "logs" / "minis.log").write_text("日志", encoding="utf-8")
    (ctx.cache_dir / "tmp.bin").write_bytes(b"x" * 10)
    skill = data / "skills" / "my-skill"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: mine\n---\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_factory_reset_wipes_runtime_data_and_keeps_config(isolated_chat_db):
    from openminis.server import chat_store, workspaces
    from openminis.server import factory_reset as fr

    _seed_runtime_data()
    data = app_context().data_dir
    ctx = app_context()

    sess = await chat_store.create_session()
    await chat_store.append_turn(sess.id, "user", "你好")
    proj = data / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    ws = await workspaces.create_workspace("AI智控")
    workspaces.set_workspace_path(ws.id, str(proj))

    report = await fr.factory_reset()

    assert report["ok"] is True
    # ── 运行数据：没了 ──
    assert not (data / "memory" / "daily" / "2026-01-01.md").exists()
    assert not (data / "memory" / "long-term" / "LONG_TERM.md").exists()
    assert not (data / "knowledge" / "note.md").exists()
    assert not (data / "scheduled" / "tasks.json").exists()
    assert not (data / "guard_events.json").exists()
    assert not (data / "guard_allowlist.json").exists()
    assert not (data / "guard_mode.json").exists()
    assert not (data / "skills" / "my-skill").exists(), "用户装的技能要清掉"
    assert not (ctx.cache_dir / "tmp.bin").exists()
    assert await chat_store.list_sessions() == []
    assert await workspaces.list_workspaces() == [], "分组要清掉"

    # ── 配置：还在 ──
    assert (data / "memory" / "SOUL.md").is_file(), "人设属于配置，不能被清掉"
    assert (data / "settings.json").is_file(), "设置与密钥要保留"
    assert (ctx.cache_dir / "logs" / "minis.log").is_file(), "日志要保留（排障用）"

    # ── 报告 ──
    labels = [x["label"] for x in report["report"]]
    for want in ("会话与消息", "记忆", "知识库", "定时任务", "已安装技能",
                 "沙箱拦截记录与白名单", "缓存"):
        assert want in labels, f"报告里少了「{want}」"
    assert all(x["ok"] for x in report["report"]), report["report"]
    assert "模型服务与密钥" in report["kept"]


@pytest.mark.asyncio
async def test_factory_reset_restores_builtin_skills(isolated_chat_db):
    """技能库清空之后，内置技能必须自动装回来（否则"还原"会连能力一起删掉）。"""
    from openminis.server import factory_reset as fr
    from openminis.skills.store import SkillStore, configured_skills_root

    root = configured_skills_root()
    # 先确保内置技能已装（模拟用过一阵子的技能库）
    SkillStore().ensure_installed()
    assert any(root.iterdir()), "夹具前提不成立：内置技能没装出来"

    await fr.factory_reset()

    names = {p.name for p in root.iterdir()} if root.is_dir() else set()
    assert names, "技能库被清空后没把内置技能装回来"


@pytest.mark.asyncio
async def test_factory_reset_deletes_workspaces_but_not_their_folders(isolated_chat_db, tmp_path):
    """清分组 ≠ 删用户的真实目录 —— 项目文件夹必须原封不动。"""
    from openminis.server import workspaces
    from openminis.server import factory_reset as fr

    real = tmp_path / "my-project"
    real.mkdir()
    (real / "keep.txt").write_text("别删我", encoding="utf-8")
    ws = await workspaces.create_workspace("项目")
    workspaces.set_workspace_path(ws.id, str(real))

    await fr.factory_reset()

    assert (real / "keep.txt").read_text(encoding="utf-8") == "别删我"
    assert await workspaces.list_workspaces() == [], "分组要清掉"


def test_pending_reset_deletes_locked_files_on_next_start(tmp_path):
    """上次删不掉的（被占用）路径：记进 pending-reset.json，下次启动补删。

    Windows 上常见的失败模式（SQLite/日志句柄占用）—— 不能因为"删不掉"就
    谎报成功，也不能永远留下垃圾。
    """
    from openminis.server import factory_reset as fr

    data = app_context().data_dir
    victim = data / "leftover.db"
    victim.write_bytes(b"x")
    fr._schedule_pending([str(victim)])
    assert (data / fr.PENDING_FILE).is_file()

    removed = fr.run_pending_reset()

    assert str(victim) in removed
    assert not victim.exists()
    assert not (data / fr.PENDING_FILE).exists(), "删干净了就别再留标记"


def test_rest_endpoint_is_on_the_kernel_side(isolated_chat_db):
    """接口必须挂在内核（``/api/system/…``）而不是桌面壳里。

    ``desktop/ui_mount.py`` 参与**壳指纹**计算 —— 在那边加一行路由，所有用户
    都得重下 78MB 整包；放内核侧就走 2.4MB 载荷更新。这条钉住位置，防止
    以后有人"顺手"把它挪回桌面壳。
    """
    from fastapi.testclient import TestClient

    from openminis.server.main import app

    with TestClient(app) as c:
        r = c.post("/api/system/factory-reset")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["report"], "要回逐项报告"
        assert "模型服务与密钥" in body["kept"]
    # 顺带钉住：桌面壳里**不该**再有这个路由（否则指纹会变）
    import inspect

    from desktop import ui_mount

    assert "factory-reset" not in inspect.getsource(ui_mount)
