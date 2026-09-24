"""強制「每日去重旗標必須登記於 kv_cache 清理白名單」的不變式。

存在理由（回歸防護）：`kv_cache` 沒有 TTL 欄位，一次性的每日去重旗標只能靠
03:00 ET 的 `purge_stale_kv_cache_dedup_keys()` 依 `_KV_CACHE_DEDUP_KEY_PREFIXES`
白名單前綴清除。新增去重旗標卻忘了加白名單，旗標就會永久堆積；既有的
test_background_perf_optimization.py 只驗證 purge 對兩個寫死前綴的行為，偵測不到這種漏加。

偵測規則（兩種寫入形態）：
1. `save_kv_cache(key, value)` 的 `value` 為常數 True / 1（去重旗標的簽名：值永遠不會被
   讀取消費）；
2. 任何呼叫的 `dedup_key=` 關鍵字引數（`services/notification_dispatcher.notify()` /
   `notify_many()` 會在入列後以此鍵寫入去重旗標）。
兩者的 `key` 為 f-string、條件式 f-string（`f"..." if cond else None`），或為函式內以
上述形式賦值的變數（同一名稱可能有多個分支賦值，全部納入）。純字串字面量的鍵（如
`macro_gex_is_fallback`）是永久快取，不在偵測範圍。
"""

from __future__ import annotations

import ast
import pathlib
from typing import Iterator, Optional

from database.cache import _KV_CACHE_DEDUP_KEY_PREFIXES

_CORE_ROOT = pathlib.Path(__file__).resolve().parents[2]

_SCAN_DIRS = (
    "database",
    "services",
    "cogs",
    "market_analysis",
    "ui",
    "risk_engine",
    "calibration",
)

_EXCLUDED_PARTS = {"build", "venv", "__pycache__", "migrations"}

# 已登記於白名單、但寫入點尚未實作的前綴。實作落地後必須從此處移除，
# test_pending_writers_are_removed_once_implemented 會強制這一點。
# （advisory_entry_ 由階段 A、advisory_exit_ 由階段 B 落地，目前無待實作項目。）
_PENDING_WRITERS: frozenset[str] = frozenset()


def _iter_source_files() -> Iterator[tuple[str, pathlib.Path]]:
    for d in _SCAN_DIRS:
        root = _CORE_ROOT / d
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part in _EXCLUDED_PARTS for part in path.parts):
                continue
            yield path.relative_to(_CORE_ROOT).as_posix(), path


def _leading_literal(node: ast.expr) -> Optional[str]:
    if (
        isinstance(node, ast.JoinedStr)
        and node.values
        and isinstance(node.values[0], ast.Constant)
        and isinstance(node.values[0].value, str)
    ):
        return node.values[0].value
    return None


def _is_flag_value(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and not isinstance(node.value, str)
        and (node.value is True or node.value == 1)
    )


def _call_name(func: ast.expr) -> Optional[str]:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _key_candidates(node: ast.expr) -> list[ast.expr]:
    """條件式 `a if cond else b` 展開為兩個候選。"""
    if isinstance(node, ast.IfExp):
        return _key_candidates(node.body) + _key_candidates(node.orelse)
    return [node]


def _collect_dedup_writers() -> list[tuple[str, int, str]]:
    """回傳 (相對路徑, 行號, 前綴) — 每個去重旗標寫入點的鍵前綴。"""
    found: list[tuple[str, int, str]] = []
    for rel, path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigns: dict[str, list[ast.expr]] = {}
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            assigns.setdefault(t.id, []).append(n.value)
            for n in ast.walk(fn):
                if not isinstance(n, ast.Call):
                    continue
                keys: list[ast.expr] = []
                if (
                    _call_name(n.func) == "save_kv_cache"
                    and len(n.args) >= 2
                    and _is_flag_value(n.args[1])
                ):
                    keys.append(n.args[0])
                keys.extend(kw.value for kw in n.keywords if kw.arg == "dedup_key")
                for key in keys:
                    for cand in _key_candidates(key):
                        exprs = (
                            assigns.get(cand.id, [])
                            if isinstance(cand, ast.Name)
                            else [cand]
                        )
                        for expr in exprs:
                            for leaf in _key_candidates(expr):
                                prefix = _leading_literal(leaf)
                                if prefix:
                                    found.append((rel, n.lineno, prefix))
    # 巢狀函式會被外層 ast.walk 重複走訪，去重後回傳
    return sorted(set(found))


def _matches(prefix: str, whitelist_entry: str) -> bool:
    return prefix.startswith(whitelist_entry)


def test_scan_actually_covers_dedup_writers() -> None:
    """守衛：掃描規則失效（路徑寫錯、AST 形態改變）時不可空跑通過。"""
    scanned = [rel for rel, _ in _iter_source_files()]
    assert len(scanned) > 100, f"掃描到的檔案過少 ({len(scanned)})，路徑設定可能有誤"
    writers = _collect_dedup_writers()
    assert (
        len(writers) >= 7
    ), f"偵測到的去重旗標寫入點過少 ({len(writers)})，掃描規則可能失效"


def test_every_dedup_flag_prefix_is_whitelisted() -> None:
    """正向：所有去重旗標的鍵前綴都必須在清理白名單內。"""
    missing = [
        f"{rel}:{lineno} → {prefix!r}"
        for rel, lineno, prefix in _collect_dedup_writers()
        if not any(_matches(prefix, w) for w in _KV_CACHE_DEDUP_KEY_PREFIXES)
    ]
    assert not missing, (
        "以下去重旗標的鍵前綴未登記於 database/cache.py::_KV_CACHE_DEDUP_KEY_PREFIXES，"
        "旗標將永久堆積於 kv_cache：\n  " + "\n  ".join(missing)
    )


def test_every_whitelist_prefix_has_a_writer() -> None:
    """反向：白名單內不得殘留沒有任何寫入點的死前綴（尚未實作者列於 _PENDING_WRITERS）。"""
    prefixes = {p for _, _, p in _collect_dedup_writers()}
    dead = [
        w
        for w in _KV_CACHE_DEDUP_KEY_PREFIXES
        if w not in _PENDING_WRITERS and not any(_matches(p, w) for p in prefixes)
    ]
    assert not dead, f"白名單前綴沒有任何寫入點（死前綴）: {dead}"


def test_pending_writers_are_whitelisted() -> None:
    """豁免清單只能包含確實在白名單內的前綴。"""
    assert _PENDING_WRITERS <= set(_KV_CACHE_DEDUP_KEY_PREFIXES)


def test_pending_writers_are_removed_once_implemented() -> None:
    """收斂護欄：寫入點一旦實作，就必須把該前綴從 _PENDING_WRITERS 移除，
    避免豁免清單永久殘留、讓反向檢查對該前綴形同虛設。"""
    prefixes = {p for _, _, p in _collect_dedup_writers()}
    implemented = [w for w in _PENDING_WRITERS if any(_matches(p, w) for p in prefixes)]
    assert not implemented, f"{implemented} 已有寫入點，請從 _PENDING_WRITERS 移除"
