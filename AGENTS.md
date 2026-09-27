# AGENTS.md

OpenMinis Desktop：把 OpenMinis 内核装进一个 Windows 原生窗口（pywebview + 运行时挂载）。
面向人的架构说明在 [`docs/DESKTOP.md`](docs/DESKTOP.md)。

## 硬规矩

- **`src/` 是从上游移植的内核，一行都不改。** 壳层要加的东西一律加在 `desktop/`。
  这条是项目存在的理由（上游更新能直接 merge），破了它整个项目就没意义了。
- 壳层通过 `desktop/ui_mount.py` 在**运行时**把路由插到内核实例上，不改内核文件。
- 界面在 `web/desktop/`（无框架、无构建步骤）；`web/src` + `web/dist` 是上游移动端 UI。
- 密钥、token 一律不进仓库、不进日志。

## 验证

```sh
python scripts/check.py          # 一条命令：pytest + 前端检查 + 冒烟测试
python scripts/check.py --fast   # 跳过冒烟测试（它要起服务）
python scripts/smoke_test.py     # 只跑桌面壳的冒烟测试
pytest tests/ -q                 # 只跑内核测试套件
cd web && npm run check:ws       # 只跑前端回归脚本（需要 node）
```

`scripts/check.py` 是唯一的验证入口：CI 跑的就是它，本地也跑它。

## Agent skills

### Issue tracker

Issue / spec / 工单以 markdown 文件存在 `.scratch/<feature>/` 下（本地 markdown，不经 GitHub）。见 `docs/agents/issue-tracker.md`。

### Domain docs

单上下文：`CONTEXT.md`（按需创建）+ `docs/adr/`（按需创建）；已有的一手架构材料在 `docs/DESKTOP.md`。见 `docs/agents/domain.md`。
