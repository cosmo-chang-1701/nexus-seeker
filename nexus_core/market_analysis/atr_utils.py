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
