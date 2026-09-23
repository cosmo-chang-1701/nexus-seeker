"""uoa_history.py — 機構級異常選擇權活動 (UOA) 歷史紀錄的存取層。

為什麼存在：右側進場條件四原本只看「評估當下」的 UOA 快照，而該快照的唯一持久
來源 `kv_cache` 的 `uoa_{SYMBOL}` 是 upsert——每輪心跳覆蓋前一輪，結構上無法回看。
Regime III-B 趨勢延續路徑需要「最近 N 個交易日內是否出現過機構買盤」，故另立本表。

所有寫入一律經 `database/connection.py` 的寫入佇列 (單一寫入者不變式，見
AGENTS.md)。資料表定義見 migrations/v077_add_uoa_history.py。
"""

import logging
import sqlite3
from typing import Any, Mapping, Sequence

from database.connection import execute_write_many_async, get_read_connection

logger = logging.getLogger(__name__)

# 與 v077 資料表欄位一一對應 (不含 id / observed_at，由 DB 預設)。
UOA_COLUMNS: tuple[str, ...] = (
    "observed_bar_ts",
    "symbol",
    "expiry",
    "strike",
    "opt_type",
    "action",
    "ratio",
    "notional_value",
)

_INSERT_SQL = (
    f"INSERT OR IGNORE INTO uoa_history ({', '.join(UOA_COLUMNS)}) "  # nosemgrep
    f"VALUES ({', '.join('?' for _ in UOA_COLUMNS)})"
)

# 保留 10 個**交易日**：涵蓋 _ENTRY_UOA_LOOKBACK_DAYS = 5 個交易日的回看窗並留
# 一倍緩衝。以交易日計算，與回看窗 (`market_time.get_trading_days_ago_utc`) 使用
# 同一把尺——日曆日保留期只能靠「多留幾天」去猜連假長度。刻意不設更長：本表的
# 唯一消費者是 5 日回看窗，多留的資料對 1GB VPS 只有成本沒有價值。
_RETENTION_TRADING_DAYS = 10


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def _to_rows(
    symbol: str, observed_bar_ts: str, uoa_list: Sequence[Mapping[str, Any]]
) -> list[tuple[Any, ...]]:
    """把 `detect_uoa` 的回傳清單轉成資料列。無法解析的項目靜默略過。

    只取條件四判定實際用得到的欄位；`detect_uoa` 回傳的 delta / iv / bid / ask
    等不落地——本表存在的目的是回答「那幾天有沒有機構買盤」，不是重現完整快照。
    """
    rows: list[tuple[Any, ...]] = []
    for entry in uoa_list or []:
        if not isinstance(entry, Mapping):
            continue
        expiry = str(entry.get("expiry", "") or "")
        opt_type = str(entry.get("type", "") or "").upper()
        action = str(entry.get("action", "") or "").upper()
        if not expiry or not opt_type or not action:
            continue
        try:
            strike = float(entry.get("strike", 0.0) or 0.0)
            ratio = float(entry.get("ratio", 0.0) or 0.0)
            notional_value = float(entry.get("notional_value", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if strike <= 0:
            continue
        rows.append(
            (
                observed_bar_ts,
                symbol.upper(),
                expiry,
                strike,
                opt_type,
                action,
                ratio,
                notional_value,
            )
        )
    return rows


async def save_uoa_observations(
    symbol: str, observed_bar_ts: str, uoa_list: Sequence[Mapping[str, Any]]
) -> int:
    """批次寫入一輪心跳觀測到的 UOA。同一根 15m K 棒的重複觀測由唯一索引忽略。"""
    rows = _to_rows(symbol, observed_bar_ts, uoa_list)
    if not rows:
        return 0
    try:
        counts = await execute_write_many_async([(_INSERT_SQL, rows, True)])
        return int(counts[0]) if counts else 0
    except Exception as e:
        logger.error(f"[{symbol}] UOA 歷史寫入失敗: {e}")
        return 0


def get_recent_uoa(
    symbol: str, since_utc: str, limit: int = 200
) -> list[dict[str, Any]]:
    """讀取 `since_utc` (UTC，'YYYY-MM-DD HH:MM:SS') 之後觀測到的 UOA 紀錄。

    回傳的 dict 刻意使用與 `detect_uoa` 相同的鍵名 (`type` 而非 `opt_type`)，
    讓條件四可以把歷史紀錄與即時快照**串成同一個清單**餵給既有的逐筆掃描迴圈，
    不需要在判定函式裡多開一條分支。

    依名目價值降序排列，與 `detect_uoa` 的既有排序語意一致——條件四是「找到第一筆
    符合門檻者就通過」，排序決定了 reason 字串裡會秀出哪一筆。
    """
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = _dict_factory
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT expiry, strike, opt_type AS type, action, ratio,
                   notional_value, observed_at
            FROM uoa_history
            WHERE symbol = ? AND observed_at >= ?
            ORDER BY notional_value DESC
            LIMIT ?
            """,
            (symbol.upper(), since_utc, int(limit)),
        )
        return list(cursor.fetchall())
    except Exception as e:
        logger.error(f"[{symbol}] UOA 歷史讀取失敗: {e}")
        return []
    finally:
        if conn:
            conn.close()


async def purge_stale_uoa_history(
    retention_trading_days: int = _RETENTION_TRADING_DAYS,
) -> int:
    """保留期清理，由 03:00 ET 離峰排程呼叫。回傳實際刪除的列數。

    截止點為往回第 `retention_trading_days` 個交易日的開盤時刻；行事曆查詢失敗時
    `for_purge=True` 會退回較長的日曆窗，寧可少刪。
    """
    try:
        from market_time import get_trading_days_ago_utc

        cutoff_utc = get_trading_days_ago_utc(retention_trading_days, for_purge=True)
        counts = await execute_write_many_async(
            [("DELETE FROM uoa_history WHERE observed_at < ?", (cutoff_utc,))]
        )
        return max(0, int(counts[0])) if counts else 0
    except Exception as e:
        logger.error(f"uoa_history 保留期清理失敗: {e}")
        return 0


__all__ = [
    "UOA_COLUMNS",
    "save_uoa_observations",
    "get_recent_uoa",
    "purge_stale_uoa_history",
]
