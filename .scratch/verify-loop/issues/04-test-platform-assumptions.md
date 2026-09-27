# 04 · 测试自身的平台前提（6 项红的属于这类）

Status: open
Type: task

13 项红里的分类（已逐项看过失败输出，不是猜的）：

| 类别 | 数量 | 归属 |
|---|---|---|
| `//var/minis/…` 前导斜杠 | **5** | 同一个真 bug → ticket 03b |
| 集成路径里完全没脱敏 | **2** | 见下面 4d，待定 |
| Windows 专用测试没 skipif | 1 | 4a |
| 依赖机器状态 | 2 | 4c |
| 平台分支期望写死一侧 | 1 | 4b（与 4a 同文件）|
| Python 3.13 专属 API | 2 | 真 bug → ticket 03a |

共计 13。**只有 6 项属于"测试自身的前提问题"**，其余 7 项是真 bug。

修完 03 之后应该把这一批也清掉 —— 目标只有一个：**让"红"重新只代表"坏了"**。

## 4a · Windows 专用测试没加 skipif（1 项）

`tests/test_plugin_process.py::test_process_runner_launches_batch_script`
→ `FileNotFoundError: 'cmd.exe'`。.bat 脚本的测试在 POSIX 上永远做不到。
应当 `@pytest.mark.skipif(os.name != "nt", reason="needs cmd.exe")`。

## 4b · 平台分支的期望值写死了一侧（1 项）

`tests/test_plugin_process.py::test_candidate_bin_dirs_covers_common_installs`
→ 断言 `'nodejs' in <候选目录串>`，但候选目录按平台生成。期望值应跟着平台走。

## 4c · 依赖机器状态（2 项）

`tests/test_skills.py::test_readonly_roots_allows_skills_dir`、
`test_file_read_can_read_skill_md` —— 只读根与技能目录来自 `app_context()`，
断言与真实目录布局耦合。应当在夹具里显式建出这些目录，而不是依赖机器上恰好有。

## 4d · 两处集成路径里脱敏完全没生效（待定，2 项）

```
test_channel_media_fallback_uses_sandbox_path   → '[图片] /tmp/pytest-…/home/workspace/image/b.png'
test_send_warns_when_an_older_image_is_delivered → 工具输出里是原始机器路径
```

这两处拿到的都是**未被替换的原始路径**（区别于 03b：那批是被换了、只是多了个斜杠）。

已知的一步：**单独跑仍红**，所以不是「被前一个测试污染」这么简单
（对 `test_read_image_round_trip` 已验证，它其实是 03b）。
下一步：检查这两条路径读的是不是夹具里那套 `app_context` ——
夹具用 `MINIS_HOME` + `set_app_context()`，而 `scrub_machine_paths` 的替换规则
来自 `app_context()`；如果冷却在错误的一份上下文上，规则里就没有这个 tmp 前缀，
于是「一条都没命中」，正好是这个症状。

**判定方法**：在测试里打印 `[str(p) for p, _ in _sandbox_rules()]`，看 tmp 前缀在不在里面。
在 = 产品路径漏了调用脱敏；不在 = 夹具上下文时序问题。

## 为什么这批不算"已知问题"就放过

把这 6 项全归成"环境差异"，等于把 ticket 03 那 7 个真 bug 一起埋掉 ——
这正是这个仓库走到今天这个状态的方式（CI 不跑测试，所以红不红没人知道）。
