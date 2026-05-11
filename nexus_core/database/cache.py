import sqlite3
import logging
import json
from typing import Any, Optional
import config
from .financials import get_cached_financials, save_financials_cache, purge_old_cache

logger = logging.getLogger(__name__)


def save_kv_cache(key: str, value: Any) -> bool:
    try:
        val_str = json.dumps(value)
        with sqlite3.connect(config.DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO kv_cache (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
                (key, val_str),
            )
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"save_kv_cache 失敗 (key: {key}): {e}")
        return False


def get_kv_cache(key: str) -> Optional[Any]:
    try:
        with sqlite3.connect(config.DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM kv_cache WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
    except Exception as e:
        logger.error(f"get_kv_cache 失敗 (key: {key}): {e}")
    return None


__all__ = [
    "get_cached_financials",
    "save_financials_cache",
    "purge_old_cache",
    "save_kv_cache",
    "get_kv_cache",
]
