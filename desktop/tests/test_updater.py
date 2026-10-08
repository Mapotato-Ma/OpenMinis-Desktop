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
import urllib.error
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


def test_shell_id_ignores_line_endings(tmp_path, monkeypatch):
    """同一个提交，Linux(LF) 与 Windows(CRLF) 必须算出**同一个**壳指纹。

    CI 实测抓到过：Verify 跑在 windows-latest 上，git 默认按 CRLF 检出，指纹就对不上。
    行尾是检出方式的差别，不是壳变了 —— 而载荷恰恰在 Windows 上用。
    """
    from scripts.shell_id import compute, normalise

    assert normalise(b"a\r\nb") == normalise(b"a\nb") == b"a\nb"

    # 把整个壳源码复制一份、全部转成 CRLF，指纹必须不变
    import shutil

    from scripts import shell_id

    fake = tmp_path / "repo"
    shutil.copytree(shell_id.REPO / "desktop", fake / "desktop",
                    ignore=shutil.ignore_patterns("__pycache__", "tests", "assets"))
    shutil.copy2(shell_id.REPO / "desktop_main.py", fake / "desktop_main.py")
    (fake / "packaging").mkdir()
    shutil.copy2(shell_id.REPO / "packaging" / "OpenMinisDesktop.spec",
                 fake / "packaging" / "OpenMinisDesktop.spec")
    before = shell_id.compute()
    for path in fake.rglob("*"):
        if path.is_file():
            # 先归一成 LF 再转 CRLF —— 直接 replace 的话，Windows 上本来就是 CRLF，
            # 会变成 \r\r\n（这条测试第一次跑在 windows-latest 上就是这么红的）。
            data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            path.write_bytes(data.replace(b"\n", b"\r\n"))
    monkeypatch.setattr(shell_id, "REPO", fake)
    assert shell_id.compute() == before, "转成 CRLF 之后壳指纹变了 —— 行尾没归一化"



# ── 抖动的网络：必须重试 ────────────────────────────────────────────────
#
# 真实用户报过：上次点「检查更新」虽然慢但出来了，这次报
# 「连不上更新服务：The read operation timed out」。从国内到 GitHub 就是时通时不通
# （实测同一地址连测 10 次，1.1s ~ 6.1s 都有），失败一次就结束等于"时灵时不灵"。


class _FakeResp:
    """够用的假响应：read(n) 逐块吐，吐完给空。"""

    def __init__(self, payload: bytes) -> None:
        self._left = payload
        self.headers = {"Content-Length": str(len(payload))}

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data, self._left = self._left, b""
            return data
        data, self._left = self._left[:n], self._left[n:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _timeout():
    return urllib.error.URLError(TimeoutError("The read operation timed out"))


def _manifest_bytes(version="9.9.9", digest="ab" * 32):
    import json as _json

    return _json.dumps(
        {
            "format": 1,
            "version": version,
            "shellId": "x" * 16,
            "assets": {
                "payload": {
                    "url": "https://example.invalid/update.zip",
                    "sha256": digest,
                    "bytes": 10,
                }
            },
        }
    ).encode()


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """重试的退避不能在测试里真的睡。"""
    monkeypatch.setattr(updater, "RETRY_BACKOFF_S", (0.0, 0.0), raising=False)


def test_check_retries_a_flaky_network(monkeypatch):
    calls = []

    def fake_open(request, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise _timeout()
        return _FakeResp(_manifest_bytes())

    monkeypatch.setattr(updater, "_open", fake_open)
    info = updater.fetch_manifest("https://example.invalid/latest.json")
    assert info.version == "9.9.9"
    assert len(calls) == 2, "应该试第二次"


def test_check_always_failing_says_how_many_times_it_tried(monkeypatch):
    calls = []

    def fake_open(request, timeout):
        calls.append(1)
        raise _timeout()

    monkeypatch.setattr(updater, "_open", fake_open)
    with pytest.raises(updater.UpdateError) as err:
        updater.fetch_manifest("https://example.invalid/latest.json")
    # 写死数字，**不要**写成 == updater.CHECK_ATTEMPTS —— 那样把常量改成 1 之后
    # 断言依然成立，等于没测（这条自我满足的写法在反向自检里被抓出来过）。
    assert len(calls) == 3
    assert "已自动重试" in str(err.value)
    assert "超时（连上了，但没等到数据）" in str(err.value)


def test_a_404_is_not_retried(monkeypatch):
    """还没发过版本不是网络问题，再试一百次也一样。"""
    calls = []

    def fake_open(request, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(updater, "_open", fake_open)
    with pytest.raises(updater.UpdateError) as err:
        updater.fetch_manifest("https://example.invalid/latest.json")
    assert len(calls) == 1
    assert "还没有发布过版本" in str(err.value)


def test_a_500_is_retried(monkeypatch):
    calls = []

    def fake_open(request, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(updater, "_open", fake_open)
    with pytest.raises(updater.UpdateError):
        updater.fetch_manifest("https://example.invalid/latest.json")
    assert len(calls) == 3, "502 是服务端抽风，该重试"


def test_the_budgets_are_actually_retries():
    """兜住上一条：光把常量调小，上面那些写死数字的断言会红，但这条会先指出原因。"""
    assert updater.CHECK_ATTEMPTS >= 2, "检查更新至少要试两次（国内到 GitHub 会抖）"
    assert updater.DOWNLOAD_ATTEMPTS >= 2, "下载大文件更要重试"


def test_download_retries_and_throws_away_the_partial_file(tmp_path, monkeypatch):
    body = b"Z" * 5000
    import hashlib

    asset = updater.Asset(
        url="https://example.invalid/update.zip",
        sha256=hashlib.sha256(body).hexdigest(),
        bytes=len(body),
    )
    seen = []

    def fake_open(request, timeout):
        seen.append(1)
        if len(seen) == 1:
            # 下到一半断掉：写点东西进去，再抛 —— 半截文件必须被清掉
            (tmp_path / "update.zip").write_bytes(body[:100])
            raise _timeout()
        return _FakeResp(body)

    monkeypatch.setattr(updater, "_open", fake_open)
    got = updater.download(asset, tmp_path)
    assert len(seen) == 2
    assert got.read_bytes() == body, "重试之后必须是完整的文件，不能接着半截写"


def test_download_reports_a_bad_checksum_after_retrying(tmp_path, monkeypatch):
    body = b"Z" * 100
    asset = updater.Asset(
        url="https://example.invalid/update.zip", sha256="00" * 32, bytes=len(body)
    )
    calls = []
    monkeypatch.setattr(
        updater, "_open", lambda r, t: (calls.append(1), _FakeResp(body))[1]
    )
    with pytest.raises(updater.UpdateError) as err:
        updater.download(asset, tmp_path)
    assert "校验不过" in str(err.value)
    assert len(calls) == 3
    assert not (tmp_path / "update.zip").exists(), "坏包不能留在盘上"


def test_network_errors_are_translated_into_words():
    assert "超时" in updater._describe(_timeout())
    assert "TLS" in updater._describe(urllib.error.URLError(Exception("certificate verify failed")))

def test_a_version_bump_alone_does_not_change_the_shell_id(tmp_path, monkeypatch):
    """发版必然要改版本号 —— 它绝不能参与壳指纹。

    否则**每一次发版都会被判成「换壳」**，2~3 MB 的载荷更新那条路永远走不到
    （实测 v0.4.10 → v0.4.11：壳源码只差 `__version__` / `DESKTOP_UI_VERSION`
    这三行，指纹却变了，于是整包那条路被走了十几遍）。
    """
    import re
    import shutil

    from scripts import shell_id

    fake = tmp_path / "repo"
    shutil.copytree(shell_id.REPO / "desktop", fake / "desktop",
                    ignore=shutil.ignore_patterns("__pycache__", "tests", "assets"))
    shutil.copy2(shell_id.REPO / "desktop_main.py", fake / "desktop_main.py")
    (fake / "packaging").mkdir()
    shutil.copy2(shell_id.REPO / "packaging" / "OpenMinisDesktop.spec",
                 fake / "packaging" / "OpenMinisDesktop.spec")

    before = shell_id.compute()
    for rel in ("desktop/__init__.py", "desktop/ui_mount.py"):
        f = fake / rel
        f.write_text(
            re.sub(r'(__version__|DESKTOP_UI_VERSION) = "[^"]*"', r'\1 = "9.9.9"',
                   f.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    monkeypatch.setattr(shell_id, "REPO", fake)
    assert shell_id.compute() == before, "改个版本号就把壳指纹改了 —— 载荷更新会永远走不到"


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


# ---------------------------------------------------------------------------
# 版本身份：当前版本 = 已装载荷的版本，不是烧在 exe 里的壳版本
#   2026-10-08 用户实测：装了 0.4.19 载荷，界面仍显示 0.4.17，检查更新永远说
#   "有新版本" → 在"更新→重启→还是 17"里打转，每次还重下 2.4MB。
# ---------------------------------------------------------------------------
def _fake_payload(tmp_path, monkeypatch, version: str):
    payload = tmp_path / "payload"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "payload.json").write_text(
        json.dumps({"format": 1, "appVersion": version, "shellId": "deadbeef"},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(updater, "payload_root", lambda: payload)
    return payload


def test_installed_payload_version_reads_the_manifest(tmp_path, monkeypatch):
    _fake_payload(tmp_path, monkeypatch, "0.4.19")
    assert updater.installed_payload_version() == "0.4.19"


def test_installed_payload_version_is_none_without_a_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "payload_root", lambda: None)
    assert updater.installed_payload_version() is None
    empty = tmp_path / "payload"
    empty.mkdir()
    monkeypatch.setattr(updater, "payload_root", lambda: empty)
    assert updater.installed_payload_version() is None


def test_own_version_prefers_the_payload_over_the_frozen_shell(tmp_path, monkeypatch):
    """这就是那个 bug 的判据：壳版本 0.4.17 + 载荷 0.4.19 → 当前版本是 0.4.19。"""
    _fake_payload(tmp_path, monkeypatch, "0.4.19")
    monkeypatch.setattr(updater, "shell_version", lambda: "0.4.17")
    assert updater.own_version() == "0.4.19"
    # 没有载荷（整装/开发）时退回壳版本
    monkeypatch.setattr(updater, "payload_root", lambda: None)
    assert updater.own_version() == "0.4.17"


def test_update_check_compares_against_the_payload_version(monkeypatch, tmp_path):
    """检查更新传给 plan 的 current_version 必须是**载荷**版本。

    原版传的是 ``desktop.__version__``（烧在 exe 里、载荷更新改不了）——
    于是装完载荷检查更新永远说"有新版本"。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from desktop import ui_mount

    seen: dict = {}
    monkeypatch.setattr(updater, "installed_payload_version", lambda: "0.4.19")
    monkeypatch.setattr(updater, "shell_version", lambda: "0.4.17")

    def _fake_plan(*, current_version, current_shell=None, url=None):  # noqa: ANN001
        seen["current"] = current_version
        raise updater.UpdateError("到此为止（测试只关心拿哪个版本去比）")

    monkeypatch.setattr(updater, "plan", _fake_plan)
    assets = Path(__file__).resolve().parent.parent / "assets"
    desktop_dir = Path(__file__).resolve().parent.parent / ".." / "web" / "desktop"
    app = FastAPI()
    ui_mount.attach(app, desktop_dir=(desktop_dir if desktop_dir.is_dir() else None))
    with TestClient(app) as c:
        body = c.get("/api/desktop/update").json()
    assert seen.get("current") == "0.4.19", seen
    assert body.get("payloadVersion") == "0.4.19", body
    assert body.get("shellVersion") == "0.4.17", body
    del assets


def test_relaunch_script_can_be_rendered_anywhere(tmp_path):
    """重启助手脚本：等进程有上限、先 cd 回安装目录、每步写日志。

    这三条都是 2026-10-08 那次"窗口没了也拉不起来"的教训（原版死等 + 不记日志，
    失败之后无据可查）。
    """
    text = updater.relaunch_script_text(
        pid=4242, exe=tmp_path / "app" / "OpenMinisDesktop.exe", app_root=tmp_path / "app"
    )
    assert "PID eq 4242" in text
    assert "GEQ" in text, "没有等待上限 —— 旧进程不退会永久卡住助手"
    assert f'cd /d "{tmp_path / "app"}"' in text, "启动前没回安装目录"
    assert "openminis-relaunch.log" in text, "没有日志 —— 失败后无据可查"
    assert "start \"\"" in text


# ---------------------------------------------------------------------------
# 两条路必须**同一个基线**：检查说"有载荷可装"，安装就得真的去装
#   2026-10-08 补漏（v0.4.20 换了检查段、安装段还留着壳版本）：
#   壳版本 > 已装载荷版本时（用户手动装了新壳、载荷还是上一版）——
#     检查 → kind=payload（"可小更新"）→ 界面弹出「下载并安装（2.4 MB）」
#     安装 → 拿壳版本去比 → kind=none → phase=done、"已是最新" → **静默无操作**
#   而用户看到的是"点了没反应"，没有任何报错。这类 bug 只会静默发生，
#   所以钉的不是文案，是"两处 current 相等、且是载荷版本"这个真行为。
# ---------------------------------------------------------------------------
def _update_app(tmp_path, monkeypatch, *, installed: str, latest: str, shell: str):
    """把两条更新路由挂起来：载荷清单是 stub，壳版本与载荷版本可分别指定。

    ``shell > installed`` 就是出事的那种状态（清单里的壳指纹与本机一致 → 只该走
    "小更新"那条路，于是"用哪个版本去比"成了唯一变量）。
    """
    from fastapi import FastAPI

    from desktop import paths, ui_mount

    payload = _fake_payload(tmp_path, monkeypatch, installed)
    monkeypatch.setattr(updater, "shell_version", lambda: shell)
    raw = _manifest(version=latest, shellId=updater.own_shell_id())
    monkeypatch.setattr(updater, "fetch_manifest", lambda url, **kw: updater.parse_manifest(raw))
    # 安装段用的是**它自己 import 的那个** ``paths.payload_root``，一并指到假载荷
    monkeypatch.setattr(paths, "payload_root", lambda: payload)

    desktop_dir = Path(__file__).resolve().parent.parent / ".." / "web" / "desktop"
    app = FastAPI()
    ui_mount.attach(app, desktop_dir=(desktop_dir if desktop_dir.is_dir() else None))
    return app, ui_mount


def _wait_for_update(ui_mount, timeout: float = 20.0) -> dict:
    """安装段在后台线程里跑 —— 等它落到终态，别用 sleep 猜进度。"""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        state = dict(ui_mount._update_state)  # noqa: SLF001 - 断言要钉的就是它
        if state.get("phase") in {"ready", "done", "failed", "manual"}:
            return state
        time.sleep(0.02)
    raise AssertionError(f"{timeout}s 内更新线程没有结束：{dict(ui_mount._update_state)}")


def test_apply_and_check_use_the_very_same_current_version(tmp_path, monkeypatch):
    """安装段的 current 必须**就是**检查段那个值，而且必须是**载荷**版本。

    只钉"两处相等"不够 —— 两边都错成壳版本也会相等。所以同时钉住它等于
    ``own_version()``（= 已装载荷 0.4.19），并且**不等于**壳版本 0.4.21：
    两处中任何一处改回 ``__version__``，这条就红。
    """
    from fastapi.testclient import TestClient

    app, ui_mount = _update_app(
        tmp_path, monkeypatch, installed="0.4.19", latest="0.4.20", shell="0.4.21"
    )

    seen: list[str] = []

    def _fake_plan(*, current_version, current_shell=None, url=None):  # noqa: ANN001
        seen.append(current_version)
        raise updater.UpdateError("到此为止（测试只关心拿哪个版本去比）")

    monkeypatch.setattr(updater, "plan", _fake_plan)
    monkeypatch.setattr(updater, "save_state", lambda state: None)
    with TestClient(app) as c:
        c.get("/api/desktop/update")          # 检查那段
        c.post("/api/desktop/update")         # 安装那段（后台线程）
        _wait_for_update(ui_mount)

    assert len(seen) == 2, f"两条路没都走到 plan：{seen}"
    assert seen[0] == seen[1], f"检查与安装的版本基线不一致：{seen}"
    assert seen[0] == updater.own_version() == "0.4.19", seen
    assert seen[0] != "0.4.21", "拿烧在 exe 里的壳版本当基线 —— 正是这次要修的 bug"


def test_apply_really_installs_when_the_shell_is_newer_than_the_payload(tmp_path, monkeypatch):
    """壳 0.4.21 > 载荷 0.4.19、清单 0.4.20：检查说"可小更新"，安装就得真的下+装。

    改之前安装段拿壳版本 0.4.21 跟 0.4.20 比 → ``kind=none`` → ``phase=done``、
    note="已是最新（0.4.21）"：界面明明弹着「下载并安装（2.4 MB）」，点下去什么
    都不会发生，而且不报错。
    """
    from fastapi.testclient import TestClient

    app, ui_mount = _update_app(
        tmp_path, monkeypatch, installed="0.4.19", latest="0.4.20", shell="0.4.21"
    )

    steps: list[tuple[str, str]] = []
    archive = tmp_path / "update.zip"
    archive.write_bytes(b"zip")
    monkeypatch.setattr(
        updater, "download",
        lambda asset, dest, on_progress=None: (steps.append(("download", asset.url)), archive)[1],
    )
    monkeypatch.setattr(
        updater, "install_payload",
        lambda zip_path, payload_dir, *, expect_shell: steps.append(("install", str(zip_path))),
    )
    monkeypatch.setattr(updater, "cleanup_old_downloads", lambda keep=None, **kw: None)
    monkeypatch.setattr(updater, "save_state", lambda state: None)

    with TestClient(app) as c:
        check = c.get("/api/desktop/update").json()
        assert check["available"] is True and check["kind"] == "payload", check
        assert check["current"] == "0.4.19", check
        c.post("/api/desktop/update")
        final = _wait_for_update(ui_mount)

    assert final["phase"] == "ready", final         # 不是 done / "已是最新"
    assert "已是最新" not in str(final.get("note") or ""), final
    assert final.get("version") == "0.4.20", final
    assert [s[0] for s in steps] == ["download", "install"], steps
