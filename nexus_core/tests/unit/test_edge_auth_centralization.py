"""強制「所有對 edge 的 httpx 請求都帶驗證標頭」的 AST 不變式。

edge 模組＝原始碼提及 `TUNNEL_URL`、使用 `get_edge_client`，或為
`services/edge_cache_client.py`／`calibration/edge_history.py`。其中：

- `httpx.AsyncClient(...)` 必須帶 `**edge_client_kwargs()`（或同時有 headers= 與 event_hooks=）。
- `httpx.get/post/put/delete/request/Client(...)` 必須帶 `headers=`。
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

_CORE_ROOT = pathlib.Path(__file__).resolve().parents[2]

_EXCLUDED_PARTS = {
    "tests",
    "scripts",
    "build",
    "venv",
    ".venv",
    "__pycache__",
    "data",
}

_ALWAYS_EDGE_MODULES = {
    "services/edge_cache_client.py",
    "calibration/edge_history.py",
}

# 提及 TUNNEL_URL 但並非 edge 呼叫的檔案（不得帶 edge 標頭，以免把權杖洩漏給第三方）。
# sector_runner：Polymarket 用戶端，不是 edge 呼叫；絕對不可帶 edge 驗證標頭。
_NOT_EDGE_ALLOWLIST = {
    "market_analysis/analyst_runners/sector_runner.py",
}

_REQUIRED_EDGE_MODULES = {
    "services/market_data_service/_core.py",
    "services/edge_cache_client.py",
    "market_analysis/index_microstructure.py",
    "services/reddit_service.py",
    "services/fundamental_service.py",
    "services/calendar_service.py",
    "cogs/terminal/system.py",
    "services/memory_manager.py",
    "calibration/edge_history.py",
}

_SYNC_FUNCS = {"get", "post", "put", "delete", "request", "Client"}


def _iter_source_files() -> Iterator[tuple[str, pathlib.Path]]:
    for path in _CORE_ROOT.rglob("*.py"):
        rel_parts = path.relative_to(_CORE_ROOT).parts
        if any(part in _EXCLUDED_PARTS for part in rel_parts[:-1]):
            continue
        yield path.relative_to(_CORE_ROOT).as_posix(), path


def _is_edge_module(rel: str, source: str) -> bool:
    if rel in _ALWAYS_EDGE_MODULES:
        return True
    return "TUNNEL_URL" in source or "get_edge_client" in source


def _httpx_attr(node: ast.Call) -> str | None:
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "httpx"
    ):
        return func.attr
    return None


def _has_edge_star_kwargs(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg is None:
            val = kw.value
            if (
                isinstance(val, ast.Call)
                and isinstance(val.func, ast.Name)
                and val.func.id == "edge_client_kwargs"
            ):
                return True
    names = {kw.arg for kw in node.keywords}
    return {"headers", "event_hooks"} <= names


def _check_source(source: str, rel: str) -> tuple[list[str], int, int]:
    """回傳 (違規清單, 已檢查的 AsyncClient 數, 已檢查的同步 httpx.get 數)。"""
    offenders: list[str] = []
    async_n = 0
    get_n = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        attr = _httpx_attr(node)
        if attr == "AsyncClient":
            async_n += 1
            if not _has_edge_star_kwargs(node):
                offenders.append(
                    f"{rel}:{node.lineno} httpx.AsyncClient 缺 **edge_client_kwargs()"
                )
        elif attr in _SYNC_FUNCS:
            if attr == "get":
                get_n += 1
            if not any(kw.arg == "headers" for kw in node.keywords):
                offenders.append(f"{rel}:{node.lineno} httpx.{attr} 缺 headers=")
    return offenders, async_n, get_n


def _scan() -> tuple[set[str], list[str], int, int]:
    edge_modules: set[str] = set()
    offenders: list[str] = []
    async_total = 0
    get_total = 0
    for rel, path in _iter_source_files():
        if rel in _NOT_EDGE_ALLOWLIST:
            continue
        source = path.read_text(encoding="utf-8")
        if not _is_edge_module(rel, source):
            continue
        edge_modules.add(rel)
        found, async_n, get_n = _check_source(source, rel)
        offenders.extend(found)
        async_total += async_n
        get_total += get_n
    return edge_modules, offenders, async_total, get_total


def test_edge_calls_carry_auth_headers() -> None:
    _, offenders, _, _ = _scan()
    assert not offenders, "edge 請求缺驗證標頭：\n" + "\n".join(offenders)


def test_scan_actually_covers_edge_modules() -> None:
    """守衛：確保掃描不是空跑通過。"""
    edge_modules, _, async_total, get_total = _scan()
    missing = _REQUIRED_EDGE_MODULES - edge_modules
    assert not missing, f"未偵測到的 edge 模組：{sorted(missing)}"
    assert async_total >= 18, f"只檢查到 {async_total} 個 httpx.AsyncClient 呼叫"
    assert get_total >= 1, "未檢查到任何同步 httpx.get 呼叫"


def test_allowlisted_files_do_not_use_edge_auth() -> None:
    for rel in _NOT_EDGE_ALLOWLIST:
        source = (_CORE_ROOT / rel).read_text(encoding="utf-8")
        assert "edge_client_kwargs" not in source, rel
        assert "edge_auth_headers" not in source, rel


def test_checker_flags_offending_snippet() -> None:
    bad = (
        "import httpx\n"
        "async def f():\n"
        "    async with httpx.AsyncClient(timeout=1.0) as c:\n"
        "        pass\n"
        "def g():\n"
        "    httpx.get('x')\n"
    )
    offenders, async_n, get_n = _check_source(bad, "x.py")
    assert len(offenders) == 2
    assert (async_n, get_n) == (1, 1)

    good = (
        "import httpx\n"
        "async def f():\n"
        "    async with httpx.AsyncClient(timeout=1.0, **edge_client_kwargs()) as c:\n"
        "        pass\n"
        "def g():\n"
        "    httpx.get('x', headers=h)\n"
    )
    assert _check_source(good, "x.py")[0] == []
