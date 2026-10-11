"""日志脱敏：访问令牌不许进日志（R18）。

本地服务的访问令牌（``?k=<token>`` / ``x-minis-access``）是**鉴权凭据** —— 它只要
有效，谁能读到谁就能驱动这个手里有 shell 的 agent。而它很容易被顺手写进日志：

* headless / ``--browser`` 那条路会把**带令牌的完整地址**打出来（"OpenMinis Desktop
  ready — http://127.0.0.1:8765/?k=…"）；
* 诊断用的日志包会把这些日志原样收进去发给别人。

所以两层：

1. **不再打印带令牌的地址**（调用点自己去掉，见 ``app.py``）；
2. 这里加一道兜底 —— 日志 handler 上装过滤器，把 ``?k=`` / ``token=`` /
   ``x-minis-access`` 形态的值换成 ``<redacted>``；导出日志包时再过一遍同样的函数，
   连**以前**写下的那些日志也一起兜住。

刻意不猜别的形态（不做"看起来像 token 的字符串"这种启发式）：误伤正常日志比漏一个
更难查，而令牌的形态是我们自己定的。
"""

from __future__ import annotations

import logging
import re
from typing import Any

REDACTED = "<redacted>"

#: URL 查询串里的令牌：``?k=…`` / ``&token=…`` / ``?access_token=…``
_QUERY = re.compile(r"(?P<pre>[?&](?:k|token|access_token|key)=)[^&\s\"'<>]+", re.I)
#: 请求头 / 报文里的令牌（日志里常见 ``x-minis-access: abc`` 或 ``'x-minis-access': 'abc'``）
_HEADER = re.compile(
    r"(?P<pre>x-minis-access['\"]?\s*[:=]\s*['\"]?)[^'\",}\s]+", re.I
)


def redact_text(text: str) -> str:
    """把文本里的令牌换成 ``<redacted>``（其它内容一字不动）。"""
    if not text:
        return text
    return _HEADER.sub(lambda m: m.group("pre") + REDACTED,
                       _QUERY.sub(lambda m: m.group("pre") + REDACTED, str(text)))


class RedactionFilter(logging.Filter):
    """装在 handler 上：所有日志在落地之前过一遍 ``redact_text``。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_text(record.msg)
            args = record.args
            if isinstance(args, tuple):
                record.args = tuple(
                    redact_text(a) if isinstance(a, str) else a for a in args
                )
            elif isinstance(args, dict):
                record.args = {
                    k: (redact_text(v) if isinstance(v, str) else v)
                    for k, v in args.items()
                }
        except Exception:  # pragma: no cover - 脱敏绝不能把日志本身弄挂
            return True
        return True


def install(logger: logging.Logger | None = None) -> None:
    """给 root（或指定 logger）的**现有 handler** 装上过滤器。幂等。"""
    target = logger or logging.getLogger()
    for handler in list(target.handlers):
        if not any(isinstance(f, RedactionFilter) for f in handler.filters):
            handler.addFilter(RedactionFilter())


def redact_any(value: Any) -> Any:
    """递归脱敏（给"要把某个 dict 打进日志"的调用方用）。"""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_any(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_any(v) for v in value)
    return value
