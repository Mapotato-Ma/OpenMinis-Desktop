# 架构说明

面向要改这份代码的人。想直接用，看 [README](../README.md) 就够了。

---

## 1. 进程模型

一个 exe，两个线程，一个 GUI 事件循环：

```
主线程                         后台线程（daemon）
──────────────────────────     ────────────────────────────────
pywebview GUI 事件循环          uvicorn (FastAPI + WebSocket)
  └─ WebView2 (Windows)           └─ openminis.server.main:app
       └─ http://127.0.0.1:8765/_desktop/   ← 同一个进程内
```

- 窗口加载的是 `http://127.0.0.1:<port>/_desktop/`，不是 `file://`。
  这样前端和内核同源，`fetch` / `WebSocket` 不需要任何 CORS 或代理配置。
- `--port` 默认 8765。端口被占则自动挑一个空闲端口（`find_free_port`）。
- 关窗口 → `webview.start()` 返回 → `server.shutdown()` 把 uvicorn 的
  `should_exit` 置位并 join，进程干净退出。

**单实例**：启动时先探测 `host:port` 上是否已有健康的内核，有就直接把新窗口
指过去。否则会出现两个内核、两套 SQLite 会话库，用户看到的是「会话丢了」。

---

## 2. 为什么是运行时挂载，而不是改内核

内核的 `src/openminis/server/main.py` 末尾注册了兜底路由：

```python
@app.get("/{full_path:path}")
async def spa_fallback(full_path: str): ...
```

Starlette 按注册顺序匹配，**任何 append 在它之后的路由都不会被命中**。
所以 `desktop/ui_mount.py` 全部用 `app.router.routes.insert(0, route)`：

```python
_insert_front(app, Mount("/_desktop", app=StaticFiles(directory=..., html=True)))
_insert_front(app, Route("/", _root_redirect))            # 307 -> /_desktop/
_insert_front(app, Route("/_desktop/", _desktop_index))
_insert_front(app, Route("/api/desktop/info", _info))
_insert_front(app, Route("/_desktop/window-bootstrap.js", _bootstrap))
```

代价是依赖 Starlette 的路由表内部结构（`app.router.routes` 是普通 list）。
收益是 `src/` 一行不用改，上游更新可以直接 merge。这个取舍是有意的：
内核是别人在维护的，我们只是加一层壳。

> 兜底路由只对 `/api/` 和 `/ws` 开头返回 JSON 404，其余一律吐 SPA index。
> 所以自定义 API 必须插到前面，否则会被当成未知页面吞掉。

---

## 3. 前端与内核的协议

前端**没有**复用上游的 `web/src/api.ts`，而是直接对接同一套接口 ——
接口是稳定的，构建步骤不是。

### WebSocket `/ws`

客户端 → 服务端：

| 帧 | 说明 |
|---|---|
| `{type:'chat', text, session_id?}` | 发起一轮对话；`session_id` 省略则新建 |
| `{type:'shell', command}` | 在内核的 shell 里跑一条命令 |
| `{type:'stop'}` | 中断当前生成 |
| `{type:'ping'}` | 心跳（25s 一次） |

服务端 → 客户端：

| 帧 | 字段 | 前端行为 |
|---|---|---|
| `delta` | `text`, `sessionId?` | 追加到当前文本块 |
| `toolStart` | `id`, `name`, `input` | 插一张折叠工具卡 |
| `toolEnd` | `id`, `ok`, `output`, `ms` | 结算卡片状态与输出 |
| `chatSession` | `sessionId`, `title` | 首次对话后回填会话 id |
| `done` | `sessionId` 或 `exitCode` | 结束本轮 / 结束终端命令 |
| `usage` | `totalTokens` | 更新状态栏 |
| `error` | `error`, `locked?` | 报错；`locked` 时提示去解锁 |

**一个坑**：`delta` 帧被对话和终端共用，唯一的区别是对话帧带 `sessionId`、
终端帧不带。前端靠 `state.termBusy && f.sessionId == null` 分流。
改协议时别把终端帧加上 `sessionId`。

### REST

| 用途 | 接口 |
|---|---|
| 会话列表 / 新建 / 删除 | `GET·POST /api/chats/sessions`、`DELETE /api/chats/sessions/{id}` |
| 历史消息 | `GET /api/chats/sessions/{id}/messages` |
| 文件树 | `GET /api/fs/tree?path=&depth=` |
| 读文件 | `GET /api/fs/read?path=&maxBytes=` |
| 技能 / 记忆 | `GET /api/skills`、`GET /api/system/memory` |
| 模型设置 | `GET·PUT /api/settings` |
| 桌面元信息 | `GET /api/desktop/info` |

历史消息里的 `runs` 字段是这一轮的工具调用记录（`{name,input,ok,output,ms}`），
刷新页面后工具卡片就是靠它画回来的 —— 不参与模型上下文。

---

## 4. 前端实现要点

单文件 `app.js`，无框架无构建。几个值得说明的地方：

- **流式渲染节流**：`delta` 每帧都来，重渲染 markdown 用 `requestAnimationFrame`
  合并，避免长回复卡死主线程。
- **工具调用打断文本块**：`toolStart` 会把当前文本块置空，之后的 `delta`
  开新块。这样卡片落在正确的时序位置上，而不是全堆在回复末尾。
- **`emptyNode()` 而不是 `$('emptyState')`**：空状态块是在对话区和消息列表
  之间**被搬来搬去**的同一个节点。一旦它被 `innerHTML=''` 摘掉，
  `getElementById` 就再也找不到它了 —— 所以脚本解析时就抓住引用。
- **diff 用 LCS**：`file_edit` 给的是 `old_string`/`new_string` 两段片段，
  直接「全删全加」很难看。行级最长公共子序列在几百行的量级上完全够快，
  超过 1500 行退化成长度直出。
- **语法高亮**：一个正则交替组一次扫完（注释 / 字符串 / 数字 / 标识符），
  按语言给关键字表。不追求完备，追求不卡。超过 3000 行不高亮。

### 设置页

`web/desktop/` 里的一个全屏 overlay，左侧导航 + 右侧面板：
模型服务、人格、技能、Agent、关于。

内核的 `/api/settings` 是**全量替换 PUT** —— 你发过去的列表就是新状态。
这带来两条必须遵守的规则：

1. 每次提交都要发**完整**的服务商列表，不能只发被改的那个；
2. 密钥留空表示「保持不变」（服务端本来就不会把密钥发回来，
   所以客户端也没法把它原样带回去）。这样客户端永远不需要搬运密文。

还有一个坑：删除服务商时必须同时清掉指向它的槽位绑定，
否则服务端会以「modelSlots.chat 指向未配置的厂商」**整单拒绝** ——
症状是「删掉一个没用的服务商，结果保存失败了」。

人格走 `/api/system/soul`，PUT 的 body 是 `{metadata:{name,emoji,icon,style,lang}, body}`。
服务端在写入时会做长度上限与提示注入检查，超限直接 400 并把原因回传。

身份与工具走 `identityEdits` + `activeIdentityId`，这里有个必须理解的语义：

```
enabledTools: null   -> 没有覆盖，回落到该身份的 recommendedTools
enabledTools: [...]  -> 显式覆盖，而空数组意味着「一个工具都不给」
```

API **没有清除覆盖的入口**，所以「恢复推荐」是显式把推荐列表写回去 ——
生效行为一样，只是存储上变成了一次覆盖。前端因此只把真正改动的身份放进
`identityEdits`，并且拿草稿和「服务端当前值」比对来判定是否算未保存，
避免点一下「恢复推荐」就亮出「未保存」。

还有三个工具的复选框是**不能关的**，界面上如实标了原因：

| 工具 | 为什么 |
|---|---|
| `skill_use` | 内核在 `chat_service` 里无条件补进工具列表（能力开关，不是角色工具） |
| `send` | 同上 —— 否则「有产物但发不出去」 |
| `subagent_delegate` | 由 Agent 面板的子代理开关控制，不归身份管 |

所以「全不选」保留这三个，否则界面会声称 0 个工具，而 agent 手里其实还有三个。

### 主题

三态而不是两态：`system` / `light` / `dark`。默认 `system`，
监听 `prefers-color-scheme` 的 change 事件实时跟随 ——
用户不需要在系统换了主题之后再来点一次。

配色刻意不含绿色：主色是蓝（深色 `#5b8cff` / 浅色 `#2f6bdb`），
成功、在线状态、diff 新增块都用主色蓝，警告用琥珀、错误用红。
语法高亮的字符串用琥珀、类型名用蓝（原本是绿和青绿）。
`scripts/make_icon.py` 里的图标配色同步改成蓝，别只改 CSS。

---

## 5. 打包

`packaging/OpenMinisDesktop.spec`。几个必须显式声明的东西，漏一个就是运行时
才炸：

| 项 | 为什么 |
|---|---|
| `aiosqlite` | SQLAlchemy 在建 engine 时才动态 import 这个 driver |
| `PIL.*` | `read_image` 用 Pillow 缩放，图片插件是懒加载的 |
| `uvicorn.loops.*` / `protocols.*` | uvicorn 按字符串名解析 loop / HTTP / lifespan 实现 |
| `openminis.plugins.drivers.*` | 插件通道驱动是 `import_module()` 动态导入 |
| `collect_all('webview')` | WebView2 走 `clr`/`pythonnet` 反射加载程序集，静态分析看不见；少了 `WebView2Loader.dll` 的表现是一个语焉不详的 COM 错误 |
| `web/desktop/**` | 桌面界面是数据不是代码 |

`console=False` 是默认值（真正的 GUI 应用行为），调试时设 `OPENMINIS_CONSOLE=1`。

CI 里的验证步骤很关键：**GUI 构建没有控制台**，所以唯一的判据是
`Start-Process --no-window --port 8799` 之后 `/api/health` 是否 200、
`/_desktop/` 是否返回桌面页面。只看「构建成功」是不够的。

---

## 6. 验证记录

在 Alpine/aarch64（iSH）上用 Python 3.12 实测通过：

**冒烟测试**（`python scripts/smoke_test.py`，14 项全过）

```
[PASS] web/desktop found
[PASS] GET /api/health -> 200
[PASS] GET / redirects to the desktop UI — 307 -> /_desktop/
[PASS] GET /_desktop/ -> 200
[PASS] desktop index served — 8487 bytes
[PASS] asset app.js served — status=200 bytes=50522
[PASS] asset style.css served — status=200 bytes=24971
[PASS] GET /api/desktop/info -> desktop mode
[PASS] desktop ui active — /_desktop
[PASS] GET /api/chats/sessions -> 200
[PASS] POST /api/chats/sessions creates a session
[PASS] new session is listed
[PASS] window bootstrap served
```

**端到端对话**（浏览器里跑真实 WebSocket 回合）

用一个本地 OpenAI 兼容桩服务（`scripts/stub_llm.py`）替代真实模型，
让整个链路不需要 API Key 就能验证：

1. 桩服务第一轮返回一个 `shell_execute` 的工具调用
2. 内核真的执行了 `echo stub-tool-ran && uname -s`
3. 桩服务第二轮返回流式文本

实测结果：工具卡片出现且状态为 `✓ 135ms`，流式文本逐段渲染，
markdown 生效，会话落库，刷新后从 `runs` 恢复卡片。

**其它已验证**：文件树按需展开、Python/JS 语法高亮、LCS diff 渲染
（2 删 / 6 增 / 1 上下文）、终端执行命令并回显、会话列表与相对时间。

**设置页写入路径实测**（浏览器里跑真实 PUT）：

- 原样往返保存后 `hasKey` 仍为 true —— 留空密钥不会抹掉已存的密文
- 新增同类型的第二个实例 → 保存 → 删除 → 保存，槽位绑定没有留下悬空引用
  （`chat -> stub` 始终有效）
- 人格保存后 `metadata.style` 生效且正文完好；技能启停生效
- 改完设置后重新跑一轮对话，工具卡片与流式文本照常 —— 没有回归

**主题**：深色 / 浅色都截图确认；全量扫描 `style.css` / `app.js` / `index.html`
的所有十六进制与 `rgb()` 颜色，确认**没有任何绿色**（判定：绿通道同时比
红、蓝高出 24 以上）。图标按相同标准复查。

**未验证**：Windows 上的实际窗口与 `.exe` 产物（需要在 Windows runner 上跑，
即 CI 的职责）。窗口层代码在无 GUI 环境下会优雅退回浏览器标签页。

**CI 上验证通过**（windows-latest，`v0.1.0`）：

```
[ ok ] Install dependencies
[ ok ] Verify the runtime dependencies the kernel needs
[ ok ] Smoke-test the desktop shell          ← 14 项全过
[ ok ] Generate icon
[ ok ] Build onefile executable              ← 37.3 MB
[ ok ] Verify the executable starts          ← 真的启动 exe，等 /api/health 200，
                                                 再确认 /_desktop/ 返回桌面界面
[ ok ] Upload artifact
[ ok ] Attach to release
```

CI 抓到并修掉的两个真实问题，都值得记一笔：

1. **无控制台构建启动即崩**。`console=False` 的 exe 在 Windows 上没有附加
   控制台，`sys.stdout` / `sys.stderr` 都是 `None`，而内核的 `setup_logging()`
   会建一个写 stdout 的 rich handler —— 启动期第一次写日志就
   `AttributeError`，进程在绑定端口之前就死了。修复见 `desktop/stdio.py`。
   *这个 bug 只在 GUI 构建里出现，源码跑永远看不到。*

2. **Windows 上缺 `greenlet`**。上游声明的是 `sqlalchemy>=2.0`，而 greenlet
   在 SQLAlchemy 元数据里是按 `platform_machine` 标记条件安装的，
   Windows 上没匹配上 —— 但内核的 DB 层用的是 `sqlalchemy.ext.asyncio`，
   导入即炸。改成 `sqlalchemy[asyncio]>=2.0`。

---

## 7. 已知限制

- 桌面界面是只读的代码查看器，编辑能力全部来自 agent（没有编辑器保存）
- 设置页覆盖了模型服务 / 人格 / 技能 / Agent 参数；身份（identity）与
  单个工具的开关还没做进去
- 会话过滤是前端 substring，不是全文检索
- 没有多标签；一次只能看一个会话
- WebView2 在很老的 Win10 上需要手动装运行时
