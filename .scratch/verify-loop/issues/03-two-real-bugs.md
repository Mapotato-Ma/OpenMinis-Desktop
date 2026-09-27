# 03 · 两个真实 bug（第一次跑测试才露出来）

Status: open
Type: task
**决策门**：两处都在 `src/`（移植内核），而项目规矩是「src/ 一行不改」。要不要破例，见文末。

第一次把 pytest 跑起来，13 项红的里面有 2 类是真 bug，不是环境差异。

---

## 3a · 用了 Python 3.13 才有的 API，而 CI 跑的是 3.12

`src/openminis/tools/search_files_tool.py:178,180`

```python
if entry.is_dir(follow_symlinks=False):     # entry 是 Path，不是 os.DirEntry
```

`pathlib.Path.is_dir(follow_symlinks=...)` 是 **Python 3.13** 才加的。本仓
`requires-python = ">=3.11"`、`.python-version` 写 **3.13**、CI 用 **3.12** ——
于是：**开发者机器上永远绿，打出来的 exe 上 `search_files` 工具直接抛 TypeError**。

症状不是崩溃，而是工具把 `TypeError: Path.is_dir() got an unexpected keyword
argument 'follow_symlinks'` 当错误文本回给模型 —— 一个功能静默失效。

对照：`src/openminis/tools/firstagenttools/search_files/search_files.py:91` 用的是
`os.DirEntry`，那个**确实**支持 `follow_symlinks`，同一个写法在那边是对的。
所以这不是"风格问题"，是"两个同名方法语义不同"。

**修法（二选一）**：改成 `entry.is_symlink()` 前置判断；或 `entry.is_dir()` +
显式 `not entry.is_symlink()`。两者都不改行为、都兼容 3.11+。

**为什么这条要优先**：它影响**真正发出的 Windows 包**，且 CI 永远看不见它
（因为 CI 不跑测试）。这正是 ticket 02 存在的理由。

---

## 3b · POSIX 上路径脱敏会多出一个前导斜杠（`//var/minis/...`）

`src/openminis/tools/path_utils.py:100-135`

```python
parts = [re.escape(p) for p in re.split(r"[\\/]+", prefix.strip("\\/")) if p]
return r"[\\/]+".join(parts)
```

`prefix.strip("\\/")` 把**开头的分隔符剥掉了**，所以正则匹配不到那个 `/`，
而替换值是绝对路径 —— 结果是 `/` + `/var/minis/workspace` = `//var/minis/workspace/…`。

- Windows 不受影响：路径以 `C:` 开头，被剥掉的是 `C:` 之后的分隔符吗 —— 不是，
  剥的是首尾的 `\` `/`，`C:` 保留，所以不会多出斜杠。
- POSIX 上必现：正则从 `tmp/...` 开始匹配，文本里那个前导 `/` 留在原地。
- 危害不只是难看：`//var/minis/workspace/…` 在 markdown 图片语法里是
  **protocol-relative URL**（`//host/path`），渲染会指向错误的地方。

**修法**：`_sep_pattern` 不要把首部分丢掉 —— 保留一个可选的前导分隔符
（`r"[\\/]?" + ...`），或在替换时把消耗掉的前导分隔符一起处理掉。

命中 5 个测试：`tests/test_sandbox_paths.py` 的 scrub ×3 + `test_plugins.py`
的 channel media fallback 家族。

---

## 决策门

`AGENTS.md` 的第一条硬规矩是「src/ 一行不改」，理由是上游更新能直接 merge。
这两处都是**上游自身的 bug**（不是移植引入的），所以更漂亮的做法是提给上游。
但 3a 会让当前发出的包功能失效 —— 等上游修不现实。

建议：**在 src/ 里做最小修复，并加 `# PORT-FIX:` 标记 + 在 `PORTING_MAP.md`
记一行**，把它变成"有编号的分叉"，而不是隐形的分叉。
（`PORTING_MAP.md` 已经就是干这个用的文件。）

需要用户点头：改 src/ 是打破项目第一原则的动作，不该由我单方面决定。
