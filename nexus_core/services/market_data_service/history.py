"""market_data_service：歷史 K 線數據與衍生指標 (SMA/EMA)。"""

from typing import Optional
import asyncio
import logging
import time

import pandas as pd
import numpy as np
import yfinance as yf

from services.market_data_service._core import (
    _is_interactive_request,
    _to_yfinance_symbol,
    _sanitize_ticker,
    call_yf,
)
from services.market_data_service.caches import (
    _ema_cache,
    _EMA_CACHE_TTL,
    _history_cache,
    _INTRADAY_BAR_SECONDS,
    _sma_cache,
    _SMA_CACHE_TTL,
    history_cache_expiry,
)
from services.market_data_service.quote import _safe_yf_history

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 併發請求合併 (single-flight)
# ---------------------------------------------------------------------------
# **為什麼 TTL 快取不足以取代 single-flight**：`_history_cache` 的寫入發生在
# `await` 之後，因此「查快取 → 發請求 → 寫快取」之間隔著一整段網路等待。在 t=0
# 一起建立的多個 task 會雙雙 miss 快取並發出完全相同的請求。TTL 快取消除的是
# 「跨輪次」重複，single-flight 消除的是「同輪次併發」重複，兩者不互相取代。
#
# 已知的併發同 key 呼叫點（此表為說明用；修復是通用的，不需逐點改動）：
#   * `cogs/unified_terminal/symbol_deep_dive.py`：`df_hist_task` 與（改版前的）
#     `fetch_atr_1d` 同為 `(sym, "1y", "1d")`
#   * `market_analysis/strategy/analyze.py`：同一個 gather 內 `symbol` 與
#     `"SPY"` 各取一次 `"1y"`——`symbol == "SPY"` 時是同一個 key
#   * `market_analysis/intraday_pipeline/metrics.py`：`get_history_df(symbol,
#     "1y")` 與同輪的 `get_sma()` / `get_ema()`（兩者內部亦走 `"1y"`）
#   * `services/market_data_service/fundamentals.py`：`^VIX 5d` 在三個函式中各
#     取一次，宏觀環境組裝時會併發
#   * 08:45 盤前預熱：`ddp_inspector` / `iv_metrics` / `volatility_inspector`
#     對同一標的的 `"1y"` 日線
#
# 調度沿用既有的 `services/single_flight.py::SingleFlightManager`（`get_option_chain`
# 與 `index_microstructure` 等既有呼叫端同一套機制），不另造一份 in-flight 表。


def _history_single_flight_key(symbol: str, period: str, interval: str) -> str:
    """SingleFlight key 與 `_history_cache` 的 cache key 同構。

    ⚠️ `force_refresh` **刻意不入 key**：它只略過快取讀取，抓取的目標與資料源
    完全相同，飛行中的那一次本身就是「現在」發出的請求，已滿足新鮮度要求。這與
    `options.py::get_option_chain` 把 `force_live` 併入 key 的作法相反——那裡的
    `force_live` 會切換資料源分層（Edge Snapshot vs 直連），共乘會拿到不同語意的
    資料，此處沒有這個分歧。
    """
    return f"hist_df_{symbol}_{period}_{interval}"


# ---------------------------------------------------------------------------
# 歷史數據與指標 (yfinance)
# ---------------------------------------------------------------------------
async def _fetch_history_uncached(
    symbol: str, period: str, interval: str, cache_key: tuple[str, str, str]
) -> pd.DataFrame:
    """實際發動 yfinance 抓取並寫入 `_history_cache`。

    刻意與 `get_history_df()` 分離，讓後者只負責「快取 / 共乘」的協調；本函式
    沿用原有的 fail-safe 語意——任何例外都記 log 並回空 DataFrame，不外拋（所有
    呼叫端都是盤中熱路徑）。
    """
    now = time.time()
    try:
        ticker = yf.Ticker(symbol)
        df = await _safe_yf_history(ticker, period=period, interval=interval)

        if df is None or getattr(df, "empty", True):
            logger.warning(
                f"[{symbol}] yfinance 歷史數據為空 (period={period}, interval={interval})"
            )
            # 空結果**不寫入快取**：暫時性失敗 (限流/Edge 斷線) 若被快取，
            # 期間所有呼叫端 (含 VIX、盤後 NAV) 都會拿到「無資料」而退回備援值。
            return pd.DataFrame()

        df.index.name = "Date"
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        result_df = df[["Open", "High", "Low", "Close", "Volume"]]
        _history_cache[cache_key] = (
            result_df.copy(),
            history_cache_expiry(interval, period, now),
        )
        return result_df
    except Exception as e:
        logger.error(f"[{symbol}] yfinance 抓取失敗: {e}")
        return pd.DataFrame()


# stale-on-error 僅限日線以上；24 小時：日線最後一根最多落後一個交易日，
# 超過就可能跨了除權息／拆股而不可信。
_STALE_OK_INTERVALS: frozenset[str] = frozenset({"1d", "5d", "1wk", "1mo", "3mo"})
_STALE_MAX_OVERDUE_SECONDS = 86400


async def get_history_df(
    symbol: str, period: str = "1y", interval: str = "1d", force_refresh: bool = False
) -> pd.DataFrame:
    """
    使用 yfinance 抓取歷史 K 線 (異步化，支援依 interval／交易時段決定期限的快取、
    併發請求合併與 Copy 隔離)。

    快取期限由 `caches.history_cache_expiry` 決定：盤中 intraday 對齊下一根 bar
    收盤＋60 秒、短期日線 15 分鐘、收盤後 30 分內 5 分鐘、盤外到次一交易日
    08:30 ET；僅盤中的指標用長週期日線維持 6 小時。

    `force_refresh=True` 會略過快取讀取（但仍會將新結果寫入快取供其他呼叫端
    受益）。**背景路徑的 intraday（< 1d）`force_refresh` 會被忽略**：bar 對齊到期
    已保證 bar 級新鮮度，同一輪多個模組各自強刷只會重複消耗 Yahoo 配額；互動
    （`/x`，已標記 interactive）與日線 `force_refresh` 行為不變。
    **`force_refresh` 仍會與飛行中的請求共乘**，理由見 `_history_single_flight_key`。

    抓取失敗（回空）時，日線以上若有逾期 < 24 小時的快取則沿用並記 warning；
    intraday 不做 stale 回退（跨 bar 的舊 K 棒比沒有資料更危險），維持回空。
    """
    from services.single_flight import SingleFlightManager

    symbol = _to_yfinance_symbol(symbol)
    cache_key = (symbol, period, interval)
    now = time.time()

    # A2：背景 intraday 的 force_refresh 交由 bar 對齊快取承接（單一改動點）
    if (
        force_refresh
        and interval in _INTRADAY_BAR_SECONDS
        and not _is_interactive_request.get()
    ):
        force_refresh = False

    if not force_refresh and cache_key in _history_cache:
        cached_df, expiry = _history_cache[cache_key]
        if now < expiry:
            return cached_df.copy()

    # 取消隔離由 SingleFlightManager.run() 內建的 shield 負責（任一呼叫端被取消
    # 不會中止共享任務），呼叫端不需再包一層。
    shared_df = await SingleFlightManager.run(
        _history_single_flight_key(symbol, period, interval),
        _fetch_history_uncached,
        symbol,
        period,
        interval,
        cache_key,
    )
    if shared_df is None or shared_df.empty:
        # stale-on-error：日線以上沿用逾期 < 24 小時的快取（不改 (df, expiry) 形狀）
        if interval in _STALE_OK_INTERVALS and cache_key in _history_cache:
            stale_df, expiry = _history_cache[cache_key]
            overdue = now - expiry
            if overdue <= _STALE_MAX_OVERDUE_SECONDS:
                logger.warning(
                    f"[{symbol}] 抓取失敗，沿用過期日線快取（逾期 {max(overdue, 0):.0f} 秒）"
                )
                return stale_df.copy()
        return pd.DataFrame()
    # 共乘者全部共用同一個 DataFrame 物件，因此一律回傳副本以維持 Copy 隔離契約。
    return shared_df.copy()


async def get_spy_history_df(
    period: str = "1y", interval: str = "1d", retries: int = 3
) -> pd.DataFrame:
    """取得 SPY 基準歷史資料，針對暫時性鎖衝突進行重試。"""
    for attempt in range(retries):
        df = await get_history_df("SPY", period=period, interval=interval)
        if not df.empty:
            return df
        await asyncio.sleep(0.4 * (attempt + 1))

    logger.error(f"[SPY] 重試 {retries} 次後仍無法取得歷史資料")
    return pd.DataFrame()


async def get_stock_splits(symbol: str) -> pd.Series:
    """取得標的的拆股歷史資料。"""
    symbol = _sanitize_ticker(symbol)
    try:
        ticker = yf.Ticker(symbol)
        splits = await call_yf(lambda: ticker.splits)
        if splits is None:
            return pd.Series(dtype=float)
        return splits
    except Exception as e:
        logger.error(f"[{symbol}] yfinance 獲取拆股歷史失敗: {e}")
        return pd.Series(dtype=float)


async def get_sma(symbol: str, window: int = 200) -> Optional[float]:
    """計算簡單移動平均線 (SMA)。"""
    current_time = time.time()
    cache_key = (symbol, window)

    if cache_key in _sma_cache:
        cached_val, expiry = _sma_cache[cache_key]
        if current_time < expiry:
            return cached_val  # type: ignore

    try:
        period = "1y" if window <= 200 else "2y"
        df = await get_history_df(symbol, period=period)

        if df.empty or len(df) < window:
            return None

        sma_series = df["Close"].rolling(window=window).mean()
        current_sma = round(float(sma_series.iloc[-1]), 4)

        if not pd.isna(current_sma):
            _sma_cache[cache_key] = (current_sma, current_time + _SMA_CACHE_TTL)

        return current_sma if not pd.isna(current_sma) else None
    except Exception as e:
        logger.error(f"[{symbol}] 計算 SMA{window} 失敗: {e}")
        return None


async def get_ema(symbol: str, window: int = 21) -> Optional[float]:
    """計算指數移動平均線 (EMA)。"""
    now = time.time()
    cache_key = (symbol, window)

    if cache_key in _ema_cache:
        val, expiry = _ema_cache[cache_key]
        if now < expiry:
            return val  # type: ignore

    try:
        period = "1mo" if window <= 21 else "1y"
        df = await get_history_df(symbol, period=period)

        if df.empty or len(df) < window:
            return None

        ema_series = df["Close"].ewm(span=window, adjust=False).mean()
        current_ema = round(float(ema_series.iloc[-1]), 4)

        if not np.isnan(current_ema):
            _ema_cache[cache_key] = (current_ema, now + _EMA_CACHE_TTL)
        return current_ema if not np.isnan(current_ema) else None
    except Exception as e:
        logger.error(f"[{symbol}] EMA{window} 計算失敗: {e}")
        return None
