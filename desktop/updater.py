"""应用内更新：拉清单 → 比版本 → 下载 → 校验 → 替换 → 重启 [T-in-app-update]。

对标的是 CC Switch（Tauri）那套，形状一样：应用去拉一个**固定地址**的
``latest.json``，比版本，下载对应资产，校验，替换自己，重启。清单由
``scripts/make_latest.py`` 生成、跟着 release 一起发。

## 为什么这个项目做起来比 Tauri 简单一截

第 2 步把内核与界面拆成了 exe 旁边的 ``payload/``（``desktop/paths.py`` 的
``payload_root``）。于是**日常更新根本不用碰正在运行的 exe**：

    只换载荷（payload）  2~3 MB   替换一个目录 → 重启生效
    换壳（full）         39 MB    得替换 exe 本身 → 交给一个"等我退出"的助手脚本

内核、界面这些天天变的东西全在第一条路上。第二条只在壳或运行时变了的时候才走。

## 为什么"替换一个目录"是安全的

Python 导入完 ``.py`` / ``.pyc`` 就把文件关掉了，不持有句柄，所以运行中重命名
``payload/`` 在 Windows 上也成立 —— 这也是当初把载荷做成**普通目录**而不是打进
exe 的附带好处。

## 校验做到哪一步（说清楚，别夸大）

清单里的 ``sha256`` 会验：防的是**下载被截断/损坏**、以及缓存/代理返回了旧包。
它**防不住** "有人能改 GitHub 上的 release" —— 那需要签名（像 CC Switch 那样每个
包配一个 ``.sig``、应用内置公钥）。这一步没做，记在这里，别以为有了 sha256 就安全了。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: 固定地址：`releases/latest` 永远指向最新的 release，应用里不用写版本号，
#: 也不吃 GitHub API 的限流（它是 release 资产，不是 API）。
DEFAULT_MANIFEST_URL = (
    "https://github.com/Mapotato-Ma/OpenMinis-Desktop"
    "/releases/latest/download/latest.json"
)

#: 允许换一个清单来源：端到端测试要用，将来架自建镜像/内网分发也用得上
#: （办公电脑连不上 GitHub 是常事）。
MANIFEST_URL = os.environ.get("OPENMINIS_UPDATE_URL", "").strip() or DEFAULT_MANIFEST_URL

#: 网络超时。检查更新是个"点一下"的动作，宁可快点失败也别让界面干等。
CHECK_TIMEOUT_S = 15
DOWNLOAD_TIMEOUT_S = 60
_CHUNK = 1 << 16

ProgressFn = Callable[[int, int], None]


class UpdateError(RuntimeError):
    """更新过程中的可展示错误（界面直接把 message 显示给用户）。"""


@dataclass(frozen=True)
class Asset:
    url: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    shell_id: str
    notes: str
    pub_date: str
    payload: Asset | None
    full: Asset | None

    @property
    def notes_first_line(self) -> str:
        for line in (self.notes or "").splitlines():
            line = line.strip()
            if line:
                return line[:120]
        return ""


# ---------------------------------------------------------------------------
# 版本比较
# ---------------------------------------------------------------------------
def parse_version(text: str) -> tuple[int, ...]:
    """``"v0.4.5"`` → ``(0, 4, 5)``。

    刻意不用字符串比较：``"0.10.0" < "0.9.0"`` 在字符串序里是真的，在版本序里是假的。
    非数字后缀（``0.4.5-rc1``）按"先看数字段"处理 —— 段数不同的短的那个更小。
    """
    cleaned = (text or "").strip().lstrip("vV")
    parts: list[int] = []
    for chunk in cleaned.replace("-", ".").replace("+", ".").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------
def _asset(raw: Any, key: str) -> Asset | None:
    if not isinstance(raw, dict):
        return None
    url = raw.get("url")
    digest = raw.get("sha256")
    if not isinstance(url, str) or not url or not isinstance(digest, str) or not digest:
        return None
    try:
        size = int(raw.get("bytes") or 0)
    except (TypeError, ValueError):
        size = 0
    return Asset(url=url, sha256=digest.lower(), bytes=size)


def parse_manifest(raw: Any) -> UpdateInfo:
    if not isinstance(raw, dict):
        raise UpdateError("更新清单格式不对（不是 JSON 对象）")
    version = raw.get("version")
    if not isinstance(version, str) or not version.strip():
        raise UpdateError("更新清单里没有 version")
    assets = raw.get("assets") if isinstance(raw.get("assets"), dict) else {}
    info = UpdateInfo(
        version=version.strip(),
        # 老清单没有 shellId 时按"同一个壳"处理 —— 最坏也只是走小更新。
        shell_id=str(raw.get("shellId") or "").strip(),
        notes=str(raw.get("notes") or ""),
        pub_date=str(raw.get("pubDate") or ""),
        payload=_asset(assets.get("payload"), "payload"),
        full=_asset(assets.get("full"), "full"),
    )
    if info.payload is None and info.full is None:
        raise UpdateError("更新清单里没有任何可下载的资产")
    return info


def fetch_manifest(url: str, *, timeout: float = CHECK_TIMEOUT_S) -> UpdateInfo:
    request = urllib.request.Request(url, headers={"User-Agent": "OpenMinisDesktop"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # 还没有发布过任何 release —— 不是错误，是"没有更新"。
            raise UpdateError("还没有发布过版本") from None
        raise UpdateError(f"读更新清单失败：HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"连不上更新服务：{exc}") from None
    except ValueError as exc:
        raise UpdateError(f"更新清单不是合法 JSON：{exc}") from None
    return parse_manifest(raw)


@dataclass(frozen=True)
class Plan:
    """这次更新该怎么做。``kind`` 只有三种字面量，界面据此换文案。"""

    kind: str                     # "none" | "payload" | "full"
    info: UpdateInfo | None = None
    reason: str = ""

    @property
    def asset(self) -> Asset | None:
        if self.info is None:
            return None
        return self.info.payload if self.kind == "payload" else self.info.full


def own_shell_id() -> str:
    """本机 exe 里那份壳指纹（冻在 desktop/build_id.py）。"""
    from .build_id import SHELL_ID  # noqa: PLC0415

    return SHELL_ID


def plan(
    *, current_version: str, current_shell: str | None = None, url: str = MANIFEST_URL
) -> Plan:
    """检查并决定怎么做。

    **壳指纹对不上就只能整包** —— 载荷里的内核与界面只对同一个壳有效。
    判据是壳源码的哈希而不是版本号：版本号每个 release 都涨，拿它当判据会让
    每次更新都退化成下整包（第一版就是这么错的）。
    """
    info = fetch_manifest(url)
    if not is_newer(info.version, current_version):
        return Plan("none", info, f"已是最新（{current_version}）")
    shell = current_shell if current_shell is not None else own_shell_id()
    if info.shell_id and info.shell_id != shell:
        if info.full is None:
            return Plan("full", info, "这次更新换了壳，但清单里没有整包")
        return Plan("full", info, f"这次更新换了壳（{shell} → {info.shell_id}），需要整包")
    if info.payload is None:
        if info.full is None:  # pragma: no cover - parse_manifest 已挡住
            return Plan("none", info, "清单里没有可用资产")
        return Plan("full", info, "清单里没有小更新包，退回整包")
    return Plan("payload", info, f"可小更新：{current_version} → {info.version}")


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------
def download(asset: Asset, dest_dir: Path, *, on_progress: ProgressFn | None = None) -> Path:
    """流式下载并**按清单里的 sha256 校验**；不匹配就删掉并报错。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = asset.url.rsplit("/", 1)[-1] or "update.bin"
    target = dest_dir / name
    digest = hashlib.sha256()
    done = 0
    request = urllib.request.Request(asset.url, headers={"User-Agent": "OpenMinisDesktop"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as resp:  # noqa: S310
            total = int(resp.headers.get("Content-Length") or asset.bytes or 0)
            with target.open("wb") as fh:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, total)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        target.unlink(missing_ok=True)
        raise UpdateError(f"下载失败：{exc}") from None

    got = digest.hexdigest()
    if asset.sha256 and got != asset.sha256:
        target.unlink(missing_ok=True)
        # 报清楚：下载被截断/被换了包，两种都要知道。
        raise UpdateError(
            f"校验不过（期望 {asset.sha256[:16]}…，实际 {got[:16]}…），已丢弃下载的文件"
        )
    return target


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------
def _safe_extract(archive: Path, dest: Path) -> None:
    """解压到 ``dest``，并拒绝任何指向目录外的成员（zip slip）。

    包是我们自己发的、还验过 sha256，按说不会出事；但这条太便宜了，不做才奇怪。
    """
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            out = (dest / member.filename).resolve()
            if out != base and base not in out.parents:
                raise UpdateError(f"压缩包里有个成员指向目录外：{member.filename}")
        zf.extractall(dest)  # noqa: S202 - 上面逐个查过


def install_payload(archive: Path, payload_dir: Path, *, expect_shell: str | None) -> None:
    """把载荷解出来并**原子替换** ``payload_dir``。

    顺序是"先解到旁边的临时目录 → 查验 → 把旧的改名让位 → 新的挪进来"。
    中途出错会把旧的回滚回去 —— 宁可更新失败，也不能把装好的东西弄坏。
    """
    staging = payload_dir.with_name(payload_dir.name + ".new")
    retired = payload_dir.with_name(payload_dir.name + ".old")
    for tmp in (staging, retired):
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    _safe_extract(archive, staging)

    manifest = staging / "payload.json"
    if not manifest.is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError("更新包里没有 payload.json —— 这不是一个载荷包")
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(f"更新包的 payload.json 读不了：{exc}") from None
    if expect_shell and str(meta.get("shellId") or "") != expect_shell:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(
            f"更新包是给壳 {meta.get('shellId')} 的，本机是 {expect_shell} —— "
            "这次更新换了壳，得走整包"
        )
    if not (staging / "openminis").is_dir():
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError("更新包里没有内核目录（openminis/）")

    moved_aside = False
    if payload_dir.exists():
        payload_dir.rename(retired)
        moved_aside = True
    try:
        staging.rename(payload_dir)
    except OSError as exc:
        if moved_aside:   # 回滚：把旧的挪回来
            retired.rename(payload_dir)
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(f"替换载荷目录失败：{exc}") from None
    if moved_aside:
        # 删不掉不影响使用（下一个版本还会覆盖它），所以只尽力。
        shutil.rmtree(retired, ignore_errors=True)


# ---------------------------------------------------------------------------
# 重启
# ---------------------------------------------------------------------------
#: 重启助手：等旧进程退出，再把新版本拉起来。用**独立进程**是因为正在跑的这个
#: 进程没法换掉自己的 exe（Windows 上那是锁定文件），只能"我先退出，你来"。
_RELAUNCH_CMD = """@echo off
rem 由 OpenMinis Desktop 的更新器写出来：等旧进程退出，再启动新版本。
setlocal
:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if not errorlevel 1 (
  timeout /t 1 /nobreak >nul
  goto wait
)
start "" "{exe}"
del "%~f0"
"""


def schedule_relaunch(*, pid: int | None = None, exe: Path | None = None) -> Path | None:
    """安排"退出后重启"。返回写出来的助手脚本路径（Windows）或 None。

    非 Windows 上不写脚本：那些平台不是发布目标，硬写一个 .sh 只会变成没人维护的
    死代码（而且本地测试想验的是**载荷替换**，不是重启）。
    """
    if sys.platform != "win32":
        return None
    exe = exe or Path(sys.executable)
    pid = pid if pid is not None else os.getpid()
    script = Path(tempfile.gettempdir()) / f"openminis-relaunch-{pid}.cmd"
    script.write_text(
        _RELAUNCH_CMD.format(pid=pid, exe=str(exe)), encoding="utf-8"
    )
    subprocess.Popen(  # noqa: S603
        ["cmd", "/c", str(script)],
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        close_fds=True,
    )
    return script


def updates_dir() -> Path:
    """下载与解压都放这儿（用户数据目录下，不污染安装目录）。"""
    from .paths import data_root  # noqa: PLC0415

    path = data_root() / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_old_downloads(keep: Path | None = None, *, older_than_s: float = 86400) -> None:
    """把一天前的下载清掉 —— 这些包几十 MB，不该在用户机器上慢慢堆积。"""
    root = updates_dir()
    now = time.time()
    for path in root.iterdir():
        if keep is not None and path == keep:
            continue
        try:
            if now - path.stat().st_mtime < older_than_s:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 清理失败不该影响更新
            continue
