"""共用的真實 15 分鐘 K 棒 ATR(14) 計算 helper。

供 symbol_deep_dive.py（/x symbol: 深度分析）與 radar_data.py（15 分鐘心跳雷達
快取）共用同一份實作，避免兩處各自維護一份逐字相同的 pandas_ta.atr 計算邏輯。
"""

import logging

from services import market_data_service

logger = logging.getLogger(__name__)


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
        if df_15m is None or df_15m.empty or len(df_15m) < 14:
            return 0.0
        import pandas_ta as ta

        atr_series = ta.atr(df_15m["High"], df_15m["Low"], df_15m["Close"], length=14)
        if atr_series is None or atr_series.empty:
            return 0.0
        return float(atr_series.iloc[-1])
    except Exception as e:
        logger.warning(f"[{symbol}] ATR_15m 計算失敗: {e}")
        return 0.0
