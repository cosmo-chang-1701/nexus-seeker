import sqlite3
from typing import Optional, Dict, Any
from database.connection import get_read_connection, execute_write


def save_market_cache(
    symbol: str,
    max_pain: float,
    expected_move_lower: float,
    expected_move_upper: float,
    reference_spot_price: Optional[float] = None,
    is_stale: int = 0,
    calculation_mode: str = "OI",
    is_degraded: int = 0,
    circuit_breaker_triggered: int = 0,
) -> bool:
    try:
        execute_write(
            """
            INSERT INTO market_cache (
                symbol, max_pain, expected_move_lower, expected_move_upper,
                reference_spot_price, is_stale, calculation_mode, is_degraded,
                circuit_breaker_triggered, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
            max_pain = excluded.max_pain,
            expected_move_lower = excluded.expected_move_lower,
            expected_move_upper = excluded.expected_move_upper,
            reference_spot_price = excluded.reference_spot_price,
            is_stale = excluded.is_stale,
            calculation_mode = excluded.calculation_mode,
            is_degraded = excluded.is_degraded,
            circuit_breaker_triggered = excluded.circuit_breaker_triggered,
            updated_at = CURRENT_TIMESTAMP
        """,
            (
                symbol.upper(),
                max_pain,
                expected_move_lower,
                expected_move_upper,
                reference_spot_price,
                is_stale,
                calculation_mode,
                is_degraded,
                circuit_breaker_triggered,
            ),
        )
        return True
    except Exception:
        return False


def mark_market_cache_stale(symbol: str) -> bool:
    try:
        execute_write(
            "UPDATE market_cache SET is_stale = 1 WHERE symbol = ?",
            (symbol.upper(),),
        )
        return True
    except Exception:
        return False


def get_market_cache(symbol: str) -> Optional[Dict[str, Any]]:
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM market_cache WHERE symbol = ?", (symbol.upper(),))
        row = cursor.fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    finally:
        if conn:
            conn.close()
    return None
