# 02 · 独立的验证流水线

Status: resolved
Type: task

## 做什么

新增 `.github/workflows/verify.yml` —— 与打包流水线分开的轻量检查。

不塞进 `build-windows.yml` 的原因：那条要 40 分钟（装依赖 + PyInstaller + 真启动 exe），
它没法承担"每次改动都跑"的角色。

- 触发路径覆盖 `src/** tests/** desktop/** web/** scripts/** pyproject.toml`
- `python-version: '3.12'` —— **与打包时用的解释器一致**。这一条本身就是踩坑：
  `.python-version` 写的是 3.13，开发者机器上绿的测试到了 3.12 才会发现用了新 API
  （见 ticket 03）。
- 加 `actions/setup-node`，让 `npm run check:ws` 真的跑得起来
- 跑 `python scripts/check.py --fast`

## 遗留

第一次落地时 `continue-on-error: true`（见 ticket 05）：先拿到 Windows 上的真实基线，
基线绿了再删掉那一行。**那之前它是信号，不是门禁。**

## 待验证

需要一次推送才能在 Windows runner 上看到结果 —— 本机没有 GitHub 凭据。
