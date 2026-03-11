import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from config import DB_NAME

logger = logging.getLogger(__name__)


def _get_payload_column(cursor: sqlite3.Cursor) -> str:
    """Resolve payload column name across legacy/new schemas."""
    cursor.execute("PRAGMA table_info(financials_cache)")
    columns = {row[1] for row in cursor.fetchall()}
    if "data" in columns:
        return "data"
    return "metrics"


def get_cached_financials(symbol: str, expiry_hours: int = 24) -> Optional[Dict[str, Any]]:
    """Read non-expired financial metrics from SQLite cache."""
    try:
        with sqlite3.connect(DB_NAME) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            payload_col = _get_payload_column(cursor)
            expiry_limit = (datetime.now() - timedelta(hours=expiry_hours)).strftime("%Y-%m-%d %H:%M:%S")

            cursor.execute(
                f"""
                SELECT {payload_col} AS payload
                FROM financials_cache
                WHERE symbol = ? AND updated_at > ?
                """,
                (symbol.upper(), expiry_limit),
            )
            row = cursor.fetchone()
            if not row or not row["payload"]:
                return None
            return json.loads(row["payload"])
    except Exception as e:
        logger.error("[%s] 讀取 financials_cache 失敗: %s", symbol, e)
        return None


def save_financials_cache(symbol: str, data: Dict[str, Any]) -> None:
    """Upsert financial metrics into SQLite cache."""
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            payload_col = _get_payload_column(cursor)
            cursor.execute(
                f"""
                INSERT OR REPLACE INTO financials_cache (symbol, {payload_col}, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                (symbol.upper(), json.dumps(data)),
            )
            conn.commit()
    except Exception as e:
        logger.error("[%s] 寫入 financials_cache 失敗: %s", symbol, e)


def purge_old_cache(days: int = 30) -> int:
    """Delete expired cache rows and return number of removed rows."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        limit = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("DELETE FROM financials_cache WHERE updated_at < ?", (limit,))
        conn.commit()
        return cursor.rowcount