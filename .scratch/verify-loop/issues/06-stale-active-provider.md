# 06 · 删掉服务商后，服务端的 activeProviderId 会留在原地

Status: open
Type: task
优先级：低（上游问题，界面侧已经堵住）

## 现象

跑 `web/scripts/settings-e2e.check.mjs` 时看到：把「当前对话」指向的服务商删掉、
槽位也已经按规则清空之后，`GET /api/settings` 里的 `activeProviderId`
仍然指着**已经不存在的实例 id**。

```
起点：0 个服务商，activeProviderId=e2e      ← 上一步删掉的实例
```

## 为什么暂时不算急

界面这边已经堵住了：`toPayload()` 会把悬空槽位发成 `null`，服务端接受；
`activeProviderId` 是服务端从 `modelSlots.chat` 推导出来的单向结果，
用户看不到它。真正会暴露它的路径是「服务端残留一个不存在的 id，
而对话服务拿它去 build_provider」—— 那时的报错比「还没有配置模型服务」更费解。

## 下一步

在 `chat-readiness`（`desktop/provider_probe.py`）里加一句：`activeProviderId`
指向的实例不存在时，直接说「当前对话指向的服务商已被删除，请重选一个」，
而不是把它当成配置完整。这是壳层能做的事，不用改内核。

顺带：如果哪天要提给上游，这是一条 ——「删除实例时应当同时清掉
`activeProviderId`」。
