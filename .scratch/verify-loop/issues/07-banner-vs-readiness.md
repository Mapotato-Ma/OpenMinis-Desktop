# 07 · 前端横幅与 `/api/desktop/chat-readiness` 是同一件事的两份实现

Status: open
Type: task

## 现象

「会话能不能跑」这个问题有两份实现，而且**都没检查引擎是否移植**：

- 服务端 `provider_probe.chat_readiness()` —— 契约测试（ticket 09 那次）
  刚抓到它对「引擎未移植」的服务商说 `ready: true`，而 `build_provider` 在同
  一个配置上抛 `ChatSetupError` —— 已经修了。
- 前端 `renderModelsHealth()` —— 自己判 `!p.model` / `!p.hasKey`，
  **不查引擎**，也不查服务端已经知道的其他原因。

更别扭的是：**前端根本没调用那个接口**（`app.js` 里搜不到 `chat-readiness`），
它只在 `smoke_test.py` 里被访问过。也就是说服务端那条是**没人用的第三条路径**。

## 该怎么做

让横幅成为服务端结论的渲染器，而不是另一份规则：

1. `renderModelsHealth()` 改成读 `/api/desktop/chat-readiness`；
2. `reason === 'no_active_provider'` 时才显示「把第一个服务商设为当前对话」那个按钮；
3. 横幅的文案直接用服务端给的 `message`（同一条事实只有一种说法）。

顺带把「引擎未移植」这条也变可见 —— 现在它只在服务商卡片的角标上，
聊天横幅不说话。

## 判据

`grep -rn "chat-readiness" web/desktop/app.js` 有命中，且横幅在
「引擎未移植」的服务商上会警告（可以拿 `scripts/stub_llm.py` 之外的办法构造：
把服务商类型设成 gemini 即可）。
