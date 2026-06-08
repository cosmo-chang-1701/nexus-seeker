import sqlite3
import config
from typing import Optional, Dict, Any


def save_market_cache(
    symbol: str,
    max_pain: float,
    expected_move_lower: float,
    expected_move_upper: float,
) -> bool:
    try:
        with sqlite3.connect(config.DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO market_cache (symbol, max_pain, expected_move_lower, expected_move_upper, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(symbol) DO UPDATE SET
                max_pain = excluded.max_pain,
                expected_move_lower = excluded.expected_move_lower,
                expected_move_upper = excluded.expected_move_upper,
                updated_at = CURRENT_TIMESTAMP
            """,
                (symbol.upper(), max_pain, expected_move_lower, expected_move_upper),
            )
            conn.commit()
            return True
    except Exception:
        return False


def get_market_cache(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        with sqlite3.connect(config.DB_NAME) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM market_cache WHERE symbol = ?", (symbol.upper(),)
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
    except Exception:
        pass
    return None
