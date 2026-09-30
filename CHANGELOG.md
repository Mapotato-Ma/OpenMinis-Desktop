# 变更日志

## v0.4.5 — 架构第 2 步：把「运行时」与「载荷」拆开，更新从 37.9 MB 变成 2.3 MB（2026-09-30）

### 问题：改内核一行 = 重发整个 37.9 MB

内核的 `.pyc` 是被**打进 exe 里**的（CI 日志实证：`Building PYZ (ZlibArchive)`
→ `Building PKG (CArchive)` → `EXE`）。所以哪怕只改一行内核代码，也要重新打包
那个 37.9 MB 的 exe。

而天天变的东西其实很小：

| 会变的部分 | 源码 | 压缩后 |
|---|---|---|
| `src/openminis`（内核，278 个 .py） | 2.8 MB | |
| `web/desktop`（界面，其中 vendor 1.6 MB） | 1.9 MB | |
| **合计** | **5.0 MB** | **~1.7 MB** |

占大头的解释器 + 依赖（~30 MB）基本不动。

### 修法：exe 旁边放一个可整体替换的 `payload/`

`desktop/paths.py` 新增 `payload_root()` / `install_payload_path()`：把 exe 旁边的
`payload/` 插到 `sys.path` **最前面**，于是它盖过 bundle 里冻住的那一份内核与界面。
引导脚本 `desktop_main.py` 与 `desktop/app.py` 在 import 任何业务模块**之前**调用它。

**刻意的边界：壳与运行时留在 exe 里，只有内核与界面走载荷。** 因为壳里有引导模块
（`paths` / `stdio` / `startup_trace`）—— 能决定"去哪儿找载荷"的代码不能由载荷自己
提供。半个桌面包能从载荷覆盖、半个不能，那种版本错配比"壳改完要重发 exe"难查得多。
而且**没有 `payload/` 时一切照旧**（落回 bundle），所以老安装包不会被这个机制弄坏。

新增 `scripts/make_payload.py` 组装载荷，产出：

* `payload/` 目录（放在 exe 旁边）—— 实测 **845 个文件 / 5.59 MB**；
* `OpenMinisDesktop-update.zip` —— **2.33 MB**，对照整包 **37.9 MB**（**16 倍**）；
* `payload.json` 清单：`kernelVersion` / `shellVersion` / `python` / 文件数 /
  `treeSha256`。更新器拿 `shellVersion` 比对 —— 壳换了就必须走整包装，没换就能
  只换载荷。

### 两个实测出来的关键取舍

1. **发源码 + 预编译字节码，而不是只发一种。** 只发源码时，更新后**第一次**启动
   要多花约 **0.19 秒**现编译（实测：冷 1.00s / 热 0.81s），而且只在安装目录**可写**
   时才缓存得下来 —— Program Files 那种只读安装就每次启动都付。
2. **必须用 `--invalidation-mode checked-hash`（PEP 552）编译。** 默认的 mtime
   校验模式下，.pyc 打包再解压（时间戳全变）就**失效重编译**；checked-hash 按源码
   **哈希**校验，实测解压后原样复用，而且**仍然能发现源码内容变了** —— 万一只换了
   一半文件，不会静默跑旧字节码。

   （顺带记一笔坑：别去读 .pyc 头里的 flag 位判断模式 —— 本机 3.12 上 checked-hash
   编出来的 flag 读出来像 unchecked。**以行为为准**。我差点照 flag 编号把它"修"错。）

### 可观察性：更新到底生效没有

`/api/desktop/info` 多了三个字段：`payloadDir` / `kernelFrom` / `desktopFrom`。
载荷没被用上时表现是"改的东西没反应"，没有这几个字段只能靠猜 —— 这个项目已经吃过
一次"WebView 跑旧界面"的亏。

### 护栏（都做过反向自检：撤回修复必红）

- `desktop/tests/test_payload.py` —— 8 项。核心那条走**子进程**验 import 优先级
  （进程内 `sys.modules` 已缓存 `openminis`，改 `sys.path` 是换不掉的，那正是这个
  机制最容易"看起来对了其实没生效"的地方）：伪造一个 PyInstaller 布局，断言内核与
  界面**都从载荷加载**；没有载荷时断言**不是**从载荷加载、且**一行都不插手**。
  另有一条钉住 `make_payload.py` 的载荷清单里**不含 `desktop/`**。
- CI（portable 任务）加两步：构建载荷 + **断言 `kernelFrom` 指向 payload\\**
  （载荷漏了或用不上必须让流水线红，不能等用户报"更新后没反应"）。

### 验证

服务器 `python scripts/check.py` 四步全绿：**901 passed / 2 skipped**。
版本号三处同步 0.4.5；NOTICE 偏离表不动（本次仍未改 `src/`）。

## v0.4.4 — 本地服务加访问闸门（桌面壳不该把本机 agent 交给任意网页）（2026-09-30）

这是一次**架构层面**的修补，不是补个参数。

### 问题：部署形态变了，威胁模型没跟着变

内核原本是给手机端用的 —— 那时唯一的客户端是 App 自己的 WebView，服务绑在
loopback 上，而 loopback 上只有自己。桌面壳把同一个服务放到**用户自己的电脑**上，
那里同时住着浏览器、别的程序、以及用户访问的每一个网站。三条都已逐条核实：

| 项 | 位置 | 事实 |
|---|---|---|
| CORS | `src/openminis/server/main.py:187` | `allow_origins=["*"]` + `allow_credentials=True`；Starlette 会把请求 Origin **原样回显**（`cors.py:163,176`）|
| 访问闸门 | `src/openminis/server/main.py:216` | 以 `has_password()` 为第一条件 → **没设过密码就完全不拦**（默认态）|
| 来源校验 | 全 `src/openminis/server/` | 除上面那个 CORS 中间件外，**没有任何** Host / Origin 校验 |
| 聊天通道 | `src/openminis/server/main.py:794` | `/ws` —— WebSocket **不受同源策略约束**，浏览器不挡跨站握手 |

合起来：用户访问的任意网页都能跨域读写这个本地 API，包括开一条
`ws://127.0.0.1:<port>/ws` 发一帧 `{"type":"chat"}` —— **而那个 agent 手里有
`shell_execute`**。

### 修法：在壳这一层加闸，**一行内核代码都没改**

新增 `desktop/access_gate.py` + 在 `ui_mount.attach()` 里装一层 ASGI 中间件。
`add_middleware` 是**头插**（后加的在外层），所以闸门排在内核 CORS **之前**，
可以先拒后放 —— 这比改内核 CORS 更对：再加一个 `CORSMiddleware` 会让
`Access-Control-Allow-Origin` 发两遍，浏览器直接拒掉整个响应。

三道闸，互相独立：

1. **Host 白名单** —— 挡 DNS rebinding（攻击者把自己的域名解析到 127.0.0.1，
   请求就带着他的 Host / Origin 进来）。
2. **Origin 白名单** —— 带了 Origin 且不是自己人一律拒，**即使它手里有有效令牌**。
   跨站这条路因此是封死的，与令牌是否泄漏无关。
3. **令牌** —— 每次启动随机生成，随窗口地址 `?k=` 交给 WebView，换成一个
   `HttpOnly + SameSite=Strict` 的 cookie。

第 3 条选 cookie 是**刻意的**：同源请求浏览器自动带上 → **前端零改动**（所有
fetch / WebSocket 都不用动）；跨站请求浏览器不会带 SameSite=Strict 的 cookie
→ 正是我们要的语义，而且比"前端记得带 header"可靠（前端漏一处就是一个洞）。

豁免只有 `/api/health`（只回答"活着吗"，不含用户数据，但**必须**能被拿不到
令牌的外部工具问到 —— CI 探针、单实例检测都靠它）。静态资源不设闸：它们是界面
自己的 JS/CSS，不含用户数据，且必须在换到 cookie 之前就能加载。

### 过程中踩到/抓到的三个坑

1. **端到端演示抓到一个单元测试照不出的洞**：界面加载的是 `/_desktop/`（不是
   `/api/*`），而第一版只在"受保护路径"上把 `?k=` 换成 cookie —— 于是窗口
   永远拿不到 cookie，**真机上是整个界面全白**。单元用例当时只喂了 `/api/` 路径，
   全绿；真起服务 curl 一次就露了。已修（任何路径的合法 `?k=` 都换），并补了
   针对 `/_desktop/` 的护栏。
2. **内核 `app` 是进程级单例，而 Starlette 的中间件卸载不掉** —— 一旦装上闸门，
   同一进程里后面所有打 `/api/*` 的测试都会 403（一次改动能炸掉几百个用例）。
   解法是中间件持有的是**可替换的 `GateHolder`**：换令牌 = 换 `holder.gate`，
   测试用完置空即放行。另加 `_add_middleware_anytime()` 处理"应用已启动后
   attach"（重建中间件栈），否则那次 attach 会当场抛异常或**静默失去中间件**。
3. **反向自检抓到"假绿"**：撤掉 Origin 闸之后，两条 Origin 用例**仍然通过** ——
   因为请求没带令牌、被令牌闸拦下了，它们根本没测到 Origin。改成"带着有效令牌
   再叠跨站 Origin"后才真正生效。

### 护栏（全部做过反向自检：撤回对应那道闸必红）

- `desktop/tests/test_access_gate.py` —— 25 项：三道闸各自的正反例、中间件层的
  ASGI 断言（被拒的请求不能进到应用里）、真 app 上的完整栈断言
  （**跨站响应里不许出现 `access-control-allow-origin`**，出现就说明闸门排到了
  内核 CORS 后面）、落盘/读回的端口校验。
- `scripts/smoke_test.py` —— 冒烟里加了"闸门必须在场"一组：无令牌 403、
  `/api/health` 仍 200、跨站 Origin 403、URL 令牌换 cookie 得到 302。
- `.github/workflows/build-windows.yml` —— 打包版多一条硬断言：
  **无令牌打 `/api/*` 必须 403**，否则流水线红。打包漏装闸门不能等用户来报。

### 真机端到端（`/var/minis/workspace/gate-e2e.sh`，14/14 通过）

起真服务，用 curl 模拟恶意网页：匿名打 `/api/chats/sessions`、`/api/settings`、
`/api/fs/read` 全 403；`Host` 换成攻击者域名 403；**带着偷来的令牌 + 跨站 Origin
也 403**；跨站响应里无任何 CORS 放行头；WS 跨站握手 403、匿名 403、带令牌 101。

### 改动前后的实测对照（同一台机器、同一份代码，只差这道闸）

用**上游无闸**的无头服务（`app.py`）量"改动前"，用桌面壳量"改动后"：

| 请求 | 改动前 | 改动后 |
|---|---|---|
| `GET /api/health` + `Origin: https://evil.example` | **200** + `ACAO: https://evil.example` + `Allow-Credentials: true` | **403**，无放行头 |
| `GET /api/chats/sessions` + 跨站 Origin | **200** + 同样回显 Origin | **403**，无放行头 |
| `GET /api/settings` + 跨站 Origin | **200** + 同样回显 Origin | **403**，无放行头 |
| 预检 `OPTIONS` + 跨站 Origin | **200**，放行 `DELETE/PATCH/PUT/POST…` | **403** |

改动前那三行就是"任意网页能读能写本机这个手里有 shell 的 agent"的直接证据。

### 对抗性实验（`/var/minis/workspace/gate-adversarial.sh`）

| 攻击面 | 结果 |
|---|---|
| 不带 `Origin` 头的跨站 GET（`<img>`/`<script>`/`no-cors fetch` 那一类） | 403 —— 跨站时浏览器**不会**带 `SameSite=Strict` 的 cookie，令牌闸兜住 |
| `Origin: null`（沙箱 iframe / `file://` 页面） | 403（受保护路径与豁免路径都是） |
| 跨域读**豁免路径** `/api/health` | 403 —— Origin 闸排在豁免判断**之前**，豁免不能被拿来当外泄通道 |
| 预检 `OPTIONS` + 跨站 Origin | 403，无放行头 |
| 重复 `Origin` 头 / `ORIGIN` 大写 | 403（请求头统一小写化后再判） |
| 路径变体 `//api/…`、`/./api/…`、`/API/…`、`%61pi`、`dotdot` 上跳、分号、尾斜杠 | 全部拿不到 2xx（它们既不匹配 `/api/*` 也不匹配 `/ws`，落到 SPA 兜底页，**根本没进处理函数**） |
| `/ws` 的尾斜杠 / 双斜杠 / 大小写 / dotdot | 403 |
| 令牌参数：空 / 错 / 重复（先错后对） | 403（`parse_qs` 取第一个，不是"任意一个匹配就算过"） |
| `Sec-Fetch-Site: same-site` / `cross-site`（带 cookie） | 403 |
| `Sec-Fetch-Site: same-origin` / 头不存在（带 cookie） | 200（**对照组**：别把正常浏览弄坏） |

### 独立复核（对抗性，30 分钟，专门去攻破它）

**结论：挡得住，无高危绕过。** 它给了 8 条发现，处置如下：

| # | 发现 | 处置 |
|---|---|---|
| 8 | **`OPENMINIS_NO_SPLASH=1` 的串行启动路径漏传令牌** → 窗口照常开、界面里每个 API 都 403 | **真 bug，已修**（`launcher.run_serial_window`）；这条最险，因为表现是"应用打开了但是坏的"，比打不开还难查 |
| 4 | cookie 的 SameSite 按**站**算，而"站"**不含端口** → 本机另一个 loopback 页面（比如别的 dev server）与本服务同站，能带上 cookie 发 `<img>`/表单 GET（那类请求**不带 Origin**） | **已修**：新增 `Sec-Fetch-Site` 闸，只认 `same-origin`/`none`。头不存在时不判（老浏览器、非浏览器客户端还得过令牌闸） |
| 5 | cookie 按域名存、**不分端口** → 同机另一个页面能塞一个同名 cookie，只认第一个的话界面整片 403（拒绝服务） | **已修**：改成"任一同名 cookie 匹配即通过" |
| 7 | 装中间件时先放一个**空持有者**、稍后才赋令牌 → 中间那段是"装着闸门却放行一切" | **已修**：先把闸门造好再装 |
| 6 | `/api/health` 的响应里有 `data_dir` / `workspace` 绝对路径，我注释里写的"无用户数据"不实 | **注释改成准确说法**；行为保留（是本机信息泄漏，跨域读它已被 Origin 闸挡住；CI 探针与单实例检测依赖这条豁免） |
| 1/2/3 | 无 Origin 的跨站 GET、`Origin: null`、跨域读豁免路径 —— 均 403 | 无需改（与我的实验一致） |

它的"未验证猜测"里有一条值得一提：若将来给内核设了 `root_path`，闸门看的是完整的
`scope["path"]`、而 Starlette 按去掉 `root_path` 的路径匹配 —— 那时可能出现分歧。
当前接线没有设 `root_path`，记在这里当预警。

### 端到端（`/var/minis/workspace/gate-e2e.sh`，18/18 通过）

真起服务、用 curl 当恶意网页打：匿名打 `/api/chats/sessions`、`/api/settings`、
`/api/fs/read` 全 403；`Host` 换成攻击者域名 403；**带着偷来的令牌 + 跨站 Origin
也 403**；跨站响应里无任何 CORS 放行头；`Sec-Fetch-Site` 四态符合预期；
WS 跨站握手 403、匿名 403、带令牌 101。

### 残余风险（知道就好 —— 这不是一道"挡一切"的闸）

1. **界面里的同源 XSS**：闸门判的是"这个请求是不是自己人"，而界面是唯一合法的
   自己人。所以**如果界面在本源渲染了用户可控的 HTML**（模型输出、读进来的文件
   内容经由 `renderMarkdown` 插进 DOM），那段脚本就是"自己人"，它发出的请求全部
   合法 —— 闸门在这一层帮不上忙，那是前端转义该管的事。复核点出了这条，**没查**，
   记在这里：它决定了这道闸能挡什么、不能挡什么。
2. **`root_path`**：闸门看完整的 `scope["path"]`，而 Starlette 的匹配按去掉
   `root_path` 之后的路径算。当前接线没有设 `root_path`；将来若设了，两处可能分歧
   （复核的未验证猜测之一）。
3. `/api/health` 的响应里带 `data_dir` / `workspace` 绝对路径（本机信息；跨域读它
   已被 Origin 闸挡住，见上表）。

### 顺带修掉一个我自己写坏的断言

冒烟里那条"路径变体不服务 API"我先写成"不是 2xx"—— 那实际上在测 **`web/dist`
存不存在**（它缺席时这些路径落到"前端未构建"页给 503，存在时落到 SPA 兜底页给
200），同一个检查会随环境翻转。改成"**2xx 且 JSON**"才是真正要测的东西：
被闸门拒 = 403 + JSON（要的）、落 SPA = 200 + HTML（无害）、**真被 API 服务 =
200 + JSON（只有这一种是洞）**。

### 验证

服务器 `python scripts/check.py` 四步全绿：**893 passed / 2 skipped**，
lint / 前端 / 冒烟全过。

已知的**非**问题（记录在案，避免以后重复怀疑）：

- cookie 没有 `Secure`：服务是 `http://127.0.0.1`，加了它反而设不上；这里没有网络
  攻击者（流量不出 loopback），而 `HttpOnly` + `SameSite=Strict` 才是防网页的那两条。
- `/api/health` 匿名可读，且它的响应里带 `data_dir` / `workspace`（会暴露本机
  用户名与目录）。这是**本机**信息泄漏（别的进程本来也能看到），不是网页那条路 ——
  跨域读它已经被 Origin 闸挡住（见上表）。CI 探针与单实例检测都要靠这个豁免。
- 令牌会落盘到用户数据目录（`~\openminis\.desktop-access.json`）：第二个实例复用
  第一个实例的服务时要用它，CI 探针也要用。这是**本机**可读，与 Jupyter / VS Code
  把自己的令牌打进控制台是同一类做法。

### 验证

服务器 `python scripts/check.py` 四步全绿：**889 passed / 3 skipped**，
lint / 前端 / 冒烟全过。

## v0.4.3 — 修「流式正文重复」与「重开会话后工具卡全堆在底部」（2026-09-30）

现场反馈两个问题，根因分别在**前端渲染**与**持久化表示**。

### 修复①：工具卡之后的正文会把前面的正文再画一遍（用户："先1再12再123再1234"）
`appendDelta` 渲染的是 `t.text` —— **整轮**累计文本，而不是**当前文本段**。
一次工具调用会把文本切成多段（`addToolCardToTurn` 里 `t.textBlock = null`，
下一段另起一个块），于是新块一出现就把工具卡**之前**的正文整段重画一遍，
而且每来一段就重画一次越来越长的累计串。用户描述完全吻合：

    模型先说"我来看看"      → block1 显示"我来看看"
    调工具（切段）           → 卡片
    再说"文件里有A"         → block2 显示"我来看看文件里有A"  ← 重复了

修法：区分 `text`（整轮，供复制/落库）与 `runText`（当前段，供渲染）；
工具卡切段时把 `runText` 清零；`endTurn` 只回填当前段（且不再走
`turnTextBlock()` —— 那会在末尾凭空造一个空块）。

### 修复②：关闭应用重开会话，所有工具执行都排在最下面
`parts_json` 原来只存得下「一整块正文 + 一串工具卡」，**交替顺序丢了** ——
实时渲染是"正文/卡片交替"，重放时只能"先全部正文、再把卡片堆在下面"，
两边画法必然不一致。

修法：在 `parts_json` 里额外存一份**有序可视时间线**（新 part 类型 `flow`，
内容 = 正文段与工具卡按发生顺序交替）。刻意用一个 `parts_to_text` /
`parts_to_runs` **都不认识**的类型标签，于是：

- 模型上下文（只读 `text` part）**一字不变**；
- 工具卡（只读 `tool` part）**一字不变**；
- 旧数据没有该 part → 前端退回老画法，**天然兼容，不需要迁移**；
- 纯正文回合不合成时间线（没有可交替的东西），同样零变化。

后端在 sink 里边收边记（`turn_timeline`），接口多吐一个 `timeline` 字段，
前端 `renderMessages` 按它画。

### 独立审核补充的三处（同一轮改动，审核子 agent 发现）

送了一个子 agent 独立复核（它把 `beginTurn/appendDelta/addToolCardToTurn/endTurn`
原样抽出来配假 DOM 跑了行为复现），**两个根因都被独立复现确认**，另外指出三处：

1. **生图引用会丢（这是本次改动引入的回归，必须补）**：`append_image_refs`
   补写的 `![](路径)` 只进了 `text=`，而界面在有 timeline 时**不再渲染 `m.text`**
   → 生图回合（必然带工具调用）重放时图片消失，正好废掉那个函数存在的唯一理由。
   已把补写的那段一并接进时间线，并加了**调用点级别**的集成测试。
2. **时间线里的卡片只留 id 引用**：卡片的 name/input/output/ms 已在 `tool` part
   里存过，再抄一份会让 `parts_json` 直接涨一倍（单条 output 上限 8KB）。
   读的时候按 id join 回 `tool` part。
3. **子代理回合不再是"空白气泡 + 一摞卡片"**：它们的正文在 `subtext` part 里，
   `parts_to_text` 看不到 → 时间线原本只有卡片。现在把子代理正文也放进时间线，
   并在界面上给一个身份标签。开着子代理时工具卡大多产生在这些行里 ——
   正是「所有工具执行都排在最下面」观感的一部分。

### 护栏（都做过反向自检：撤回修复必红）
- `tests/test_chat_sessions.py::test_timeline_roundtrip_keeps_interleaving`
- `tests/test_chat_sessions.py::test_timeline_does_not_touch_model_context`
- `tests/test_chat_sessions.py::test_legacy_rows_without_timeline_fall_back`
- `tests/test_chat_sessions.py::test_plain_turn_has_no_timeline`
- `tests/test_chat_sessions.py::test_messages_api_returns_timeline`
- `tests/test_chat_sessions.py::test_flow_part_stores_only_tool_refs`
- `tests/test_chat_sessions.py::test_subagent_row_timeline_includes_text`
- `tests/test_turn_timeline.py::test_turn_timeline_persists_interleaved_order`
  （驱动真实的 `_run_chat`，断言落库顺序 = 流式发生顺序）
- `tests/test_turn_timeline.py::test_image_refs_are_written_into_the_timeline`
  （**调用点级别**：把补写那行删掉，这条必红）

## v0.4.2 — 修「清空数据把工作区也删了」引发的连锁 + 正则片段被误判越界（2026-09-30）

现场反馈「还是有问题的」+ 一整份日志。日志把两条独立的 bug 摆得很清楚，都已修并带护栏。

### 修复①：一键清空数据不再删掉工作区（分组）
`clear_all_chat_data()` 原来连**分组**一起删。工作区不是聊天记录、是**配置** ——
它带着用户绑定的真实项目目录。删掉之后前端 localStorage 里缓存的
`currentWorkspace` 变成悬空 id，于是：

```
PATCH /chats/sessions/<sid>/workspace  → 404  工作空间不存在
→ 会话没归入工作区 → shell 退回空的 db-<会话id> 沙箱 → agent 看不到项目文件
```

日志证据：`desktop clear-data: {'sessions': 4, 'messages': 10, 'folders': 1, 'markers': 4}`。
现在只清会话/消息/压缩标记，工作区与路径绑定原样保留；
前端遇到 404 也不再卡死，而是清掉悬空 id、退回默认并提示重选。

### 修复②：正则/转义片段被当成「工作区之外的路径」
`tr -d '\r'`、`sed 's/\s\+//'`、`grep -P '\d{2}'` 这类命令**全被越界守卫拦下**：
`_TOKEN_RE` 把引号当分隔符剥掉，`\r` / `\s+` 各自成为独立 token，以反斜杠开头 →
Windows 上 `ntpath.isabs('\\r')` 为真 → 解析成盘根下的 `C:\r` → 判越界。
日志里 agent **连续 14 次被拦**并触发 `CRITICAL effect_tool_runaway streak=14`。
现在 `_looks_like_path()` 认得转义片段（`\r` `\n` `\t` `\s+` `\d{2}` `\w` `\x00`），
真正的反斜杠路径（`\Windows\System32`、UNC `\\server\share`）照旧拦。

### 修复③：发送前把会话钉进工作区（agent 不再在空沙箱里转）
首条消息不带 `session_id` 时由服务端建会话，前端拿到 id 后**从来没绑定过工作区**；
更早版本建的**老会话也从未绑过**。现在两条路都补上：`send()` 发消息前 `await` 绑定
（幂等），`chatSession` 收到服务端新建的 id 时也立刻绑定。空工作区不动 —— 那可能是
用户有意让会话待在默认沙箱。

### 护栏（都做过反向自检：撤回修复必红）
- `tests/test_guard.py::test_regex_escapes_are_not_paths` / `test_path_like_backslash_tokens_still_count` / `test_scan_escape_allows_common_text_commands`
- `tests/test_chat_sessions.py::test_clear_all_chat_data_keeps_workspaces`

> 注意：修复②**只在 Windows 上会显形** —— POSIX 下 `\r` 解析成 `<cwd>/\r`，本来就在
> 工作区内。所以护栏里那条按平台无关的 `_looks_like_path()` 断言来锁。

## v0.4.1 — 修「agent 看不到工作区文件」+ 重复输出诊断 + 一键清空数据（2026-09-30）

三件事，都带回归护栏。

### 修复：归到工作区的会话，agent 现在真能读到项目文件
v0.3.x 把「会话↔工作区」绑定后，只把 **shell** 钉进了项目目录；`ls` /
`search_files` / `file_read` / `file_write` / `file_edit` 这些文件类工具**还在
硬解析全局工作区根** → agent 用它们只看到空的 `db-<会话id>` 目录，读不到项目里
的文件（现场截图就是这个）。现在这些工具解析路径时会先查会话是否绑定了真实
目录（`ExecutionCoordinator.sandbox_root_for()`），是就以项目目录为根；未绑定的
会话行为完全不变。护栏：`tests/test_sandbox_paths.py::test_filed_session_resolves_inside_its_bound_workspace`。

### 新增：重复输出诊断日志（排查「输出一直重复一大段」）
在 OpenAI 兼容流式路径和 agent 回合循环里加了**只读不改行为**的探针：
- 流内检测到 runaway 重复块 → `WARNING [repeat-diag] stream is repeating …`
- 某回合正文与上一回合高度重合 → `WARNING [repeat-diag] turn=N repeats …`
- 每条流结束一行 `INFO [repeat-diag] … sse_frames=… visible_chars=… finish_reason=… done_sentinel=…`
  —— 用来区分「是中转在重复发 delta」还是「模型自己在循环」。

**日志在哪**：设置 → 关于 → 诊断日志，那里直接显示内核 / 桌面两个日志目录，
并有「下载日志压缩包」按钮（打包成 `openminis-logs.zip`）。跑一轮复现后下载发回即可。
（内核日志默认在 `<数据目录>/logs`，Windows 上是 `%USERPROFILE%\openminis\logs`。）

### 新增：一键清空数据（保留供应商）
设置 → 关于 → 数据 → 「清空所有对话数据」：删除全部会话、消息、分组与压缩标记，
用于排除历史数据把模型带偏的嫌疑。**供应商、模型、人格、技能与其它一切设置都保留**
（清空只动对话表，碰不到 SettingsStore）。护栏：
`tests/test_chat_sessions.py::test_clear_all_chat_data_wipes_sessions_and_folders`。

验证：`scripts/check.py` 四步全过（pytest 852 passed / 2 skipped、ruff F821/F811、
前端 25 项、冒烟）。

## v0.4.0 — 补齐 6 个设置面板 + 附件上传（内核接口大量接通）（2026-09-29）

内核有 14 个路由模块，桌面界面之前只调了 5 个。这一版把用户会用到的接上：

- **助理**（子代理）：列出/新建/编辑/删除、让模型起草。`subagent_delegate` 报错时
  指向的「助理页」以前根本不存在 —— 现在有了，派活用的 id 直接标在每一项上。
- **沙箱**：守卫拦下的（目录越界/异常删除/敏感信息）在这里能看到原因、手动放行
  （本次/本会话/永久）、撤销永久白名单、清空记录。
- **插件 / 定时任务 / 知识库 / 技能市场 / 用量**：分别接 `/api/plugins`、
  `/api/scheduled`、`/api/knowledge`、`/api/marketplace`、`/api/usage`。
  定时任务面板带一条醒目提示：桌面进程被系统挂起时定时器不保证触发。
- **附件上传**：输入区加回形针按钮 + 拖拽 + 粘贴。走 `/api/upload` 落到工作区，
  消息里**只留路径**（`![name](path)`）——不把图片读成 base64 塞进上下文
  （一张手机照片 base64 后 ~7MB，会卡死输入框、按体积烧 token）。

实现约束：**组件一律用组件库、图标一律用图标库，不手写**。为此扩容 vendor：
Web Awesome 组件 +27（button/input/textarea/checkbox/switch/tab/popover/dropdown/
toast/badge/callout/card/…），Lucide 图标 +44（shield/plug/clock/book-open/store/
paperclip/chart-column/…）。修了 wa-dialog 用法（标题走 `setAttribute('label')`，
关闭补 `inner.close()` 兜底）。前端检查 `ui-check.mjs` 22 → 25 项。

## v0.3.9 — 测试平台感知修复（2026-09-29）

v0.3.8 的 Verify 在 windows-latest 上红了 1 项：Git Bash 路径用例把 POSIX 的预期
写死了，而 Windows 上 `/c/Users/…` 本来就该翻成 `C:/Users/…`。改成按 `os.name`
分两支各自断言。只改测试，exe 内容与 v0.3.8 相同。CI：**846 passed**（Windows）。

## v0.3.8 — 沙箱误判自己的工作区 + 界面把会话绑到工作区 + 修流式光标（2026-09-29）

### 内核（`src/openminis/sandbox/guard.py`，走 PORT-FIX 三件套，见 NOTICE.md 偏离表）
- `_resolve_outside()`：**会话自己的工作目录也算它的沙箱根**。工作区绑了真实目录时
  （桌面版把项目目录设为工作区），shell 的 cwd 就是那个项目 —— 原来允许根只有
  `<data>/workspace` + 技能库，于是「访问自己的工作区」被判「目录越界」。
  `cwd` 没有父目录时不当作根，`cwd=/` 不会一次放行整台机器。
- 新增 `_git_bash_drive_path()`：**只在 Windows** 把 `/c/Users/…` 翻成 `C:/Users/…` 再判越界。
  Windows 上内核的 shell 是 Git Bash，而 Windows 语义里 `ntpath.abspath('/c/x')` 是 `'\c\x'`
  → 模型访问**自己的工作区根**也被拦（用户实测拦截编号 `g-179067946811-c46651`）。
- 测试 `tests/test_guard.py` **43 项**（+5）；反向自检：撤回修复 → 钉住的 2 项必红。
  另外验证过：手工往 `guard_allowlist.json` 里加白名单**救不了**这个场景（它的键是精确目录，子目录不匹配）。

### 界面（`web/desktop/`）
- **会话 ↔ 工作区两端绑定**（以前从来不调 `PATCH /api/chats/sessions/{id}/workspace`）：
  在文件面板选/建工作区 → 当前会话搬过去；新建会话 → 归入面板当前工作区；
  切会话 → 面板跟着切到那个会话的工作区。**这一步决定 agent 的 shell 在哪启动** ——
  没绑定的会话只有内核默认沙箱（`<data>/workspace/db-<会话id>`）可用，
  所以以前会出现「文件面板里明明有文件，agent 说工作区是空的」。
- 文件面板加一行「**agent 工作目录**」提示：绑了目录就说 shell 就在这里启动，没绑就说
  agent 只在内核默认沙箱里、看不到你自己的项目。
- **修流式光标泄漏**：`<span class="cursor-blink">` 原来只插不删 → 每轮都在页面上留下一个
  永远在闪的光标（用户实测「多个闪烁的蓝色光标，会话结束还在闪」）。现在光标只属于正在
  流式输出的那一个块：换块先清旧的、工具调用时收掉、回合结束收掉、新回合开始兜底再清一次。
- 前端检查 `ui-check.mjs` **20 → 22 项**（新增：光标必须收掉；会话必须绑工作区）。

### 还没开放出来的设置（对账结果，待办）
内核有 14 个路由模块，桌面界面只调了 5 个。**从未调用**：`/api/guard`（沙箱，15 个端点）、
`/api/subagents`（助理，6）、`/api/knowledge`（知识库，3）、`/api/plugins`（插件，9）、
`/api/marketplace`（技能市场，2）、`/api/scheduled`（定时任务，6）、`/api/usage`（用量，2）、
`/api/upload`（附件上传，2）、`/api/appearance`（背景，2）。
其中「助理」是 `subagent_delegate` 报「subagent 不存在」时指向的页面 —— 桌面版根本没有它。

## v0.3.7 — 原生缩放的读写都回到 UI 线程（2026-09-29）

v0.3.6 的 CI 带窗口探针第一次给出了真凭实据：
`{"handle":true,"ready":false,"reason":"读不到 ZoomFactor：InvalidOperationException"}`
—— 说明**钩子挂上了、后端对象也接住了**，但控件属性不能从 uvicorn 线程碰：
WinForms 的跨线程访问一律抛 `InvalidOperationException`。我上一版只给「写」做了
UI 线程兜底，「读」和「是否就绪」没有，于是能力查询永远报 `ready:false`，
界面白白回落 CSS。

- `capability()` / `set_zoom()` 的**读、写、就绪判断**全部走同一个
  `_on_ui_thread()`（`form.Invoke`）；没有 pythonnet / 没有窗体时再退回直连访问，
  所以非 Windows 平台也不会因为这条路径永远失败。
- 设 + 读回放在**同一次** UI 线程调用里完成 —— 分开做会读到跨线程的假象。
- CI 探针不再只看 `handle`：它会真的 `POST {"factor":1.0}`（不改变视觉），
  只有 `ok:true`（设进去 + 读回来一致）才会打「native zoom is usable on Windows」。

回归测试：`test_native_zoom.py` **22 项**（新增「读也要回 UI 线程」
「读回不是跨线程假象」「没有 pythonnet 时退回直连」）。

## v0.3.6 — 修 v0.3.5 打不开（原生缩放的钩子签名写错）（2026-09-29）

**症状**：v0.3.5 的 exe 双击没反应（进程静默退出）。
**原因**：原生缩放要在建窗口之前包一层 pywebview 的 `EdgeChrome.__init__`，我按
`(self, window)` 写，而 pywebview 6.2.1 的真实签名是 `(self, form, window, cache_dir)`
→ `EdgeChrome(...)` 一调就 `TypeError: patched() takes 2 positional arguments but 4
were given`，窗口建不出来；窗口模式没有控制台，所以什么都看不到。
**修法**：
- 包装器改成 `(self, *args, **kwargs)` **原样透传**（签名无关，pywebview 以后再改也不怕）；
- 我们自己的记账代码（接住后端对象）整块吞异常 —— 加的东西绝不许影响建窗口；
- 真实签名记进 `GET /api/desktop/zoom` 的返回，下次一眼能定位。

**为什么没被拦住**：CI 的探活是 `--no-window` 起的（那种进程里根本没有窗口），
而唯一那条带窗口的探针，把「进程崩了」误读成了「runner 没有桌面」。
现在带窗口的探针会打印**进程退出码**与应用 `startup.log` 尾部，退出过早时打 `::error::`。

**回归测试**：`desktop/tests/test_native_zoom.py` 19 项 —— 新增「按真实三参数签名调用」
「参数原样透传」「记账代码炸了也不许影响建窗口」三项；旧写法在这三项下必红。

## v0.3.5 — 缩放的两个坑 + 原生缩放（2026-09-29）

### 修掉你报的两个问题（144% 缩放下的截图）
- **「新会话」按钮撑成一整块**：它在纵向 flex 里是 `flex: 1 1 auto`，会和会话列表
  **平分剩余空间** —— 会话少时按钮长到几百 px。改 `flex: 0 0 auto`。
  （一直潜伏着，你的实例只有 1 个会话正好踩中。）
- **点完按钮整页被顶上去（标题栏消失）**：`#app` 用了 `height: 100vh`，而 **`vh` 不会
  跟着根节点的 `zoom` 缩小** → 页面比视口高 44%，输入框一被聚焦，浏览器把根容器滚下去。
  改为按「缩放前的视口」补偿（`--ui-h = innerHeight / zoom`，随缩放和窗口大小重算），
  并给根节点 `overflow: clip`（`hidden` 仍可被 focus/scrollIntoView 程序化滚动）。
  100% / 120% / 144% / 173% / 50% 实测：页面高度都等于视口、零溢出。

### 缩放改走 WebView2 原生（`ZoomFactor`），CSS zoom 留作兜底
- 原生缩放是**引擎级**的：CSS px 变大、布局视口变小，`vh` / `100%` / 媒体查询全部自洽，
  文字也按真实字号渲染 —— 上面那类坑天生不存在。
- pywebview 不给 `window.native` 赋值（Windows 后端独缺这一步），所以壳层在建窗口前
  自己把 Edge 后端接住；**只有「设进去 + 读回来一致」才算成功**，失败一律回落 CSS。
- 新增 `GET/POST /api/desktop/zoom`；「设置 → 界面」里直接写清当前用的是哪种缩放
  （原生 / 界面内）以及回落原因 —— 不用猜，也不用看日志。

## v0.3.4 — 界面缩放 + 从 cc-switch 导入供应商（2026-09-29）

### 界面缩放（对齐 VS Code）
- 档位 `1.2^n`，夹到 **0.5–5**；`Ctrl+=` / `Ctrl+-` / `Ctrl+0` / `Ctrl+滚轮`，
  以及「视图 → 放大/缩小/重置界面缩放」（到边界自动置灰）。
- 新增「设置 → 界面」页：缩放、主题、界面字体、等宽字体、界面密度、减少动效、
  显示状态栏、启动时恢复上次会话、回车发送方式。**只存本机**，不进内核配置
  （那边是全量替换，未知字段会被拒）。
- 缩放收在一个新模块 `web/desktop/ui-prefs.js`（单一入口 + 订阅广播），三处入口不会各说各话。

### 从 cc-switch 导入供应商
- 「设置 → 模型」新增按钮：只读读取 cc-switch 的库（**绝不写你的 cc-switch 目录**），
  列出供应商供勾选，默认只勾可用的，并标注「已存在 / 地址缺失 / OAuth 凭据不支持」。
- 导入只写进草稿，**要你自己点「保存」**才生效；密钥走和手输完全同一条路，
  清单里只有掩码，明文不渲染进界面。
- 按真实库（17 行 / 4 种 app_type）修正了三处映射：`providers.id` 是 UUID 字符串而不是整数、
  claude 的模型要精确取 `ANTHROPIC_MODEL`（一行里有多个 `DEFAULT_*_MODEL` 且值常不同）、
  codex 的接口地址藏在 `config` 那段 TOML 里。

### 修掉你报的两个问题
- **`127.0.0.1:8765 显示` 那个原生弹窗没了**：`confirm()` 四个调用点全部换成组件库的
  `<wa-dialog>`（应用内弹层，标题「确认」，居中显示）。
- 顺手修一个真 bug：**缩放后拖面板越拖越宽** —— `getBoundingClientRect` 给的是
  「放大后」的 CSS px，而 `--sidebar-w` 是布局 px，差一个倍率；实测拖 100 视觉 px
  面板会长 334 px，现在按实测倍率换算（拖 100 就是 100）。

### 打包
- vendored 组件库新增 `wa-dialog`（+28KB，772K → 800K）。
- 启动时间线、日志路径与 v0.3.3 一致（见 `docs/DESKTOP.md`）。


## v0.3.3 — 让时间线把"解包"那段算进去（2026-09-29）

v0.3.2 的 CI 数据暴露了一个测量问题：onefile 版报 `origin=self`，也就是 0 点取在
**子进程**（真正跑 Python 的那个）创建时刻 —— 于是"父进程解包里那几百毫秒"被从总数里
抹掉了，而那正是办公电脑上最可疑的一段（企业杀软会逐个扫刚解出来的文件）。

- 父进程判据加了一个更稳的信号：PyInstaller 的 onefile 子进程环境里带着
  `_PYI_PARENT_PROCESS_LEVEL`（路径比较会受长短路径名/大小写影响）。
- `_is_our_own_image` 里那几处 `argtypes` 声明撤回：声明之后这条路径在 CI 上不再命中
  （异常被 `except` 吞掉），退化成 `origin=self`；v0.3.1 的写法已经验证可用。
- `GetProcessTimes` 的 `argtypes` **保留** —— 那个是真 bug（`GetCurrentProcess` 的
  伪句柄 `(HANDLE)-1` 被当 32 位截断，导致连"自己"都量不到）。

附带说明：`origin=parent` 的准确性靠 CI 数据核对，本地没有 Windows 无法单测；失效时只会
退化成 `origin=self`（少算解包，不会算错别的），所以是个可以安全观察的降级。

## v0.3.2 — 启动诊断 + 一处数据目录路径写错了（2026-09-29）

v0.3.1 的启动优化本身没问题，这一版修的是"出问题时看不到内部发生了什么"：
用户要在自己那台（有企业杀软的）电脑上给出启动耗时的证据，工具得先靠得住。

- **`startup.log` 改成界面一出来就写**（原来要等退出才写），启动失败时也留一行 ——
  用户说"起不来"时，我们至少知道它走到哪一步、用了多久。
- **Windows 取进程创建时刻的 ctypes 调用补上 `argtypes`**：`GetCurrentProcess` 返回的伪句柄
  是 `(HANDLE)-1`，不声明类型会被当 32 位截断，`GetProcessTimes` 于是静默失败 ——
  CI 上 onedir 版的 `origin=` 一直退化成 `python`（量得到别处，量不到自己）。
- **数据目录路径写错了**：内核在 Windows 用 `%USERPROFILE%\openminis`（`LOCALAPPDATA` 只放缓存），
  而我在文档 / 发布说明 / CI 探针里写成了 `%LOCALAPPDATA%\openminis` —— 后果是探针
  "失败时打印日志"永远打印不出来。现在探针**问应用要**（`/api/health` 的 `data_dir`），
  启动画面不再硬编码路径（`ui-check` 加了断言），`desktop/paths.py` 的退路与内核
  `_default_data_dir()` 对齐。
- 顺手：`scripts/check.py` 新增 `ruff --select F821,F811` 门禁（这个门禁本来能拦住 v0.3.1
  那次 `NameError`），CI 探针改用它原来的、可信的杀进程方式（`taskkill /T`）。

## v0.3.1 — 启动更快（2026-09-29）

用户反馈：公司电脑上双击 exe 到窗口出来要等好几秒。真因不是"Python 慢"，而是启动
**严格串行**：onefile 解包 → 内核 import → uvicorn 起好（0.53 是**先跑完 lifespan 的
文件 I/O 才 bind 端口**）→ 然后才 create_window、初始化 WebView2、加载页面。窗口只在
最后一段的末尾出现，前面几秒屏幕上什么都没有。

### 启动顺序改成「窗口先行」
- 主线程立刻建窗口并渲染 `web/desktop/splash.html`（内联、不依赖后端），内核交给后台
  线程 boot，好了再 `load_url` 到真实界面 —— WebView2 的初始化和内核 import 并行，
  「窗口出现」不再包含内核那几秒。
- splash 显示真实阶段文案与已等待秒数（**不做假进度条**）；**启动失败把错误写进窗口**
  并指向 `logs\desktop.log`，不会让人对着一个永远转圈的窗口干等。
- 启动期间关窗：boot 线程不是 daemon（避免把正在写盘的会话库硬杀），关窗立刻打标记、
  健康检查放弃、已起的服务收掉，进程干净退出。
- `OPENMINIS_NO_SPLASH=1` 退回老顺序 —— 出问题时第一个该试的开关，也是 A/B 的对照组。

### 新增启动时间线（先能测，再谈优化）
- `desktop/startup_trace.py`：Windows 用 `GetProcessTimes` 取**进程创建时刻**当 0 点
  （这样 onefile 的解包耗时才算得进去；解包发生在父进程，所以父进程是同一个 exe 时用
  父进程的时刻）。每次启动追加**一行**到 `%USERPROFILE%\openminis\logs\startup.log`。
- `desktop.log` 补上时间戳 —— 以前没有，用户发日志过来也看不出"几秒"。

### 顺手
- `wait_for_health` 前 3 秒改 25ms 轮询（原来固定 150ms，平均白等 ~75ms）。

### 打包：多一个免解包版本
- CI 新增 `portable` 任务，把同一份代码打成 onedir，产出
  `OpenMinisDesktop-portable.zip`。onefile **每次启动都要把载荷解到 `%TEMP%`**，
  办公电脑上还要过一遍杀软实时扫描；onedir 没有这一步，代价是要先解压一次。
- 两个包一起发，配 `scripts/startup_probe.ps1` 在 CI 上量冷/热两次启动耗时 ——
  CI 机器没有企业杀软，真正决定性的数字得在用户那台机器上取（`startup.log`）。

### 验证
- `desktop/tests` 30 → 47 项：新增 launcher 的顺序/状态机/兜底契约、startup_trace、
  以及"页面里的函数名必须和壳层推的 JS 一致"的契约测试。
- 前端检查 11 → 12 项：splash 必须自包含（它渲染时后端还不存在，任何外链都是 404）。
- 本地没有 GUI/pywebview，窗口路径用注入的假 `webview` 模块验顺序；真窗口靠 CI 冒烟
  （`--no-window`）与真机实测。

## v0.3.0 — 编辑器式界面（2026-09-29）

这一版把界面往「像 VS Code」推了一步：菜单栏、面包屑、多标签、状态栏信息、目录树的
折叠箭头与引导线。每一处都先在浏览器里真点过、截了图，再提交。

### 界面结构
- **活动栏 + 四栏布局**：活动栏（文件 / 会话 / 信息）│ 侧栏 │ **编辑器** │ 对话。
  看代码时对话不挡路；会话列表从右侧搬进侧栏。
- **文件在中间全宽打开**（只读，带行号与高亮）—— 以前只能在左侧那块几百像素宽的面板里看。
- **撤掉「代码」「变更」两个面板**：文件已经在中间看了，左侧面板重复。
- **菜单栏**：文件 / 视图 / 帮助。「打开文件夹…」接上了工作区机制，也能手填路径。
- **文件树可指向任意目录**：顶部选择器 + 「指定目录」，打包版能调起系统文件夹对话框。

### 编辑器
- **多标签**：同时开多个文件，点标签切换、`×` 或**中键**关闭；切标签不重新读盘
  （内存缓存），同时最多 10 个（超了会提示，不做静默淘汰）；`↻` 重新读取当前文件。
  关掉当前标签自动切到右邻，全关回欢迎页。
- **面包屑**：`工作区 › 目录… › 文件名`；点目录段会在文件树里展开并定位过去。
  深路径省略中间段，保证看得到末两级和文件名。
- **状态栏**：右下显示当前文件的 `语言 · 行数`（截断时标 `+`）。
- **目录树**：折叠箭头 + 缩进引导线。

### 顺手修掉的真问题
- **「在指定目录里点文件夹是空的」**：目录展开那处 `/fs/tree` 调用漏带了工作区参数。
  已修，并在前端检查里加了回归护栏（不允许任何绕过 `fsUrl()` 的裸 `/fs/` 调用）。
- **帮助 → 关于点了没反应**：设置页的页签叫 `about`，而菜单里写的是 `info`
  （`info` 是左侧栏那个面板，两个概念）。已修 + 护栏：扫描所有 `openSettings('x')`，
  参数必须命中真实存在的页签。
- **`.svg` 在状态栏被标成 JavaScript**：复用了给高亮器用的 `langOf()`（认不出就回退 `js`）。
  状态栏写错的信息比不写更糟 → 改成按扩展名单独判定，认不出就写「文本」。

### 已知取舍（写在明处，不是忘了）
- `Esc` 仍然关闭当前标签（VS Code 的 Esc 只关浮层）。界面上一只写着「Esc 关闭文件」，
  改成不响应才是行为倒退。优先级不变：浮层 → 菜单 → 文件。
- 状态栏**不做**「行列 / 选中字符数」：文件预览是只读渲染表格，没有光标，
  造一个假的坐标是误导。也不显示编码（内核没告诉我们编码）。

## v0.2.0 — 组件库、图标与布局（2026-09-28）

这一版主要是界面，另外修了一个「升级后看不到新界面」的真问题。

### 引入组件库与图标（都是按需 vendored，仍然没有构建步骤）

桌面界面一直是纯 HTML/CSS/JS、无构建步骤（见 `docs/adr/0001`）。这次也没有为了
组件库打破这条规矩：两个库都只把**用到的文件**抄进 `web/desktop/vendor/`。

- **Web Awesome**（MIT）：按 ES module 的 import 图只取闭包里的文件，
  60 个模块 + 31 个样式 = **510KB**（整包 2366 个文件 / 17MB）。
  主题不各管各的：`wa-theme.css` 把它的 token 映射到我们自己的 token，
  `applyTheme()` 再把 `data-theme` 同步成 `wa-dark` / `wa-light`，
  于是组件天生跟着深浅主题走，不用各维护一套。
- **Lucide 图标**（ISC，无需署名）：只带 **37 个 SVG / 19KB**（整包 2118 个 / 1MB）。
  替掉了原来的 Unicode 字符和 emoji —— 那些是**文本**，emoji 还自带颜色、
  各家系统渲染不同，跟这套深色 IDE 界面放一起很打架。
  现在线宽统一、颜色跟 `currentColor`、尺寸跟 `font-size`，深浅主题都不用另写样式。

要加组件或图标，改 `web/scripts/vendor-*.mjs` 里的清单再重跑；
`vendor/` 目录是生成物，不要手改。

### 布局：面板左右互换

「文件 / 代码 / 变更 / 信息」从右侧移到左侧，与会话栏位置互换。

**这不是挪个 div 就完事**：这两块是可拖拽调宽的，有三处方向性依赖要一起换 ——
DOM 顺序、内侧分隔线的方向（`border-right` ↔ `border-left`）、以及 splitter 的
`invert` 标记。最后那个最容易漏：它控制拖拽的加减方向，面板换边之后不跟着换，
手感就变成反的（往左拖反而变窄）。

### 输入框改成一体式

文本域与控件现在在**同一个**圆角容器里：左边是当前对话模型胶囊（点开跳「模型服务」），
右边是提示 / 用量 / 发送按钮。

顺带删掉一个**死控件**：原来挂在输入框外面的「自动执行工具」勾选框，
全项目只有 `index.html` 引用它，没有任何 JS 读它，内核协议里也没有「审批工具」
这个概念（发送只有 `{type:'chat', text, session_id}`）—— 点不点完全没影响。
那个位置换成了有真数据来源的模型胶囊（`/api/desktop/chat-readiness` 的
`{ready, label, model}`），未配置时用警示色提示。

### 修的

- **升级后 WebView 会继续跑旧界面**。静态资源只发 `etag`、没有 `Cache-Control`，
  浏览器于是按「启发式新鲜度」自己缓存 —— 症状是**新 HTML 配旧 JS**（比全旧更难查：
  我改完 `app.js`，页面里执行的还是上一版函数，白排查一轮）。
  现在两处治：`/_desktop/` 的响应加 `no-store`；首页的资源引用加
  `?v=<mtime>` 指纹（无构建步骤的等价物）。
- 状态栏的**版本号以前一直是空的**（没有任何代码去填那个 span），而这份日志里
  却写着「看右下角版本号对照自己装的是哪一版」。现在它真的会显示 `v0.2.0` 了 ——
  数据来自 `/api/desktop/info` 的 `uiVersion`。

### 已知

浅色主题下，**原生外观的表单控件**（侧栏搜索框）在深色系统的 iOS WebView 里
仍渲染成深底。已排除：不是组件库带来的（禁用其样式表后依旧）、不是 CSS 优先级
（连 `background: yellow !important` 都不改计算值）、不是缓存（浅色冷启动依旧），
而且**同样标记的新建元素渲染是正确的** —— 说明不是我们的 CSS 问题，是这个
WebView 对某个已存在节点的重绘异常。产品跑在 Windows WebView2 上（另一个引擎），
需要在真机复测。没有证据的绕法没有留在代码里。

## v0.1.2 — 发消息不再弹黑框（2026-09-27）

症状：每发一条消息，就弹出一个黑框，标题是 `C:\Program Files\Git\bin\bash`。

原因：`console=False` 打出来的 exe 是 **GUI 子系统**进程，本身没有控制台。
这种进程再去启动一个控制台子程序、又不带 `CREATE_NO_WINDOW` 时，Windows 会
给这个子进程**新分配一个控制台**；Windows 11 把「新建控制台」交给 Windows
Terminal 处理，所以你看到的是一个带标签栏的黑窗口。

而 agent 每次跑命令都要起一次 shell（`shell_execute` → `bash`），所以是一发
消息弹一次。内核其实**知道**这件事 —— `chrome_launcher.py` 启动 Chrome 时
是老老实实带上 `CREATE_NO_WINDOW` 的 —— 只是跑 agent 命令的那条路径漏了。

### 改了什么

- 新增 `desktop/no_console.py`：在进程没有控制台时，给所有子进程补上
  `CREATE_NO_WINDOW`（用 OR，不会覆盖调用方自己设的标志）。
  同一处补丁同时覆盖 `subprocess.run/Popen` 和
  `asyncio.create_subprocess_*`（后者走的是
  `asyncio.windows_utils.Popen`，它 `super().__init__` 上来）。
  实现放在 `desktop/`，内核 `src/` 依旧一行未改。
- 顺带发现：**Python 3.12 已经从 `asyncio.windows_utils` 里移除了类级的
  `SW_HIDE`**，所以终端抽屉和插件进程本来也会弹窗 —— 一并修好。
- 逃生门：如果某个 Windows 程序确实需要真实控制台句柄，
  设 `OPENMINIS_DESKTOP_SHOW_CONSOLE=1` 即可关掉这个补丁，不用重新打包。
- 启动日志会写明补丁状态，便于排查。

### 另一处修正：密钥路径写错了

设置页原文写「密钥保存在 `%LOCALAPPDATA%\openminis`」，但内核在 Windows 上
用的是 `%USERPROFILE%\openminis`（`LOCALAPPDATA` 只放缓存目录）。
既然这句话是在交代密钥存哪，就不该是猜的 —— 现在改成从
`/api/desktop/info` 读真实路径显示。


文中的日期是构建当天。下载页永远指向最新版；要对照自己装的是哪一版，看窗口标题栏右下角的版本号。

## v0.1.1 — 「配了却没生效」的修复（2026-09-25）

v0.1.0 的「模型服务」页有一个真正会把人挡住的缺陷：**它没有保存按钮**。
配好服务商之后找不到任何提交入口，也没法确认到底存没存上；而去点「人格」的
保存是白点的 —— 那个按钮走的是 `/api/system/soul`，跟服务商完全无关。

结果就是：服务商卡片看起来配得好好的、用途绑定也选了，但服务端的
`activeProviderId` 始终是空的，会话于是永远回一句「还没有配置模型服务」。

### 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| 模型服务页 | **没有保存按钮** | 底部「保存 / 重新载入」，顶部还有全局保存 |
| 未保存状态 | 完全不可见 | 标题栏出现「● 有未保存的改动」，关闭时拦截确认 |
| 添加服务商 | 只是加了一行，不激活 | 自动把它设为「当前对话」—— 否则配完仍然用不了 |
| 服务商卡片 | 只有删除 | 增加「设为当前对话 / 测试连接 / 拉取模型列表 / 其余用途自动绑定」 |
| 出错时 | 会话里一句话，不知道去哪修 | 「设置」按钮高亮提示，进去就是原因 |
| Agent 页输入框 | 改了什么都不会亮 | 同样计入未保存 |
| 冒烟测试 | 读真实用户目录，结果随机器而变 | 用临时 profile，断言才可靠 |

### 两个新增的后端接口（都在 `desktop/`，内核 `src/` 依旧一行未改）

- `POST /api/desktop/test-provider` `{id}` —— **真的发一次最小请求**
  （`max_tokens=16`），走的是和对话完全相同的 `build_provider` 路径，
  所以它给的结论就是对话会遇到的结论。上游已有的
  `/api/settings/fetch-models` 只探测 `GET /models`，地址和密钥通、模型 ID
  写错它也是绿的 —— 这个补上了模型 ID 这一环。
  失败会分类成可操作的建议（密钥被拒 / 404 路径或模型 / 限流 / 连不上 / 超时）。
- `GET /api/desktop/chat-readiness` —— 事先告诉你这轮对话能不能跑起来，
  免得再吃一次同样的错。

两个接口都读**已保存**的配置，所以界面上的「测试连接」会先保存再测 ——
这样测的和看到的是同一份东西。

### 一个值得记住的内核细节

`activeProviderId` **不由 providers 列表推导**，而是由「用途绑定 → 对话」
这一行写入的（`store.apply_full` 把 `modelSlots.chat` 翻译成
`activeProviderId` + 该实例的 `model`）。所以「加了服务商」和「对话能跑」
是两件事，界面必须显式把前者接到后者上。
