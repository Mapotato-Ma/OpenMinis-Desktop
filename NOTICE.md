# NOTICE

## 这是什么

`OpenMinis Desktop` 是 **OpenMinis** 的派生作品（derivative work），
按上游的 **GNU General Public License v3.0** 分发。

## 上游来源

| 项目 | 地址 | 许可 | 本项目用到的部分 |
|---|---|---|---|
| OpenMinis | https://github.com/OpenMinis/OpenMinis | GPL-3.0 | agent 内核的设计、协议、技能格式、soul/memory 概念 |
| littlhub/PythonOpenMinis | https://github.com/littlhub/PythonOpenMinis | GPL-3.0 | **`src/` 下的全部 Python 代码、`web/dist/` 移动端界面、`tests/`、`pyproject.toml`、上游 `build.bat` / `run.bat` 等** |
| pywebview | https://pywebview.flowrl.com | BSD-3-Clause | 原生窗口（依赖，非代码复制） |

`src/`、`web/dist/`、`tests/`、`PORTING.md`、`PORTING_MAP.md`、`trace.md`、
`app.py`、`run.bat`、`stop.bat`、`setup.bat`、`tui.bat`、`build.bat`、
`pyproject.toml`、`uv.lock`、`LICENSE` 均原样来自 `littlhub/PythonOpenMinis`，
**除下面「与上游的偏离」一节列出的两处外**未作修改。

## 与上游的偏离（PORT-FIX）

`src/` 下的代码原则上一行不改（这样上游更新能直接 merge）。目前只有两处例外，
两处都是**上游自身的 bug**，都在注释里打了 `# PORT-FIX:` 标记：

| 文件 | 偏离内容 | 为什么 |
|---|---|---|
| `src/openminis/tools/search_files_tool.py`（`os_walk()`） | 用 `entry.is_symlink()` + `is_dir()` / `is_file()` 取代 `entry.is_dir(follow_symlinks=False)` | `Path.is_dir(follow_symlinks=…)` 是 **Python 3.13** 才有的参数；本仓声明 `>=3.11`、打包用 **3.12**，原写法在 3.12 上抛 `TypeError`，「搜索文件」工具静默失效 |
| `src/openminis/tools/path_utils.py`（`_sep_pattern()`） | 保留路径的**前导分隔符** | 原来 `prefix.strip("\\/")` 把前导分隔符剥掉，正则匹配不到它，而替换值是绝对路径 → POSIX 上多出一个前导斜杠（`//var/minis/workspace/…`）。`//host/path` 在 markdown 里是 protocol-relative URL |

两处都已在本仓 `tests/` 对应的用例上验证（`tests/test_firstagent_tools.py`、
`tests/test_sandbox_paths.py` 等 9 项从红转绿）。

上游修好之后，删掉这两处改动即可回到「一行未改」的状态 —— 判据是
`curl` 一份上游 `main` 的对应文件，确认 `follow_symlinks` 与 `strip("\\/")`
都已经不在。

## 本项目的原创部分

以下文件为本项目新增，同样以 GPL-3.0 授权：

```
desktop/                        原生窗口外壳（pywebview + 后台 uvicorn + 单实例）
  __init__.py
  app.py                        命令行入口
  paths.py                      源码 / 冻结两种布局的路径解析
  server_runner.py              后台线程跑 uvicorn + 健康探测
  ui_mount.py                   把桌面路由挂到内核 FastAPI 应用上
  window.py                     pywebview 窗口与 JS API
  assets/icon.ico               应用图标
  assets/icon.png

web/desktop/                    桌面界面（纯 HTML/CSS/JS，无构建步骤）
  index.html
  style.css
  app.js

desktop_main.py                 PyInstaller 打包入口
build-desktop.bat               Windows 本地构建脚本
packaging/OpenMinisDesktop.spec 桌面版 PyInstaller 配方
scripts/make_icon.py            图标生成
scripts/smoke_test.py           无图形环境的冒烟测试
scripts/stub_llm.py             本地 OpenAI 兼容桩服务（仅用于验证链路）

.github/workflows/build-windows.yml   Windows exe 构建与验证流水线
docs/DESKTOP.md                 架构说明
README.md                       本文档重写为桌面版说明
```

## 上游内核基本没有被修改

`src/openminis/**` 默认一行不改。桌面路由是通过 `desktop/ui_mount.py` 在运行时
插入到 FastAPI 路由表前端的，这样上游更新可以直接 merge。

目前有**两处**例外（都是上游自身的 bug，见上面「与上游的偏离」一节），
一旦上游修好就可以删掉。规矩是：任何对 `src/` 的改动都必须

1. 在代码里打 `# PORT-FIX:` 标记并写清原因，
2. 在本 NOTICE 的偏离表里记一行，
3. 改完把相关测试跑绿（`python scripts/check.py`）。

没有这三样，就不算改完 —— 隐形的分叉是最贵的那种。

## GPL-3.0 的义务

分发本软件（包括分发包中的 `.exe`）时：

1. 必须附带完整源代码，或提供获取源代码的途径；
2. 必须保留本 NOTICE 与 LICENSE；
3. 修改后的版本必须同样以 GPL-3.0 授权。

本仓库满足第 1 条 —— 构建 `.exe` 的全部源码都在仓库内，
且 `.github/workflows/build-windows.yml` 就是从这份源码构建的完整可复现流程。
