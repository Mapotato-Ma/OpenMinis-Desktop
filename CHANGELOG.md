# 变更日志

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
