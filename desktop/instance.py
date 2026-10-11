"""单实例与"再开一个窗口"的交接 [R06/R07]。

改前每个启动都是一个独立进程，第二个进程发现端口被占就**复用第一个的 HTTP**、
在自己进程里再开一个窗口。于是：

* 关掉第一个窗口 → 它的 backend 跟着走 → 第二个窗口还开着却断网；
* 在第二个窗口点"重启以完成更新" → HTTP 打到第一个进程，执行的是**它的**退出钩子
  → 用户看到的是"另一个窗口没了"；
* 整包替换只等服务器那个 PID，别的窗口还可能锁着同一份安装目录。

现在契约是**单宿主**：第一个启动的进程拿到宿主锁，拥有 backend 与**所有**窗口；
后来者不建 backend、不建窗口，只把"再开一个窗口"这个请求交给宿主，然后自己退出。

落盘（都在 ``data_root()/instance/``）：

* ``host.lock`` —— 独占锁定的文件（谁锁着谁是宿主，进程死了锁自动释放）
* ``host.json`` —— 宿主的名片：pid / port / token / instanceId / 协议版本 / 启动时刻
* ``window-request-<ts>.json`` —— 后来者留下的请求，宿主建完窗就删掉

为什么用**文件**而不是命名管道：它天然跨进程、跨版本（旧宿主读不懂的东西会被当成
"不是我的请求"忽略），而且出问题能直接看 —— 这个项目的排障全靠日志和文件。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: 宿主 ↔ 后来者之间的协议版本。握手的形状变了就要改它 —— 对不上的两边一律当成
#: "不是同一种东西"，宁可各开各的也不要错着用。
PROTOCOL = 2

HOST_FILE = "host.json"
LOCK_FILE = "host.lock"
REQUEST_PREFIX = "window-request-"


def instance_dir() -> Path:
    from .paths import data_root  # noqa: PLC0415

    path = data_root() / "instance"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# 宿主名片
# ---------------------------------------------------------------------------
def read_host() -> dict[str, Any]:
    try:
        data = json.loads((instance_dir() / HOST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_host(*, port: int, token: str, ui_active: bool,
               pid: int | None = None) -> dict[str, Any]:
    record = {
        "pid": int(pid if pid is not None else os.getpid()),
        "port": int(port),
        "token": token,
        "uiActive": bool(ui_active),
        "instanceId": uuid.uuid4().hex,
        "protocol": PROTOCOL,
        "startedAt": int(time.time() * 1000),
    }
    path = instance_dir() / HOST_FILE
    try:
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:  # pragma: no cover - 写不进去只影响"后来者能不能交接"
        logger.debug("host record write failed", exc_info=True)
    return record


def clear_host(*, instance_id: str = "") -> None:
    """删掉名片。带上 instance_id 时只删**自己那一张**（别把新宿主的删了）。"""
    current = read_host()
    if instance_id and current and str(current.get("instanceId") or "") != instance_id:
        return
    try:
        (instance_dir() / HOST_FILE).unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        logger.debug("host record clear failed", exc_info=True)


# ---------------------------------------------------------------------------
# 单实例锁
# ---------------------------------------------------------------------------
class HostLock:
    """独占锁定 ``instance/host.lock``：拿到的是宿主，拿不到说明已经有一个在跑。

    进程退出（哪怕是崩的）时操作系统会释放文件锁 —— 不需要"清理陈旧文件"那套，
    也就不会出现"上次崩了导致再也起不来"。
    """

    def __init__(self) -> None:
        self._fh: Any = None
        self._path = instance_dir() / LOCK_FILE

    def acquire(self) -> bool:
        if self._fh is not None:
            return True
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self._path, "a+b")  # noqa: PTH123, SIM115 - 全程持有这个句柄
        except OSError:  # pragma: no cover
            logger.debug("host lock open failed", exc_info=True)
            return False
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - 发布目标只有 Windows
                import fcntl  # noqa: PLC0415

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover
            pass
        finally:
            try:
                fh.close()
            except OSError:  # pragma: no cover
                pass

    def __enter__(self) -> HostLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# 握手（R07：health 200 不证明这是本产品）
# ---------------------------------------------------------------------------
def probe_host(record: dict[str, Any], *, timeout: float = 2.0) -> bool:
    """这张名片上的宿主还活着吗、是**我们的**宿主吗。

    判据不是"端口上有东西在听"，也不是"``/api/health`` 返回 200"（任何 HTTP 服务
    都可能是 200）—— 而是 ``/api/desktop/handshake`` 里的产品、协议版本、pid 与
    instanceId 四项全对上。
    """
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    port = record.get("port")
    if not isinstance(port, int) or not port:
        return False
    url = f"http://127.0.0.1:{port}/api/desktop/handshake"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                return False
            info = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False
    if not isinstance(info, dict) or not info.get("desktop"):
        return False
    try:
        return (
            int(info.get("protocol") or 0) == PROTOCOL
            and str(info.get("instanceId") or "") == str(record.get("instanceId") or "")
            and int(info.get("pid") or 0) == int(record.get("pid") or 0)
        )
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# "再开一个窗口"
# ---------------------------------------------------------------------------
def request_window(*, timeout: float = 8.0, poll: float = 0.15) -> bool:
    """把这个请求交给宿主，等它消费（删掉文件）。返回"交出去了没有"。"""
    path = instance_dir() / f"{REQUEST_PREFIX}{int(time.time() * 1000)}-{os.getpid()}.json"
    payload = {
        "pid": os.getpid(),
        "at": int(time.time() * 1000),
        "argv": list(sys.argv[1:]),
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.debug("window request write failed", exc_info=True)
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not path.exists():
            return True
        time.sleep(poll)
    try:                       # 宿主没理我们：把请求撤掉，别留垃圾
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass
    return False


def pending_window_requests() -> list[Path]:
    """宿主侧：还有哪些没人处理的开窗请求（按时间排序 = 先来先开）。"""
    try:
        return sorted(instance_dir().glob(f"{REQUEST_PREFIX}*.json"))
    except OSError:  # pragma: no cover
        return []


def consume_window_request(path: Path) -> dict[str, Any]:
    """宿主读走一个请求并删掉它（删掉 = 告诉后来者"交给我了"）。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass
    return payload if isinstance(payload, dict) else {}


class WindowRequestWatcher(threading.Thread):
    """宿主侧的后台线程：盯着请求目录，有人要窗口就回调（在宿主进程里建窗口）。"""

    def __init__(self, on_request: Callable[[dict[str, Any]], None],
                 *, interval: float = 1.0) -> None:
        super().__init__(name="openminis-window-requests", daemon=True)
        self._on_request = on_request
        self._interval = interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover - GUI 侧，靠真机验证
        while not self._stop.wait(self._interval):
            for path in pending_window_requests():
                payload = consume_window_request(path)
                try:
                    self._on_request(payload)
                except Exception:  # pragma: no cover - 一个请求失败不该停掉看门狗
                    logger.exception("opening an extra window failed")
