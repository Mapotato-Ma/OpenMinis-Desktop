# 01 · 一条命令的检查入口

Status: resolved
Type: task

## 做什么

`scripts/check.py`：把 pytest、前端回归检查、冒烟测试串成一个入口。

- `--fast` 跳过要起服务的冒烟测试
- `--only <name>` 只跑某一步；`--list` 列步骤
- 任何一步失败 → 非 0
- **跳过的步骤在总结里显式列出**（缺 node 时前端检查会跳过 —— 跳过不是通过）
- 子进程与自身都钉 utf-8：Windows 控制台默认 cp1252，中文输出会让 print 抛
  UnicodeEncodeError，而一次「通过」的断言就能把整个套件挂掉（v0.1.1 踩过）
- `PYTHONPATH` 带上仓库根，保证 `import openminis` 与在 CI 上一致

## 结果

已落地。验证：`python scripts/check.py --only smoke` → 21 项全过，21.1s。
新增检查 = 往 `STEPS` 里加一条声明，不需要再教任何人"该跑哪几个命令"。
