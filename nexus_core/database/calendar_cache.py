import logging
import sqlite3
from typing import Any, Optional

import config

logger = logging.getLogger(__name__)


def get_macro_month_status(month_key: str) -> Optional[dict[str, Any]]:
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
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


def replace_macro_month_events(month_key: str, events: list[dict[str, Any]]) -> None:
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM economic_calendar_events WHERE month_key = ?",
            (month_key,),
        )
        if events:
            cursor.executemany(
                """
                INSERT INTO economic_calendar_events
                (month_key, event, event_time, impact, country, consensus_value, fedwatch_probability)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        month_key,
                        item["event"],
                        item["time"],
                        item["impact"],
                        item.get("country", "US"),
                        item.get("consensus_value"),
                        item.get("fedwatch_probability"),
                    )
                    for item in events
                ],
            )
        cursor.execute(
            """
            INSERT INTO economic_calendar_month_cache (month_key, checked_at, event_count)
            VALUES (?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(month_key) DO UPDATE SET
                checked_at = CURRENT_TIMESTAMP,
                event_count = excluded.event_count
            """,
            (month_key, len(events)),
        )
        conn.commit()
    except Exception as e:
        logger.error("寫入 economic_calendar_events 失敗 (%s): %s", month_key, e)
    finally:
        if conn:
            conn.close()


def get_macro_events_between(start_date: str, end_date: str) -> list[dict[str, Any]]:
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
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


def get_cached_earnings(symbol: str) -> Optional[dict[str, Any]]:
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT symbol, earnings_date, checked_at
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


def save_earnings_cache(symbol: str, earnings_date: str | None) -> None:
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO earnings_calendar_cache (symbol, earnings_date, checked_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
                earnings_date = excluded.earnings_date,
                checked_at = CURRENT_TIMESTAMP
            """,
            (symbol.upper(), earnings_date),
        )
        conn.commit()
    except Exception as e:
        logger.error("寫入 earnings_calendar_cache 失敗 (%s): %s", symbol, e)
    finally:
        if conn:
            conn.close()
