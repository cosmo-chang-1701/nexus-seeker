"""共用的當前交易時段 Session VWAP 計算 helper。

比照 atr_utils.py 的呼叫慣例，供 symbol_deep_dive.py（/x symbol: 深度分析）使用。
"""

import logging

from services import market_data_service

logger = logging.getLogger(__name__)


async def fetch_session_vwap(symbol: str, force_refresh: bool = True) -> float:
    """計算當前（或最近一個已結束）交易時段的 Session VWAP。

    使用 period="1d" 而非 ATR/確認K棒共用的 period="5d"：VWAP 的定義本身
    就是單一交易時段內的累積成交量加權均價，period="1d" 讓 yfinance 自然
    只回傳當前/最近一個 session 的 15 分鐘K棒，不需要額外依交易日過濾。

    資料不足、零成交量或任何例外一律 fail-safe 回傳 0.0，交由呼叫端視為
    資料缺失（不應顯示為 $0.00）。
    """
    try:
        df_1d = await market_data_service.get_history_df(
            symbol, period="1d", interval="15m", force_refresh=force_refresh
        )
        if df_1d is None or df_1d.empty:
            return 0.0

        total_volume = float(df_1d["Volume"].sum())
        if total_volume <= 0:
            return 0.0

        typical_price = (df_1d["High"] + df_1d["Low"] + df_1d["Close"]) / 3.0
        vwap = float((typical_price * df_1d["Volume"]).sum() / total_volume)
        return vwap
    except Exception as e:
        logger.warning(f"[{symbol}] Session VWAP 計算失敗: {e}")
        return 0.0
