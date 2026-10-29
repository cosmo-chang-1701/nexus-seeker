"""wall_break.py — GEX 牆突破／跌破的錨點與事件存取層。

edge 約每 30 分鐘依當下現價重算 CallWall/PutWall，被突破的牆必須是穩定參照，故以
「前一交易日收盤牆」為錨點：管線每輪對 (symbol, 今日) upsert 當下看到的牆，當日最後
一輪即留下收盤牆；隔日以 `trade_date < today` 的最新一列作為錨點。

事件以 (symbol, trade_date, direction, trigger_kind) 唯一；次一交易日首輪掃描補寫
T+1 結算欄位，事件表同時是後續校準用的前向收集紀錄 (保留 180 天)。

所有寫入一律經 `database/connection.py` 的寫入佇列 (單一寫入者不變式)。資料表定義見
migrations/v097_add_wall_break_tables.py。
"""

import logging
import sqlite3
from typing import Any, Mapping, Optional

from database.connection import (
    execute_write_async,
    execute_write_many_async,
    execute_write_rowcount_async,
    get_read_connection,
)

logger = logging.getLogger(__name__)

# 可寫入欄位：不含 id / t1_* / created_at (由 DB 預設或 T+1 結算寫入)。
WALL_BREAK_EVENT_COLUMNS: tuple[str, ...] = (
    "symbol",
    "trade_date",
    "direction",
    "trigger_kind",
    "bar_time",
    "anchor_trade_date",
    "old_wall",
    "close",
    "verdict",
    "label",
    "gates_json",
    "old_wall_oi",
    "oi_expiry",
    "gex_expiry",
    "old_wall_gex",
)

_INSERT_EVENT_SQL = (
    f"INSERT OR IGNORE INTO wall_break_events ({', '.join(WALL_BREAK_EVENT_COLUMNS)}) "  # nosemgrep
    f"VALUES ({', '.join('?' for _ in WALL_BREAK_EVENT_COLUMNS)})"
)


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def _wall_or_none(value: Optional[float]) -> Optional[float]:
    """非正數或無法解析的牆價視為缺值。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


async def upsert_wall_anchor(
    symbol: str,
    trade_date: str,
    call_wall: Optional[float],
    put_wall: Optional[float],
    gex_expiry: Optional[str],
) -> bool:
    """寫入／覆蓋 (symbol, trade_date) 的牆。失敗回傳 False，不拋例外。"""
    try:
        await execute_write_async(
            """
            INSERT INTO wall_anchor (symbol, trade_date, call_wall, put_wall, gex_expiry)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol, trade_date) DO UPDATE SET
                call_wall = excluded.call_wall,
                put_wall = excluded.put_wall,
                gex_expiry = excluded.gex_expiry,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                symbol.upper(),
                trade_date,
                _wall_or_none(call_wall),
                _wall_or_none(put_wall),
                gex_expiry,
            ),
        )
        return True
    except Exception as e:
        logger.error(f"[{symbol}] 牆錨點寫入失敗: {e}")
        return False


def get_latest_wall_anchor_before(
    symbol: str, trade_date: str
) -> Optional[dict[str, Any]]:
    """取 trade_date 之前 (不含) 最近一個交易日的牆。查無或失敗回傳 None。"""
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = _dict_factory
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT symbol, trade_date, call_wall, put_wall, gex_expiry
            FROM wall_anchor
            WHERE symbol = ? AND trade_date < ?
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (symbol.upper(), trade_date),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"[{symbol}] 牆錨點讀取失敗: {e}")
        return None
    finally:
        if conn:
            conn.close()


async def insert_wall_break_event(event: Mapping[str, Any]) -> bool:
    """INSERT OR IGNORE 一筆事件。僅在新增成功時回傳 True；不拋例外。"""
    try:
        values = [event.get(col) for col in WALL_BREAK_EVENT_COLUMNS]
        values[0] = str(event.get("symbol") or "").upper()
        n = await execute_write_rowcount_async(_INSERT_EVENT_SQL, tuple(values))
        return n > 0
    except Exception as e:
        logger.error(f"[{event.get('symbol')}] 牆突破事件寫入失敗: {e}")
        return False


def get_wall_break_event(
    symbol: str, trade_date: str, direction: str, trigger_kind: str
) -> Optional[dict[str, Any]]:
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = _dict_factory
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM wall_break_events
            WHERE symbol = ? AND trade_date = ? AND direction = ? AND trigger_kind = ?
            """,
            (symbol.upper(), trade_date, direction, trigger_kind),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"[{symbol}] 牆突破事件讀取失敗: {e}")
        return None
    finally:
        if conn:
            conn.close()


def get_pending_t1_events(symbol: str, before_trade_date: str) -> list[dict[str, Any]]:
    """尚未做 T+1 結算的收盤突破事件 (僅 CLOSE_BEYOND、且事件日早於 before_trade_date)。"""
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = _dict_factory
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM wall_break_events
            WHERE symbol = ? AND trigger_kind = 'CLOSE_BEYOND'
              AND t1_verdict IS NULL AND trade_date < ?
            ORDER BY trade_date, id
            """,
            (symbol.upper(), before_trade_date),
        )
        return list(cursor.fetchall())
    except Exception as e:
        logger.error(f"[{symbol}] 待結算牆突破事件讀取失敗: {e}")
        return []
    finally:
        if conn:
            conn.close()


async def save_wall_break_t1(
    event_id: int,
    t1_trade_date: str,
    t1_verdict: str,
    t1_label: str,
    t1_gates_json: str,
) -> bool:
    """寫入 T+1 結算。僅在尚未結算時更新；實際更新一列才回傳 True。"""
    try:
        n = await execute_write_rowcount_async(
            """
            UPDATE wall_break_events
            SET t1_trade_date = ?, t1_verdict = ?, t1_label = ?,
                t1_gates_json = ?, t1_checked_at = CURRENT_TIMESTAMP
            WHERE id = ? AND t1_verdict IS NULL
            """,
            (t1_trade_date, t1_verdict, t1_label, t1_gates_json, int(event_id)),
        )
        return n == 1
    except Exception as e:
        logger.error(f"牆突破 T+1 結算寫入失敗 (id={event_id}): {e}")
        return False


async def purge_stale_wall_break(
    anchor_retention_days: int = 14, event_retention_days: int = 180
) -> int:
    """保留期清理。回傳實際刪除的總列數；失敗回傳 0。"""
    try:
        counts = await execute_write_many_async(
            [
                (
                    "DELETE FROM wall_anchor WHERE trade_date < date('now', ?)",
                    (f"-{int(anchor_retention_days)} days",),
                ),
                (
                    "DELETE FROM wall_break_events WHERE created_at < datetime('now', ?)",
                    (f"-{int(event_retention_days)} days",),
                ),
            ]
        )
        return max(0, sum(max(0, int(c)) for c in counts))
    except Exception as e:
        logger.error(f"wall_break 保留期清理失敗: {e}")
        return 0


__all__ = [
    "WALL_BREAK_EVENT_COLUMNS",
    "upsert_wall_anchor",
    "get_latest_wall_anchor_before",
    "insert_wall_break_event",
    "get_wall_break_event",
    "get_pending_t1_events",
    "save_wall_break_t1",
    "purge_stale_wall_break",
]
