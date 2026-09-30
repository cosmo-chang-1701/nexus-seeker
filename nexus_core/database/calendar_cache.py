import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from database.connection import execute_write, execute_write_many, get_read_connection

logger = logging.getLogger(__name__)


def get_macro_month_status(month_key: str) -> Optional[dict[str, Any]]:
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT month_key, checked_at, event_count
            FROM economic_calendar_month_cache
            WHERE month_key = ?
            """,
            (month_key,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("讀取 economic_calendar_month_cache 失敗 (%s): %s", month_key, e)
        return None
    finally:
        if conn:
            conn.close()


_UPSERT_MACRO_EVENT_SQL = """
    INSERT INTO economic_calendar_events
    (month_key, event, event_time, impact, country, consensus_value, fedwatch_probability, actual_value)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(month_key, event, event_time, country) DO UPDATE SET
        impact = excluded.impact,
        consensus_value = excluded.consensus_value,
        actual_value = excluded.actual_value,
        fedwatch_probability = COALESCE(
            excluded.fedwatch_probability,
            economic_calendar_events.fedwatch_probability
        )
"""


def replace_macro_month_events(month_key: str, events: list[dict[str, Any]]) -> None:
    """以最新抓取結果覆寫整月總經事件，但保留 FedWatch 寫入的欄位。

    日曆來源（Edge Scraper / TradingView）不提供 `fedwatch_probability`，該欄位
    由 `CalendarService.update_fedwatch_probability()` 事後以 UPDATE 寫入既有事件列。
    早期實作為整月 DELETE + INSERT，新列的 `fedwatch_probability` 一律為 NULL，
    導致任何觸發日曆重抓的路徑（4h 排程的 CPI 偏差更新、/market、Analyst Agent、
    /force_macro_update）都會洗掉已寫入的 FedWatch 定價。

    現行作法以主鍵 `(month_key, event, event_time, country)` 對應同一事件：
    1. 刪除本次抓取結果中已不存在的舊事件（事件取消、改期或改名）。
    2. 其餘事件以 UPSERT 寫入：日曆欄位（impact / consensus / actual）一律以新值
       覆寫，`fedwatch_probability` 僅在新資料有值時覆寫，否則保留原值。
    """
    # 刪除 + UPSERT + 月份快取戳記必須同屬一個交易，否則整月事件可能出現
    # 「舊資料已刪、新資料未寫入」的空窗。走批次寫入入口，只 commit 一次。
    statements: list[tuple] = []
    if events:
        rows = [
            (
                month_key,
                item["event"],
                item["time"],
                item["impact"],
                item.get("country", "US"),
                item.get("consensus_value"),
                item.get("fedwatch_probability"),
                item.get("actual_value"),
            )
            for item in events
        ]
        # 僅由 "?" 組成的佔位符，事件內容全部以參數綁定傳入。
        keep_placeholders = ", ".join("(?, ?, ?)" for _ in rows)
        keep_params: list[Any] = [month_key]
        for row in rows:
            keep_params.extend((row[1], row[2], row[4]))
        # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
        delete_stale_sql = (
            "DELETE FROM economic_calendar_events "
            "WHERE month_key = ? "
            f"AND (event, event_time, country) NOT IN (VALUES {keep_placeholders})"
        )
        statements.append((delete_stale_sql, tuple(keep_params)))
        statements.append((_UPSERT_MACRO_EVENT_SQL, rows, True))
    else:
        statements.append(
            (
                "DELETE FROM economic_calendar_events WHERE month_key = ?",
                (month_key,),
            )
        )
    statements.append(
        (
            """
            INSERT INTO economic_calendar_month_cache (month_key, checked_at, event_count)
            VALUES (?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(month_key) DO UPDATE SET
                checked_at = CURRENT_TIMESTAMP,
                event_count = excluded.event_count
            """,
            (month_key, len(events)),
        )
    )
    try:
        execute_write_many(statements)
    except Exception as e:
        logger.error("寫入 economic_calendar_events 失敗 (%s): %s", month_key, e)


def get_macro_events_between(start_date: str, end_date: str) -> list[dict[str, Any]]:
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT event, event_time, impact, country, consensus_value, fedwatch_probability
            FROM economic_calendar_events
            WHERE substr(event_time, 1, 10) BETWEEN ? AND ?
            ORDER BY event_time ASC
            """,
            (start_date, end_date),
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(
            "讀取 economic_calendar_events 失敗 (%s -> %s): %s",
            start_date,
            end_date,
            e,
        )
        return []
    finally:
        if conn:
            conn.close()


def get_latest_released_economic_event(
    event_name: str, as_of: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """取得指定事件名稱（須為翻譯後的精確中文全名，如「CPI 年增率」）中，
    最近一筆「已公布且含實際值」的紀錄。用於 CPI 等 actual-vs-expected 比對。"""
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cutoff = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor.execute(
            """
            SELECT event, event_time, actual_value, consensus_value
            FROM economic_calendar_events
            WHERE event = ?
              AND event_time <= ?
              AND actual_value IS NOT NULL
            ORDER BY event_time DESC
            LIMIT 1
            """,
            (event_name, cutoff),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("讀取最新已公布事件失敗 (%s): %s", event_name, e)
        return None
    finally:
        if conn:
            conn.close()


def get_cached_earnings(symbol: str) -> Optional[dict[str, Any]]:
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT *
            FROM earnings_calendar_cache
            WHERE symbol = ?
            """,
            (symbol.upper(),),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("讀取 earnings_calendar_cache 失敗 (%s): %s", symbol, e)
        return None
    finally:
        if conn:
            conn.close()


def save_earnings_cache(
    symbol: str, earnings_date: str | None, hour: str | None = None
) -> None:
    conn = None
    try:
        conn = get_read_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(earnings_calendar_cache)")
            cols = {c[1] for c in cursor.fetchall()}
        finally:
            conn.close()
            conn = None

        if "hour" in cols:
            execute_write(
                """
                INSERT INTO earnings_calendar_cache (symbol, earnings_date, hour, checked_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(symbol) DO UPDATE SET
                    earnings_date = excluded.earnings_date,
                    hour = excluded.hour,
                    checked_at = CURRENT_TIMESTAMP
                """,
                (symbol.upper(), earnings_date, hour),
            )
        else:
            execute_write(
                """
                INSERT INTO earnings_calendar_cache (symbol, earnings_date, checked_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(symbol) DO UPDATE SET
                    earnings_date = excluded.earnings_date,
                    checked_at = CURRENT_TIMESTAMP
                """,
                (symbol.upper(), earnings_date),
            )
    except Exception as e:
        logger.error("寫入 earnings_calendar_cache 失敗 (%s): %s", symbol, e)
    finally:
        if conn:
            conn.close()
