#!/usr/bin/env python3
"""A throwaway OpenAI-compatible stub, used only to smoke-test the desktop app.

The desktop shell's job is to talk to the kernel over HTTP/WebSocket. Verifying
that end to end should not require a real API key, so this serves just enough of
`/v1/chat/completions` to drive one full agent turn:

  turn 1 -> a streaming tool call to ``shell_execute``
  turn 2 -> a streaming text answer (after the tool result comes back)

That exercises every frame the UI has to render: ``delta``, ``toolStart``,
``toolEnd``, ``done``.
"""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "stub-1"
PORT = 8791


def chunk(delta: dict, finish: str | None = None) -> bytes:
    body = {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the console readable
        pass

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": {"message": "bad json"}})
            return

        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return

        messages = req.get("messages") or []
        saw_tool_result = any(m.get("role") == "tool" for m in messages)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write(payload: bytes) -> None:
            self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")

        if not saw_tool_result:
            # Turn 1: ask for a shell command so the UI gets a tool card.
            args = json.dumps({"command": "echo stub-tool-ran && uname -s"})
            write(chunk({"role": "assistant", "content": "我先跑个命令看看。"}))
            write(chunk({"tool_calls": [{
                "index": 0,
                "id": "call_stub_1",
                "type": "function",
                "function": {"name": "shell_execute", "arguments": args},
            }]}))
            write(chunk({}, "tool_calls"))
        else:
            # Turn 2: final answer, streamed word by word like a real model.
            text = "工具已经执行完了。这是一次**端到端**验证：流式文本、工具卡片、会话持久化都通了。"
            write(chunk({"role": "assistant", "content": ""}))
            for i in range(0, len(text), 6):
                write(chunk({"content": text[i:i + 6]}))
                time.sleep(0.02)
            write(chunk({}, "stop"))

        done = b"data: [DONE]\n\n"
        write(done)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    print(f"stub LLM listening on http://127.0.0.1:{PORT}/v1", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
