"""单宿主与实例握手（R06/R07）。

契约：**第一个进程是宿主**（拿到锁、拥有 backend 与所有窗口），后来者只把"再开一个
窗口"交过去。判据必须是握手（产品 + 协议 + pid + 实例 id 四项全对），不能是
"端口上有东西在听"或者"``/api/health`` 返回 200" —— 任何 HTTP 服务都可能是 200。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from desktop import instance


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from desktop import paths

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# 宿主名片
# ---------------------------------------------------------------------------
def test_host_record_roundtrip(home):
    assert instance.read_host() == {}
    record = instance.write_host(port=8765, token="tok", ui_active=True, pid=4242)
    assert record["protocol"] == instance.PROTOCOL

    on_disk = instance.read_host()
    assert on_disk["port"] == 8765 and on_disk["pid"] == 4242
    assert on_disk["instanceId"] == record["instanceId"]


def test_clearing_only_removes_your_own_record(home):
    """旧宿主退出时不许把**新**宿主的名片删掉（重启交接的真实竞态）。"""
    mine = instance.write_host(port=1, token="a", ui_active=True)
    instance.clear_host(instance_id=mine["instanceId"])
    assert instance.read_host() == {}

    instance.write_host(port=2, token="b", ui_active=True)
    instance.clear_host(instance_id="别的实例")
    assert instance.read_host()["port"] == 2, "把别人的名片删了"


# ---------------------------------------------------------------------------
# 单实例锁
# ---------------------------------------------------------------------------
def test_the_host_lock_is_exclusive(home):
    first = instance.HostLock()
    second = instance.HostLock()
    assert first.acquire() is True
    try:
        assert second.acquire() is False, "第二个进程也拿到锁 —— 单实例形同虚设"
    finally:
        first.release()
    assert second.acquire() is True, "前一个释放之后应该能拿到"
    second.release()


def test_acquiring_twice_is_idempotent(home):
    lock = instance.HostLock()
    assert lock.acquire() is True
    assert lock.acquire() is True      # 同一个对象重复 acquire 不该失败
    lock.release()


# ---------------------------------------------------------------------------
# 开窗请求：交给宿主、宿主消费掉
# ---------------------------------------------------------------------------
def test_a_window_request_is_consumed_by_the_host(home):
    result: list[bool] = []

    def client() -> None:
        result.append(instance.request_window(timeout=5.0, poll=0.05))

    thread = threading.Thread(target=client, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while time.time() < deadline and not instance.pending_window_requests():
        time.sleep(0.05)
    pending = instance.pending_window_requests()
    assert pending, "客户端没把请求写出来"
    payload = instance.consume_window_request(pending[0])

    thread.join(timeout=5)
    assert payload.get("pid"), payload
    assert result == [True], "宿主消费之后客户端应当收到「交出去了」"
    assert instance.pending_window_requests() == []


def test_an_ignored_window_request_times_out_and_cleans_up(home):
    assert instance.request_window(timeout=0.4, poll=0.05) is False
    assert instance.pending_window_requests() == [], "没人接的请求要撤掉，别留垃圾"


# ---------------------------------------------------------------------------
# 握手：200 不等于"这是我们的宿主"
# ---------------------------------------------------------------------------
class _FakeService(BaseHTTPRequestHandler):
    """一个"什么请求都回 200"的假服务（模拟端口被别的程序占着）。"""

    body = b'{"desktop": true, "protocol": 2, "instanceId": "x", "pid": 1}'

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *_args: object) -> None:  # noqa: ANN401
        return


@pytest.fixture()
def fake_service():
    server = HTTPServer(("127.0.0.1", 0), _FakeService)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def test_probe_rejects_a_service_that_is_not_ours(fake_service):
    """端口上有东西、而且它也回 200 —— 仍然不许当成我们的宿主。"""
    record = {"port": fake_service.server_port, "pid": 1, "instanceId": "x"}

    # 协议版本对不上
    _FakeService.body = b'{"desktop": true, "protocol": 999, "instanceId": "x", "pid": 1}'
    assert instance.probe_host(record) is False

    # 实例 id 对不上（端口被别的实例/别的产品占着）
    _FakeService.body = json.dumps(
        {"desktop": True, "protocol": 2, "instanceId": "别的实例", "pid": 1}
    ).encode("utf-8")
    assert instance.probe_host(record) is False

    # pid 对不上
    _FakeService.body = b'{"desktop": true, "protocol": 2, "instanceId": "x", "pid": 2}'
    assert instance.probe_host(record) is False

    # 四项全对 → 认
    _FakeService.body = (
        json.dumps({"desktop": True, "protocol": instance.PROTOCOL,
                    "instanceId": "x", "pid": 1}).encode()
    )
    assert instance.probe_host(record) is True


def test_probe_is_false_when_nobody_is_listening(home):
    assert instance.probe_host({"port": 1, "pid": 1, "instanceId": "x"},
                               timeout=0.5) is False
