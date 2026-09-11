"""強制「單一寫入者」不變式：除了 database/connection.py 與 migration 執行器之外，
任何模組都不得自開 SQLite 連線進行寫入。

存在理由（回歸防護）：`DatabaseWriteQueue` 是為了讓全程序只有一條寫入連線而建的，
但這個不變式一度在 ~35 個函式上被破壞——它們各自 `sqlite3.connect(config.DB_NAME)`
（預設 5 秒 busy timeout、無任何 PRAGMA）然後 `conn.commit()`，與佇列的寫入連線互搶
WAL 寫入鎖，正是 production 上 `database is locked` 的來源。

本測試比照 tests/unit/test_output_centralization.py 的 AST 掃描作法，讓這條規則由
CI 強制，而不是靠人工紀律維持。
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

# tests/unit/<this file> → parents[2] 即 nexus_core 專案根目錄
_CORE_ROOT = pathlib.Path(__file__).resolve().parents[2]

_SCAN_DIRS = ("database", "services", "cogs", "market_analysis", "ui", "risk_engine")

# 唯一允許自行建立連線並提交的地方：
#   - database/connection.py：連線工廠與寫入佇列本身
#   - database/core.py：migration 執行器，跑在佇列啟動之前
_ALLOWED = {
    "database/connection.py",
    "database/core.py",
}

_EXCLUDED_PARTS = {"build", "venv", "__pycache__", "migrations"}


def _iter_source_files() -> Any:
    for d in _SCAN_DIRS:
        root = _CORE_ROOT / d
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            rel = path.relative_to(_CORE_ROOT).as_posix()
            if any(part in _EXCLUDED_PARTS for part in path.parts):
                continue
            if rel in _ALLOWED:
                continue
            yield rel, path


def test_scan_actually_covers_source_files() -> None:
    """守衛：確保上述掃描不是因為路徑寫錯而空跑通過。"""
    scanned = [rel for rel, _ in _iter_source_files()]
    assert len(scanned) > 100, f"掃描到的檔案過少 ({len(scanned)})，路徑設定可能有誤"
    assert "database/watchlist.py" in scanned
    assert "services/asset_manager.py" in scanned


def _calls(tree: ast.AST) -> Any:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                yield node, func.attr
            elif isinstance(func, ast.Name):
                yield node, func.id


def test_no_module_opens_its_own_sqlite_connection() -> None:
    """除白名單外，不得出現 `sqlite3.connect(...)`。

    讀取請用 `database.connection.get_read_connection()` / `connect_db()`（統一套用
    busy_timeout 與 synchronous PRAGMA），寫入請用 `execute_write*` 系列入口。
    """
    offenders: list[str] = []
    for rel, path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "connect"
                and isinstance(func.value, ast.Name)
                and func.value.id == "sqlite3"
            ):
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "以下位置自行建立 SQLite 連線，繞過了 database/connection.py 的統一連線工廠：\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n讀取請改用 get_read_connection()，寫入請改用 execute_write* 入口。"
    )


def test_no_module_commits_outside_the_write_queue() -> None:
    """除白名單外，不得出現 `conn.commit()`。

    任何 commit 都代表該處持有自己的寫入交易，也就代表它會與寫入佇列的連線互搶鎖。
    """
    offenders: list[str] = []
    for rel, path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node, name in _calls(tree):
            if name == "commit":
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "以下位置在寫入佇列之外自行 commit，破壞了「程序內單一寫入連線」的不變式：\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n請改用 execute_write / execute_write_async / execute_write_many* 入口。"
    )


def test_sqlite_connections_are_never_used_as_bare_context_managers() -> None:
    """禁止 `with sqlite3.connect(...) as conn:` / `with self._get_conn() as conn:`。

    sqlite3 的 context manager 只會 commit/rollback，**不會關閉連線**——連線會一直
    活到被 GC 回收為止。請改用 `try/finally: conn.close()`。
    """
    offenders: list[str] = []
    suspicious_names = {"connect", "_get_conn", "get_read_connection", "connect_db"}
    for rel, path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            for item in node.items:
                expr = item.context_expr
                if isinstance(expr, ast.Call):
                    func = expr.func
                    name = (
                        func.attr
                        if isinstance(func, ast.Attribute)
                        else (func.id if isinstance(func, ast.Name) else None)
                    )
                    if name in suspicious_names:
                        offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "以下位置把 SQLite 連線當成 context manager 使用，連線不會被關閉：\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n請改用 try/finally 明確 close()。"
    )
