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
import logging
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

from .paths import payload_root

logger = logging.getLogger(__name__)

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

#: 重试。**必须重试**：从国内到 GitHub 是"时通时不通"—— 实测同一个地址连续 10 次里
#: 有 2 次要 5.9 秒以上（1.1s ~ 6.1s 抖动），偶发直接读超时。失败一次就结束，在用户
#: 眼里就是"这功能时灵时不灵"：真实用户报过，上一次点能出来，这一次报
#: 「连不上更新服务：The read operation timed out」。
#:
#: 抖动通常是瞬时的，所以退避几秒再试比"把超时调大"有效得多 —— 调大只是让界面干等。
CHECK_ATTEMPTS = 3
DOWNLOAD_ATTEMPTS = 3
RETRY_BACKOFF_S = (2.0, 6.0)

ProgressFn = Callable[[int, int], None]


class UpdateError(RuntimeError):
    """更新过程中的可展示错误（界面直接把 message 显示给用户）。

    ``retryable`` 是给重试用的：网络抖一下值得再试，HTTP 404 或者清单不是合法
    JSON 再试一百次也一样。
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def _open(request: urllib.request.Request, timeout: float):
    """唯一一处真正发请求的地方 —— 测试用它注入假网络。"""
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310


def _describe(exc: BaseException) -> str:
    """把底层网络异常翻成一句人话。用户会直接把这句话截图发过来。"""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    reason = getattr(exc, "reason", exc)
    text = str(reason) or type(reason).__name__
    lowered = text.lower()
    if isinstance(reason, TimeoutError) or "timed out" in lowered:
        return "超时（连上了，但没等到数据）"
    if "getaddrinfo" in lowered or "name or service not known" in lowered:
        return f"域名解析失败（DNS）：{text}"
    if "certificate" in lowered or "ssl" in lowered:
        return f"TLS 出错：{text}"
    if "connection" in lowered or "refused" in lowered or "unreachable" in lowered:
        return f"连不上：{text}"
    return text


def _retry(what: str, attempts: int, run: Callable[[], Any]) -> Any:
    """跑 ``run``，失败就退避重试。只重试"值得重试"的：见 ``UpdateError.retryable``。

    最后一次失败时把"试了几次"缀在错误里 —— 用户看到的是一个稳定的失败，而不是
    一个不知道试没试过的报错。
    """
    last: UpdateError | None = None
    for index in range(attempts):
        try:
            return run()
        except UpdateError as exc:
            last = exc
            if not exc.retryable or index == attempts - 1:
                break
            wait = RETRY_BACKOFF_S[min(index, len(RETRY_BACKOFF_S) - 1)]
            logger.warning(
                "%s 第 %d/%d 次失败（%s）—— %.0f 秒后重试", what, index + 1, attempts, exc, wait
            )
            time.sleep(wait)
    assert last is not None
    if last.retryable and attempts > 1:
        raise UpdateError(f"{last}（已自动重试 {attempts} 次）") from None
    raise last


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


def _fetch_once(url: str, timeout: float) -> UpdateInfo:
    request = urllib.request.Request(url, headers={"User-Agent": "OpenMinisDesktop"})
    try:
        with _open(request, timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # 还没有发布过任何 release —— 不是错误，是"没有更新"。重试也没用。
            raise UpdateError("还没有发布过版本", retryable=False) from None
        # 5xx 是服务端抽风，值得再试；4xx 是我们自己的问题，再试也一样。
        raise UpdateError(
            f"读更新清单失败：{_describe(exc)}", retryable=exc.code >= 500
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"连不上更新服务：{_describe(exc)}") from None
    except ValueError as exc:
        raise UpdateError(f"更新清单不是合法 JSON：{exc}", retryable=False) from None
    return parse_manifest(raw)


def fetch_manifest(
    url: str, *, timeout: float = CHECK_TIMEOUT_S, attempts: int = CHECK_ATTEMPTS
) -> UpdateInfo:
    """拉清单。**会重试** —— 见 ``RETRY_BACKOFF_S`` 上的说明（国内到 GitHub 抖动）。"""
    logger.debug("检查更新：%s（最多试 %d 次）", url, attempts)
    return _retry("检查更新", attempts, lambda: _fetch_once(url, timeout))


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


def shell_version() -> str:
    """本机 exe 里那份**壳**的版本号（冻在 ``desktop/__init__.py``）。"""
    from . import __version__  # noqa: PLC0415

    return str(__version__)


def installed_payload_version() -> str | None:
    """已装载荷的版本号（``payload/payload.json`` 里的 ``appVersion``）。

    **为什么不拿 ``__version__`` 当"当前版本"**（2026-10-08 用户实测的问题）：
    载荷只装内核与界面（``scripts/make_payload.py`` 的 ``PAYLOAD_TREES`` **刻意
    不含** ``desktop/``，因为决定"去哪找载荷"的引导代码不能由载荷自己给），
    所以 ``desktop/__init__.py`` 里那个版本号是**烧进 exe** 的 —— 载荷更新永远
    改不了它。后果：装了 0.4.19 的载荷，界面照旧显示 0.4.17，检查更新永远说
    "有新版本"，于是用户在"更新 → 重启 → 还是 17"里打转，而且**每次都会重新下
    一遍 2.4MB**。真正的当前版本必须读载荷清单。
    """
    root = payload_root()
    if root is None:
        return None
    try:
        meta = json.loads((root / "payload.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    version = str(meta.get("appVersion") or meta.get("kernelVersion") or "").strip()
    return version or None


def own_version() -> str:
    """用于比较的"当前版本"：优先已装载荷，退回壳版本。"""
    return installed_payload_version() or shell_version()


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
def _download_once(
    asset: Asset, target: Path, on_progress: ProgressFn | None
) -> str:
    """下**一次**。返回 sha256；出错就删掉半截文件再抛。"""
    digest = hashlib.sha256()
    done = 0
    request = urllib.request.Request(asset.url, headers={"User-Agent": "OpenMinisDesktop"})
    try:
        with _open(request, DOWNLOAD_TIMEOUT_S) as resp:
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
        if on_progress is not None:
            on_progress(0, 0)  # 重试会从头下，把进度条收回零，否则看着像卡住了
        raise UpdateError(f"下载失败：{_describe(exc)}") from None

    got = digest.hexdigest()
    if asset.sha256 and got != asset.sha256:
        target.unlink(missing_ok=True)
        # 报清楚：下载被截断/被换了包，两种都要知道。截断值得重试一次（大文件走
        # 抖动网络时很常见），被换了包就不是重试能解决的了 —— 但两者都只是"再下一遍"。
        raise UpdateError(
            f"校验不过（期望 {asset.sha256[:16]}…，实际 {got[:16]}…），已丢弃下载的文件"
        )
    return got


def download(
    asset: Asset,
    dest_dir: Path,
    *,
    on_progress: ProgressFn | None = None,
    attempts: int = DOWNLOAD_ATTEMPTS,
) -> Path:
    """流式下载并**按清单里的 sha256 校验**；不匹配就删掉并报错。

    大文件在抖动的网络上是"下到一半断掉"的重灾区（39 MB 的整包尤其如此），所以这里
    也要重试：每次从头下，半截文件在重试前就删掉了。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = asset.url.rsplit("/", 1)[-1] or "update.bin"
    target = dest_dir / name
    _retry("下载", attempts, lambda: _download_once(asset, target, on_progress))
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
#:
#: 2026-10-08 用户实测"点了重启，窗口没了也拉不起来"之后加了三道保险：
#:   * **等 pid 有上限**（``WAITED``）—— 旧进程哪怕因为句柄没退干净，
#:     也不会把助手永久卡死（原版在这里死等）；
#:   * **先 ``cd /d`` 回安装目录**再启动，避免继承一个奇怪的当前目录；
#:   * **每一步写日志**（``openminis-relaunch.log``）—— 助手脚本自己会自删，
#:     不留日志的话失败之后**无据可查**（现在就查不到）。
_RELAUNCH_CMD = """@echo off
rem 由 OpenMinis Desktop 的更新器写出来：等旧进程退出 → 回安装目录 → 启动新版本。
setlocal
set "LOG=%~dp0openminis-relaunch.log"
echo [%DATE% %TIME%] 等待 PID {pid} 退出 >> "%LOG%"
set /a WAITED=0
:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if errorlevel 1 goto start
set /a WAITED+=1
if %WAITED% GEQ {waited_max} (
  echo [%DATE% %TIME%] 等满 {waited_max} 秒进程仍在，不再等，直接启动 >> "%LOG%"
  goto start
)
timeout /t 1 /nobreak >nul
goto wait
:start
cd /d "{app_root}"
echo [%DATE% %TIME%] 启动 "{exe}"（工作目录 %CD%） >> "%LOG%"
start "" "{exe}"
echo [%DATE% %TIME%] start 返回 %errorlevel% >> "%LOG%"
del "%~f0"
"""

#: 等旧进程退出的上限（秒）。超过就照启动 —— 让用户拿到窗口比等到天荒地老有用。
_RELAUNCH_WAIT_MAX_S = 60


def relaunch_script_text(*, pid: int, exe: Path, app_root: Path) -> str:
    """渲染重启助手脚本。单独抽出来是为了能在**任何平台**上测它写了什么 ——
    真正要 spawn ``cmd`` 的那步只在 Windows 上跑得起来。"""
    return _RELAUNCH_CMD.format(
        pid=pid, exe=str(exe), app_root=str(app_root),
        waited_max=_RELAUNCH_WAIT_MAX_S,
    )


def schedule_relaunch(*, pid: int | None = None, exe: Path | None = None) -> Path | None:
    """安排"退出后重启"。返回写出来的助手脚本路径（Windows）或 None。

    非 Windows 上不写脚本：那些平台不是发布目标，硬写一个 .sh 只会变成没人维护的
    死代码（而且本地测试想验的是**载荷替换**，不是重启）。
    """
    if sys.platform != "win32":
        return None
    exe = exe or Path(sys.executable)
    pid = pid if pid is not None else os.getpid()
    app_dir = exe.resolve().parent
    script = Path(tempfile.gettempdir()) / f"openminis-relaunch-{pid}.cmd"
    script.write_text(
        relaunch_script_text(pid=pid, exe=exe, app_root=app_dir), encoding="utf-8"
    )
    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    base: dict[str, Any] = {
        # **三个流都要接 DEVNULL**：原版让子进程继承父进程的句柄，父进程一退，
        # cmd 的句柄就悬了 —— 助手死在半路，用户看到"窗口没了也不回来"。
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    try:
        # 带上 BREAKAWAY：万一这个进程活在某个 Job 对象里（安装器/启动器常见），
        # 不带它的话子进程会跟着父进程一起被杀 —— 助手根本没跑过。
        # 不在 Job 里时这个标志会报 Access denied，所以失败了要不带它重试一次。
        subprocess.Popen(  # noqa: S603
            ["cmd", "/c", str(script)],
            creationflags=flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0),
            **base,
        )
    except OSError as exc:
        logger.warning("relaunch helper (breakaway) failed: %s — 去掉该标志重试", exc)
        subprocess.Popen(  # noqa: S603
            ["cmd", "/c", str(script)], creationflags=flags, **base
        )
    return script


#: 上一次更新的结果（落盘）。进程内的 ``_update_state`` 一重启就空 ——
#: 用户点完重启再打开，看到的是"还没检查"，没人知道刚才到底成没成。
STATE_FILE = "state.json"


def state_path() -> Path:
    return updates_dir() / STATE_FILE


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, Any]) -> None:
    """只落一份"人话摘要"，不落 phase —— 免得重启后界面以为还有活儿在跑。"""
    try:
        state_path().write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:  # pragma: no cover - 写不进去不该影响更新本身
        logger.debug("update state save failed", exc_info=True)


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
