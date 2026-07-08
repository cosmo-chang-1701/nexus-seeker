import sqlite3
from typing import Optional, Dict, Any
from database.connection import get_read_connection, execute_write


def save_squeeze_cache(
    symbol: str, is_squeezing: bool, momentum: float, direction: str
) -> bool:
    try:
        execute_write(
            """
            INSERT INTO squeeze_cache (symbol, is_squeezing, momentum, direction, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
            is_squeezing = excluded.is_squeezing,
            momentum = excluded.momentum,
            direction = excluded.direction,
            updated_at = CURRENT_TIMESTAMP
            """,
            (symbol.upper(), int(is_squeezing), momentum, direction),
        )
        return True
    except Exception:
        return False


def get_squeeze_cache(symbol: str) -> Optional[Dict[str, Any]]:
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # TTL = 30 minutes
        cursor.execute(
            """
            SELECT * FROM squeeze_cache
            WHERE symbol = ? AND datetime(updated_at, '+30 minutes') > CURRENT_TIMESTAMP
            """,
            (symbol.upper(),),
        )
        row = cursor.fetchone()
        if row:
            # Convert SQLite row to dictionary and handle boolean casting if necessary
            data = dict(row)
            data["is_squeezing"] = bool(data.get("is_squeezing", 0))
            return data
    except Exception:
        pass
    finally:
        if conn:
            conn.close()
    return None
