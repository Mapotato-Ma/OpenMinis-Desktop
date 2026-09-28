# Domain Docs

工程类技能在探索本仓库时，应该如何消费这里的领域文档。

## Before exploring, read these

- **`CONTEXT.md`**（仓库根）—— 术语表。不存在时**静默跳过**，不要提示缺失、也不要建议先建它；
  由 `/domain-modeling` 在术语或决策真正定下来时按需创建。
- **`docs/adr/`** —— 动手前读与该区域相关的 ADR。目前有 `0001-desktop-ui-is-the-product-face.md`
  （两份前端谁是产品的脸）；其它决策按需新增，编号递增。
- 此外，本仓库已有的**面向人的架构说明**在 `docs/DESKTOP.md`（进程模型、为什么运行时挂载而不是改内核、
  前端协议、打包、验证记录、已知限制），`docs/PORTING.md` / `PORTING_MAP.md` 记录与上游的对应关系。
  探索之前先读 `docs/DESKTOP.md`，它是最省时间的一手材料。

## File structure

单上下文仓库（本仓库）：

```
/
├── CONTEXT.md          ← 按需创建
├── docs/adr/           ← 按需创建
├── docs/DESKTOP.md     ← 已有的架构说明（一手材料）
├── desktop/            ← 壳：窗口、运行时挂载、探针
├── web/desktop/        ← 桌面界面（无构建步骤）
└── src/openminis/      ← 上游内核（移植而来，不改）
```

## Use the glossary's vocabulary

输出里命名一个领域概念时（issue 标题、重构提案、假设、测试名），用 `CONTEXT.md` 里定义的词，
不要漂到术语表明文避开的同义词上。

需要的概念还不在术语表里，这本身是个信号：要么你在发明项目不用的语言（重新考虑），
要么这是个真实的缺口（记下来交给 `/domain-modeling`）。

## Flag ADR conflicts

如果输出与已有 ADR 冲突，显式指出而不是悄悄覆盖：

> _与 ADR-0007 冲突，但值得重开，因为……_
