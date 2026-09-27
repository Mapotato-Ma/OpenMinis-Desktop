# 04 · 测试自身的平台前提 —— 已收

Status: resolved
Type: task

13 项红最终的去处：

| 类别 | 数量 | 结果 |
|---|---|---|
| `//var/minis/…` 前导斜杠 | 5 | ticket 03b 修掉（产品 bug） |
| Python 3.13 专属 API | 2 | ticket 03a 修掉（产品 bug） |
| 绝对路径被 `lstrip` 成相对 | 2 | **本工单修掉**（产品 bug，POSIX 专有）——`path_utils.resolve_workspace_path` + `file_read_tool._resolve_session_host_path`，都是「只读根里的绝对路径必须放行」这条既有契约在 POSIX 上没兑现 |
| Windows 专用测试没 skipif | 1 | 本工单加 `skipif(os.name != "nt")` |
| 依赖平台分支的期望值 | 1 | 同上（同一文件） |
| Windows 上时钟赛跑 | 1 | **本工单修掉**（用例 bug）——`test_bridge_does_not_top_up_when_nothing_was_delivered` 把夹具文件 mtime 钉到过去 |

## 关键教训：两条基线都得看

本地（POSIX）红的 4 项，**在 Windows 上全是绿的**；而 Windows 上唯一红的那一项
（时钟赛跑）在本地永远是绿的。也就是说：

- 只跑本地 → 以为有 4 个坏东西，其实产品没问题；
- 只跑 CI → 以为有 1 个坏东西，其实是用例自己的时钟赛跑。

**两边都跑，才能真正做到「红只代表坏」。** 这正是 ticket 02 那条流水线的价值。

## 遗留（不修，只记）

产品侧确实存在一个 ≤1 个时钟 tick（Windows 约 15.6ms）的窗口：`since` 由
`time.time()` 得到，而文件时间戳更细，所以「恰好在这条消息之前落盘的图」可能被
当成新增补发。影响可忽略（要防的是「闲聊也蹦出图」那种量级的问题），
但如果哪天要提给上游，这是一条。
