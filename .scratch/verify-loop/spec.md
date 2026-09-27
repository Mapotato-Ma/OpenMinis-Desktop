# Spec：让「改完怎么知道没弄坏」有一条命令

Status: done
来源：架构调研候选 C2（`/var/minis/workspace/architecture-review-20260927-204044.html`）
基线：commit `0ac4dae`

## 问题

这个仓库目前没有任何可信的验证回路：

- CI（`build-windows.yml`）的 `paths` 只覆盖 `desktop/** web/desktop/** scripts/** spec`，
  **改 `src/`、`tests/`、`web/src/` 不触发任何流水线**。
- 仓库里有 758 个测试（45 个文件、12,970 行），**CI 从来没跑过 pytest**。
- 前端唯一的回归脚本 `web/scripts/ws-reconnect.check.ts`（钉死过一个真 bug）有
  `npm run check:ws` 入口，但 CI 里没有 node 步骤、README 也没提 —— 没人执行。
- 于是所有行为验证都落在 `docs/DESKTOP.md` 的「验证记录」里，也就是当时那一次人肉操作。

重构要做的第一件事不是改结构，而是先有一个「红了就说明真坏了」的回路。

## 目标

1. **一条命令**能跑完这个仓库的全部检查（本地与 CI 跑的是同一条）。
2. CI 在 `src/`、`tests/`、`web/` 改动时**会**跑这条命令。
3. 跳过某一步时必须**显式说出来**（跳过不是通过）。

## 不在范围内

- 修所有红的测试（那是 ticket 03 / 04）。
- 把验证做成门禁（基线绿了之后是 ticket 05）。
- 动 `src/` 的重构本身。

## 验收标准

- `python scripts/check.py` 一条命令覆盖 pytest + 前端检查 + 冒烟测试，退出码可信。
- 缺 node 的机器上前端检查被**明确标为跳过**，而不是静默通过。
- CI 上有一条独立的验证流水线，与 40 分钟的打包流水线分开。
- 第一次跑出来的红项被记录成工单，而不是被"已知问题"糊过去。

## 落点

- `scripts/check.py`
- `.github/workflows/verify.yml`
- `AGENTS.md` 的「验证」一节指向这条命令

---

## 结果（2026-09-27）

- `scripts/check.py` 是唯一入口，本地与 CI 跑同一条命令。
- `.github/workflows/verify.yml` 在每次分支推送时跑，Windows 上 **758 passed**，
  前端回归检查（`ws-reconnect.check.ts`，此前从未被执行过）**2.4s 通过**。
- 第一次跑起来的红项全部收干净：4 个产品 bug（`src/`，六处偏离记在 NOTICE.md）、
  3 个用例前提问题。本地剩 2 项按平台 skip。
- 下一步交给 C1：把 `web/desktop/app.js` 的设置状态抽成深模块。
