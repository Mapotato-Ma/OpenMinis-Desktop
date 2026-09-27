# Issue tracker: Local Markdown

Issue、spec、工单在本仓库里以 markdown 文件存在 `.scratch/` 下。

**为什么是本地而不是 GitHub Issues**：本机（iSH）没有 GitHub 凭据，
`gh` 未登录、`git credential fill` 也是空的。GitHub 路线一旦登录就能切回来
（改这个文件 + 跑一次 `/setup-matt-pocock-skills` 即可）。远端仓库仍然是
`github.com/Mapotato-Ma/OpenMinis-Desktop`，推送走 `scripts/push_via_api.py`。

## Conventions

- 一个特性一个目录：`.scratch/<feature-slug>/`
- spec 是 `.scratch/<feature-slug>/spec.md`
- 实现工单：一单一文件，`.scratch/<feature-slug>/issues/<NN>-<slug>.md`，从 `01` 编号，永远不要合并成一个 tickets 文件
- 状态记录为文件顶部附近的 `Status:` 行
- 评论与对话历史追加到文件底部 `## Comments` 标题下

## When a skill says "publish to the issue tracker"

在 `.scratch/<feature-slug>/` 下新建文件（需要就建目录）。

## When a skill says "fetch the relevant ticket"

读引用的那个路径的文件。用户通常会直接给路径或编号。

## Wayfinding operations

由 `/wayfinder` 使用。**map** 是一个文件，每个工单一个**子文件**。

- **Map**：`.scratch/<effort>/map.md`（Notes / Decisions-so-far / Fog 正文）
- **子工单**：`.scratch/<effort>/issues/NN-<slug>.md`，从 `01` 编号，问题写在正文里。`Type:` 行记录工单类型（`research`/`prototype`/`grilling`/`task`）；`Status:` 行记录 `claimed`/`resolved`
- **阻塞**：顶部附近的 `Blocked by: NN, NN` 行。它列出的每个文件都 `resolved` 后，本工单才解除阻塞
- **Frontier**：扫 `.scratch/<effort>/issues/`，找开放、未被阻塞、未被认领的文件；编号最小者优先
- **认领**：动手前先写 `Status: claimed` 并保存
- **解决**：把答案追加到 `## Answer` 标题下，写 `Status: resolved`，然后把一条上下文指针（要点 + 链接）追加到 `map.md` 的 Decisions-so-far
