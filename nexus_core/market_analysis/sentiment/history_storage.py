from typing import Any
import logging
import sqlite3  # noqa: F401
import asyncio
from typing import Optional


from .cache import _iv_cache

logger = logging.getLogger(__name__)


INDEX_SYMBOLS = {"SPY", "QQQ", "DIA", "IWM", "SPX", "NDX", "RUT", "VIX"}
_revalidating_symbols: set[str] = set()


def _trigger_background_cache_clear(symbol: str) -> Any:
    symbol_upper = symbol.upper()
    if symbol_upper in _revalidating_symbols:
        logger.info(
            f"[{symbol_upper}] Revalidation already in progress, skipping background task launch."
        )
        return

    _revalidating_symbols.add(symbol_upper)

    async def _async_clear_and_revalidate() -> None:
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


async def save_sentiment_history(symbol: str, indicator: str, value: float) -> Any:
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


# 百分位排名的最低樣本數。低於此值一律回傳 None（資料不足），而不是硬給一個
# 看起來合理的數字：
#   - 舊實作在「完全無歷史」與「DB 例外」時都回傳 50.0，而 50.0 正好落在
#     skew_commentary 的 30-70 抑制窗內，於是資料全滅會被報成「屬常態，已抑制警報」；
#   - 只有 1 筆樣本時 count(v < current) == 0 → 回傳 0.0%，單一觀測值就足以點燃
#     `skew_percentile <= 20` 的「市場上行看漲需求爆發」與 evaluation.py 的
#     `< 15.0` 背離閘門。
# 呼叫端（option_skew / skew_percentile）本來就是 Optional，且判讀層已有
# 「分位數據缺失」分支，沉默比憑空生成極端訊號安全。
_MIN_PERCENTILE_SAMPLES = 20

# 排名視窗仍是「最近 100 列」而非固定時間視窗。這是 AGENTS.md 明列、刻意留待
# 獨立變更處理的已知限制；此處只補上樣本數守衛與同值處理，不改動視窗定義。
_PERCENTILE_WINDOW_ROWS = 100


def get_indicator_percentile_with_sample_size(
    symbol: str,
    indicator: str,
    current_value: float,
    min_samples: int = _MIN_PERCENTILE_SAMPLES,
) -> tuple[Optional[float], int]:
    """回傳 (百分位, 實際樣本數)；樣本不足或查詢失敗時百分位為 None。

    同值採 midrank（`count_less + 0.5 * count_equal`）而非嚴格 `<`：舊實作在
    「所有樣本相等」（行情停滯或資料源卡住）時會塌陷成 0.0%，與真正的極端低位
    無法區分。midrank 讓這種情況回到 50.0%，語意正確。
    """
    try:
        from database.connection import get_read_connection

        conn = get_read_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT value FROM sentiment_history
            WHERE symbol = ? AND indicator = ?
            ORDER BY timestamp DESC LIMIT ?
        """,
            (symbol, indicator, _PERCENTILE_WINDOW_ROWS),
        )
        values = [row[0] for row in cursor.fetchall()]
        conn.close()
    except Exception as e:
        logger.warning(f"[{symbol}] 讀取 {indicator} 歷史分位失敗: {e}")
        return None, 0

    sample_size = len(values)
    if sample_size < min_samples:
        logger.debug(
            f"[{symbol}] {indicator} 歷史樣本不足 ({sample_size} < {min_samples})，不輸出百分位"
        )
        return None, sample_size

    count_less = sum(1 for v in values if v < current_value)
    count_equal = sum(1 for v in values if v == current_value)
    percentile = (count_less + 0.5 * count_equal) / sample_size * 100
    return percentile, sample_size


def get_indicator_percentile(
    symbol: str,
    indicator: str,
    current_value: float,
    min_samples: int = _MIN_PERCENTILE_SAMPLES,
) -> Optional[float]:
    """計算目前值在歷史數據中的百分位數；樣本不足或查詢失敗時回傳 None。"""
    percentile, _ = get_indicator_percentile_with_sample_size(
        symbol, indicator, current_value, min_samples=min_samples
    )
    return percentile


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
            return row[0]  # type: ignore
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


async def save_historical_iv(symbol: str, iv: float, date_str: str) -> Any:
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
