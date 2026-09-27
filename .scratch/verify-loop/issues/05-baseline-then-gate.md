# 05 · 基线绿了之后，把验证从信号变成门禁

Status: open
Type: task
Blocked by: 03, 04

## 做什么

`verify.yml` 里那一行 `continue-on-error: true` 删掉。

它不是偷懒的产物，是刻意的第一步：先让流水线在 Windows 上**跑起来并看到真实基线**，
再决定门禁。在没有基线的情况下直接开门禁，结果只会是"红的流水线被无视"，
那比没有流水线更糟。

## 判据

- Windows 上 `python scripts/check.py --fast` 全绿（或只剩明确 skip 的步骤）
- 顺手把 `build-windows.yml` 的 paths 也覆盖 `src/**` —— 内核改动会影响发出去的 exe，
  只是那一步要付 40 分钟打包的代价，想清楚再动

## 注意

`AGENTS.md` 里写的是「`scripts/check.py` 是唯一的验证入口」，门禁落地前这句话
在 CI 上还不成立（CI 目前只跑 smoke）。别让文档比现实超前太多 —— 要么推进入、
要么改文档。
