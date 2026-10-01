# 07 · 前端横幅与 `/api/desktop/chat-readiness` 是同一件事的两份实现 —— 已收

Status: resolved
Type: task

## 做了什么

横幅不再自己判「有没有 model / 有没有 key」，而是**渲染服务端的结论**：

- `settings-model.js` 新增纯函数 `readinessBanner(readiness, {dirty, providerCount})`
  —— 把 `/api/desktop/chat-readiness` 的结论翻译成界面要画的东西
  （标题 / 服务端原话 / 下一步提示 / 未保存提示 / 要不要「设为当前对话」按钮）。
  规则是纯函数，7 项断言在 `web/scripts/settings-model.check.mjs` 里，不需要浏览器。
- `app.js` 的 `renderModelsHealth()` 改成问服务端 + 画 spec；
  用序号丢弃过期响应（并发请求不会画出旧结论）。
- 脏状态变化只重画那一句（`refreshHealthNote`），**不重新发请求** ——
  打字时不该每敲一个字发一个 HTTP。
- 新增 `web/scripts/banner-check.mjs`（2 项静态断言）防止有人再把判断搬回前端；
  接进 `npm run check`。

## 效果（浏览器里实测）

| 状态 | 横幅 |
|---|---|
| 服务商引擎未移植（本轮新修的假绿） | ⚠ 会话还不能用：当前服务商的引擎尚未移植 + 服务商原话 + 「换一个标着引擎就绪的」 |
| 又改了没保存 | 上面那段 + 「有未保存的改动 —— 保存之后这里会重新判定。」 |
| 保存后换成已就绪的引擎 | ✓ 当前对话：X / model（`notice ok`） |
| 服务商被删掉但 activeProviderId 还指着它（ticket 06 那个状态） | ⚠ 会话还不能用：当前服务商不见了 + 「在用途绑定里重新选一个，然后保存」 |
| 问不到服务端 | ⚠ 拿不到「会话能不能跑」的结论（**不假装就绪**） |

## 副作用（有意的取舍）

横幅说的是**已保存**的配置 —— 编辑未保存时它不会立刻变，
但会补一句「有未保存的改动」。这换来的是「一条事实只有一种说法」；
上一版是前端自己算，好处是立刻反映草稿，坏处就是它两次漏判（引擎那条两边都漏）。

ticket 06 的**用户可见部分**由这次一并解决（悬空 activeProviderId 现在有人话解释），
剩下的是上游那条「删除实例时应当清掉 activeProviderId」。
