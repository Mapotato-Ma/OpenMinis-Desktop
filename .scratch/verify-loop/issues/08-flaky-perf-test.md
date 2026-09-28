# 08 · 一个能跑的 perf 测试：负载下会偶发红 —— 已收

Status: resolved
Type: task

## 结论：问题不在阈值紧，在**判据选错了**

原来断言的是「两个各 0.3s 的工具并发跑，总耗时 < 0.55s」。
总耗时是**机器负载的代理**：空载时 0.31s，忙的时候（本机同时跑着测试服务 +
npm 检查）会超过 0.55s —— 于是一个**正确的实现被报成红的**。
调阈值只是让偶发变少，没有改变「量的是机器，不是代码」这件事。

## 改法：直接断言**时间区间有交集**

```python
async def _read(args_json, session_id, **kw):
    started = time.monotonic()
    await asyncio.sleep(delay)
    spans.append((args_json, started, time.monotonic()))
...
(na, a0, a1), (nb, b0, b1) = spans
assert a0 < b1 and b0 < a1, "两个工具没有重叠，似乎串行了"
assert overlap > delay * 0.5      # 别只蹭到一丁点重叠就算过
```

并发必有交集、串行必无交集，而且**与机器快慢无关**。

## 验证

| 场景 | 结果 |
|---|---|
| 正常跑 3 次 | 3/3 通过 |
| 加 4 个忙循环制造负载 | 通过（旧版在这个负载下红过） |
| 临时把工具实现改成**强制串行** | 区间不再重叠 → 断言的条件为假（证明这条断言有牙齿，不是摆设） |

已记进 `NOTICE.md` 的偏离表（`tests/` 下的第 3 处，都有 `PORT-FIX` 说明）。
