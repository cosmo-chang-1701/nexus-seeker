"""共用的當前交易時段 Session VWAP 計算 helper。

比照 atr_utils.py 的呼叫慣例，供 symbol_deep_dive.py（/x symbol: 深度分析）使用。
"""

import logging
from datetime import date
from typing import NamedTuple, Optional

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


class SessionStats(NamedTuple):
    """同一份當日 15m K 線推導出的 Session VWAP 與區間極值。

    VWAP 與高低點刻意出自**同一份** K 線：`get_quote` 的 Tier 0 走 Alpaca IEX
    單一交易所成交，其日內高低點比全市場窄；若 VWAP（yfinance 全市場）與日高低
    （IEX）各自取數，就會出現「VWAP 高於當日最高價」這種數學上不可能的組合。
    """

    vwap: float
    high: float
    low: float
    volume: float
    bar_count: int
    session_date: Optional[date]


async def fetch_session_stats(
    symbol: str, force_refresh: bool = True
) -> Optional[SessionStats]:
    """與 `fetch_session_vwap()` 同一份資料，額外回傳區間極值與總量；失敗回傳 None。"""
    try:
        df_1d = await market_data_service.get_history_df(
            symbol, period="1d", interval="15m", force_refresh=force_refresh
        )
        if df_1d is None or df_1d.empty:
            return None

        total_volume = float(df_1d["Volume"].sum())
        if total_volume <= 0:
            return None

        typical_price = (df_1d["High"] + df_1d["Low"] + df_1d["Close"]) / 3.0
        vwap = float((typical_price * df_1d["Volume"]).sum() / total_volume)

        last_idx = df_1d.index[-1]
        session_date = (
            last_idx.to_pydatetime().date()
            if hasattr(last_idx, "to_pydatetime")
            else None
        )
        return SessionStats(
            vwap=vwap,
            high=float(df_1d["High"].max()),
            low=float(df_1d["Low"].min()),
            volume=total_volume,
            bar_count=len(df_1d),
            session_date=session_date,
        )
    except Exception as e:
        logger.warning(f"[{symbol}] Session 統計計算失敗: {e}")
        return None
