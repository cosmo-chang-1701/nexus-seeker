"""個股 15 分鐘價量突破警報 — 分析引擎。

核心職責：取得某標的最近一根「已收盤」的 15 分鐘實體 K 線，並計算其相對
20 根均量的放量倍數。與使用者的目標價/方向門檻比對邏輯刻意分離
(`evaluate_watch_trigger`)，因為同一標的可能被多位使用者監測，K 棒資料
只需抓取一次即可供所有使用者共用比對。

注意：不可沿用 `market_analysis/dynamic_rollover/opportunity_cost.py::
_confirm_entry_signal` 的作法直接取用 `df_15m.iloc[-1]` —— 在盤中查詢時，
yfinance 回傳的最後一根 K 棒通常仍在成型中 (尚未收盤)，必須以「起始時間 +
15 分鐘 <= 現在」排除尚未收盤的最後一根。
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

import market_time
from database.price_volume_watch import WatchDirection
from services import market_data_service

logger = logging.getLogger(__name__)

_VOLUME_LOOKBACK_BARS: int = 20  # 放量基準所需回看根數（不含確認根）
_HISTORY_PERIOD: str = "5d"  # 遠低於 Yahoo Finance 對 15m 週期約 60 天的保留上限
_HISTORY_INTERVAL: str = "15m"


@dataclass
class Confirmed15mBar:
    """某標的最近一根已收盤的 15 分鐘 K 棒與其相對均量。"""

    symbol: str
    bar_time: datetime
    close: float
    volume: float
    avg_volume: float  # 前 _VOLUME_LOOKBACK_BARS 根（不含本根）均量
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None


async def get_confirmed_15m_bar(symbol: str) -> Optional[Confirmed15mBar]:
    """抓取並回傳某標的最近一根已收盤的 15 分鐘 K 棒資料。

    強制繞過 `get_history_df` 的 6 小時快取 (`force_refresh=True`)，因為
    15 分鐘週期的排程掃描若沿用該快取，會在 6 小時內重複拿到同一份
    （甚至尚未收盤時的）過期資料。
    """
    try:
        df_15m = await market_data_service.get_history_df(
            symbol,
            period=_HISTORY_PERIOD,
            interval=_HISTORY_INTERVAL,
            force_refresh=True,
        )
    except Exception as e:
        logger.warning(f"[{symbol}] 15m K 線抓取失敗: {e}")
        return None

    if df_15m is None or df_15m.empty or len(df_15m) < _VOLUME_LOOKBACK_BARS + 1:
        return None

    now_ny = datetime.now(market_time.ny_tz).replace(tzinfo=None)
    last_idx = df_15m.index[-1].to_pydatetime()
    # yfinance 15m K 棒索引代表該根的「起始時間」；只有起始時間 + 15 分鐘
    # 已經過去，才代表這根 K 棒真正收盤，避免用尚在成型的即時價格誤觸發。
    is_last_bar_closed = (last_idx + timedelta(minutes=15)) <= now_ny
    confirmed_pos = len(df_15m) - 1 if is_last_bar_closed else len(df_15m) - 2

    if confirmed_pos - _VOLUME_LOOKBACK_BARS < 0:
        return None

    confirmed_bar = df_15m.iloc[confirmed_pos]
    lookback = df_15m.iloc[confirmed_pos - _VOLUME_LOOKBACK_BARS : confirmed_pos]
    avg_volume = float(lookback["Volume"].mean())

    open_val = (
        float(confirmed_bar["Open"])
        if "Open" in confirmed_bar and pd.notna(confirmed_bar["Open"])
        else None
    )
    high_val = (
        float(confirmed_bar["High"])
        if "High" in confirmed_bar and pd.notna(confirmed_bar["High"])
        else None
    )
    low_val = (
        float(confirmed_bar["Low"])
        if "Low" in confirmed_bar and pd.notna(confirmed_bar["Low"])
        else None
    )

    return Confirmed15mBar(
        symbol=symbol,
        bar_time=df_15m.index[confirmed_pos].to_pydatetime(),
        close=float(confirmed_bar["Close"]),
        volume=float(confirmed_bar["Volume"]),
        avg_volume=avg_volume,
        open=open_val,
        high=high_val,
        low=low_val,
    )


def evaluate_watch_trigger(
    bar: Confirmed15mBar,
    target_price: float,
    direction: WatchDirection,
    volume_multiplier: float,
) -> bool:
    """判斷已收盤 K 棒是否同時滿足目標價方向條件與放量條件。"""
    if direction == WatchDirection.ABOVE:
        price_ok = bar.close >= target_price
    else:
        price_ok = bar.close <= target_price

    if volume_multiplier <= 0:
        volume_ok = True
    else:
        volume_ok = (
            bar.avg_volume > 0 and bar.volume >= bar.avg_volume * volume_multiplier
        )
    return price_ok and volume_ok


__all__: list[str] = [
    "Confirmed15mBar",
    "get_confirmed_15m_bar",
    "evaluate_watch_trigger",
]
