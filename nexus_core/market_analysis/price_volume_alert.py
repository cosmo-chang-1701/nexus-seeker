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


def trim_to_confirmed_15m_bars(
    df_15m: Optional[pd.DataFrame],
) -> Optional[pd.DataFrame]:
    """將 15m K 線 DataFrame 截斷至「最近一根已收盤 K 棒」為止。

    yfinance 的 15m K 棒索引代表該根的**起始時間**；盤中抓取時最後一根通常
    仍在成型中。任何以 `df.iloc[-1]` 直接取用最後一根的日內邏輯，都會拿到
    尚未收盤的即時價格與**只累積了一部分**的成交量——後者對「縮量」類判定
    尤其危險：一根剛成型的 K 棒必然「縮量」，會讓該類子條件在每根 K 棒的
    前段時間恆為真。

    本函式是 `get_confirmed_15m_bar` 與 `market_analysis/dynamic_rollover/`
    左側進場鐵律、Regime 分類器共用的**單一截斷定義**，避免各處重複實作。

    回傳截斷後的 DataFrame；若截斷後不足 `_VOLUME_LOOKBACK_BARS + 1` 根
    (無法計算均量基準) 則回傳 None。
    """
    if df_15m is None or df_15m.empty:
        return None

    last_idx_raw = df_15m.index[-1]
    if not hasattr(last_idx_raw, "to_pydatetime"):
        # 生產環境的 get_history_df 一律回傳 DatetimeIndex；索引非日期型別代表
        # 無從判定 K 棒是否已收盤，一律 fail-safe 視為資料不可用，而非退回
        # 「假設已收盤」——後者會讓成型中的 K 棒重新溜進判定。
        logger.warning("15m K 線索引非日期型別，無法判定收盤狀態，視為資料不可用")
        return None

    now_ny = datetime.now(market_time.ny_tz).replace(tzinfo=None)
    last_idx = last_idx_raw.to_pydatetime()
    # 只有起始時間 + 15 分鐘已經過去，才代表這根 K 棒真正收盤。
    is_last_bar_closed = (last_idx + timedelta(minutes=15)) <= now_ny
    confirmed_pos = len(df_15m) - 1 if is_last_bar_closed else len(df_15m) - 2

    if confirmed_pos - _VOLUME_LOOKBACK_BARS < 0:
        return None

    return df_15m.iloc[: confirmed_pos + 1]


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

    df_confirmed = trim_to_confirmed_15m_bars(df_15m)
    if df_confirmed is None:
        return None

    confirmed_pos = len(df_confirmed) - 1
    confirmed_bar = df_confirmed.iloc[confirmed_pos]
    lookback = df_confirmed.iloc[confirmed_pos - _VOLUME_LOOKBACK_BARS : confirmed_pos]
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
        bar_time=df_confirmed.index[confirmed_pos].to_pydatetime(),
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
