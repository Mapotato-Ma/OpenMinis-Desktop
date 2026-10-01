# 架构说明

面向要改这份代码的人。想直接用，看 [README](../README.md) 就够了。

---

## 1. 进程模型

一个 exe，两个线程，一个 GUI 事件循环：

```
主线程                                boot 线程（非 daemon）
──────────────────────────────        ────────────────────────────────
pywebview GUI 事件循环                启动期：内核 import → uvicorn
  ├─ WebView2 (Windows)                 └─ openminis.server.main:app
  │    ├─ 先渲染自带 splash               （uvicorn 先跑完 lifespan 的
  │    └─ 就绪后 load_url 到 ────────────┐   文件 I/O 才 bind 端口）
  └─ http://127.0.0.1:8765/_desktop/ ←──┘
```

- 窗口加载的是 `http://127.0.0.1:<port>/_desktop/`，不是 `file://`。
  这样前端和内核同源，`fetch` / `WebSocket` 不需要任何 CORS 或代理配置。
- `--port` 默认 8765。端口被占则自动挑一个空闲端口（`find_free_port`）。
- 关窗口 → `webview.start()` 返回 → `server.shutdown()` 把 uvicorn 的
  `should_exit` 置位并 join，进程干净退出。

### 启动顺序是「窗口先行」（v0.3.1）

```
解包 exe → 建窗口（渲染自带 splash）→ ┬─ WebView2 初始化 ┐ 并行
                                      └─ 内核 import + uvicorn ┘
                                      → load_url 到真实界面
```

串行的老顺序（起内核 → 健康 → 才建窗口）会让用户盯着桌面等好几秒，见
`desktop/launcher.py` 的模块注释。三条约束：

- **boot 线程不是 daemon**：关窗时内核可能正写在盘上（会话库、JSON 配置），
  硬杀比多活几百毫秒危险得多。主线程关窗后 join 它，再收服务。
- **启动失败必须看得见**：错误通过 `window.__bootFail(...)` 写进窗口并指向
  `logs\desktop.log`（`web/desktop/splash.html` 提供这两个钩子，前端检查里钉住了
  「页面里的名字必须和壳层推的一致」）。
- **`OPENMINIS_NO_SPLASH=1`** 退回串行顺序：出问题时第一个该试的开关，也是量启动
  耗时的对照组。

启动耗时的证据链：`desktop/startup_trace.py` 每次启动往
`%USERPROFILE%\openminis\logs\startup.log` 追加**一行**（`origin=parent|self|python`
标明 0 点取自哪里 —— 从父进程时刻算起，才能把真正的启动开销算进去）。

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
所以 `desktop/ui_mount.py` 把桌面壳的全部路由插到路由表最前面：

```python
FIRST = 0
for route in reversed(routes):            # 倒着插 → 清单顺序即匹配优先级
    app.router.routes.insert(FIRST, route)
```

代价是依赖 Starlette 的路由表内部结构（`app.router.routes` 是普通 list）。
收益是 `src/` 一行不用改，上游更新可以直接 merge。这个取舍是有意的：
内核是别人在维护的，我们只是加一层壳。

> 兜底路由只对 `/api/` 和 `/ws` 开头返回 JSON 404，其余一律吐 SPA index。
> 所以自定义 API 必须插到前面，否则会被当成未知页面吞掉。

### 顺序是显式契约，不再是「碰巧成立」

以前「插到最前」这条规则散在四个 `attach_*` 函数里，每个各自 `insert(0)`；
清单内部的相对顺序则靠**调用顺序**碰巧满足。两个后果：

1. 新增一个接口的人得先读懂 Starlette 的匹配规则，否则症状是静默的 ——
   请求被兜底吞掉，返回首页 HTML 或 JSON 404；
2. `/_desktop/window-bootstrap.js` 是**磁盘上并不存在、由处理器现算**的路由，
   一旦排到 `Mount("/_desktop", StaticFiles(...))` 后面就会被静态挂载接走并 404
   （界面的「我在原生窗口里」标记静默失效）。

现在规则只有两条：**往 `desktop_routes()` 这个清单里加一项**，`attach()`
负责一次性前置安装，装完再自查**两条顺序不变量**并告警：

1. 没有路由排在内核**兜底路由**之后（否则永远不会被命中）；
2. 精确路径排在**会吞掉它的静态挂载**之前（排在后面就会被 Mount 接走）。

契约由 `desktop/tests/test_ui_mount.py` 钉住（20 项断言，含「自检真的能发现
被吞掉的路由」「认不出兜底路由时要叫出来而不是当成安全」）。

> 这两条不变量是**独立评审**逼出来的：我原来只查第 1 条，而第 2 条只写在测试里、
> 运行时不查 —— 清单被重排不会有任何告警。同一次评审还找到一个真 bug：
> 幂等分支返回的是「本次重算的值」而不是「首次的实际结论」，
> 于是「首次资源缺失返回 false、资源就位后再调一次会谎报 true」。
> 评审的原话有一句方向是反的（说「Mount 之前不该有同前缀的精确 Route」），
> 照它实现会让正常路径一直告警 —— **评审意见要自己核实**。

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
模型服务、人格、身份与工具、技能、Agent、关于。

**状态与规则在 `web/desktop/settings-model.js`**（一个不碰 DOM 的深模块，
渲染层只负责画和派发动作）：

```
load(server, prev)    → state     服务端载荷 → 新状态（带 baseline 用于脏判定）
reduce(state, action) → state     所有改动走这里，返回新对象
toPayload(state)      → {body, dirty}
view(state)           → 渲染层要的投影
```

它被拆出来的原因很实在：内核的 `/api/settings` 是**全量替换 PUT**，
于是「发什么」自带四条必须遵守的规则，以前它们散在 `app.js` 的 28 个变更点上，
靠一份全局可变草稿传递 —— 错一条就是「配置看着配好了、其实没生效」这种静默故障，
而两条真实事故（保存不生效、删服务商导致保存失败）都出在这里。

| 规则 | 说明 |
|---|---|
| 全量列表 | 每次提交都要发**完整**的服务商列表，不能只发被改的那个 |
| 密钥留空 = 不变 | `apiKey` 只在用户真的敲了时才带上；服务端本来就不把密钥发回来，客户端也从不搬运密文 |
| 槽位二选一 | 要么完整（`instanceId` + `model`），要么**显式 `null`** |
| 删服务商连带清槽位 | 指向被删实例的槽位必须一起清掉，否则服务端以「指向未配置的厂商」**整单拒绝** |

还有两条从事故里长出来的：

- **未保存状态是派生的**（草稿 vs 服务端值），不再手工 `markDirty`/`clearDirty`。
  所以「恢复推荐」是一个无操作、不会凭空空亮出「未保存」；身份卡片上的
  「未保存」标签也来自同一份判定。
- **添加服务商时若「对话」槽还空着就自动接上** —— 「加了服务商」和「对话能跑」
  在内核里是两件事，界面必须把前者接到后者上。零配置的新装用户第一眼看到的是
  「还缺 API Key」，而不是「还没有配置模型服务」。

这套东西有两层检查盯着：

```sh
cd web && npm run check:settings     # 18 项纯函数断言，不需要浏览器
node web/scripts/settings-e2e.check.mjs   # 对**真实内核**跑一遍四条规则（需要先起服务）
```

`settings-model.js` 整块包在 IIFE 里，只往外放一个 `window.SettingsModel`。
**桌面界面是没有构建步骤的多个 classic script，共享同一个全局作用域** ——
顶层多声明一个 `const`/`function` 就会和 `app.js` 撞名，而撞名的表现不是覆盖，
是 `SyntaxError`：整个 `app.js` 不执行，页面看起来"渲染出来了但什么都点不动"
（静态 HTML 还在，交互全死）。踩过一次，所以写在这里。

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

#### 未保存状态是必须可见的

所有面板改的都是同一份内存草稿，只有 `saveSettings()` 才把它发到服务端。
v0.1.0 的「模型服务」页**没有保存按钮**，于是出现了一个非常难查的死角：
卡片看起来配好了、用途绑定也选了，但 `activeProviderId` 从没被写进去，
会话永远回「还没有配置模型服务」—— 而用户去点「人格」的保存是白点的，
那个按钮走 `/api/system/soul`，跟服务商没有任何关系。

现在的规则：

- 任何改动都调 `markDirty(area)`，标题栏立刻出现「● 有未保存的改动」，
  并显示一个全局保存按钮；
- 关闭设置页（按钮或 Esc）时若有未保存改动会拦截确认；
- **添加服务商时若「对话」槽还是空的，自动把它设为当前** ——
  「加了服务商」和「对话能跑」在内核里是两件事，界面必须把前者接到后者上。

#### `activeProviderId` 的来路

这一点不看内核很容易做错：它**不是**从 providers 列表推导出来的，
而是由「用途绑定 → 对话」这一行写入的。`store.apply_full` 会把
`modelSlots.chat = {instanceId, model}` 翻译成
`activeProviderId = instanceId` 加上 `providers[instanceId].model = model`，
以此保证单一事实来源。所以界面上真正决定「会话用哪个模型」的是那个下拉框。

#### 测试连接是真的发请求

`POST /api/desktop/test-provider` `{id}` 会用和对话**完全相同**的
`build_provider` 路径发一次 `max_tokens=16` 的最小请求，所以它给出的结论
就是对话会遇到的结论。上游已有的 `/api/settings/fetch-models` 只探测
`GET {base}/models` —— 地址和密钥都对、模型 ID 写错时它依然是绿的，
这个盲区正好是用户最容易踩的一个。

失败会分类成能直接照做的建议：密钥被拒（401/403）、路径或模型不存在（404）、
限流额度（429）、连不上（DNS/TLS/连接）、超时、以及配置本身的问题
（没填 Key、引擎未移植）。探针**永远返回 200**，失败也是正常答案的一部分，
免得前端在解析错误体上再栽一次。

两个接口都读**已保存**的配置，所以界面上点「测试连接」会先保存再测 ——
测的和看到的是同一份东西。

配套的 `GET /api/desktop/chat-readiness` 是**「会话能不能跑」这件事的唯一规则**：
模型服务页顶部那条横幅就是它的渲染器（`SettingsModel.readinessBanner()` 把结论
翻译成标题/原话/下一步提示，纯函数、有 node 断言）。它自己只跑内核那几条 setup
守卫，所以契约测试把它和 `build_provider` 绑在一起
（`desktop/tests/test_provider_probe.py`）—— 它曾经对「引擎未移植」的服务商说
`ready: true`，也就是横幅说已就绪、第一条消息照样报错。**假绿比不提示更糟**，
所以这条不变量有测试盯着。

### 子进程不弹黑框（Windows）

`console=False` 打出来的 exe 是 **GUI 子系统**进程，自己没有控制台。这种进程
启动控制台子程序、又没带 `CREATE_NO_WINDOW` 时，Windows 会给子进程新分配一个
控制台 —— Windows 11 把「新建控制台」交给 Windows Terminal，于是用户看到一个
带标签栏的黑窗口。agent 每跑一条命令都要起一次 shell，所以是发一条消息弹一次。

内核对 Chrome 是处理了的（`chrome_launcher.py` 带 `CREATE_NO_WINDOW`），
跑 agent 命令的那条路径没有。`desktop/no_console.py` 在启动时补上：

- 只在**进程确实没有控制台**时才打补丁；有控制台时子进程会继承它，
  本来就不会弹窗，隐藏反而更难调试。
- 用 OR 合并而不是覆盖 —— `chrome_launcher` 等调用方已经设过的标志要保留。
- 覆盖范围包括 `asyncio.create_subprocess_*`：它走
  `asyncio.windows_utils.Popen`，那个子类 `super().__init__` 到
  `subprocess.Popen`，所以同一处补丁就够了。
  （顺带一提，Python 3.12 已经移除了那里的类级 `SW_HIDE`，
  所以终端抽屉和插件进程本来也会弹窗，一并修好。）
- 逃生门：`OPENMINIS_DESKTOP_SHOW_CONSOLE=1` 可关闭该补丁。
  需要它是因为 `CREATE_NO_WINDOW` 同时也会清掉子进程的**控制台句柄**
  （不只是窗口），极少数依赖真实控制台的 Windows 程序会因此异常。

冒烟测试会在 windows-latest 上真的验证这件事：把 `Popen.__init__` 换成一个
记录器、装上补丁、发起调用，断言标志确实被注入（普通调用与子类调用两条路径
都测），最后再真起一个子进程确认设了标志之后 stdio 依然正常。

### 主题

三态而不是两态：`system` / `light` / `dark`。默认 `system`，
监听 `prefers-color-scheme` 的 change 事件实时跟随 ——
用户不需要在系统换了主题之后再来点一次。

配色刻意不含绿色：主色是蓝（深色 `#5b8cff` / 浅色 `#2f6bdb`），
成功、在线状态、diff 新增块都用主色蓝，警告用琥珀、错误用红。
语法高亮的字符串用琥珀、类型名用蓝（原本是绿和青绿）。
`scripts/make_icon.py` 里的图标配色同步改成蓝，别只改 CSS。

### 界面缩放：先要原生的，拿不到才在页面里缩

缩放**优先走 WebView2 的原生 `ZoomFactor`**（`desktop/native_zoom.py`）：
它是引擎级缩放 —— CSS px 变大、布局视口变小，`vh` / `100%` / 媒体查询全部自洽，
文字按真实字号渲染。pywebview 6 的 Windows 后端**不给 `window.native` 赋值**
（只有 cocoa/gtk/qt/winforms 赋），所以壳层在建窗口之前把 `EdgeChrome.__init__`
包一层自己接住控件。**只有「设进去 + 读回来一致」才算成功**（不接受"可能生效"），
失败一律回落 CSS zoom —— 界面不会两头空。

回落路径是页面内的 `zoom`（`web/desktop/ui-prefs.js`），它有两个必须记住的坑：

1. **`vh` 不跟着 `zoom` 缩小**（Chromium 与 WebKit 实测一致）。所以根高度按
   「缩放前的视口」补偿：`--ui-h = innerHeight / zoom`，随缩放与窗口大小重算。
   不补的话 144% 下页面比视口高 44%，输入框一被聚焦浏览器就把根容器滚下去
   （用户看到的是「点一下整页上移、标题栏消失」）。
2. 根节点要 `overflow: clip` 而不是 `hidden` —— `hidden` 仍然能被
   `focus()` / `scrollIntoView()` 程序化滚动。

「设置 → 界面」里直接写出当前用的是哪种缩放（原生 / 界面内）以及回落原因，
`GET /api/desktop/zoom` 是同一份数据；CI 的打包探活会把它打进日志，
所以「Windows 上到底能不能原生缩放」不需要靠用户回报。

**别按印象包 `__init__`**：pywebview 6.2.1 的签名是
`EdgeChrome.__init__(self, form, window, cache_dir)`，包装器一律写
`(self, *args, **kwargs)` 原样透传。v0.3.5 就是按 `(self, window)` 包了一层，
`EdgeChrome(...)` 一调就 `TypeError`，窗口建不出来 —— **exe 双击毫无反应**
（窗口模式没有控制台，连报错都看不到）。我们自己的记账代码也必须整块吞异常。

---

## 4.5 网络来源标记（MOTW）：便携版在别人电脑上打不开的头号原因

**症状**：用户从 GitHub 下载 `OpenMinisDesktop-portable.zip`，解压、双击 exe →
SmartScreen 弹一次 → 点「仍要运行」→ **什么都没有**（任务管理器里也没有进程）。
第二次双击连 SmartScreen 都不弹，鼠标转两圈就没了。

**日志**（`%USERPROFILE%\openminis\logs\desktop.log`，无控制台版的 stderr 就重定向到
这里）：

```
File "webview\platforms\winforms.py", line 17, in <module>
    import clr
RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize
  from ...\_internal\pythonnet\runtime\Python.Runtime.dll
```

**机制**：Windows 给「从网络下载来的文件」写一个 `Zone.Identifier` 备用数据流（MOTW），
**而 Windows 自带的解压工具会把它传染给解压出来的每一个文件**。.NET 拒绝从带这个标记的
文件加载程序集（`HRESULT: 0x80131515`「不支持操作」）→ pythonnet 起不来 → pywebview 建不出
窗口 → `webview.start()` 抛异常 → 退出码 1。窗口版没有控制台，所以用户端只看到「双击没
反应」。

注意**不止一个文件**：只解除 `Python.Runtime.dll` 的话，错误会转移到
`Microsoft.Web.WebView2.Core.dll`（pywebview 自己的 .NET 桥）。所以要么整个文件夹一起
解除，要么一开始就用 7-Zip / Bandizip 解压（第三方解压工具不传染这个标记）。

**为什么 CI 抓不到**：CI 上所有文件都是**本机构建**的，天生没有标记；而真实用户的文件
**永远**来自浏览器下载。这是自动化流程和真实用户之间一个结构性的盲区，靠"多加断言"是
补不上的 —— 所以 `build-windows.yml` 里现在会**人为给产物打上标记**再启动一次。

**为什么当初是 onefile 的时候没这问题**：PyInstaller 的 onefile 每次启动把内容解到
`%TEMP%\_MEIxxxx`，那些文件是**当场写出来的**、没有标记；而 onedir（便携版）的文件就躺在
用户解压出来的目录里，带着下载时继承的标记。**换成便携版是 v0.4.5 的功能升级，副作用就是
这个** —— 所以 v0.4.6 的那次发布，等于把一个只在真实用户机器上才出现的问题带了出去。

**处理**（`desktop/motw.py`）：

* 在建窗口**之前**（也就是 `import webview` 之前）扫一遍安装目录，发现有带标记的文件就
  用 `user32.MessageBoxW` 弹窗问用户 —— 这个对话框不依赖 WebView2 也不依赖 pythonnet，
  所以在这条路上一定弹得出来；
* 用户点「是」才解除，`OPENMINIS_MOTW=unblock` 供 CI 无人值守，`ignore` 完全跳过；
* **刻意不做「静默解除」**：删除 MOTW 是攻击者的常用手法（MITRE T1553.005），偷偷做容易
  招杀软误报，也不尊重用户；
* 只在**打包版**里做（源码运行时 `sys.executable` 旁边是整个解释器目录，不该去动）；
* 只在**要开窗口**的那条路上做 —— `--no-window` 根本不加载 .NET，没有这个问题。

配套（`desktop/fatal.py`）：启动期任何没人接的异常都写日志**并弹原生对话框**，带上异常
类型、末尾几行堆栈和日志路径。同一个「双击没反应」的坑已经咬过三次（v0.3.5 的钩子签名、
v0.4.6 的 MOTW、以及任何一次窗口还没画出来就抛异常），三次都是因为**无控制台 = 什么都看
不见**。

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

打包只出**便携版（onedir）**：`dist\OpenMinisDesktop\`，压成
`OpenMinisDesktop-portable.zip` 发出去。

早期还发过一个 onefile 的单 exe，已经删掉 —— 它每次启动都要把载荷解到
`%TEMP%\_MEIxxxxx`（办公电脑上还要过一遍杀软实时扫描），而两个形态并存只会让人
不知道该下哪个。真要那份对比数据，去翻 v0.3.x 的 release。

`scripts/startup_probe.ps1`
在 CI 上量冷/热两次（`--no-window`，报"进程起来 → 端口通 → `/api/health` 200"），
真机上则看 `logs\startup.log` 那一行。

CI 里的验证步骤很关键：**GUI 构建没有控制台**，所以唯一的判据是
`Start-Process --no-window --port 8799` 之后 `/api/health` 是否 200、
`/_desktop/` 是否返回桌面页面。只看「构建成功」是不够的。

### 自己构建

```bat
git clone <this repo>
cd OpenMinisDesktop
build-desktop.bat
```

需要 Windows + Python 3.11+（`py -3` 或 `python` 在 PATH 里）。脚本会自己建 `.venv`、
装依赖、跑 PyInstaller、组装 `payload/`，产物在 `dist\OpenMinisDesktop\`。

### 从源码跑（任何平台）

```bash
pip install -e . "pywebview>=5.0"
python desktop_main.py                 # 原生窗口
python desktop_main.py --browser       # 没有 GUI 工具链时，退回浏览器标签页
python desktop_main.py --no-window     # 只跑后端（服务器 / 无头模式）
python desktop_main.py --upstream-ui   # 用上游移动端界面
```

| 参数 | 说明 |
|---|---|
| `--host` / `--port` | 绑定地址与端口，默认 `127.0.0.1:8765`；端口被占用时自动换一个 |
| `--browser` / `--no-window` | 浏览器标签页 / 只跑后端 |
| `--width` / `--height` | 初始窗口尺寸，默认 1440×900 |
| `--upstream-ui` | `/` 用上游移动端界面 |
| `--debug` | 打开 WebView devtools 与详细日志 |

### 验证

**唯一入口**是 `python scripts/check.py`（CI 跑的就是它），四步走完才算过：

| 步 | 内容 |
|---|---|
| pytest | `tests/` + `desktop/tests/` |
| lint | 静态检查未定义 / 重复定义的名字（ruff F821/F811） |
| frontend | `npm run check`（界面的几个一致性检查） |
| smoke | `scripts/smoke_test.py`，起真的 ASGI 栈 |

`--fast` 跳过冒烟测试。换平台之后这份基线必须重跑 —— 同一套测试在本地 POSIX 与
CI 的 Windows 上红的地方可以完全不同（见下面的验证记录）。

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
[ ok ] Build portable (onedir) app
[ ok ] Build the update payload (payload/)    ← 内核与界面单独装成一个可替换目录
[ ok ] Verify the payload actually shadows…   ← 断言内核真的从 payload\ 加载
[ ok ] Build the update manifest (latest.json)
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
