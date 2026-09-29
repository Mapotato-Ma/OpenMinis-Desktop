"""从 cc-switch 的库里读供应商配置，供桌面界面「导入供应商」用。

只读，只读，只读：绝不往用户的 cc-switch 目录写任何东西（见 .scratch/plan-ui-scale-ccswitch.md B 段）。

cc-switch（github.com/farion1231/cc-switch）的存储（源码依据 src-tauri/src/config.rs、
database/dao/providers.rs、database/migration.rs）：

    ~/.cc-switch/cc-switch.db    SQLite
      providers(id, app_type, name, settings_config JSON, website_url, category,
                created_at, sort_index, notes, icon, icon_color, meta JSON,
                is_current, in_failover_queue)

每家真正的配置在 settings_config 里，形状随 app_type 变（claude 系是 env.ANTHROPIC_*，
codex 是 auth.OPENAI_API_KEY + 一段 config TOML，gemini 是 env.GEMINI_API_KEY 等）。
这些键名在实现时没法在本地逐一对完，所以提取器写成「已知键优先 + 唯一命中兜底」，
抗 schema 漂移：命中不唯一就留空让人手填，绝不猜。
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
from typing import Any, Iterable

# cc-switch 的 app_type → OpenMinis 的供应商类型
# （OpenMinis 侧的类型见 src/openminis/settings/catalog.py 的 PROVIDER_TYPES）
APP_TYPE_MAP: dict[str, str] = {
    "claude": "anthropic",
    "claude-code": "anthropic",
    "codex": "openAI",
    "gemini": "gemini",
    "grok": "xAI",
    "openrouter": "openRouter",
    "kimi": "kimiCode",
}

STORE_DIRNAME = ".cc-switch"
STORE_FILES = ("cc-switch.db", "config.json")


def candidate_dirs() -> list[pathlib.Path]:
    """cc-switch 会去查的目录，按可信度排序（Windows 上是 %USERPROFILE%\\.cc-switch）。"""
    home = pathlib.Path(os.path.expanduser("~"))
    out = [home / STORE_DIRNAME]
    alt = os.environ.get("CC_SWITCH_HOME") or os.environ.get("CC_SWITCH_DIR")
    if alt:
        out.insert(0, pathlib.Path(alt))
    return out


def find_store(base: pathlib.Path | None = None) -> tuple[pathlib.Path | None, str | None, list[str]]:
    """返回 (路径, 'sqlite'|'json', 查过的路径)。找到第一个存在的就用它。

    base 只给测试用（指向一个假的家目录），生产代码永远走 candidate_dirs()。
    """
    searched: list[str] = []
    dirs = [base / STORE_DIRNAME] if base is not None else candidate_dirs()
    for d in dirs:
        for name in STORE_FILES:
            p = d / name
            searched.append(str(p))
            if p.is_file():
                return p, ("sqlite" if name.endswith(".db") else "json"), searched
    return None, None, searched


def _readonly_uri(path: pathlib.Path) -> str:
    """SQLite 的只读 URI。as_uri() 负责 Windows 盘符/反斜杠/非 ASCII 的转义。"""
    return path.as_uri() + "?mode=ro&immutable=0"


def _connect(path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(_readonly_uri(path), uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=2000")
    con.execute("PRAGMA query_only=ON")
    return con

# ── 从 settings_config 里抠 URL / key / model ────────────────────────────────
_URL_HINTS = ("base_url", "baseurl", "base-url", "endpoint", "api_base")
_KEY_HINTS = ("api_key", "apikey", "api-key", "auth_token", "auth-token", "access_token")
_MODEL_HINTS = ("model",)
# cc-switch 里 codex 的 OAuth 凭据（tokens/refresh_token）不是 API key，不能当 key 导入
_NOT_A_KEY = ("tokens", "refresh_token", "id_token", "account_id")

# 已知键优先：命中就直接用，命中不了再退回「唯一命中」兜底
_KNOWN: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "anthropic": (("anthropic_base_url",), ("anthropic_auth_token", "anthropic_api_key")),
    "openai": (("openai_base_url", "base_url"), ("openai_api_key", "api_key")),
    "gemini": (("google_gemini_base_url", "gemini_base_url"), ("gemini_api_key", "google_api_key")),
}


def _leaves(obj: Any, prefix: str = "") -> list[tuple[str, str]]:
    """把 JSON 摊平成 (小写键路径, 值)，只收标量。"""
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_leaves(v, f"{prefix}[{i}]"))
    elif isinstance(obj, (str, int, float)) and not isinstance(obj, bool):
        out.append((prefix.lower(), str(obj).strip()))
    return out


def _pick(leaves: Iterable[tuple[str, str]], hints: tuple[str, ...],
          exclude: tuple[str, ...] = ()) -> tuple[str, str | None]:
    """唯一命中才返回。不唯一 → 空值 + 说明（让用户手填，绝不猜）。"""
    hits = {v for k, v in leaves
            if any(h in k for h in hints) and v and not any(x in k for x in exclude)}
    if len(hits) == 1:
        return hits.pop(), None
    if len(hits) > 1:
        return "", "有多处候选，请手填"
    return "", None


def mask_key(key: str) -> str:
    """掩码。≤8 字符一律全遮 —— 短 key 露头露尾约等于全泄。"""
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 8:
        return "\u2022" * 8
    return f"{k[:3]}\u2026{k[-4:]}"

def _extract(settings_config: Any, app_type: str) -> dict:
    """抠出 base_url / api_key / model。返回 {baseUrl, apiKey, model, notes}。"""
    notes: list[str] = []
    if isinstance(settings_config, str):
        try:
            settings_config = json.loads(settings_config or "{}")
        except json.JSONDecodeError:
            return {"baseUrl": "", "apiKey": "", "model": "", "notes": ["settings_config 不是合法 JSON"]}
    if not isinstance(settings_config, dict):
        return {"baseUrl": "", "apiKey": "", "model": "", "notes": ["settings_config 结构不认识"]}

    leaves = _leaves(settings_config)
    url = key = model = ""

    # ① 已知键优先
    known = _KNOWN.get(app_type)
    if known:
        for k in known[0]:
            url = next((v for path, v in leaves if path == k), "")
            if url:
                break
        for k in known[1]:
            key = next((v for path, v in leaves if path == k), "")
            if key:
                break

    # ② codex 的 toml 文本里也有 base_url / model
    for path, value in leaves:
        if path.endswith("config") and value and ("=" in value):
            for line in value.splitlines():
                line = line.strip()
                for name, slot in (("base_url", "url"), ("model", "model")):
                    if line.replace(" ", "").startswith(f'{name}='):
                        got = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if slot == "url" and not url:
                            url = got
                        elif slot == "model" and not model:
                            model = got

    # ③ 唯一命中兜底
    if not url:
        url, why = _pick(leaves, _URL_HINTS)
        if why:
            notes.append(f"接口地址{why}")
    if not key:
        key, why = _pick(leaves, _KEY_HINTS, exclude=_NOT_A_KEY)
        if why:
            notes.append(f"密钥{why}")
        elif not key and any(x in path for path, _ in leaves for x in _NOT_A_KEY):
            notes.append("只有 OAuth 凭据（tokens），不支持导入，请手填 API Key")
    if not model:
        # 实测（真实 cc-switch 库）：claude 一行里同时有 ANTHROPIC_MODEL 和一堆
        # ANTHROPIC_DEFAULT_*_MODEL，值往往不同 → 「唯一命中」会被判成歧义而留空。
        # 所以先按已知优先级精确取，再退到唯一命中。
        for k in ("env.anthropic_model", "model", "env.openai_model",
                  "modelcatalog.models[0].model", "env.gemini_model"):
            model = next((v for path, v in leaves if path == k), "")
            if model:
                break
    if not model:
        model, _ = _pick(leaves, _MODEL_HINTS)

    return {"baseUrl": url.strip(), "apiKey": key.strip(), "model": model.strip(), "notes": notes}
class ReadOnlyError(RuntimeError):
    """只读读取失败。**绝不允许**为了「能读」而改成可写打开 ——
    那会往用户的 cc-switch 目录写文件，属于越界。"""


def _rows_from_json(data: Any) -> list[dict]:
    """老版本 cc-switch 只有 config.json 时的兜底（尽力而为，形状不保证）。"""
    out: list[dict] = []
    if not isinstance(data, dict):
        return out
    for app, group in (data.get("providers") or {}).items():
        items = group.get("providers") if isinstance(group, dict) else None
        if isinstance(items, dict):
            for pid, item in items.items():
                if isinstance(item, dict):
                    out.append({
                        "id": pid, "app_type": item.get("app_type") or app,
                        "name": item.get("name") or pid,
                        "settings_config": item.get("settings_config") or item,
                        "is_current": False,
                    })
    return out
def _read_rows(store: pathlib.Path, kind: str) -> list[dict]:
    """读 providers 表。只读打开；打不开就报错，不回退成可写。"""
    if kind == "json":
        raw = store.read_text(encoding="utf-8", errors="replace") or "{}"
        try:
            return _rows_from_json(json.loads(raw))
        except json.JSONDecodeError as e:
            raise ReadOnlyError(f"cc-switch 的 config.json 不是合法 JSON：{e}") from e
    try:
        con = _connect(store)
    except sqlite3.Error as e:
        raise ReadOnlyError(
            "打不开 cc-switch 的库（它可能正在运行、正持着 WAL）。"
            "先退出 cc-switch 再试。"
        ) from e
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM providers")]
    except sqlite3.Error as e:
        raise ReadOnlyError("这个库里没有 providers 表（cc-switch 版本不支持？）") from e
    finally:
        con.close()
    return rows
def _candidate(row: dict, *, with_key: bool = False) -> dict:
    app_type = str(row.get("app_type") or "").strip().lower()
    info = _extract(row.get("settings_config"), app_type)
    notes = list(info["notes"])
    suggested = APP_TYPE_MAP.get(app_type, "")
    if not suggested:
        notes.append(f"app_type「{app_type or '空'}」没有对应的供应商类型，请手选")
    item = {
        "id": row.get("id"),
        "appType": app_type,
        "name": str(row.get("name") or "").strip(),
        "suggestedType": suggested,
        "baseUrl": info["baseUrl"],
        "model": info["model"],
        "mask": mask_key(info["apiKey"]),
        "hasKey": bool(info["apiKey"]),
        "isCurrent": bool(row.get("is_current")),
        "notes": notes,
    }
    if with_key:
        item["apiKey"] = info["apiKey"]
    return item
def scan(base: pathlib.Path | None = None) -> dict:
    """扫一遍候选路径，给出可勾选清单。**响应里不含明文密钥。**"""
    store, kind, searched = find_store(base)
    if store is None:
        return {
            "available": False,
            "searched": searched,
            "items": [],
            "reason": "没找到 cc-switch 的配置（在 cc-switch 里加过供应商吗？）",
        }
    try:
        rows = _read_rows(store, kind)
    except ReadOnlyError as e:
        return {"available": False, "store": str(store), "searched": searched,
                "items": [], "reason": str(e)}
    return {"available": True, "store": str(store), "searched": searched,
            "items": [_candidate(r) for r in rows]}


def entries(ids: list[str], base: pathlib.Path | None = None) -> dict:
    """按 id 取明文凭据。只在用户显式点「导入」时调用。

    注意：实测真实库里 providers.id 是 **TEXT**（UUID 或 'gemini-official' 这类串），
    不是自增整数 —— 所以全程按字符串处理，别做 int() 转换。
    """
    wanted = [str(i) for i in ids]
    store, kind, _ = find_store(base)
    if store is None:
        return {"items": [], "missing": sorted(wanted),
                "reason": "没找到 cc-switch 的配置"}
    try:
        rows = _read_rows(store, kind)
    except ReadOnlyError as e:
        return {"items": [], "missing": sorted(wanted), "reason": str(e)}
    by_id = {str(r["id"]): r for r in rows if r.get("id") is not None}
    return {
        "items": [_candidate(by_id[i], with_key=True) for i in wanted if i in by_id],
        "missing": [i for i in wanted if i not in by_id],
    }
