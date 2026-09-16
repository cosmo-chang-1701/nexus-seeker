import logging
import sqlite3
from typing import Optional, Dict, Any
from database.connection import get_read_connection, execute_write_async

logger = logging.getLogger(__name__)


async def save_market_cache(
    symbol: str,
    max_pain: float,
    expected_move_lower: float,
    expected_move_upper: float,
    reference_spot_price: Optional[float] = None,
    is_stale: int = 0,
    calculation_mode: str = "OI",
    is_degraded: int = 0,
    circuit_breaker_triggered: int = 0,
    expiry: Optional[str] = None,
    call_wall: Optional[float] = None,
    previous_call_wall: Optional[float] = None,
    put_wall: Optional[float] = None,
    previous_put_wall: Optional[float] = None,
) -> bool:
    """寫入 market_cache。

    ``call_wall`` / ``previous_call_wall`` 追蹤做市商**阻力**牆的跨週期遷移，
    供多頭 TP2-空間擴展的「牆向上遷移 >= 3%」判定使用（v069）。
    ``put_wall`` / ``previous_put_wall`` 是其鏡像，追蹤**支撐**牆的跨週期遷移，
    供做空 TP2 的「牆向下遷移 >= 3%」判定使用（v074）。

    兩組欄位的 UPSERT 語意完全對稱：只有在新舊牆位皆有效且**確實不同**時，才把
    舊值搬進 previous_*；呼叫端也可顯式傳入 previous_* 覆蓋。牆位為 None 或
    <= 0 時保留既有值，避免一次抓取失敗就抹掉整條遷移軌跡。
    """
    if not expiry:
        expiry = "WEEKLY"
    call_wall_val = (
        float(call_wall) if (call_wall is not None and float(call_wall) > 0) else None
    )
    prev_cw_val = (
        float(previous_call_wall)
        if (previous_call_wall is not None and float(previous_call_wall) > 0)
        else None
    )
    put_wall_val = (
        float(put_wall) if (put_wall is not None and float(put_wall) > 0) else None
    )
    prev_pw_val = (
        float(previous_put_wall)
        if (previous_put_wall is not None and float(previous_put_wall) > 0)
        else None
    )
    try:
        await execute_write_async(
            """
            INSERT INTO market_cache (
                symbol, expiry, max_pain, expected_move_lower, expected_move_upper,
                reference_spot_price, is_stale, calculation_mode, is_degraded,
                circuit_breaker_triggered, call_wall, previous_call_wall,
                put_wall, previous_put_wall, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol, expiry) DO UPDATE SET
            max_pain = excluded.max_pain,
            expected_move_lower = excluded.expected_move_lower,
            expected_move_upper = excluded.expected_move_upper,
            reference_spot_price = excluded.reference_spot_price,
            is_stale = excluded.is_stale,
            calculation_mode = excluded.calculation_mode,
            is_degraded = excluded.is_degraded,
            circuit_breaker_triggered = excluded.circuit_breaker_triggered,
            previous_call_wall = CASE
                WHEN excluded.previous_call_wall IS NOT NULL THEN excluded.previous_call_wall
                WHEN excluded.call_wall IS NOT NULL
                     AND excluded.call_wall > 0
                     AND market_cache.call_wall IS NOT NULL
                     AND market_cache.call_wall > 0
                     AND excluded.call_wall != market_cache.call_wall
                THEN market_cache.call_wall
                ELSE market_cache.previous_call_wall
            END,
            call_wall = CASE
                WHEN excluded.call_wall IS NOT NULL AND excluded.call_wall > 0 THEN excluded.call_wall
                ELSE market_cache.call_wall
            END,
            previous_put_wall = CASE
                WHEN excluded.previous_put_wall IS NOT NULL THEN excluded.previous_put_wall
                WHEN excluded.put_wall IS NOT NULL
                     AND excluded.put_wall > 0
                     AND market_cache.put_wall IS NOT NULL
                     AND market_cache.put_wall > 0
                     AND excluded.put_wall != market_cache.put_wall
                THEN market_cache.put_wall
                ELSE market_cache.previous_put_wall
            END,
            put_wall = CASE
                WHEN excluded.put_wall IS NOT NULL AND excluded.put_wall > 0 THEN excluded.put_wall
                ELSE market_cache.put_wall
            END,
            updated_at = CURRENT_TIMESTAMP
        """,
            (
                symbol.upper(),
                expiry,
                max_pain,
                expected_move_lower,
                expected_move_upper,
                reference_spot_price,
                is_stale,
                calculation_mode,
                is_degraded,
                circuit_breaker_triggered,
                call_wall_val,
                prev_cw_val,
                put_wall_val,
                prev_pw_val,
            ),
        )
        return True
    except Exception as e:
        logger.error(f"[{symbol}] save_market_cache 寫入失敗: {e}")
        return False


async def mark_market_cache_stale(symbol: str, expiry: Optional[str] = None) -> bool:
    try:
        if expiry:
            await execute_write_async(
                "UPDATE market_cache SET is_stale = 1 WHERE symbol = ? AND expiry = ?",
                (symbol.upper(), expiry),
            )
        else:
            await execute_write_async(
                "UPDATE market_cache SET is_stale = 1 WHERE symbol = ?",
                (symbol.upper(),),
            )
        return True
    except Exception as e:
        logger.error(f"[{symbol}] mark_market_cache_stale 寫入失敗: {e}")
        return False


def get_market_cache(
    symbol: str, expiry: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if expiry:
            cursor.execute(
                "SELECT * FROM market_cache WHERE symbol = ? AND expiry = ?",
                (symbol.upper(), expiry),
            )
        else:
            cursor.execute(
                "SELECT * FROM market_cache WHERE symbol = ? ORDER BY updated_at DESC LIMIT 1",
                (symbol.upper(),),
            )
        row = cursor.fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    finally:
        if conn:
            conn.close()
    return None


async def save_fundamental_cache(
    symbol: str, is_broken: bool, confidence: float, reasoning: str
) -> bool:
    try:
        await execute_write_async(
            """
            INSERT INTO fundamental_cache (symbol, is_broken, confidence, reasoning, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
            is_broken = excluded.is_broken,
            confidence = excluded.confidence,
            reasoning = excluded.reasoning,
            updated_at = CURRENT_TIMESTAMP
            """,
            (symbol.upper(), int(is_broken), confidence, reasoning),
        )
        return True
    except Exception as e:
        logger.error(f"[{symbol}] save_fundamental_cache 寫入失敗: {e}")
        return False


def get_fundamental_cache(symbol: str) -> Optional[Dict[str, Any]]:
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT is_broken, confidence, reasoning, updated_at FROM fundamental_cache WHERE symbol = ?",
            (symbol.upper(),),
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    finally:
        if conn:
            conn.close()
    return None


async def save_fundamental_scan_state(
    symbol: str, accession_number: str, form_type: str
) -> bool:
    """記錄某標的最後一次自動掃描已分析過的 SEC 申報 (accession_number)，
    作為每日排程的去重游標，避免同一份文件被重複送入 LLM 分析。"""
    try:
        await execute_write_async(
            """
            INSERT INTO fundamental_scan_state (symbol, last_accession_number, last_form_type, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
            last_accession_number = excluded.last_accession_number,
            last_form_type = excluded.last_form_type,
            updated_at = CURRENT_TIMESTAMP
            """,
            (symbol.upper(), accession_number, form_type),
        )
        return True
    except Exception as e:
        logger.error(f"[{symbol}] save_fundamental_scan_state 寫入失敗: {e}")
        return False


def get_fundamental_scan_state(symbol: str) -> Optional[Dict[str, Any]]:
    conn = None
    try:
        conn = get_read_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT last_accession_number, last_form_type, updated_at FROM fundamental_scan_state WHERE symbol = ?",
            (symbol.upper(),),
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    finally:
        if conn:
            conn.close()
    return None
