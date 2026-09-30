"""应用内更新 [T-in-app-update]。

对标 CC Switch 那套：拉清单 → 比版本 → 下载 → 校验 → 替换 → 重启。

这里最重要的是**端到端那条**：起一个真的 HTTP 服务发载荷包，走完
下载 → sha256 校验 → 原子替换，然后**在子进程里**确认新的内核真的生效了
（进程外确认是必须的 —— 正在跑的解释器早就把旧代码读进内存了，那正是"更新完
必须重启"的原因）。
"""

from __future__ import annotations

import hashlib
import http.server
import json
import subprocess
import sys
import textwrap
import threading
import zipfile
from pathlib import Path

import pytest

from desktop import updater

# ---------------------------------------------------------------------------
# 版本比较
# ---------------------------------------------------------------------------
def test_parse_version_handles_the_usual_shapes():
    assert updater.parse_version("v0.4.5") == (0, 4, 5)
    assert updater.parse_version("0.4.5") == (0, 4, 5)
    assert updater.parse_version("1.0.3") == (1, 0, 3)
    assert updater.parse_version("") == ()
    assert updater.parse_version("nightly") == ()


def test_is_newer_is_numeric_not_lexicographic():
    """字符串比较会把 0.10.0 判成比 0.9.0 旧 —— 那是个经典事故。"""
    assert updater.is_newer("0.10.0", "0.9.0")
    assert not updater.is_newer("0.9.0", "0.10.0")
    assert not updater.is_newer("0.4.5", "0.4.5")
    assert updater.is_newer("0.4.5.1", "0.4.5")


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------
def _manifest(**over):
    base = {
        "format": 1,
        "version": "0.4.6",
        #: 壳指纹（不是版本号）—— 判据是"壳有没有真的变"。见 scripts/shell_id.py。
        "shellId": "shell-current",
        "notes": "修了三个 bug",
        "pubDate": "2026-09-30T00:00:00Z",
        "assets": {
            "payload": {"url": "https://x/p.zip", "sha256": "AB" * 32, "bytes": 100},
            "full": {"url": "https://x/f.zip", "sha256": "CD" * 32, "bytes": 900},
        },
    }
    base.update(over)
    return base


def test_parse_manifest_normalises_sha256_case():
    info = updater.parse_manifest(_manifest())
    assert info.payload.sha256 == "ab" * 32       # 比较要不分大小写
    assert info.payload.bytes == 100 and info.full.bytes == 900


def test_parse_manifest_rejects_junk():
    for bad in ({}, {"version": ""}, {"version": "1.0"}, {"version": "1.0", "assets": {}}):
        with pytest.raises(updater.UpdateError):
            updater.parse_manifest(bad)


def test_missing_shell_id_means_same_shell():
    """老清单没有 shellId 时按"同一个壳"处理 —— 最坏也只是走小更新。"""
    raw = _manifest()
    del raw["shellId"]
    assert updater.parse_manifest(raw).shell_id == ""


# ---------------------------------------------------------------------------
# 决策
# ---------------------------------------------------------------------------
def _plan_with(monkeypatch, raw, *, current, shell="shell-current"):
    """``shell`` 是**本机 exe 里的壳指纹**，默认与清单一致（= 壳没变）。"""
    monkeypatch.setattr(updater, "fetch_manifest", lambda url, **kw: updater.parse_manifest(raw))
    return updater.plan(current_version=current, current_shell=shell)


def test_plan_none_when_already_current(monkeypatch):
    assert _plan_with(monkeypatch, _manifest(version="0.4.5"), current="0.4.5").kind == "none"


def test_plan_payload_when_only_the_body_changed(monkeypatch):
    """**版本号涨了但壳没变** → 走小更新。

    这是这个机制存在的意义：第一版拿版本号当判据，结果每次更新都退化成 39MB
    的整包（因为版本号每个 release 都涨）。
    """
    p = _plan_with(monkeypatch, _manifest(version="0.4.6"), current="0.4.5")
    assert p.kind == "payload"
    assert p.asset.sha256 == "ab" * 32            # 小更新拿的是 payload 那个资产


def test_plan_full_when_the_shell_changed(monkeypatch):
    """壳指纹对不上就必须整包 —— 载荷里的内核与界面只对同一个壳有效。"""
    raw = _manifest(version="0.5.0", shellId="shell-new")
    p = _plan_with(monkeypatch, raw, current="0.4.5", shell="shell-current")
    assert p.kind == "full"
    assert p.asset.sha256 == "cd" * 32


def test_plan_falls_back_to_full_when_payload_is_absent(monkeypatch):
    raw = _manifest(version="0.4.6")
    del raw["assets"]["payload"]
    assert _plan_with(monkeypatch, raw, current="0.4.5").kind == "full"


def test_fetch_manifest_translates_http_errors(monkeypatch):
    import urllib.error

    def boom(*_a, **_k):
        raise urllib.error.HTTPError("u", 404, "nf", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    with pytest.raises(updater.UpdateError, match="还没有发布过版本"):
        updater.fetch_manifest("https://x/latest.json")


# ---------------------------------------------------------------------------
# 下载与校验
# ---------------------------------------------------------------------------
class _Server:
    """最小的 HTTP 服务，只用来把载荷包发出去。"""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.requests: list[str] = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                server.requests.append(self.path)
                body = server.files.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):  # 静音
                return

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = _Server({})
    yield s
    s.close()


def _asset(server: _Server, path: str, body: bytes) -> updater.Asset:
    server.files[path] = body
    return updater.Asset(
        url=server.url(path), sha256=hashlib.sha256(body).hexdigest(), bytes=len(body)
    )


def test_download_verifies_sha256(tmp_path, server):
    asset = _asset(server, "/good.bin", b"hello payload")
    got = updater.download(asset, tmp_path)
    assert got.read_bytes() == b"hello payload"


def test_download_discards_a_corrupted_file(tmp_path, server):
    """校验不过必须**删掉**下载的文件，并把两个哈希都报出来。"""
    asset = _asset(server, "/bad.bin", b"hello payload")
    server.files["/bad.bin"] = b"tampered"          # 服务端换了内容
    with pytest.raises(updater.UpdateError, match="校验不过"):
        updater.download(asset, tmp_path)
    assert not (tmp_path / "bad.bin").exists()


def test_download_reports_progress(tmp_path, server):
    asset = _asset(server, "/p.bin", b"x" * 300_000)
    seen: list[tuple[int, int]] = []
    updater.download(asset, tmp_path, on_progress=lambda d, t: seen.append((d, t)))
    assert seen and seen[-1][0] == 300_000
    assert all(total == 300_000 for _done, total in seen)


# ---------------------------------------------------------------------------
# 解压与替换
# ---------------------------------------------------------------------------
def _payload_zip(path: Path, *, version: str, shell: str, extra: dict | None = None) -> bytes:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("payload.json", json.dumps({"format": 1, "shellId": shell}))
        zf.writestr("openminis/__init__.py", f'__version__ = "{version}"\n')
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return path.read_bytes()


def test_safe_extract_refuses_zip_slip(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../escaped.txt", "nope")
    with pytest.raises(updater.UpdateError, match="目录外"):
        updater._safe_extract(evil, tmp_path / "out")  # noqa: SLF001


def test_install_payload_rejects_a_package_for_another_shell(tmp_path):
    """壳对不上就该拒绝 —— 装进去只会得到一个起不来的应用。"""
    zip_path = tmp_path / "p.zip"
    _payload_zip(zip_path, version="9.9.9", shell="shell-new")
    target = tmp_path / "payload"
    (target / "openminis").mkdir(parents=True)
    (target / "openminis" / "__init__.py").write_text('__version__ = "0.1.0"\n')

    with pytest.raises(updater.UpdateError, match="换了壳"):
        updater.install_payload(zip_path, target, expect_shell="shell-current")
    # 原来的载荷**一点没动**（拒绝要发生在动旧的之前）
    assert '__version__ = "0.1.0"' in (target / "openminis" / "__init__.py").read_text()


def test_install_payload_replaces_atomically(tmp_path):
    zip_path = tmp_path / "p.zip"
    _payload_zip(zip_path, version="0.4.6", shell="shell-current")
    target = tmp_path / "payload"
    (target / "openminis").mkdir(parents=True)
    (target / "openminis" / "__init__.py").write_text('__version__ = "0.4.5"\n')

    updater.install_payload(zip_path, target, expect_shell="shell-current")

    assert '__version__ = "0.4.6"' in (target / "openminis" / "__init__.py").read_text()
    # 中间目录不能留下（.new / .old 都是实现细节）
    assert not target.with_name("payload.new").exists()
    assert not target.with_name("payload.old").exists()


def test_install_payload_creates_it_when_missing(tmp_path):
    """老安装包没有 payload/ —— 第一次更新要能凭空建出来。"""
    zip_path = tmp_path / "p.zip"
    _payload_zip(zip_path, version="0.4.6", shell="shell-current")
    target = tmp_path / "payload"
    updater.install_payload(zip_path, target, expect_shell="shell-current")
    assert (target / "openminis" / "__init__.py").is_file()


# ---------------------------------------------------------------------------
# 端到端：下载 → 校验 → 替换 → **新进程里真的生效**
# ---------------------------------------------------------------------------
_PROBE = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {repo!r})
    sys.frozen = True
    sys.executable = {exe!r}
    sys._MEIPASS = {internal!r}
    sys.path.append({internal!r})
    from desktop import paths
    paths.install_payload_path()
    import openminis
    print(openminis.__version__)
    """
)


def test_end_to_end_payload_update(server, tmp_path):
    """走完整条链路，然后在**子进程**里确认新内核生效。"""
    repo = Path(__file__).resolve().parent.parent.parent
    app = tmp_path / "OpenMinisDesktop"
    internal = app / "_internal"
    (internal / "openminis").mkdir(parents=True)
    (internal / "openminis" / "__init__.py").write_text('__version__ = "0.4.5-frozen"\n')
    payload = app / "payload"
    (payload / "openminis").mkdir(parents=True)
    (payload / "openminis" / "__init__.py").write_text('__version__ = "0.4.5"\n')

    def installed() -> str:
        probe = _PROBE.format(
            repo=str(repo), exe=str(app / "OpenMinisDesktop.exe"), internal=str(internal)
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
        )
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    assert installed() == "0.4.5"           # 起点：载荷里的版本

    # 服务端发一个"新版本"的载荷包
    zip_path = tmp_path / "new.zip"
    body = _payload_zip(zip_path, version="0.4.6", shell="shell-current")
    asset = _asset(server, "/OpenMinisDesktop-update.zip", body)

    archive = updater.download(asset, tmp_path / "downloads")
    updater.install_payload(archive, payload, expect_shell="shell-current")

    assert installed() == "0.4.6", "更新装完了，新进程里还是旧版本"
    assert server.requests, "下载根本没发生"


def test_pinned_shell_id_matches_the_shell_sources():
    """``desktop/build_id.py`` 里签的壳指纹必须和壳源码算出来的一致。

    对不上意味着"改了壳却没重算" —— 那时载荷会带着旧壳的身份发出去，更新器就会
    把"需要整包"的更新当成小更新发下去，**装完应用起不来**。所以这条要能红。
    """
    from desktop.build_id import SHELL_ID

    from scripts.shell_id import compute

    assert compute() == SHELL_ID, "改了壳就跑 python scripts/shell_id.py --write"


def test_cleanup_removes_old_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "updates_dir", lambda: tmp_path)
    old = tmp_path / "old.zip"
    old.write_bytes(b"x")
    import os

    os.utime(old, (0, 0))
    fresh = tmp_path / "fresh.zip"
    fresh.write_bytes(b"y")

    updater.cleanup_old_downloads()
    assert not old.exists()
    assert fresh.exists()
