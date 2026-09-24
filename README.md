# OpenMinis Desktop

**把 [OpenMinis](https://github.com/OpenMinis/OpenMinis) 的 agent 内核搬进 Windows 桌面，
做成一个类似 Cursor 的原生窗口应用。**

OpenMinis 官方客户端目前只有 iOS / Android / macOS。这个项目补上 Windows：
同一套 agent 内核（工具调用、技能、记忆、soul、会话持久化），换成原生窗口 +
IDE 风格的桌面界面，并且用 GitHub Actions 直接产出免安装的 `.exe`。

```
┌──────────────────────────────────────────────────────────────────────┐
│  ◈ OpenMinis  Desktop        [workspace]      ⌘K  终端  面板   ◐  ?  │
├──────────┬───────────────────────────────────┬───────────────────────┤
│ ＋ 新会话 │  对话（流式 + 工具卡片）           │ 文件 / 代码 / 变更     │
│ 过滤…     │                                   │  ├ src/                │
│ ▸ 会话 A  │  ❯ shell_execute  ls -la   ✓12ms │  │  └ app.py  ← 高亮    │
│ ▸ 会话 B  │  agent 回复…                      │  └ 变更：+/− diff      │
├──────────┴───────────────────────────────────┴───────────────────────┤
│  终端  $ npm run build                                                │
├──────────────────────────────────────────────────────────────────────┤
│ ● 已连接   session:3f16…             win32            v0.1.0         │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 它是什么 / 不是什么

**是**：一个能真正在 Windows 上跑的桌面 agent 应用。原生窗口（WebView2），
内置终端、文件树、代码查看、agent 改文件的 diff、命令面板。数据落在本机
`%LOCALAPPDATA%\openminis`，会话与上游客户端格式一致。

**不是**：Cursor 的替代品。没有 LSP、没有多文件重构、没有代码补全模型。
它的定位是「带一个真 shell 的 agent 工作台」，编辑能力来自 agent 本身，
不是来自编辑器。

**也不是**：从零写的。内核 100% 来自开源上游，见下面的署名。

---

## 快速开始

### 方式一：直接下 exe（推荐）

到 [Releases](../../releases) 或 Actions 的 artifact 里下载
`OpenMinisDesktop.exe`，双击运行。**不需要装 Python、Node 或任何运行时。**

> 极少数老版本 Win10 会缺 WebView2 运行时，装一次微软官方的
> [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) 即可。
> Win11 与较新的 Win10 已内置。

首次启动后进 **设置 → 模型服务**，填一个 OpenAI / Anthropic / 任意
OpenAI 兼容网关的 API Key，就能开始对话。

### 方式二：自己构建

```bat
git clone <this repo>
cd OpenMinisDesktop
build-desktop.bat            rem onedir  -> dist\OpenMinisDesktop\OpenMinisDesktop.exe
build-desktop.bat onefile    rem onefile -> dist\OpenMinisDesktop.exe
```

需要 Windows + Python 3.11+，脚本自己建 `.venv` 并装依赖。

### 方式三：从源码直接跑（任何平台）

```bash
pip install -e . "pywebview>=5.0"
python desktop_main.py                 # 原生窗口
python desktop_main.py --browser       # 没有 GUI 工具链时，退回浏览器标签页
python desktop_main.py --no-window     # 只跑后端（服务器模式）
python desktop_main.py --upstream-ui   # 用上游移动端界面
python desktop_main.py --port 9000     # 固定端口
```

冒烟测试（不需要图形环境，CI 里也跑这个）：

```bash
python scripts/smoke_test.py
```

---

## 桌面版做了什么

上游的 Python 移植提供了 agent 内核 + FastAPI + 一个移动端形态的 Web UI。
桌面版在这之上加了三层，**没有改动内核一行**：

| 层 | 位置 | 说明 |
|---|---|---|
| 窗口外壳 | `desktop/` | pywebview 原生窗口（Windows 走 WebView2），uvicorn 跑在后台线程，单实例复用，窗口关闭即停后端 |
| 桌面界面 | `web/desktop/` | 纯 HTML/CSS/JS，**无构建步骤**，IDE 风格三栏 + 终端抽屉 + 命令面板 |
| 打包 | `packaging/`, `.github/workflows/` | PyInstaller 单文件 exe，CI 在 windows-latest 上构建并验证 |

### 为什么用运行时挂载而不是改内核

`desktop/ui_mount.py` 把桌面路由**插到 FastAPI 路由表最前面**，而不是去改
`src/openminis/server/main.py`。原因是内核里有个 `@app.get("/{full_path:path}")`
兜底路由，任何追加在它后面的路由都是死代码；插到前面既能生效，又让
「上游更新」和「我们的改动」在 diff 里泾渭分明，合并冲突几乎为零。

### 界面能力

- **流式对话**：WebSocket `/ws`，逐字渲染，markdown + 代码块语法高亮
- **工具卡片**：`toolStart`/`toolEnd` 折叠卡片，显示参数、输出、耗时、成功/失败
- **文件 diff**：`file_edit` / `file_write` 的改动在「变更」页以 LCS 行级 diff 呈现
- **文件树 + 代码查看**：按需展开，多语言高亮
- **内置终端**：命令经内核的 shell 工具执行，支持历史上下键
- **命令面板**：`Ctrl+K`，会话与操作统一检索
- **可拖动分栏**，宽度、主题、面板状态记在 `localStorage`

### 快捷键

| 键 | 作用 |
|---|---|
| `Ctrl+K` | 命令面板 |
| `Ctrl+\`` | 终端抽屉 |
| `Ctrl+B` | 右侧面板 |
| `Ctrl+N` | 新会话 |
| `Enter` / `Shift+Enter` | 发送 / 换行 |
| `Esc` | 关面板 / 停止生成 |

---

## 命令行参数

| 参数 | 说明 |
|---|---|
| `--host` | 绑定地址，默认 `127.0.0.1` |
| `--port` | 端口，默认 `8765`；被占用时自动换一个空闲端口 |
| `--browser` | 用浏览器标签页代替原生窗口 |
| `--no-window` | 只起后端（服务器 / 无头模式） |
| `--width` / `--height` | 初始窗口尺寸，默认 1440×900 |
| `--upstream-ui` | `/` 用上游移动端界面 |
| `--debug` | 打开 WebView devtools 与详细日志 |

再次启动时会检测端口上已有的实例并复用，不会起第二个内核、出现两套会话库。

---

## 署名与许可

本项目是 **[OpenMinis](https://github.com/OpenMinis/OpenMinis)**（GPL-3.0）的派生作品，
内核代码来自社区的 **[littlhub/PythonOpenMinis](https://github.com/littlhub/PythonOpenMinis)**
（Kotlin/Android 客户端的 Python 移植）。

因此本项目同样以 **GPL-3.0** 分发，完整许可见 [LICENSE](LICENSE)，
来源与改动范围见 [NOTICE.md](NOTICE.md)。

感谢上游作者把这么好的东西开源。

---

## 路线图

已经在做 / 想做的：

- [ ] 设置页（模型服务、soul、技能）直接内嵌进桌面界面，不用回退到移动端 UI
- [ ] 多标签工作区（同时开多个会话）
- [ ] 文件保存 / 编辑器内直接改（目前编辑能力全在 agent 侧）
- [ ] 深色/浅色主题跟随系统
- [ ] 会话搜索走 FTS 而不是前端过滤
- [ ] macOS / Linux 打包产物（内核已经跨平台，主要是 pywebview 与打包脚本的事）

架构与实现细节见 [docs/DESKTOP.md](docs/DESKTOP.md)。
