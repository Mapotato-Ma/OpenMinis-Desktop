# 计划：界面缩放 + cc-switch 供应商导入

## 0. 边界（先划清，避免把架构搞重）
- 目标 A：界面缩放比例，设置里可调 + 快捷键，行为对齐 VS Code
- 目标 B：从 cc-switch 导入供应商，可勾选
- 不引入构建步骤（ADR 0001）；内核 `src/` 不改；没证据的改动不留
- 这轮不上 Monaco（已定）

## A. 界面缩放

### 已核实现状
- 前端**没有任何** zoom/scale 实现；`style.css` 尺寸全是 px（52KB、上千个值）
- 桌面本地偏好走 localStorage：`om.themeMode`（app.js:1573）、`om.inspector`、面板宽度 `om.*`（app.js:1239-1259）
- 快捷键集中在 app.js:1647 的 `onKeydown`（已有 Ctrl+` 终端、Ctrl+, 设置）
- 设置页 nav 是 index.html:245 的静态按钮（models/soul/identity/skills/agent/about）；
  设置内容由**内核 schema 驱动 + 全量替换 PUT，未知字段会被拒** → 缩放**不能**进这个 payload

### 设计
1. 存储：localStorage `om.uiScale`（默认 1），与主题同级：即时生效、即时持久化，不进内核 payload
2. 应用：`applyUiScale(z)` 只做一件事 → `document.documentElement.style.zoom = z`；启动时读回
3. 档位（VS Code 风格）：`[0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0]`
   - `Ctrl+=` 放大、`Ctrl+-` 缩小、`Ctrl+0` 回 100%
4. 三个入口：① 菜单栏「视图」→ 放大/缩小/重置缩放（右侧显示快捷键）
   ② 设置新增「界面」pane：主题（与顶栏菜单同源）+ 界面缩放（− / 当前百分比 / + / 重置）
   ③ 快捷键
### A 的唯一真风险与对策
Chromium 的 CSS `zoom` 会影响「鼠标坐标 ↔ getBoundingClientRect」的换算关系，
而全项目只有**一处**用到这个换算：面板宽度拖拽（app.js:1239-1259）。

- 对策：拖拽处不写死 `delta = ev.clientX - startX`，改成**自校准**：
  `ratio = el.getBoundingClientRect().width / el.offsetWidth`（100% 时为 1，被 zoom 拉伸时为 zoom）
  → `newWidth = startWidth + (ev.clientX - startX) / ratio`
  这样在两种 Chromium 语义下都对，且可以在浏览器里 100%/150% 各测一遍。
- 验证：缩放后截图核对设置弹窗居中、编辑器行高、终端面板、滚动条、`position:fixed` 覆盖层
- 备选（只在 Windows 实测发现坐标问题时才做，本轮不实现）：走 WebView2 原生 `ZoomFactor`
  （pywebview 的 native handle），代价是只在 WebView 内生效、浏览器打开 UI 时失效
- `splash.html` 是独立页面，不受影响

## B. 从 cc-switch 导入供应商

### 已核实现状
- cc-switch = `farion1231/cc-switch`（138k★，Rust/Tauri，支持 claude/codex/gemini/opencode/
  openclaw/grok/hermes 等）
- 存储：**SQLite** `~/.cc-switch/cc-switch.db`
  （`src-tauri/src/config.rs`：`get_app_config_dir()` = `HOME/.cc-switch`；`get_app_config_path()` = 同名目录下 `config.json` 仅存应用级设置）
- 表结构（`src-tauri/src/database/dao/providers.rs` 的 INSERT 列表）：
  `providers(id, app_type, name, settings_config JSON, website_url, category, created_at,
  sort_index, notes, icon, icon_color, meta JSON, is_current, in_failover_queue)`
  + `provider_endpoints(provider_id, app_type, url, added_at)`
- `settings_config` 形状按 app_type 不同，已拿到一条实证：
  gemini → `{"env": {"GEMINI_API_KEY": ..., "GOOGLE_GEMINI_BASE_URL": ...}}`
  codex → `{"auth": {"OPENAI_API_KEY" | "tokens", "auth_mode": ...}, "config": "<toml>"}`
  （claude/其它家的形状本轮远端读源码被网络挡住，未拿到 → 见「实施前必须先验证的一件事」）
- OpenMinis 供应商字段（settings-model.js:62 / 内核 main.py:490）：
  `{id, type, label, baseUrl, model, hasKey, engine}`；草稿里的明文 key 存在 `draft.keys[id]`，**从不进 view()/DOM**
- 内核类型表只有 6 个：`anthropic`、`openAI`（两者引擎就绪）、`gemini`、`openRouter`、`xAI`、`kimiCode`（后四引擎未移植）
- desktop 路由必须加进 `desktop/ui_mount.py::desktop_routes()`，顺序受 `ordering_problems()` 契约测试保护
### 设计
1. **新路由 `GET /api/desktop/import/cc-switch`**（`desktop/ui_mount.py`）
   - 探测候选路径：`%USERPROFILE%\.cc-switch\cc-switch.db` → 旧版同目录 `config.json` → `~/`
   - SQLite **只读**打开（`file:...?mode=ro`，`PRAGMA query_only`），不锁库、不写任何东西
   - 返回：`{available, searched:[...], candidates:[{id, app_type, name, suggestedType,
     baseUrl, model, hasKey, keyMask, existsAlready, warnings}], dbPath}`
   - **响应体不含明文 key**（只有掩码 `sk-…abcd`）
2. **新路由 `POST /api/desktop/import/cc-switch`**，body `{ids:[...], customPath?}`
   - 只在用户明确点「导入选中」后调用，**这一次**才回明文 key，与「手输 key」同等信任级别、只走 127.0.0.1
   - 响应：`[{type,label,baseUrl,model,apiKey,engine}]`
3. **前端**（模型 pane 的 `+ 供应商` 旁加「从 cc-switch 导入」）
   - 弹层：每行 = 勾选框 + 名称 + 类型（可下拉改）+ baseUrl + key 掩码 + 来源标签
   - 已存在（baseUrl 归一化去重）默认不勾选并标注
   - 确认后逐条 `dispatch({type:'provider/add'})` + 写 `draft.keys[id]`，**不自动保存**：
     仍走现有「改动 → 保存」，用户能先看一遍再点保存
4. **类型映射**：`claude→anthropic`、`codex→openAI`、`gemini→gemini`、`grok→xAI`、`hermes→openAI`
   - 无法映射的（opencode/openclaw/mcode/pi…）若能从 settings_config 里提取到 base_url，
     以「openAI 兼容」建议导入；否则**灰掉并写明原因**，不猜
   - `model` 只在能明确提取到时带上（不许编）
5. **安全性**：不打印 key / 不打印请求体；候选列表只给掩码；不写日志；
   `desktop/logging` 里不做 request body dump；导入路径仅在 127.0.0.1
6. **提取器设计（抗 schema 漂移）**：已知键优先 + 大小写不敏感递归兜底
   （`*BASE_URL*` / `*AUTH_TOKEN*` / `*API_KEY*`），并记录「用哪条规则命中的」写进 warnings

### 实施前必须先验证的一件事（不许凭猜写映射）
claude/codex 的 `settings_config` 精确键名本轮没拿到（raw.githubusercontent 反复失败）。
→ 实施第一步：在 Windows 上用 `sqlite3`（或 python3 的 sqlite3）读一条真实 row，
把 `app_type` × `settings_config` 形状记录到 `.scratch/cc-switch-shape.md`，再定提取规则。
没有这一步就不动提取代码。

## C. 验证（能红的才算数）
1. **纯函数测试**
   - 缩放：档位步进/夹取/回默认（含 0.05 之类的非法值容错）
   - 导入：`cc-switch row JSON → 候选`（掩码、类型映射、已存在去重、提不出来的字段标 warnings）
   - 提取器：喂 4 条假 settings_config（gemini 形 / codex 形 / claude 形 / 未知形）
2. **契约测试**
   - `ordering_problems()` 必须覆盖两条新路由（漏了会静默失效 —— 这是本仓的既有机制）
   - `ui-check.mjs` 新增断言：① `applyUiScale` 是唯一写 `documentElement.style.zoom` 的地方
     ② 导入弹层代码里不出现把 `apiKey` 写进 DOM 文本/属性的路径 ③ 缩放不进 settings payload
3. **浏览器实测（本机）**
   - 100% / 125% / 150% 三档截图：设置弹窗、编辑器、左侧栏、终端面板、滚动条
   - 三档各拖一次面板宽度，确认拖拽跟手（自校准 ratio 是否生效）
   - 假 DB（自造一个 `cc-switch.db` 塞两行）走一遍导入弹层 → 勾选 → 草稿出现供应商 → 点保存
4. **Windows 真机（用户）**
   - 缩放后拖面板宽度、Ctrl+= / Ctrl+- / Ctrl+0
   - 用真实 cc-switch 库导入一次（这一步同时验证「settings_config 形状」假设）

## D. 本轮不做（避免架构变重）
- 不做「导入后自动保存 / 自动探活所有模型」
- 不引入 Monaco / 任何编辑器内核
- 不改内核 `src/`、不加构建步骤、不加新依赖（SQLite 用 Python 标准库 `sqlite3`）
- 不做导入的「反向导出」

## E. 待用户拍板
1. 缩放档位用固定档（推荐）还是连续 10% 步进？
2. 缩放范围 0.8–2.0（VS Code 是 0.5–5）够不够？
3. 「界面」pane 是否顺手把主题开关也搬进去（顶栏主题菜单保留）？

---

## F. 独立评审结论与计划修订（两模型 20 条，已逐条核实）

评审通道记录（别再踩）：三个模型都是「一直思考」型 —— 小请求 2 秒回完，
评审级 prompt 会把整个 `max_tokens` 烧在推理上、正文返回**空串**；非流式还有 ~60 秒的墙。
可用姿势：**后台 `setsid` + `--stream --output`（流式不吃那个墙）+ 大 max-tokens + 30–90 秒轮询**。
注意 `--stream --output` 只保留最后一个事件，`--stream` 直接重定向 stdout 会被 TTY 门控（拿到 1 字节）。

### A 段（缩放，GLM 10 条）
**核实为真、已进计划：**
1. **WebView2 自带 Ctrl+= 缩放会和 CSS zoom 叠加**（页面 `preventDefault` 拦不住浏览器加速键）
   → 硬前置：host 侧尝试 `AreBrowserAcceleratorKeysEnabled=False`。`desktop/window.py` 已有
   `js_api=WindowAPI`（含 `platform()`），加一个 host 方法即可 —— 属壳层，不碰内核。
   pywebview 源码本轮没取到（网络），属性路径**未证** → 按「best-effort + 可降级」实现。
2. **「界面」pane 必须与设置 payload 隔离**：pane 手写渲染，绝不进 settings 表单的序列化 state；
   加回归用例「打开界面 pane 后保存 models pane 不 400」。
3. **ratio 自校准在 dragstart 测一次**，不要逐帧重算（宽度过渡会漂移）；
   删掉注释里「两种 zoom 语义」的旧推理（Chromium ≥128 已标准化 zoom）。
4. **三个入口收敛成单一 setter + 事件广播**，否则菜单读数与 pane 显示必漂移。
5. **冷启动先闪一帧 100%**：`index.html` head 已有同步 `window-bootstrap.js`（已核实）→ 缩放读回放这里。
6. **localStorage 读回要防御**：parseFloat + clamp + 非法回 1。
7. **键位用 `ev.code`**（Equal/Minus/NumpadAdd/Digit0），别用 `ev.key`（Shift 下变 '+'、非美式布局更糟）。
8. **换算点机械收口**（已 grep 核实）：`web/desktop/*.js` 共 4 处读 rect —— `1247/1258`（面板宽度，唯一算术点）、
   `1444`（菜单定位）、`1614`（主题菜单锚点）；无 iframe、无 canvas/xterm。
   → 评审第 3 条「canvas 会糊」在本项目**不成立**（文字是矢量重栅格化）。

**降级/不采纳：**
- 「CSS zoom 会糊 canvas」→ 已核实项目内无 canvas/webgl/xterm，SVG 图标是矢量，不成立（保留为将来引入 canvas 的注意项）。
- 「改原生 ZoomFactor-only」→ 仍作**备选**保留，触发条件从「坐标问题」改为「实测出现双重缩放或清晰度问题」。

### B 段（cc-switch，DeepSeek 10 条）—— 核实后全部采纳
1. **映射面本身就是猜** → 实证范围从「claude/codex 键名」扩大到**全部 app_type**：
   `SELECT app_type, name, settings_config FROM providers` 连同 `provider_endpoints` 一起看，
   先产出 `.scratch/cc-switch-shape.md` 再写任何提取代码。
2. **明文 key 生命周期要写死**：`draft.keys` 仅内存，关弹层/取消即清；导入响应加 `Cache-Control: no-store`；
   确认内核没有 request/response body 日志中间件；**掩码对 ≤8 字符的 key 只给固定占位符**
   （`sk-…abcd` 对短 key 等于全量泄漏）。
3. **`mode=ro` 在 WAL 库上不稳**（`-wal`/`-shm` 缺失或无权限会打不开）：
   **绝不回退成读写打开**（那会往用户 cc-switch 目录写文件）；失败映射成「请先退出 cc-switch 再试」；
   设 `busy_timeout`；实测要覆盖「cc-switch 正在运行」这一态。
4. **URI 构造**：`file:...?mode=ro` 必须 `uri=True` + Windows 路径转义（盘符冒号/反斜杠）
   → 统一用 `pathlib.Path.as_uri()`；补一条「反斜杠 + 中文用户名」用例。
5. **未移植引擎（gemini/xAI/openRouter）默认灰掉并写明「引擎未支持」**，别等运行期才炸。
6. **递归兜底会命中错值**：每行只接受唯一命中，命中数 ≠1 就置空并提示「请手填」；
   codex 的 `auth.tokens`（OAuth）不算 API key → 该行灰掉并说明不支持 OAuth 凭据。
7. **去重键写死**：小写 scheme+host、去尾 `/`、去默认端口；键 = 归一化 baseUrl + type；
   baseUrl 为空的行不参与去重只标注（避免「同地址不同 model」误判已存在）。
8. **路由遮蔽要先 grep**（前缀/静态/兜底路由）；GET 与 POST 紧邻注册；以 `ordering_problems()` 通过为完成标准。
9. **POST 逐条返回 `{id, ok, reason}`**，单条失败不整批中止；
   locked / 表结构漂移 / TOCTOU（GET 与 POST 之间用户改了 cc-switch）/ 缺 key 各写一句人话。
10. **砍掉 `customPath`**（等于开放任意本地 SQLite 读取）；`ids` 强制 int 列表 + 数量上限 + 参数化查询。

### 修订后的开工顺序（含硬前置）
1. **B0 实证**：读用户机器上真实 cc-switch 库 → `.scratch/cc-switch-shape.md`
2. **A0 硬前置**：host 关加速键（关不掉就改判原生 ZoomFactor）+ 界面 pane 与 payload 隔离方案
3. A 段实现（单一 setter + 三入口 + 快捷键 + 界面 pane + bootstrap 应用 + 防御读回）
4. A 段验证（三档截图 + 拖拽跟手 + 浏览器模式各一遍）
5. B 段后端（两条路由 + 只读探测 + 提取器 + 逐条结果）
6. B 段前端（导入弹层 + 勾选 + 去重标注 + 写进草稿不自动保存）
7. 契约/纯函数测试 + `scripts/check.py` 全量 + 打包

---

## G. 用户追加（2026-09-29）：缩放范围 + 顺手做的一批本地设置

### 缩放定稿
- **范围 0.5–5**，档位走 **VS Code 的 `zoomLevel` 语义**：`zoom = 1.2^n`，n 为整数，夹到 [0.5, 5]
  （跟 VS Code 完全对齐：n=0 → 100%，n=8 → 约 430%，n=8 再往上有 5.0 上限；n=-4 → 0.482 → 夹到 0.5）
- 存储 `om.uiZoomLevel`（整数 n），显示成百分比；`Ctrl+=` / `Ctrl+-` / `Ctrl+0`（归零到 n=0）
- 菜单与 pane 显示同一份状态，读写都走唯一 setter（评审第 4 条）

### 顺手一起做的桌面本地设置（全部 localStorage，全部不进内核 payload）
载体：**新增 `web/desktop/ui-prefs.js`**（IIFE，单一出口 `window.UiPrefs`），
一张 `PREFS` 注册表 {key, 默认值, 解析/夹取, 应用方式}，对外只有 `get/set/subscribe/applyAll`。
好处：app.js 只负责渲染行、调 `UiPrefs.set`；「单一 setter + 事件广播」天然满足，不会长出 9 处散落 handler。

| # | 设置 | 实现方式 | 成本 |
|---|---|---|---|
| 1 | 界面缩放 | `documentElement.style.zoom`（= 上面的定稿） | 中 |
| 2 | 主题（深/浅/跟随系统） | 搬进 pane，复用现有 `applyTheme` | 低 |
| 3 | 界面字体 / 等宽字体 | 改 `--sans` / `--mono` 两个 CSS 变量，下拉预设 + 自定义输入 | 低 |
| 4 | 界面密度（舒适/紧凑） | `body.compact` 覆盖一组 padding/行高 | 低 |
| 5 | 减少动效 | `body.no-motion`（`* { transition/animation: none !important }`） | 极低 |
| 6 | 显示状态栏 | `body.no-statusbar`（`--statusbar-h: 0` + 隐藏） | 极低 |
| 7 | 启动时恢复上次会话 | 复用现有 `WS_KEY` / `om.tab` 恢复逻辑，加开关 | 低 |
| 8 | 回车发送 vs Ctrl+回车换行 | 复用 app.js:1669 现有判定，加开关 | 低 |
| 9 | 帮助 → 键盘快捷键一览 | 手写表（现有 8 条 + 新增缩放 3 条） | 极低 |

**新引入的一处风险（要专门处理）**：第 3 项的自定义字体名会进 CSS 变量。
→ 只用 `style.setProperty()`（不会解析成多条声明），加长度上限 + 去掉 `;{}` 与换行，
**绝不拼进 innerHTML 或字符串样式**；ui-check 加一条断言盯着这个。

**不做（写明理由，避免以后又被问）**：
- **单独「界面字号」**：与缩放是两套机制，会互相打架 → 由缩放负责，不做第二套。
- 编辑器行号/字号/Tab 宽度：代码视图是**只读**的，收益低 → 本轮不动。
- 内核级设置（供应商、模型、工具、记忆…）：那些属于 settings payload，走内核 schema，不在本轮范围。

## H. 真实 cc-switch 库实测（2026-09-29，公司电脑，脱敏输出）

库：`C:\Users\22679\.cc-switch\cc-switch.db`，2.8MB，SQLite 3.50.4。
**目录里没有 -wal/-shm** → 只读打开不必和 WAL 附带文件打架（兜底提示仍保留）。
`providers` 实际 18 列，和我从源码读到的有**三处不一样**：

- **`id` 是 TEXT**：UUID（`32ba9054-…`），也有 `gemini-official`、
  `产研公共token-1785201979407` 这种 → 后端/前端全改成字符串 id。
  （我原来按自增整数写，会把整个库挡在门外。）
- 多出 `provider_type`（'custom'/'official'）、`cost_multiplier`、
  `limit_daily_usd`、`limit_monthly_usd`。
- `is_current` 是 BOOLEAN、`meta` 是 TEXT(JSON)。

17 行供应商，app_type 实际只有 4 种：`claude`(13) / `codex`(4) / `claude-desktop` / `gemini`；
没有 opencode/openclaw/grok/hermes 的行 → 映射表按这 4 种做，其余留兜底。

取值形状（决定提取规则）：

- **claude**：`env.ANTHROPIC_BASE_URL` + `env.ANTHROPIC_AUTH_TOKEN`（13 行无一例外）
  + `env.ANTHROPIC_MODEL`，另有一大堆 `ANTHROPIC_DEFAULT_*_MODEL`（值常不同）
  → 「唯一命中」会把模型误判成歧义留空，**模型必须先精确取 `env.ANTHROPIC_MODEL`**（已改）。
  部分行还带 `model` / `permissions.allow[]` / `hooks.*` / `autoUpdatesChannel`。
- **codex**：`auth.OPENAI_API_KEY` + `config`（**TOML 字符串**，内含 `model = "…"` 与
  `[model_providers.custom] base_url = "…"`）→ 从 TOML 里正则抠 base_url/model 这条路是对的（实测通过）。
- **占位行**：`claude-official` / `gemini-official` / `codex-official` 的 settings_config 是空的
  → 界面默认不勾、标「地址缺失」。
- 内网地址（`http://192.168.20.200:3000`、`http://192.168.50.18:3333`）能正常带出来。
- 端到端实测（本地按真实形状造的假库，UUID id + TOML + 占位行）：
  清单 → 勾选 2/3 → 导入 → 供应商 1→3、进草稿未保存、**DOM 里无明文密钥**。
