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

                await mark_market_cache_stale(symbol_upper)
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

# 排名視窗擴展至最近 500 列（約 20 個交易日盤中取樣），避免 100 列 (~3.8 天)
# 造成宏觀分位數統計失真 (ISSUE-2.1)。
_PERCENTILE_WINDOW_ROWS = 500


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
            SELECT value, date(timestamp) FROM sentiment_history
            WHERE symbol = ? AND indicator = ?
            ORDER BY timestamp DESC LIMIT ?
        """,
            (symbol, indicator, _PERCENTILE_WINDOW_ROWS),
        )
        rows = cursor.fetchall()
        conn.close()
        values = [row[0] for row in rows]
        unique_dates = {row[1] for row in rows if len(row) > 1 and row[1]}
    except Exception as e:
        logger.warning(f"[{symbol}] 讀取 {indicator} 歷史分位失敗: {e}")
        return None, 0

    sample_size = len(values)
    if sample_size < min_samples:
        logger.debug(
            f"[{symbol}] {indicator} 歷史樣本不足 ({sample_size} < {min_samples})，不輸出百分位"
        )
        return None, sample_size

    # 冷啟動跨日保護 (ISSUE-2.2)：若樣本跨越的獨立交易日不足 3 天且總樣本未達 60 筆，
    # 代表仍處於加入自選首日或次日的盤中高頻噪聲期，避免將短暫日內波動誤判為 97.5% 世紀極端。
    if len(unique_dates) < 3 and sample_size < 60:
        logger.debug(
            f"[{symbol}] {indicator} 處於冷啟動積累期 (跨度 {len(unique_dates)} 天 < 3 天, 樣本 {sample_size} < 60)，不輸出統計分位"
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


async def _resolve_iv_for_storage(symbol: str, iv: Any) -> Optional[float]:
    """在**入列之前**把無效 IV 解析完畢（自癒）。

    刻意放在呼叫端而非寫入 worker 內：worker 是全程序唯一且序列化的寫入者，
    早期版本把 Fallback 2 的 `get_history_df()` 網路請求放進 worker 的
    `_process_task`，那筆請求期間**全程序所有 `execute_write_async` 一起卡住**
    （head-of-line blocking）。這裡先解析完，worker 只需要收一筆純 SQL 寫入。
    """
    import math

    def _invalid(v: Any) -> bool:
        return v is None or (isinstance(v, float) and math.isnan(v))

    if not _invalid(iv):
        return float(iv)

    logger.warning(
        f"[{symbol}] save_historical_iv 收到無效 IV ({iv})，啟動自癒 fallback。"
    )

    # Fallback 1: 前一交易日收盤 IV（沿用本模組既有的讀取函式）
    last_iv = await asyncio.to_thread(get_last_stored_iv, symbol)
    if not _invalid(last_iv):
        logger.info(f"[{symbol}] Fallback 1: 沿用前一交易日收盤 IV ({last_iv})。")
        return float(last_iv)  # type: ignore[arg-type]

    # Fallback 2: 30 日歷史波動率（HV）代理
    try:
        from services.market_data_service import get_history_df
        import pandas as pd
        import numpy as np

        df_temp = await get_history_df(symbol, period="1mo")
        if not df_temp.empty and len(df_temp) >= 2:
            log_ret = np.log(df_temp["Close"] / df_temp["Close"].shift(1))
            hv = float(log_ret.std() * np.sqrt(252))
            if not pd.isna(hv) and hv > 0:
                logger.info(f"[{symbol}] Fallback 2: 以 30 日 HV ({hv}) 代理。")
                return hv
    except Exception as hv_err:
        logger.error(f"[{symbol}] Fallback 2 計算失敗: {hv_err}")

    return None


async def save_historical_iv(symbol: str, iv: float, date_str: str) -> Any:
    """將每日 IV 存入 database。"""
    try:
        from database.connection import execute_write_async

        resolved = await _resolve_iv_for_storage(symbol, iv)
        if resolved is None:
            logger.warning(
                f"⚠️ [{symbol}] 所有 IV fallback 均失敗，略過寫入 historical_iv "
                "以避免 NOT NULL 約束錯誤。"
            )
            return

        await execute_write_async(
            """
            INSERT OR REPLACE INTO historical_iv (symbol, iv, date)
            VALUES (?, ?, ?)
            """,
            (symbol, resolved, date_str),
        )
    except Exception as e:
        logger.error(f"儲存歷史 IV 失敗: {e}")
