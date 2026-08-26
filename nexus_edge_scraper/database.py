"""
database.py

nexus_edge_scraper 的本地 SQLite 快取層。此服務原本純粹是請求驅動、無任何
持久化狀態；本模組讓 scheduler.py 能把定期輪詢到的 GEX / Option Chain
快照寫入本地磁碟，並讓 local_api.py 的新讀取端點能毫秒級回應
nexus_core 的查詢，不必每次都重新即時抓取。

刻意維持最單純的 sqlite3 + CREATE TABLE IF NOT EXISTS 寫法，不套用
nexus_core 那套 migration engine —— 這是獨立服務，維持既有的輕量單檔風格。
"""

from typing import Any, Optional
import json
import os
import sqlite3

DB_PATH = os.environ.get(
    "EDGE_CACHE_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "edge_cache.db"),
)


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracked_symbols (
                symbol TEXT PRIMARY KEY,
                is_priority INTEGER NOT NULL DEFAULT 0,
                last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS gex_snapshot (
                symbol TEXT PRIMARY KEY,
                spot REAL,
                net_gex REAL,
                call_wall REAL,
                put_wall REAL,
                gex_profile_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS option_chain_snapshot (
                symbol TEXT NOT NULL,
                expiry TEXT NOT NULL,
                calls_json TEXT,
                puts_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (symbol, expiry)
            );
            """
        )
        # 舊版 DB 檔案（is_priority 欄位加入前建立的）不會被上面的
        # CREATE TABLE IF NOT EXISTS 補上新欄位，需要額外用 ALTER TABLE 補齊，
        # 沿用本檔案一貫不套用 migration engine 的輕量風格。
        existing_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(tracked_symbols)")
        }
        if "is_priority" not in existing_cols:
            conn.execute(
                "ALTER TABLE tracked_symbols ADD COLUMN is_priority INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()
    finally:
        conn.close()


def upsert_tracked_symbols(
    symbols: list[str], priority_symbols: Optional[list[str]] = None
) -> None:
    """由 nexus_core 於每次心跳前 best-effort 同步過來的自選標的清單，以及
    應優先每輪必抓、不受批次輪替影響的持倉標的（priority_symbols，可能包含
    未列在一般自選清單中的標的，例如未加入 watchlist 的持倉）。

    每次呼叫都會用傳入的 priority_symbols 覆寫既有標記，讓已出清的持倉
    正確降級回一般批次輪替，不會殘留過期的優先旗標。"""
    priority_clean = {s.upper() for s in (priority_symbols or []) if s}
    clean = {s.upper() for s in symbols if s} | priority_clean
    if not clean:
        return
    conn = _get_connection()
    try:
        conn.executemany(
            """
            INSERT INTO tracked_symbols (symbol, is_priority, last_synced_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
                is_priority = excluded.is_priority,
                last_synced_at = CURRENT_TIMESTAMP
            """,
            [(s, 1 if s in priority_clean else 0) for s in clean],
        )
        conn.commit()
    finally:
        conn.close()


def get_tracked_symbols() -> list[str]:
    conn = _get_connection()
    try:
        cursor = conn.execute("SELECT symbol FROM tracked_symbols ORDER BY symbol")
        return [row["symbol"] for row in cursor.fetchall()]
    finally:
        conn.close()


def get_priority_symbols() -> list[str]:
    """取得每輪必抓、不受批次輪替影響的持倉標的清單。"""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "SELECT symbol FROM tracked_symbols WHERE is_priority = 1 ORDER BY symbol"
        )
        return [row["symbol"] for row in cursor.fetchall()]
    finally:
        conn.close()


def get_non_priority_symbols() -> list[str]:
    """取得走一般批次輪替的標的清單（與 get_priority_symbols() 互斥分割）。"""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "SELECT symbol FROM tracked_symbols WHERE is_priority = 0 ORDER BY symbol"
        )
        return [row["symbol"] for row in cursor.fetchall()]
    finally:
        conn.close()


def prune_stale_symbols(older_than_hours: int = 48) -> int:
    """清除超過 `older_than_hours` 未被 nexus_core 同步過的追蹤標的，
    避免排程的輪詢清單無限成長。"""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM tracked_symbols WHERE last_synced_at < datetime('now', ?)",
            (f"-{older_than_hours} hours",),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def save_gex_snapshot(
    symbol: str,
    spot: float,
    net_gex: float,
    call_wall: float,
    put_wall: float,
    gex_profile: dict[str, float],
) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO gex_snapshot (symbol, spot, net_gex, call_wall, put_wall, gex_profile_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
                spot = excluded.spot,
                net_gex = excluded.net_gex,
                call_wall = excluded.call_wall,
                put_wall = excluded.put_wall,
                gex_profile_json = excluded.gex_profile_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                symbol.upper(),
                spot,
                net_gex,
                call_wall,
                put_wall,
                json.dumps(gex_profile),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_gex_snapshot(symbol: str) -> Optional[dict[str, Any]]:
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM gex_snapshot WHERE symbol = ?", (symbol.upper(),)
        )
        row = cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        data["gex_profile"] = json.loads(data.pop("gex_profile_json") or "{}")
        return data
    finally:
        conn.close()


def save_option_chain_snapshot(
    symbol: str,
    expiry: str,
    calls: list[dict[str, Any]],
    puts: list[dict[str, Any]],
) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO option_chain_snapshot (symbol, expiry, calls_json, puts_json, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol, expiry) DO UPDATE SET
                calls_json = excluded.calls_json,
                puts_json = excluded.puts_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (symbol.upper(), expiry, json.dumps(calls), json.dumps(puts)),
        )
        conn.commit()
    finally:
        conn.close()


def get_option_chain_snapshot(
    symbol: str, expiry: Optional[str] = None
) -> Optional[dict[str, Any]]:
    conn = _get_connection()
    try:
        if expiry:
            cursor = conn.execute(
                "SELECT * FROM option_chain_snapshot WHERE symbol = ? AND expiry = ?",
                (symbol.upper(), expiry),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM option_chain_snapshot WHERE symbol = ? ORDER BY updated_at DESC LIMIT 1",
                (symbol.upper(),),
            )
        row = cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        data["calls"] = json.loads(data.pop("calls_json") or "[]")
        data["puts"] = json.loads(data.pop("puts_json") or "[]")
        return data
    finally:
        conn.close()
