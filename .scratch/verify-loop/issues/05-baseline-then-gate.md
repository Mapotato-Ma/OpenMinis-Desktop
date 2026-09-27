# 05 · 把验证从信号变成门禁 —— 已收

Status: resolved
Type: task

原计划是「先 informational（`continue-on-error`），基线绿了再转门禁」。
实际执行时改成了**一开始就当门禁**，理由是：

> 没有 token 时，`continue-on-error` 会把失败步骤的 conclusion 也报成 `success`，
> 于是从 Actions API 根本看不出红不红 —— 那条流水线就白跑了。

门禁的表现符合预期：前两次运行红（红在接线、在用例的时钟赛跑），修完立刻绿。

## 最终状态（两条基线都实测）

| 平台 | 结果 |
|---|---|
| Windows（CI, Python 3.12） | **758 passed**，0 failed；前端回归检查 2.4s 通过 |
| POSIX（本地 iSH, Python 3.12） | 756 passed，**2 skipped**（Windows 专有断言） |

## 还没做的

`build-windows.yml` 的 paths 仍然没覆盖 `src/**` —— 内核改动会影响发出去的 exe，
但那一步要付 40 分钟打包的代价。现在内核改动至少有 `verify.yml` 兜着（50 秒），
所以这件事不再紧急。真要做的话建议加 `dorny/paths-filter` 让 build 只在
`src/**`、`desktop/**`、`packaging/**` 变更时才跑。
