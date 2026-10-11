"""应用内更新：拉清单 → 比版本 → 下载 → 校验 → 暂存 → 退出后切换 [T-in-app-update]。

对标的是 CC Switch（Tauri）那套，形状一样：应用去拉一个**固定地址**的
``latest.json``，比版本，下载对应资产，校验，替换自己，重启。清单由
``scripts/make_latest.py`` 生成、跟着 release 一起发。

**本模块只负责"怎么换"**（下载/校验/解压/助手脚本/落盘摘要）；"什么时候换、
谁说了算、失败了算什么"在 ``desktop/update_transaction.py``（一个 job、一个状态机）。
这条分工是 R01–R05 的整改结果：准备（下载解压）与提交（起助手）必须分开。

## 为什么这个项目做起来比 Tauri 简单一截

第 2 步把内核与界面拆成了 exe 旁边的 ``payload/``（``desktop/paths.py`` 的
``payload_root``）。于是**日常更新根本不用碰正在运行的 exe**：

    只换载荷（payload）  2~3 MB   替换 payload/ 一个目录 → 退出后生效
    换壳（full）         39 MB    得替换 exe 本身 → 交给同一个"等我退出"的助手脚本

内核、界面这些天天变的东西全在第一条路上。第二条只在壳或运行时变了的时候才走。
两条路**共用同一个助手**（``schedule_swap``）—— 改前 payload 那条不给助手，
整包那条自己会 start 新 exe、点重启时又另起一个 relaunch 助手（R02）。

## 为什么"替换一个目录"是安全的（以及为什么必须退出后再换）

Python 导入完 ``.py`` / ``.pyc`` 就把文件关掉了，不持有句柄，所以运行中重命名
``payload/`` 在 Windows 上也成立。**但能换不等于该换**：正在跑的进程已经把
``openminis`` 的老模块 import 进内存了，替换目录之后新的 lazy import / 资源读取
会来自新目录 —— 同一个解释器里新旧代码混着跑（R05）。所以载荷也走"解到旁边 →
退出 → 助手切换"，进程活着的时候一个字节都不动。

## 旧版本什么时候才能删

**新版进程报告启动成功之后**（``updates/boot-ok.json``，见 :func:`mark_boot_ok`）。
发出 ``start`` 不算证据 —— 坏依赖、起不来的时候，旧目录被删掉用户就没得退了（R04）。

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


def safe_job_id(job_id: str) -> str:
    """job id 会进目录名与 ``.cmd`` 的环境变量 —— 先钉死它的字符集。"""
    text = str(job_id or "").strip()
    if not text or any(ch not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_"
                       for ch in text):
        raise UpdateError(f"非法的更新任务 id：{job_id!r}")
    return text


def stage_payload(
    archive: Path, payload_dir: Path, *, expect_shell: str | None, job_id: str
) -> Path:
    """把载荷解到 ``payload.new-<jobId>`` 并校验，返回那个暂存目录。

    **刻意不在这里替换 ``payload_dir``**（改前是就地原子替换）。原因：正在跑的
    进程已经把 ``openminis`` 的模块 import 进内存了，替换目录之后新起的 lazy
    import / 资源读取会来自新目录 —— 同一进程里新旧代码混着跑。改动后只有
    "退出再切换"这一条路（见 ``schedule_swap``），进程活着的时候一个字节都不动。

    顺序仍是"先解到旁边的临时目录 → 逐项查验"；失败只删自己那个暂存目录。
    """
    staged = payload_dir.with_name(f"{payload_dir.name}.new-{safe_job_id(job_id)}")
    shutil.rmtree(staged, ignore_errors=True)

    _safe_extract(archive, staged)

    manifest = staged / "payload.json"
    if not manifest.is_file():
        shutil.rmtree(staged, ignore_errors=True)
        raise UpdateError("更新包里没有 payload.json —— 这不是一个载荷包")
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        shutil.rmtree(staged, ignore_errors=True)
        raise UpdateError(f"更新包的 payload.json 读不了：{exc}") from None
    if expect_shell and str(meta.get("shellId") or "") != expect_shell:
        shutil.rmtree(staged, ignore_errors=True)
        raise UpdateError(
            f"更新包是给壳 {meta.get('shellId')} 的，本机是 {expect_shell} —— "
            "这次更新换了壳，得走整包"
        )
    if not (staged / "openminis").is_dir():
        shutil.rmtree(staged, ignore_errors=True)
        raise UpdateError("更新包里没有内核目录（openminis/）")
    return staged


def staged_payload_version(staged: Path) -> str:
    """暂存载荷的版本号（界面要同时报「正在跑哪个版本」和「待启用哪个版本」）。"""
    try:
        meta = json.loads((staged / "payload.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("appVersion") or meta.get("kernelVersion") or "").strip()


# ---------------------------------------------------------------------------
# 退出后切换（唯一一份助手脚本）
# ---------------------------------------------------------------------------
#: 助手脚本：等旧进程退出 → 让位 → 就位 → 启动新 exe → **等新版报告启动成功**
#: → 才删旧目录 → 最后清事务暂存目录。
#:
#: 为什么只有一个助手：改前 payload 与 full 各有一条路，而 full 那条自己会
#: ``start`` 新 exe、点「重启」时又另起一个 relaunch 助手 —— 两个启动者抢同一个
#: exe（R02）。现在两条路都只在这里、只有一个执行者。
#:
#: ``OM_UPD_TARGET`` 是要被替换的那个目录（整包 = 安装目录，载荷 = ``payload/``），
#: 由 Python 侧 ``assert_owned()`` 证明它属于本次事务后才传进来。这条脚本**只**
#: 整体删两样东西：``%OLD%``（``%OM_UPD_TARGET%.old-<stamp>``）与
#: ``%OM_UPD_STAGING%``（本次事务的暂存根），两者都带后缀、不可能是安装目录父目录。
#:
#: 为什么等"启动成功标记"而不是"start 返回"：R04 —— 发出 start 不等于新版真的
#: 起来了（坏依赖、起不来时旧版会被删掉）。新版进程自己在后端健康后写
#: ``updates/boot-ok.json``（见 :func:`mark_boot_ok`），脚本只认那个文件。
_SWAP_CMD = r"""@echo off
rem OpenMinis Desktop: finish an update after the app has exited.
rem ASCII only on purpose - every path travels in an environment variable, because
rem cmd.exe reads this file in the console code page and install paths are often
rem non-ASCII (e.g. F:\<chinese>\...). See updater.helper_env.
rem
rem Sleeps use `ping`, NOT `timeout`: this script is spawned with stdin=DEVNULL
rem (see schedule_swap), and `timeout /t` refuses to run without a console
rem ("Input redirection is not supported") - it returns immediately, so a whole
rem 90-iteration wait loop would spin out in milliseconds and "give up" at once.
setlocal
set "LOG=%~dp0openminis-swap.log"
set "TARGET=%OM_UPD_TARGET%"
set "NEW=%OM_UPD_NEW%"
set "OLD=%OM_UPD_TARGET%.old-%OM_UPD_STAMP%"
set "APP=%OM_UPD_APPROOT%"
set "EXE=%OM_UPD_EXE%"
set "BOOTOK=%OM_UPD_BOOTOK%"
echo [%DATE% %TIME%] waiting for pid %OM_UPD_PID% to exit >> "%LOG%"
set /a WAITED=0
:wait
tasklist /FI "PID eq %OM_UPD_PID%" 2>nul | find "%OM_UPD_PID%" >nul
if errorlevel 1 goto moveold
set /a WAITED+=1
if %WAITED% GEQ %OM_UPD_WAITMAX% (
  echo [%DATE% %TIME%] gave up after %OM_UPD_WAITMAX%s - old process still running >> "%LOG%"
  goto done
)
ping -n 2 127.0.0.1 >nul
goto wait
:moveold
set /a TRY=0
:retryold
move "%TARGET%" "%OLD%" >> "%LOG%" 2>&1
if not errorlevel 1 goto movenew
set /a TRY+=1
if %TRY% GEQ 15 (
  echo [%DATE% %TIME%] could not rename the target dir (still locked) - giving up >> "%LOG%"
  goto done
)
ping -n 3 127.0.0.1 >nul
goto retryold
:movenew
move "%NEW%" "%TARGET%" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [%DATE% %TIME%] could not move the new build in - rolling back >> "%LOG%"
  move "%OLD%" "%TARGET%" >> "%LOG%" 2>&1
  goto done
)
echo [%DATE% %TIME%] swapped the new build into "%TARGET%" >> "%LOG%"
cd /d "%APP%"
start "" "%APP%\%EXE%"
echo [%DATE% %TIME%] started the new build >> "%LOG%"
set /a WAITED=0
:awaitok
if exist "%BOOTOK%" goto ok
set /a WAITED+=1
if %WAITED% GEQ %OM_UPD_BOOTMAX% (
  echo [%DATE% %TIME%] no boot report after %OM_UPD_BOOTMAX%s - keeping the old copy at "%OLD%" >> "%LOG%"
  goto done
)
ping -n 2 127.0.0.1 >nul
goto awaitok
:ok
echo [%DATE% %TIME%] new build reported a boot - dropping the old copy >> "%LOG%"
set /a TRY=0
:cleanold
rmdir /s /q "%OLD%" 2>nul
if not exist "%OLD%" goto done
set /a TRY+=1
if %TRY% LSS 15 ( ping -n 3 127.0.0.1 >nul & goto cleanold )
echo [%DATE% %TIME%] old dir left behind (harmless; the next update cleans it) >> "%LOG%"
:done
rmdir /s /q "%OM_UPD_STAGING%" 2>nul
exit /b 0
"""

def script_encoding() -> str:
    """写 ``.cmd`` 助手脚本用的编码。

    **必须是系统的 ANSI 码页**（Windows 上是 ``mbcs``，简中机器 = CP936）：``cmd.exe``
    读 .cmd 文件是按 ANSI 解的，而我们写进去的东西里有**安装路径** ——
    用户完全可能装在 ``F:\\桌面\\07-代码\\...`` 这种目录下，写成 UTF-8 就会被读成乱码，
    ``move`` / ``start`` 全部指向不存在的路径。这类失败还很隐蔽：脚本照样"跑完"了，
    只是什么都没动。

    非 Windows 上返回 utf-8（那里没有 cmd，这两个助手脚本也用不上）。
    """
    return "mbcs" if sys.platform == "win32" else "utf-8"


def write_helper_script(path: Path, text: str) -> Path:
    """把助手脚本写盘：按 ``script_encoding()`` 编码，并且**显式写成 CRLF**。

    CRLF 不是风格问题：``cmd.exe`` 解析 .cmd 时要 CRLF，纯 LF 的文件会**整个跑不起来**
    （真机演练：LF 的脚本 `cmd /c` 返回 1、日志一个字都没有）。Windows 上
    ``write_text`` 的换行翻译碰巧能救回来，但那是隐式的 —— 这里写死，别指望运气。
    """
    body = text.replace("\r\n", "\n")
    for enc in (script_encoding(), "utf-8"):
        try:
            path.write_text(body, encoding=enc, newline="\r\n")
            return path
        except (LookupError, UnicodeEncodeError):  # pragma: no cover - 只在怪环境
            continue
    path.write_bytes(body.encode("utf-8", "replace"))  # pragma: no cover
    return path


#: 等旧进程退出的上限（秒）。超了就**放弃切换**（不像重启那样照启动）——
#: 半替换比不替换糟得多。
_SWAP_WAIT_MAX_S = 90

#: 等新版报告「我起来了」的上限（秒）。等不到就保留旧目录（R04）。
_BOOT_OK_WAIT_MAX_S = 120

#: 启动成功标记：新版进程在后端健康之后自己写（见 :func:`mark_boot_ok`）。
BOOT_OK_FILE = "boot-ok.json"

#: 助手脚本共用的环境变量名。**为什么参数走环境变量、不写进脚本**：
#: ``.cmd`` 里的字节要按控制台码页解释，而安装路径完全可能是
#: ``F:\桌面\07-代码\...`` —— 无论按 UTF-8 还是 ANSI 写进去，cmd 都可能读成
#: 另一条不存在的路径，而失败是**静默**的：脚本照跑、日志照写，只是什么都没动。
#: Windows 的环境变量是 UTF-16，跟文件编码、控制台码页都无关。
#: 真机演练（2026-10-08）：路径写进 ANSI 的 .cmd → ``move`` 连试 15 次全失败；
#: 改成环境变量传参 → 一次成功。
_ENV_TARGET = "OM_UPD_TARGET"
_ENV_NEW = "OM_UPD_NEW"
_ENV_STAGING = "OM_UPD_STAGING"
_ENV_EXE = "OM_UPD_EXE"
_ENV_APPROOT = "OM_UPD_APPROOT"
_ENV_STAMP = "OM_UPD_STAMP"
_ENV_PID = "OM_UPD_PID"
_ENV_WAITMAX = "OM_UPD_WAITMAX"
_ENV_BOOTMAX = "OM_UPD_BOOTMAX"
_ENV_BOOTOK = "OM_UPD_BOOTOK"

#: 助手脚本写在临时目录里的名字前缀。**它自己不自删**（见下）。
_HELPER_PREFIX = "openminis-swap-"


def cleanup_old_helpers(*, older_than_s: float = 86400) -> None:
    """清掉一天前留下的助手脚本与它的日志（尽力而为）。

    为什么不靠脚本里的 ``del "%~f0"`` 自删：那个惯用法会让 cmd 在删掉自己之后
    读不到下一行，**打印 "The batch file cannot be found." 并以退出码 1 结束** ——
    自删之后的任何命令都不会执行（实测，``scripts/update_drill.py`` 也盯着这条）。
    留着脚本还能事后翻它当时的日志，所以改成"应用下次更新时顺手清"。
    """
    root = Path(tempfile.gettempdir())
    now = time.time()
    for path in list(root.glob(f"{_HELPER_PREFIX}*.cmd")) + list(
        root.glob("openminis-swap.log")
    ):
        try:
            if now - path.stat().st_mtime < older_than_s:
                continue
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 清理失败不该影响更新
            continue


def helper_env(**extra: object) -> dict[str, str]:
    """当前进程环境 + 助手脚本要的参数（值转字符串，None 丢掉）。"""
    env = dict(os.environ)
    for key, value in extra.items():
        if value is not None:
            env[key] = str(value)
    return env




def swap_script_text() -> str:
    """渲染切换助手脚本（纯 ASCII；参数走环境变量，见 ``helper_env``）。

    抽成函数是为了能在**任何平台**上检查它写了什么 —— 真正要 spawn ``cmd``
    的那步只在 Windows 上跑得起来。
    """
    return _SWAP_CMD


def boot_ok_path() -> Path:
    """启动成功标记的落点（``updates/boot-ok.json``）。"""
    return updates_dir() / BOOT_OK_FILE


def mark_boot_ok(*, pid: int | None = None) -> Path | None:
    """新版进程启动成功时留一个标记，**只有它**能让助手删掉旧版本（R04）。

    调用点是「后端已经健康」那一处（``server_runner.start_server``），不是
    进程刚起来的时候 —— 坏依赖、起不来时这里根本走不到，助手于是保留旧目录。

    标记里带 ``jobId``：下一次启动 ``recover()`` 拿它和 journal 对着看，判"这次
    更新到底是成了还是没成"，如实写进界面能看到的那句话里。

    **只有正在切换时才写**（``state.json`` 里 ``phase == "committing"``）：日常
    启动一个文件都不落 —— 否则每次开 App 都在用户数据目录里留一份噪音，
    也让"这个标记是不是本次的"没法判断。
    """
    state = load_state()
    if str(state.get("phase") or "") != "committing":
        return None
    job_id = str(state.get("jobId") or "")
    if not job_id:
        return None
    payload: dict[str, Any] = {
        "jobId": job_id,
        "pid": int(pid if pid is not None else os.getpid()),
        "version": installed_payload_version() or shell_version(),
        "at": int(time.time() * 1000),
    }
    path = boot_ok_path()
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:  # pragma: no cover - 写不进去只影响"更新成没成"的判据
        logger.debug("boot-ok marker write failed", exc_info=True)
        return None
    return path


def read_boot_ok() -> dict[str, Any]:
    """读启动成功标记；没有/读不了就当空。"""
    try:
        data = json.loads(boot_ok_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def clear_boot_ok() -> None:
    """删掉标记。**在起助手之前**调用：残留的旧标记会被当成"新版起来了"。"""
    try:
        boot_ok_path().unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        logger.debug("boot-ok marker clear failed", exc_info=True)




def assert_owned(
    path: Path, *, allowed: tuple[Path, ...], what: str, forbidden: tuple[Path, ...] = ()
) -> Path:
    """破坏性动作前的最后一关：这个路径**属于本次事务**吗？

    改前那个 R01 就是没问这一句：无顶层目录的 ZIP 被接受后 ``staged`` 就是
    暂存根，而调度助手时拿 ``staged.parent`` 冒充"清理目录"——那条分支下它恰好是
    安装目录的**父目录**，助手于是对着一整层目录 ``rmdir /s /q``。

    所以现在：路径必须等于 ``allowed`` 之一或在其下，且不许是 ``forbidden``
    （安装目录的父目录、数据目录这类"再往上一步就是用户别的东西"的位置）。
    """
    resolved = Path(path).resolve()
    roots = tuple(Path(p).resolve() for p in allowed)
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise UpdateError(f"{what} 不在本次更新的目录里：{resolved}")
    for bad in forbidden:
        if resolved == Path(bad).resolve():
            raise UpdateError(f"{what} 指向了受保护的目录（{resolved}）—— 拒绝执行")
    return resolved


def schedule_swap(
    *,
    target: Path,
    staged: Path,
    staging_root: Path,
    install_dir: Path,
    exe_name: str = "OpenMinisDesktop.exe",
    pid: int | None = None,
    now: float | None = None,
    waited_max: int = _SWAP_WAIT_MAX_S,
    boot_max: int = _BOOT_OK_WAIT_MAX_S,
) -> Path:
    """写脚本 + 起**唯一**一个独立进程去完成切换。返回助手脚本路径。

    调用它的时机只有一个：用户点了「重启并完成替换」（见
    ``update_transaction.UpdateTransaction.commit``）。**准备阶段绝不调用它**：
    改前是"下载解压完就把助手叫起来等当前进程 90 秒"，而界面还在让用户慢慢决定，
    超时后助手自删、界面却还说"重启即完成替换"（R02）；点重启又另起一个
    relaunch 助手，于是两个进程抢着启动同一个 exe。

    三条路径（``target`` / ``staged`` / ``staging_root``）都必须是本次事务的
    目录 —— 由调用方用 :func:`assert_owned` 证明。这里只负责把它们**原样**交给
    脚本（走环境变量，见 :func:`helper_env`）。
    """
    pid = pid if pid is not None else os.getpid()
    script = Path(tempfile.gettempdir()) / f"openminis-swap-{pid}.cmd"
    if sys.platform != "win32":
        return script
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now else time.time()))
    write_helper_script(script, swap_script_text())
    env = helper_env(**{
        _ENV_PID: pid,
        _ENV_EXE: exe_name,
        _ENV_APPROOT: install_dir,
        _ENV_TARGET: target,
        _ENV_NEW: staged,
        _ENV_STAGING: staging_root,
        _ENV_BOOTOK: boot_ok_path(),
        _ENV_STAMP: stamp,
        _ENV_WAITMAX: waited_max,
        _ENV_BOOTMAX: boot_max,
    })
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
        "env": env,
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
        logger.warning("swap helper (breakaway) failed: %s — 去掉该标志重试", exc)
        subprocess.Popen(  # noqa: S603
            ["cmd", "/c", str(script)], creationflags=flags, **base
        )
    return script




# ---------------------------------------------------------------------------
# 整包（换壳）：解到旁边，等退出后由同一个助手换掉安装目录
# ---------------------------------------------------------------------------
#: 便携包的顶层目录名（``Compress-Archive -Path dist\OpenMinisDesktop`` 打出来的
#: zip 里就是这一个目录），安装目录本身也叫这个名字。
PORTABLE_DIR_NAME = "OpenMinisDesktop"

#: 整包解出来要占多少空间 —— 按 zip 的倍数估，留够余量再动手。
_FULL_PACKAGE_SPACE_FACTOR = 3.0


def install_root() -> Path | None:
    """整包替换的目标目录 = 装着 exe 的那个目录。

    源码运行（开发/测试）时返回 None —— 那里没有"安装目录"这个概念，
    换掉 cwd 只会把开发者的仓库搞坏。
    """
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def staging_root_for(install_dir: Path, job_id: str) -> Path:
    """暂存目录：**同一个父目录**（同一个卷）才能用 move 而不是跨盘拷贝。

    名字里带 job id —— 两个更新任务即便同时在跑，也不会抢同一个暂存目录、
    更不会把对方的暂存删掉（改前两者都用 ``_openminis-update-<pid>``，R03）。
    """
    return install_dir.with_name(f"_openminis-update-{safe_job_id(job_id)}")


def clean_leftovers(install_dir: Path, *, keep: Path | None = None) -> list[str]:
    """收拾上一次更新留下的 ``*.old-*`` 与 ``_openminis-update-*``（尽力而为）。

    放在准备阶段的**解压之前**（不是启动路径上）：启动路径要短要稳，而这里本来就
    是"慢活"。删除失败（还有进程占着）就留着，下次再来。

    ``keep`` 是本次事务自己的暂存根 —— 绝不能被自己清掉。
    """
    removed: list[str] = []
    parent = install_dir.parent
    for path in list(parent.glob(f"{install_dir.name}.old-*")) + list(
        parent.glob("_openminis-update-*")
    ):
        if path == install_dir or install_dir in path.parents:
            continue
        if keep is not None and path.resolve() == Path(keep).resolve():
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed.append(path.name)
    return removed


def retired_dirs(target: Path) -> list[Path]:
    """被让位的旧目录（``<target>.old-*``），名字倒序 = 最新的在前。"""
    return sorted(target.parent.glob(f"{target.name}.old-*"), key=lambda p: p.name, reverse=True)


def stage_full_package(
    archive: Path, *, install_dir: Path, job_id: str,
    exe_name: str = "OpenMinisDesktop.exe",
) -> Path:
    """把整包解到旁边、校验，返回"以后要变成安装目录"的那个暂存目录。

    **不安排助手、不动安装目录** —— 切换是用户点「重启并完成替换」之后的事
    （``update_transaction.UpdateTransaction.commit``）。改前是一边解压一边就把
    助手叫起来等当前进程退出（R02）。

    顶层目录是硬要求：``Compress-Archive -Path dist\\OpenMinisDesktop`` 打出来的
    zip 一定带它。手工重打包的"平铺"zip 直接拒绝 —— 那条路上"哪一层才是安装目录"
    只能靠猜，而猜错的代价是把安装目录的**父目录**整个删掉（R01）。
    """
    if not archive.is_file():
        raise UpdateError(f"整包文件不在：{archive}")
    staging = staging_root_for(install_dir, job_id)
    shutil.rmtree(staging, ignore_errors=True)
    # 收拾上次的残留必须**在解压之前**：`clean_leftovers` 按通配删
    # `_openminis-update-*`，放到解压之后会把刚解出来的这份也一起删掉（测试抓过）。
    clean_leftovers(install_dir, keep=staging)

    need = int(archive.stat().st_size * _FULL_PACKAGE_SPACE_FACTOR)
    try:
        free = shutil.disk_usage(install_dir.parent).free
    except OSError:  # 拿不到就不拦，交给解压失败去报
        free = need
    if free < need:
        raise UpdateError(
            f"安装目录所在盘只剩 {free / 1048576:.0f}MB，整包解压大约要 "
            f"{need / 1048576:.0f}MB —— 先腾点空间"
        )

    _safe_extract(archive, staging)
    staged = staging / PORTABLE_DIR_NAME
    if not (staged / exe_name).is_file():
        flat = (staging / exe_name).is_file()
        shutil.rmtree(staging, ignore_errors=True)
        if flat:
            raise UpdateError(
                "整包里没有顶层目录（OpenMinisDesktop/）—— 我们不会去猜哪一层是"
                "安装目录，所以这种包直接拒绝。请用 Compress-Archive 打出的便携包。"
            )
        raise UpdateError(f"整包里找不到 {exe_name} —— 这不是便携包")
    if not (staged / "payload" / "payload.json").is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError("整包里没有载荷清单（payload/payload.json）")
    return staged


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
    """落一份"人话摘要"（界面直接显示它）。

    ``phase`` 只在**正在切换**那一小段时间里出现（``committing``）：它同时是
    :func:`mark_boot_ok` 的开关，也免得重启之后界面以为还有活儿在跑。
    """
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
