"""共用的真實 15 分鐘 K 棒 ATR(14) 計算 helper。

供 symbol_deep_dive.py（/x symbol: 深度分析）與 radar_data.py（15 分鐘心跳雷達
快取）共用同一份實作，避免兩處各自維護一份逐字相同的 pandas_ta.atr 計算邏輯。
"""

import logging
from typing import Any, Optional

from services import market_data_service

logger = logging.getLogger(__name__)


def compute_atr_15m_from_df(df_15m: Optional[Any]) -> float:
    """從既有的 15m K 線 DataFrame 直接計算 ATR(14)，不發動任何抓取。

    供已經持有同一份 `period="5d", interval="15m"` frame 的呼叫端
    (dynamic_rollover 左側進場鐵律條件一、Regime 分類器) 重用——它們原本各自
    再呼叫一次 `fetch_atr_15m()`，會對同一標的重複發動 force_refresh 的真實
    網路請求，且可能取到與手上 K 棒不同快照的 ATR。改為就地計算後，ATR 與
    K 棒資料在建構上保證同源。

    資料不足或任何例外一律 fail-safe 回傳 0.0，語意與 `fetch_atr_15m()` 一致。
    """
    try:
        if df_15m is None or df_15m.empty or len(df_15m) < 14:
            return 0.0
        import pandas_ta as ta

        atr_series = ta.atr(df_15m["High"], df_15m["Low"], df_15m["Close"], length=14)
        if atr_series is None or atr_series.empty:
            return 0.0
        return float(atr_series.iloc[-1])
    except Exception as e:
        logger.warning(f"ATR_15m 就地計算失敗: {e}")
        return 0.0


async def fetch_atr_15m(symbol: str, force_refresh: bool = True) -> float:
    """計算真正的 15 分鐘 K 棒 ATR(14)，供防洗盤停損參考使用。

    force_refresh=True：ATR_15m 的價值建立在盤中即時性上，沿用
    get_history_df() docstring 建議的短週期新鮮度模式（見 15 分鐘價量警報）。
    資料不足或任何例外一律 fail-safe 回傳 0.0，交由呼叫端視為資料缺失。
    """
    try:
        df_15m = await market_data_service.get_history_df(
            symbol, period="5d", interval="15m", force_refresh=force_refresh
        )
        return compute_atr_15m_from_df(df_15m)
    except Exception as e:
        logger.warning(f"[{symbol}] ATR_15m 計算失敗: {e}")
        return 0.0


def compute_atr_14_from_daily_df(df_daily: Optional[Any]) -> float:
    """從既有的日線 K 線 DataFrame 直接計算 ATR(14)，不發動任何抓取。

    供 `intraday_pipeline/metrics.py::build_enhanced_watchlist_metrics()` 重用它
    本來就已經抓好的 `period="1y"` 日線 frame——該處原本把 `atr_14` 寫死為 0.01
    佔位值（`_calculate_technical_indicators()` 並不回傳 ATR），導致下游宣稱的
    「1.5x ATR 防洗盤緩衝」實際只有 $0.015、`dynamic_grid_step` 恆為 0.01。
    就地計算後 ATR 與該 frame 保證同源，且零額外網路成本。

    與 `compute_atr_15m_from_df()` 刻意維持相同的 fail-safe 語意：資料不足或
    任何例外一律回傳 0.0，交由呼叫端決定佔位值。
    """
    try:
        if df_daily is None or df_daily.empty or len(df_daily) < 14:
            return 0.0
        import pandas_ta as ta

        atr_series = ta.atr(
            df_daily["High"], df_daily["Low"], df_daily["Close"], length=14
        )
        if atr_series is None or atr_series.empty:
            return 0.0
        atr_val = float(atr_series.iloc[-1])
        if atr_val != atr_val or atr_val <= 0.0:  # NaN 或非正值
            return 0.0
        return atr_val
    except Exception as e:
        logger.warning(f"日線 ATR(14) 就地計算失敗: {e}")
        return 0.0


async def fetch_atr_1d(symbol: str) -> float:
    """計算日線 K 棒 ATR(14)，供動態自適應空間門檻的單日波幅項使用。

    與 `fetch_atr_15m()` 刻意分開的兩點：
      1. **不 force_refresh**。日線 ATR 的量級在盤中幾乎不動，`get_history_df()`
         對 `period="1y", interval="1d"` 的既有快取足以覆蓋整個交易日；每個候選
         標的都強制重抓一年份日線是純粹的浪費。
      2. 抓取區間沿用 repo 既有的 `period="1y"` 慣例（見
         `intraday_pipeline/metrics.py` 與 `cogs/unified_terminal/radar_data.py`），
         不另立新的視窗長度。

    呼叫端應優先沿用手上已有的 `atr_14`（radar 快取、EnhancedWatchlistMetrics
    等皆已攜帶），只有真的取不到才呼叫本函式。資料不足或任何例外一律 fail-safe
    回傳 0.0，語意與 `fetch_atr_15m()` 一致。
    """
    try:
        df_daily = await market_data_service.get_history_df(
            symbol, period="1y", interval="1d"
        )
        return compute_atr_14_from_daily_df(df_daily)
    except Exception as e:
        logger.warning(f"[{symbol}] ATR_1D 計算失敗: {e}")
        return 0.0


def compute_high_60d_from_daily_df(df_daily: Optional[Any]) -> float:
    """從既有的日線 K 線 DataFrame 直接計算「前 60 個交易日最高價」，不發動任何抓取。

    供 `room_threshold.py::resolve_effective_target()`（晴空萬里天花板，見
    `docs/strategies/06_dynamic_adaptive_room_threshold.md` 公式 D）使用：
    標的創新高時，上方沒有存量 OI 可形成有效 Call Wall，此時需要以 60 日高點
    搭配 ATR 外推的動態目標取代裸 Call Wall。

    `shift(1)` 排除當日高點——防前視偏差，若不 shift，當日盤中創新高的那一根
    K 棒會把自己算進「歷史高點」，等於用未來資料驗證自己突破成立。

    需要至少 61 根日線（60 根供 rolling window + 1 根供 shift 位移）；資料不足
    或任何例外一律 fail-safe 回傳 0.0，交由呼叫端的 `max()` 天花板邏輯自動剔除
    此項、退回裸 Call Wall。
    """
    try:
        if df_daily is None or df_daily.empty or len(df_daily) < 61:
            return 0.0
        high_60d = df_daily["High"].shift(1).rolling(window=60).max().iloc[-1]
        if high_60d != high_60d or high_60d <= 0.0:  # NaN 或非正值
            return 0.0
        return float(high_60d)
    except Exception as e:
        logger.warning(f"60 日高點就地計算失敗: {e}")
        return 0.0


async def fetch_high_60d(symbol: str) -> float:
    """計算前 60 個交易日最高價，供晴空萬里天花板的有效目標計算使用。

    與 `fetch_atr_1d()` 共用同一份日線抓取慣例：`period="1y"` 不 `force_refresh`
    （60 日高點在盤中同樣幾乎不動）。呼叫端若已持有同一份 `period="1y"` 日線
    frame（例如已呼叫過 `fetch_atr_1d()` 的路徑），應優先直接呼叫
    `compute_high_60d_from_daily_df()` 重用該 frame，避免重複抓取；只有真的
    取不到既有 frame 時才呼叫本函式。資料不足或任何例外一律 fail-safe
    回傳 0.0，語意與 `fetch_atr_1d()` 一致。
    """
    try:
        df_daily = await market_data_service.get_history_df(
            symbol, period="1y", interval="1d"
        )
        return compute_high_60d_from_daily_df(df_daily)
    except Exception as e:
        logger.warning(f"[{symbol}] 60 日高點計算失敗: {e}")
        return 0.0
