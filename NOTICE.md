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
**除下面「与上游的偏离」一节列出的各处外**未作修改。

## 与上游的偏离（PORT-FIX）

`src/` 与 `tests/` 原则上一行不改（这样上游更新能直接 merge）。目前的例外都是
**上游自身的 bug**，都在注释里打了 `# PORT-FIX:` 标记：

| 文件 | 偏离内容 | 为什么 |
|---|---|---|
| `src/openminis/tools/search_files_tool.py`（`os_walk()`） | 用 `entry.is_symlink()` + `is_dir()`/`is_file()` 取代 `is_dir(follow_symlinks=False)` | `Path.is_dir(follow_symlinks=…)` 是 **Python 3.13** 才有的参数；本仓声明 `>=3.11`、打包用 **3.12** → 打出的包里「搜索文件」工具静默失效 |
| `src/openminis/tools/path_utils.py`（`_sep_pattern()`） | 保留路径的**前导分隔符** | 原来 `prefix.strip("\\/")` 把前导分隔符剥掉，正则匹配不到它，而替换值是绝对路径 → POSIX 上多出一个前导斜杠（`//var/minis/workspace/…`）。`//host/path` 在 markdown 里是 protocol-relative URL |
| `src/openminis/tools/path_utils.py`（`resolve_workspace_path()`） | POSIX 上按 Windows 的实际语义处理绝对路径：只读根里的放行、其它一律拒绝 | 原来 `lstrip("/")` 把绝对路径变成相对路径再拼进工作区根 → 「技能库里的绝对路径永远找不到」。Windows 因为盘符在 `pathlib` 里会替换掉左操作数而看不出问题 |
| `src/openminis/tools/file_read_tool.py`（`_resolve_session_host_path()`） | 同上：只读根里的绝对路径原样接受 | 与上一条是同一件事的另一处。不改的话模型读不到技能目录里的 `SKILL.md` |
| `src/openminis/sandbox/execution_coordinator.py` / `tools/path_utils.py`（`session_workspace_root`）/ `ls_tool.py` / `search_files_tool.py` / `file_read_tool.py` / `file_write_tool.py` | 文件类工具（`ls`/`search_files`/`file_read`/`file_write`/`file_edit`）解析路径时，**先看会话是否已归属某个绑定了真实目录的工作区**（coordinator 新增 `sandbox_root_for()`），是就以那个项目目录为根；否则回落原来的全局 `external_files_dir` | 桌面版把「会话↔工作区」双向绑定后，**shell 已经能 boot 进项目目录，但文件类工具仍硬解析全局根** → 现场症状：agent 用 `ls`/`search_files` 只看到空的 `db-<sid>` 目录、读不到项目文件。各工具 `execute(args_json, session_id)` 签名早已带 `session_id`，据此改造；未绑定会话行为完全不变。回归护栏见 `tests/test_sandbox_paths.py::test_filed_session_resolves_inside_its_bound_workspace` |
| `src/openminis/provider/openai/openai_provider.py`（流式循环）/ `agent/agent_runtime.py`（回合循环） | 加**只读不改行为**的重复输出探针：流内检测 runaway 重复块、跨回合检测与上一回合高度重合的正文，命中打 `WARNING [repeat-diag] …`；每条流结束打一行 `INFO` 汇总（SSE 帧数 / 可见字符数 / finish_reason / 是否收到 `[DONE]`） | 追现场「大模型输出一直重复一大段」。**纯诊断**：不吞、不截断、不改流内容，只是让用户发回来的日志能区分「是中转在重复发 delta、还是模型自己在循环」。判据锁在 `src/openminis/core/repeat_diag.py` + `tests/test_repeat_diag.py` |
| `tests/test_plugins.py`（`test_bridge_does_not_top_up_when_nothing_was_delivered`） | 把夹具文件的 mtime 钉到过去 | 原用例依赖「刚写的文件早于随后取的 `time.time()`」，而 Windows 的 `time.time()` 只有约 15.6ms 时钟粒度、文件时间戳更细 → 旧图被当成新增补发。**这是用例的时钟赛跑，不是产品 bug**（产品侧那个 ≤1 tick 的窗口可以忽略） |
| `tests/test_perf_and_freshness.py`（并发两项） | 断言从「总耗时 < 0.55s」改成**屏障**：每个工具进门先登记、再等「所有人到齐」；另加三项变体检测隐性并发上限。同时把「结果顺序」断言从**完成顺序**改成**回填顺序**（对外契约） | 总耗时是**机器负载的代理**：空载 0.31s、忙时会超过 0.55s → 正确的实现被报成红。中途试过「时间区间有交集」，但那只是把「总时长敏感」换成「启动偏移敏感」；屏障完全不依赖计时。完成顺序取决于调度、不是契约，断言它本身就是偶发红的来源 |
| `tests/test_plugin_process.py`（两项） | 加 `skipif(os.name != "nt")` | 两项断言的是 Windows 专有行为：`.cmd/.bat` 需要 `cmd.exe` 套壳、盘符下的 node 候选目录。在 POSIX 上它们不可能成立 |
| `src/openminis/sandbox/guard.py`（`_resolve_outside()`） | 额外把**会话自己的工作目录**算作沙箱根（`cwd` 有父目录时才加） | 工作区可以绑一个真实目录（桌面版：把项目目录设为工作区），此时 shell 的 cwd 就是那个项目。原实现允许根只有 `<data>/workspace` + 技能库，于是**访问自己的工作区**也被判「目录越界」（实测：cwd 已经是项目目录、命令照拦；白名单也救不了 —— 它的键是精确目录，子目录不匹配）。`cwd=/` 这类没有父目录的路径不当根，避免一次放行整台机器 |
| `src/openminis/sandbox/guard.py`（`_git_bash_drive_path()`） | Windows 上把 `/c/Users/…` 形态翻成 `C:/Users/…` 再判越界 | Windows 上内核的 shell 是 **Git Bash**（`persistent_shell.py` 优先找 Git 自带的 bash），模型于是写 `/c/Users/…`；而 Windows 语义里 `ntpath.abspath('/c/x')` 是 `'\\c\\x'`（当前盘根下的 c 目录）→ 访问**自己的工作区根**也被判越界（用户实测拦截编号 `g-179067946811-c46651`）。只在 `os.name == "nt"` 时生效，POSIX 上 `/c/...` 是普通路径 |
| `src/openminis/sandbox/guard.py`（`_looks_like_path()`） | 正则/转义片段（`\r` `\n` `\s+` `\d{2}` `\x00`）不再算路径 | `_TOKEN_RE` 把引号当分隔符剥掉，于是 `tr -d '\r'`、`sed 's/\s\+//'` 里的 `\r`/`\s+` 各自成为独立 token；它们以反斜杠开头 → Windows 上 `ntpath.isabs('\\r')` 为真 → 被解析成盘根下的 `C:\r` → 判「工作区之外」而拦下。实测后果：agent **连续 14 次被拦**（`CRITICAL effect_tool_runaway tool=shell_execute streak=14`），正常文本处理命令一条都跑不了。只匹配「反斜杠 + 1~2 个字母 + 可选量词」与 `\xHH`，**不碰**真正以反斜杠开头的 Windows 路径（`\Windows\System32`、UNC `\\server\share`） |
| `src/openminis/server/chat_store.py`（新增 `clear_all_chat_data()`） | 新增一个「清空全部会话数据」函数；**会话/消息/压缩标记清掉，工作区（分组）保留** | 桌面版「一键清空数据（保留供应商）」按钮的后端。工作区属于**配置**不是聊天记录：它带着用户绑定的真实项目目录，删了会连带毁掉「agent 在项目目录里干活」这条链路（实测：清空后前端缓存的工作区 id 变悬空 → 「把会话放进工作区失败：工作空间不存在」→ shell 退回空的 `db-<会话id>` 沙箱 → agent 看不到用户的文件）。护栏：`tests/test_chat_sessions.py::test_clear_all_chat_data_keeps_workspaces` |
| `src/openminis/server/chat_store.py`（`_parts_with_runs()` / 新增 `parts_to_timeline()` / `ChatMessageInfo.timeline`）、`src/openminis/server/main.py`（回合时间线）、`src/openminis/server/chat_api.py`（接口加 `timeline`） | 在 `parts_json` 里额外存一份**有序可视时间线**（新 part 类型 `flow`，内容 = 正文段与工具卡按发生顺序交替） | `parts_json` 原来只存得下「一整块正文 + 一串工具卡」，**交替顺序丢了** → 重放历史只能"先画全部正文、再把所有工具卡堆在底部"（用户实测：关闭应用再打开、重开会话，所有工具执行都排在最下面）。用 `parts_to_text` / `parts_to_runs` 都不认识的类型标签，所以**模型上下文与工具卡都一字不变**；旧数据没有该 part，前端退回老画法，天然兼容。时间线里的卡片**只留 id 引用**（内容在 `tool` part 里已有，读时按 id join），否则 `parts_json` 会涨一倍。两处配套：`append_image_refs` 补写的 `![](路径)` 也要接进时间线（界面有 timeline 时不再渲染 `m.text`，漏了它生图回合重放就没图）；子代理回合的正文在 `subtext` part 里，也要进时间线，否则那几行是「空白气泡 + 一摞卡片」。护栏：`tests/test_chat_sessions.py::test_timeline_roundtrip_keeps_interleaving` / `test_timeline_does_not_touch_model_context` / `test_legacy_rows_without_timeline_fall_back` / `test_plain_turn_has_no_timeline` / `test_messages_api_returns_timeline` |
| `src/openminis/config/audit/config_audit_log.py`（`usage()`） | 裸 `Usage(...)` 改成 `self.Usage(...)` | `Usage` 是本类的**嵌套类**，而方法体里的名字解析**不查类作用域**（只查局部 → 闭包 → 全局 → 内置）→ 裸写必然 `NameError: name 'Usage' is not defined`（已实测复现）。该函数当时无调用方所以一直没暴露，config-audit 界面一接上就炸。上游自身的问题 |
| `src/openminis/tools/image_gen_tool.py`（导入区） | 加 `if TYPE_CHECKING: import httpx` | `_generate_once(client: "httpx.AsyncClient", …)` 用的是**字符串注解**，运行时从不求值（文件已有 `from __future__ import annotations`），但静态检查报 F821「未定义的名字」。补一个只在类型检查期存在的导入，运行时行为零变化 |
| `tests/test_scheduled.py` / `test_settings.py` / `test_skills.py` / `test_subagent_events.py` / `test_subagents.py`（共 21 处同名重定义） | 删掉**被遮蔽的那一份**死定义 | 上游的合并残留：同名函数定义两次，Python 只保留最后一个 → 前面那份永远不运行（F811）。其中 20 组逐字节相同，1 组（`test_skill_entry_exposes_declared_env`）死的那份把 `"metadata:\n"` 写成 `"metadata:/n"`（YAML 会解析失败），活的才是对的。删除前后 pytest 收集数**都是 124**，证明这些代码确实从未运行 |
| `pyproject.toml`（`[tool.pytest.ini_options]`） | `testpaths` 加上 `desktop/tests` | 桌面壳自己的测试（`desktop/` 是本项目新增的代码）不该混进上游的 `tests/` 目录里 |
| `src/openminis/provider/openai/openai_provider.py`（`_raw_stream_message` 里的 `repeat_diag` 导入） | `from ..core.repeat_diag` → **`from ...core.repeat_diag`**，并加 `# PORT-FIX(import-depth)` 注释 | 这个文件在 `openminis/provider/openai/` 里，比 `openminis` **低三层**，`..core` 只到 `openminis.provider` → 去找不存在的 `openminis.provider.core`。后果不是诊断功能失效，而是**所有 OpenAI 兼容的服务商**（OpenAI / 七牛云 / DeepSeek / 自建网关）一到流式那一步就 `ModuleNotFoundError`，对话根本走不下去 —— 从 v0.4.1 起跨了九个版本没人发现。`src/openminis/core/repeat_diag.py` 一直都在，只是少了一个点。同类：`src/openminis/plugins/drivers/__init__.py` 的 `from .base` → `from ..base`（在 `TYPE_CHECKING` 里，运行时无害，但同样是错的）。护栏：`desktop/tests/test_relative_imports.py` 用 AST 静态解析全部相对导入 |
| `src/openminis/sandbox/guard.py`（`_CD_RE`） | cd 目标的正则改成只取**第一个参数**：`(?:"([^"]+)"|'([^']+)'|([^\s\n|;&]+))`，取值处改成 `next(g for g in m.groups() if g)` | 原来是 `([^\n|;&]+)` —— 不停在空格，于是 `cd /c/Users/me/openminis/workspace 2>/dev/null` 抓下来的"路径"是 `…/workspace 2>/dev/null`，判定必然越界。**后果：agent 连自己的沙盒目录都进不去**，换个写法再试、再被拦，一轮对话空转到 20+ 个回合（真机日志 2026-10-01，用户描述「一直在回复」）。三种写法都要认：双引号 / 单引号 / 裸词（带空格的路径不能被截断）。护栏：`tests/test_guard.py::test_scan_escape_reads_only_the_first_cd_argument` |
| `src/openminis/server/main.py`（`_handle_stop` / `_register_running` / `_RUNNING_BY_CLIENT` / `_dismiss_pending_confirms` / `_interrupt_session_shell`） | 停止三件事：**① 按 id 找不到就按连接找**（`_RUNNING_BY_CLIENT`，回话里的 id 用登记里的那个）；**② 杀进程树** + 收掉挂着的沙箱确认框；**③ 全程如实回报**（真停掉才 `stopped: true`）。登记动作移到 `_run_chat` 里 **sid 产生之后**再补一次 | 两轮现场：2026-10-06 是「不分情况一律回 `stopped: true`」（谎报成功，停止按钮从来没生效过）；2026-10-08 是**新会话首轮根本没进表** —— `_handle_chat` 只在帧带 session_id 时登记，而新会话首条消息**没有 id**（id 在 `_run_chat` 里才创建）→ 用户按停止拿到 `stop requested but nothing is running`，模型继续跑，只能重启 App。护栏：`tests/test_ws_control.py`（7 条，含"首轮登记"的现场快照与 db- 前缀） |
| `src/openminis/sandbox/persistent_shell.py`（`kill_process_tree` / `new_group_kwargs` / `interrupt_now` / `execute_command` 的取消与超时分支 / `stop`） | shell 用**独立进程组**启动（POSIX `start_new_session` / Windows `CREATE_NEW_PROCESS_GROUP`）；取消与超时都改为**杀掉整棵进程树**（`killpg` / `taskkill /F /T`）并让 shell 下次重建；`stop()` 也走同一套。**看 `taskkill` 的返回码**（失败要如实报，并把 pid 记在 `_unreaped_pid` 上供下次重试）；`interrupt_now` 顺手取消读循环，读循环的 `finally` 也不再动"已经不是自己那个进程"的共享状态 | `cancel()` 只停住等待 —— 命令在会话级 shell 里**继续跑**，用户看到的是「按了停止，工具还在跑」（原话：停止要把所有工具调用全杀掉）。不开新进程组的话 `killpg` 会连自己一起杀。超时原来只回 124 就把命令丢在 shell 里，它的余音会掺进下一条命令的回显（模型以为命令写错，换写法再撞）。护栏：`tests/test_interrupt_kills_tools.py`（真进程判定：命令 4 秒后写文件，取消/超时后等 5 秒文件必须不出现） |
| `src/openminis/sandbox/execution_coordinator.py`（`interrupt` / `register_fallback`） | 新增：按会话键中断正在跑的命令（**前缀匹配**：`db-<sid>` 之外还有子代理的 `db-<sid>:sub:<id>`，分隔符取冒号以免误伤 `db-<sid>x`）；登记/注销一次性兜底的子进程 | 停止按钮在内核侧的落点。key 必须是 `db-<sid>`（工具侧用的就是这个），写成裸 sid 会**静默地什么都不杀**。护栏：`tests/test_interrupt_kills_tools.py::test_coordinator_interrupt_covers_shell_and_fallback` + `test_ws_control.py::test_interrupt_session_shell_uses_the_db_prefixed_key` |
| `src/openminis/tools/shell_execute_tool.py`（`_one_shot_fallback`） | `subprocess.run` → `Popen` + `asyncio.to_thread(proc.communicate, timeout=…)`，并把子进程登记给协调器；取消时杀掉它 | `run()` 整个跑在工作线程里，外层取消协程**杀不动它** —— 持久 shell 恰好失效时会走这条路，停止按钮就停不住。护栏同上（协调器那条） |
| `src/openminis/sandbox/guard.py`（`sanitize_outbound`） | 危险模式（`danger`）下**跳过凭据脱敏**；路径改写（`scrub_paths`）保留 | 用户对危险模式的定义是"沙箱不要拦任何东西"，但 `sanitize_outbound` 只看白名单、不看模式：开着危险模式，模型手上的 token / cookie 照样被换成"部分显示"，它复原不了就反复重试（现场一次会话刷出 142 条 `guard[secret] blocked tool=outbound:llm`）。路径改写不是"拦"、出站与回填都依赖它，所以留着。护栏：`tests/test_guard.py::test_danger_mode_does_not_redact_outbound_text` |
| `src/openminis/tools/browser_use_tool.py`（新增 `_screenshot_dir`） | 截图动作在模型没给 `path` 时，默认落到**本会话工作区的 `.minis/shots/`**（点目录，工作区常常就是用户的代码仓库，别污染他的 `git status`；`cwd_for` 返回空串时当"拿不到"返回 None —— `Path("")` 会在服务进程的工作目录里建目录并给出相对路径，read_image 照样读不回来） | 驱动默认写 POSIX `/tmp/minis-shots`，Windows 上 `Path("/tmp/…")` 是**盘根**的 `\tmp\minis-shots`（跟 shell 认的 `%TEMP%` 不是一回事）；更要命的是 `read_image` 只在会话工作区内解析路径 → 截图**永远读不回来**（`Cannot resolve path: \tmp\minis-shots\shot-….png`），截图工具等于白截。护栏：`tests/test_browser_tool_screenshot.py`（3 条） |
| `src/openminis/tools/file_read_tool.py`（`_resolve_session_host_path`）/ `read_image_tool.py`（`_resolve_global`） | 工作区外的**根路径**（`/x`、`\x`、`C:\x`）不再当"工作区相对路径"去猜 —— 直接回 None，由上层如实说"不在工作区内，先拷进来" | 原行为会报**谎话**：用户 shell 里 `cp /tmp/baidu_shot.png` 回的是 "are the same file"（文件明明在），read_image 却说 `File not found` —— 它把绝对路径拼成了 `<工作区>/tmp/baidu_shot.png`。模型据此以为文件丢了，换写法反复重试。只改**读**侧（写侧 `file_write_tool` 那份不动，语义不变）。护栏：`tests/test_image_tools.py::test_read_image_does_not_pretend_an_outside_path_is_missing` |
| `src/openminis/agent/repeat_guard.py`（`effect_tool_repeat` 的措辞） | `[LOOP BLOCKED] … 任务大概率已完成` → 「先停下来自检：手上的结果够不够回答用户？确实要继续就把多条命令**合并成一次调用**」 | 原句是**假断言**：现场那次任务明显没完成（还在逐项排查），模型被这句话误导后又继续刷了 6 轮 shell。判据（连续次数）没动，只去掉不实陈述并给出可执行的下一步 |
| `desktop/updater.py`（新增 `installed_payload_version()` / `own_version()` / `relaunch_script_text()`；改写重启助手）/ `desktop/ui_mount.py`（`/api/desktop/info` 与 `/api/desktop/update` 同时给出 `shellVersion` 与 `payloadVersion`；更新结果落盘 `updates/state.json`） | **当前版本改成"已装载荷的版本"**（读 `payload/payload.json` 的 `appVersion`），不再用烧进 exe 的 `desktop.__version__`；重启助手加等待上限（60s）、`cd /d` 回安装目录、逐步写 `openminis-relaunch.log`、三个流接 `DEVNULL`、`CREATE_BREAKAWAY_FROM_JOB` 失败重试 | 2026-10-08 用户实测：载荷包**刻意不含** `desktop/`（引导代码不能由载荷自己给），于是载荷更新改不了壳版本号 → 界面永远显示旧版本、检查更新永远说"有新版本"、每次重下 2.4MB，用户在"更新→重启→还是 17"里打转。重启助手原版死等旧进程、子进程继承父进程句柄、Job 里会被连坐、且脚本自删不留日志 → "窗口没了也拉不起来"且无据可查。护栏：`desktop/tests/test_updater.py`（5 条：载荷优先/无载荷退回/传给 plan 的是载荷版本/助手脚本必须带上限与日志/版本字段成功失败都给） |
| `src/openminis/server/factory_reset.py`（新增）/ `src/openminis/server/chat_store.py`（新增 `clear_all_workspaces()`）/ `src/openminis/server/system_api.py`（新增 `POST /api/system/factory-reset`） | 新增"还原出厂"：清会话（SQL，不删库文件）、分组、记忆（保留 `SOUL.md`）、知识库、定时任务、已安装技能（清空技能根目录后 `ensure_installed()` 装回内置的）、沙箱状态与白名单、配置审计、缓存（保留 `logs/`）；删不掉的路径写 `pending-reset.json` 由 `lifespan` 补删 | 桌面端要一个"回到刚装好的状态"的按钮（只清会话那个不够）。两个必须记的点：**接口放内核侧**而不是 `desktop/ui_mount.py` —— 后者参与壳指纹计算，在那儿加一行路由就要所有用户重下 78MB 整包（护栏 `tests/test_factory_reset.py::test_rest_endpoint_is_on_the_kernel_side` 连"桌面壳里不该出现 factory-reset"一起钉住）；**Windows 上被占用的文件删不掉**，所以库文件走 SQL、其余用 pending 机制补删，绝不谎报成功 |
| `src/openminis/agent/subagents.py`（哨兵 `@current` + `ensure_builtin_subagent`）/ `src/openminis/server/main.py`（`lifespan` 播种 + `_run_chat` 写当轮模型） | 内置「通用代理」：providerId/model 用哨兵 `@current`，派发时取**这一轮对话**的 provider/model（上下文变量，`_run_chat` 写入）；播种只播一次（`subagents.seeded` 标记，还原出厂删标记 → 重新播种）；`_validate` 对哨兵跳过实例/Key 校验 | 用户要求"内置一个通用代理、沿用派发时当前对话模型"——否则每个子代理都得再挑一次模型，且聊天换了模型子代理还留在老模型上。护栏：`tests/test_builtin_subagent.py`（7 条，含"派发时真正传给 provider 的 pid/model"与"没有模型服务时报错要说清去哪配"） |
| `src/openminis/provider/openai/openai_provider.py`（`is_deepseek`） | DeepSeek 不参与 `reasoning_content` 回灌（`forbid_reasoning_field = is_mistral or is_deepseek`） | DeepSeek 文档要求不要把上一轮的思考塞回请求。真接口实测：塞回去 → 模型把自己的「打算」当成已经说过的话，原地反复表态（用户原话「像在发神经」），且上下文随轮次平方级膨胀。Mistral 已有同类先例（直接 422）。护栏：`tests/test_reasoning_echo.py`（4 条，含对照组） |

上游修好之后删掉这些改动即可回到「一行未改」——判据是 `curl` 一份上游 `main`
的对应文件，确认问题已经不在。

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

目前有**18 处**例外（按上表行数计，都是上游自身的问题或本项目需要的增补，见上面
「与上游的偏离」一节），
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
