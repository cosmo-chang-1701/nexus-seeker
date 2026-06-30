import logging
import sqlite3  # noqa: F401
import asyncio
from typing import Optional


from .cache import _iv_cache

logger = logging.getLogger(__name__)


INDEX_SYMBOLS = {"SPY", "QQQ", "DIA", "IWM", "SPX", "NDX", "RUT", "VIX"}
_revalidating_symbols: set[str] = set()


def _trigger_background_cache_clear(symbol: str):
    symbol_upper = symbol.upper()
    if symbol_upper in _revalidating_symbols:
        logger.info(
            f"[{symbol_upper}] Revalidation already in progress, skipping background task launch."
        )
        return

    _revalidating_symbols.add(symbol_upper)

    async def _async_clear_and_revalidate():
        try:
            logger.info(
                f"🔄 [Self-Healing] Clearing SQLite/yfinance cache for {symbol_upper} due to circuit breaker breach..."
            )

            # 1. Clear memory caches
            if symbol_upper in _iv_cache:
                del _iv_cache[symbol_upper]

            from services.market_data_service import (
                _option_chain_cache,
                _option_expiries_cache,
            )

            if symbol_upper in _option_expiries_cache:
                del _option_expiries_cache[symbol_upper]

            keys_to_del = [
                k
                for k in _option_chain_cache.keys()
                if isinstance(k, tuple) and k[0].upper() == symbol_upper
            ]
            for k in keys_to_del:
                del _option_chain_cache[k]

            # 2. Clear SQLite KV cache
            try:
                from database.connection import execute_write_async

                await execute_write_async(
                    "DELETE FROM kv_cache WHERE key LIKE ?",
                    (f"max_pain_{symbol_upper}%",),
                )
            except Exception as db_err:
                logger.warning(
                    f"Failed to clear SQLite KV cache for {symbol_upper}: {db_err}"
                )

            # 3. Mark database cache stale
            try:
                from database import mark_market_cache_stale

                await asyncio.to_thread(mark_market_cache_stale, symbol_upper)
            except Exception as stale_err:
                logger.warning(
                    f"Failed to mark market_cache stale for {symbol_upper}: {stale_err}"
                )

            # 4. Pre-warm / Revalidate
            logger.info(
                f"🔄 [Self-Healing] Pre-warming cache with retry for {symbol_upper}..."
            )
            from .max_pain import calculate_max_pain

            await calculate_max_pain(symbol_upper, _retry=True)

        except Exception as ex:
            logger.error(
                f"❌ [Self-Healing] Background cache clearing failed for {symbol_upper}: {ex}"
            )
        finally:
            _revalidating_symbols.discard(symbol_upper)

    asyncio.create_task(_async_clear_and_revalidate())


async def save_sentiment_history(symbol: str, indicator: str, value: float):
    """將情緒指標存入資料庫。"""
    try:
        from database.connection import execute_write_async

        await execute_write_async(
            """
            INSERT INTO sentiment_history (symbol, indicator, value)
            VALUES (?, ?, ?)
        """,
            (symbol, indicator, value),
        )
    except Exception as e:
        logger.error(f"儲存情緒歷史失敗: {e}")


def get_indicator_percentile(
    symbol: str, indicator: str, current_value: float
) -> float:
    """計算目前值在歷史數據中的百分位數。"""
    try:
        from database.connection import get_read_connection

        conn = get_read_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT value FROM sentiment_history
            WHERE symbol = ? AND indicator = ?
            ORDER BY timestamp DESC LIMIT 100
        """,
            (symbol, indicator),
        )
        values = [row[0] for row in cursor.fetchall()]
        conn.close()

        if not values:
            return 50.0  # 預設中值

        count = sum(1 for v in values if v < current_value)
        return (count / len(values)) * 100
    except Exception:
        return 50.0


def get_last_stored_iv(symbol: str) -> Optional[float]:
    """從資料庫中取得最後一次記錄的 IV。"""
    try:
        from database.connection import get_read_connection

        conn = get_read_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT iv FROM historical_iv WHERE symbol = ? ORDER BY date DESC LIMIT 1",
            (symbol,),
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]
    except Exception as e:
        logger.error(f"取得資料庫最後 IV 失敗: {e}")
    return None


def get_last_stored_sentiment(symbol: str, indicator: str) -> Optional[float]:
    """從 sentiment_history 中取得最後一次記錄的情緒指標值。"""
    try:
        from database.connection import get_read_connection

        conn = get_read_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT value FROM sentiment_history
            WHERE symbol = ? AND indicator = ?
            ORDER BY timestamp DESC LIMIT 1
            """,
            (symbol, indicator),
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return float(row[0])
    except Exception as e:
        logger.error(f"取得資料庫最後情緒歷史失敗 ({indicator}): {e}")
    return None


async def save_historical_iv(symbol: str, iv: float, date_str: str):
    """將每日 IV 存入 database。"""
    try:
        from bot import NexusBot
        from database.connection import DatabaseWriteQueue

        bot = NexusBot.get_instance()
        if bot and hasattr(bot, "db_write_queue") and bot.db_write_queue:
            await bot.db_write_queue.put_task(
                "save_historical_iv", (symbol, iv, date_str)
            )
        else:
            await DatabaseWriteQueue.put_task(
                "save_historical_iv", (symbol, iv, date_str)
            )
    except Exception as e:
        logger.error(f"儲存歷史 IV 失敗: {e}")
