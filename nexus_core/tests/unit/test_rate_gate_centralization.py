"""強制「對外限流 API 一律經過 services/rate_gate.py」的 AST 不變式。

存在理由：限流缺口（各自為政的 AsyncLimiter、沒有冷卻、LLM 無併發上限）曾是 429 與
背景任務互相擠壓的來源。本測試比照 test_db_write_centralization.py 的 AST 掃描作法，
讓這條規則由 CI 強制：

- `AsyncLimiter(` 僅允許出現在 allowlist（PR-B 清空後不得再出現）。
- `finnhub.Client(` 僅允許出現在 `services/market_data_service/_core.py`。
- `.chat.completions.create/.parse` 僅允許出現在 allowlist（收斂至 `llm_service.py`）。
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

# tests/unit/<this file> → parents[2] 即 nexus_core 專案根目錄
_CORE_ROOT = pathlib.Path(__file__).resolve().parents[2]

_EXCLUDED_PARTS = {
    "tests",
    "calibration",
    "scripts",
    "build",
    "venv",
    ".venv",
    "__pycache__",
    "data",
}

# 仍保有自己 AsyncLimiter 的檔案（遷移完成後應為空）
_ASYNC_LIMITER_ALLOWLIST = {
    "services/sec_edgar_client.py",
}

_FINNHUB_CLIENT_ALLOWLIST = {
    "services/market_data_service/_core.py",
}

# 直接呼叫 OpenAI chat completions 的檔案（遷移完成後應只剩 llm_service.py）
_LLM_CALL_ALLOWLIST = {
    "services/llm_service.py",
    "market_analysis/attribution.py",
    "market_analysis/dynamic_rollover/fundamental_thesis.py",
    "services/earnings_surprise_service.py",
    "services/hedge_monitor_service.py",
}


def _iter_source_files() -> Iterator[tuple[str, pathlib.Path]]:
    for path in _CORE_ROOT.rglob("*.py"):
        rel_parts = path.relative_to(_CORE_ROOT).parts
        if any(part in _EXCLUDED_PARTS for part in rel_parts[:-1]):
            continue
        yield path.relative_to(_CORE_ROOT).as_posix(), path


def _calls(tree: ast.AST) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def test_scan_actually_covers_source_files() -> None:
    """守衛：確保掃描不是因為路徑寫錯而空跑通過。"""
    scanned = [rel for rel, _ in _iter_source_files()]
    assert len(scanned) > 100, f"掃描到的檔案過少 ({len(scanned)})，路徑設定可能有誤"
    assert "services/rate_gate.py" in scanned
    assert "services/market_data_service/_core.py" in scanned
    assert not any(rel.startswith("tests/") for rel in scanned)


def test_async_limiter_only_in_allowlist() -> None:
    offenders: list[str] = []
    for rel, path in _iter_source_files():
        if rel in _ASYNC_LIMITER_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _calls(tree):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else ""
            )
            if name == "AsyncLimiter":
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "以下位置自建 AsyncLimiter，繞過了 services/rate_gate.py 的統一限流閘門：\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n請改用 get_gate(<name>).slot()（參數集中於 rate_gate.POLICIES）。"
    )


def test_allowlisted_limiter_files_still_exist() -> None:
    """allowlist 不得殘留已遷移完畢的檔案（遷移後請從 allowlist 移除）。"""
    for rel in _ASYNC_LIMITER_ALLOWLIST:
        path = _CORE_ROOT / rel
        assert path.exists(), f"{rel} 已不存在，請從 allowlist 移除"
        assert "AsyncLimiter(" in path.read_text(
            encoding="utf-8"
        ), f"{rel} 已不再使用 AsyncLimiter，請從 allowlist 移除"


def test_finnhub_client_only_constructed_in_core() -> None:
    offenders: list[str] = []
    for rel, path in _iter_source_files():
        if rel in _FINNHUB_CLIENT_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _calls(tree):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "Client"
                and isinstance(func.value, ast.Name)
                and func.value.id == "finnhub"
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "以下位置自行建立 finnhub.Client，繞過了 _core._execute_api_call 的限流閘門：\n"
        + "\n".join(f"  - {o}" for o in offenders)
    )


def _is_chat_completion_call(node: ast.Call) -> bool:
    """`<x>.chat.completions.create(...)` 或 `.parse(...)`。"""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in {"create", "parse"}):
        return False
    comp = func.value
    if not (isinstance(comp, ast.Attribute) and comp.attr == "completions"):
        return False
    chat = comp.value
    return isinstance(chat, ast.Attribute) and chat.attr == "chat"


def test_llm_chat_completions_only_in_allowlist() -> None:
    offenders: list[str] = []
    for rel, path in _iter_source_files():
        if rel in _LLM_CALL_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _calls(tree):
            if _is_chat_completion_call(node):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "以下位置直接呼叫 chat.completions，繞過了 LLM 閘門：\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n請改用 services.llm_service 的 llm_parse / llm_create。"
    )


def test_rate_gate_is_a_leaf_module() -> None:
    """rate_gate 不得 import 任何 service／業務模組（避免循環相依）。"""
    tree = ast.parse(
        (_CORE_ROOT / "services" / "rate_gate.py").read_text(encoding="utf-8")
    )
    forbidden = ("services", "market_analysis", "cogs", "database", "config")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in forbidden, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden, alias.name
