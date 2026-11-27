"""週線 RSI(14) 與日線超賣重返狀態計算（僅呈現與前向紀錄，不參與任何策略判定）。

本模組為純函式、零 I/O 葉模組，重用 `squeeze_entry.timeframes` 的已收盤切分與週線
重採樣函式，避免重複抓檔或跨時區偏移（`df_daily` 索引須為 tz-naive US/Eastern）。

規格：`docs/strategies/10_multi_timeframe_squeeze_entry.md` §5.y、
`docs/architecture/05_calibration_harness_and_forward_collection.md` §5.17。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from market_analysis.squeeze_entry.timeframes import (
    resample_weekly,
    split_daily_confirmed,
)

# ---------------------------------------------------------------------------
# 具名常數（PRE_CALIBRATION／僅呈現與前向紀錄，不參與判定）
# ---------------------------------------------------------------------------
RSI_LENGTH: int = 14  # PRE_CALIBRATION／僅呈現與前向紀錄，不參與判定
WEEKLY_RSI_OVERSOLD: float = 30.0  # PRE_CALIBRATION／僅呈現與前向紀錄，不參與判定
WEEKLY_RSI_NEAR_OVERSOLD: float = 35.0  # PRE_CALIBRATION／僅呈現與前向紀錄，不參與判定
WEEKLY_RSI_OVERBOUGHT: float = 70.0  # PRE_CALIBRATION／僅呈現與前向紀錄，不參與判定
DAILY_RSI_OVERSOLD: float = 30.0  # PRE_CALIBRATION／僅呈現與前向紀錄，不參與判定
DAILY_REENTRY_LOOKBACK_BARS: int = 5  # PRE_CALIBRATION／僅呈現與前向紀錄，不參與判定
DAILY_REENTRY_SMA_LENGTH: int = 20  # PRE_CALIBRATION／僅呈現與前向紀錄，不參與判定

# 週線 RSI 狀態
WEEKLY_OVERSOLD: str = "OVERSOLD"
WEEKLY_NEAR_OVERSOLD: str = "NEAR_OVERSOLD"
WEEKLY_NORMAL: str = "NORMAL"
WEEKLY_OVERBOUGHT: str = "OVERBOUGHT"
WEEKLY_NO_DATA: str = "NO_DATA"

# 日線重返狀態
REENTRY_TRIGGERED: str = "TRIGGERED"
REENTRY_OVERSOLD_WATCH: str = "OVERSOLD_WATCH"
REENTRY_NONE: str = "NONE"
REENTRY_NO_DATA: str = "NO_DATA"


@dataclass(frozen=True)
class RsiReentryStatus:
    """週線 RSI(14) 與日線重返狀態摘要（僅供顯示與前向紀錄）。"""

    weekly_rsi: Optional[float]
    weekly_rsi_live: Optional[float]
    weekly_state: str
    weekly_is_oversold: Optional[bool]
    daily_rsi: Optional[float]
    daily_prev_rsi: Optional[float]
    daily_reentry_state: str
    daily_above_sma20: Optional[bool]
    daily_reentry_bars_since_oversold: Optional[int]


def _no_data_status() -> RsiReentryStatus:
    return RsiReentryStatus(
        weekly_rsi=None,
        weekly_rsi_live=None,
        weekly_state=WEEKLY_NO_DATA,
        weekly_is_oversold=None,
        daily_rsi=None,
        daily_prev_rsi=None,
        daily_reentry_state=REENTRY_NO_DATA,
        daily_above_sma20=None,
        daily_reentry_bars_since_oversold=None,
    )


def _wilder_rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss <= 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    if avg_gain <= 0.0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def wilder_rsi_series(closes: pd.Series, length: int = RSI_LENGTH) -> pd.Series:
    """計算 Wilder RSI 序列（與 TradingView 及 `advisor_backtest` 一致）。

    以前 `length` 根價差的簡單平均作為初始種子，後續以 `(prev * (length - 1) + curr) / length`
    遞迴平滑；零漲跌平盤回傳 50.0，純漲回傳 100.0，純跌回傳 0.0，NaN 位置保留為 NaN。
    """
    if length <= 0:
        raise ValueError(f"RSI 期數須為正整數，收到: {length}")
    if closes.empty:
        return pd.Series(dtype=float, index=closes.index)

    numeric = pd.to_numeric(closes, errors="coerce").to_numpy(dtype=float)
    out = np.full(len(numeric), np.nan, dtype=float)
    valid_mask = np.isfinite(numeric)
    valid_vals = numeric[valid_mask]
    if len(valid_vals) <= length:
        return pd.Series(out, index=closes.index, dtype=float)

    deltas = np.diff(valid_vals)
    gains = np.where(deltas > 0.0, deltas, 0.0)
    losses = np.where(deltas < 0.0, -deltas, 0.0)

    avg_gain = float(gains[:length].mean())
    avg_loss = float(losses[:length].mean())

    valid_out = np.full(len(valid_vals), np.nan, dtype=float)
    valid_out[length] = _wilder_rsi_value(avg_gain, avg_loss)
    for j in range(length, len(deltas)):
        avg_gain = (avg_gain * (length - 1) + float(gains[j])) / length
        avg_loss = (avg_loss * (length - 1) + float(losses[j])) / length
        valid_out[j + 1] = _wilder_rsi_value(avg_gain, avg_loss)

    out[valid_mask] = valid_out
    return pd.Series(out, index=closes.index, dtype=float)


def _classify_weekly_state(weekly_rsi: float) -> str:
    if weekly_rsi <= WEEKLY_RSI_OVERSOLD:
        return WEEKLY_OVERSOLD
    if weekly_rsi <= WEEKLY_RSI_NEAR_OVERSOLD:
        return WEEKLY_NEAR_OVERSOLD
    if weekly_rsi >= WEEKLY_RSI_OVERBOUGHT:
        return WEEKLY_OVERBOUGHT
    return WEEKLY_NORMAL


def _latest_finite_rsi(series: pd.Series) -> Optional[float]:
    if series.empty:
        return None
    val = series.iloc[-1]
    if not isinstance(val, (int, float, np.floating, np.integer)):
        return None
    f = float(val)
    return round(f, 2) if math.isfinite(f) else None


def compute_rsi_reentry(
    df_daily: Optional[pd.DataFrame], today: date
) -> RsiReentryStatus:
    """從已抓好的日線 DataFrame 計算週線 RSI(14) 與日線重返狀態（零 I/O）。"""
    if (
        df_daily is None
        or df_daily.empty
        or "Close" not in df_daily.columns
        or not isinstance(df_daily.index, pd.DatetimeIndex)
    ):
        return _no_data_status()

    clean_df = (
        df_daily.loc[df_daily.index.notna()] if df_daily.index.hasnans else df_daily
    )
    if clean_df.empty or not hasattr(clean_df.index[-1], "date"):
        return _no_data_status()

    # 1. 週線 RSI(14)：只用已收盤週線判定，若本週尚未收盤則另算即時預覽
    w_conf, w_live = resample_weekly(clean_df, today)
    weekly_rsi: Optional[float] = None
    weekly_state = WEEKLY_NO_DATA
    weekly_is_oversold: Optional[bool] = None
    if len(w_conf) >= RSI_LENGTH + 1:
        w_rsi_series = wilder_rsi_series(w_conf["Close"], RSI_LENGTH)
        weekly_rsi = _latest_finite_rsi(w_rsi_series)
        if weekly_rsi is not None:
            weekly_is_oversold = weekly_rsi <= WEEKLY_RSI_OVERSOLD
            weekly_state = _classify_weekly_state(weekly_rsi)

    weekly_rsi_live: Optional[float] = None
    if w_live:
        w_full, _ = resample_weekly(clean_df, today + timedelta(days=7))
        if len(w_full) >= RSI_LENGTH + 1:
            weekly_rsi_live = _latest_finite_rsi(
                wilder_rsi_series(w_full["Close"], RSI_LENGTH)
            )

    # 2. 日線重返狀態：只用已收盤日線（排除今日成型中 K 棒）
    d_conf, _ = split_daily_confirmed(clean_df, today)
    d_closes = pd.to_numeric(d_conf["Close"], errors="coerce").dropna()
    daily_above_sma20: Optional[bool] = None
    if len(d_closes) >= DAILY_REENTRY_SMA_LENGTH:
        sma20 = float(d_closes.iloc[-DAILY_REENTRY_SMA_LENGTH:].mean())
        if math.isfinite(sma20):
            daily_above_sma20 = bool(float(d_closes.iloc[-1]) > sma20)

    daily_rsi: Optional[float] = None
    daily_prev_rsi: Optional[float] = None
    daily_reentry_state = REENTRY_NO_DATA
    bars_since_oversold: Optional[int] = None

    if len(d_conf) >= RSI_LENGTH + 2:
        d_rsi_valid = wilder_rsi_series(d_conf["Close"], RSI_LENGTH).dropna()
        if len(d_rsi_valid) >= 2:
            curr_rsi = round(float(d_rsi_valid.iloc[-1]), 2)
            prev_rsi = round(float(d_rsi_valid.iloc[-2]), 2)
            if math.isfinite(curr_rsi) and math.isfinite(prev_rsi):
                daily_rsi = curr_rsi
                daily_prev_rsi = prev_rsi
                if curr_rsi <= DAILY_RSI_OVERSOLD:
                    daily_reentry_state = REENTRY_OVERSOLD_WATCH
                elif curr_rsi > prev_rsi:
                    for offset in range(1, DAILY_REENTRY_LOOKBACK_BARS + 1):
                        if offset + 1 > len(d_rsi_valid):
                            break
                        past_rsi = round(float(d_rsi_valid.iloc[-1 - offset]), 2)
                        if past_rsi <= DAILY_RSI_OVERSOLD:
                            daily_reentry_state = REENTRY_TRIGGERED
                            bars_since_oversold = offset
                            break
                    if daily_reentry_state == REENTRY_NO_DATA:
                        daily_reentry_state = REENTRY_NONE
                else:
                    daily_reentry_state = REENTRY_NONE

    return RsiReentryStatus(
        weekly_rsi=weekly_rsi,
        weekly_rsi_live=weekly_rsi_live,
        weekly_state=weekly_state,
        weekly_is_oversold=weekly_is_oversold,
        daily_rsi=daily_rsi,
        daily_prev_rsi=daily_prev_rsi,
        daily_reentry_state=daily_reentry_state,
        daily_above_sma20=daily_above_sma20,
        daily_reentry_bars_since_oversold=bars_since_oversold,
    )


_WEEKLY_BADGE: dict[str, str] = {
    WEEKLY_OVERSOLD: "🟢超賣",
    WEEKLY_NEAR_OVERSOLD: "🟡近超賣",
    WEEKLY_OVERBOUGHT: "🔴過熱",
}

_REENTRY_LABEL: dict[str, str] = {
    REENTRY_TRIGGERED: "🟢觸發重返",
    REENTRY_OVERSOLD_WATCH: "🟡超賣觀察",
    REENTRY_NONE: "無訊號",
    REENTRY_NO_DATA: "資料不足",
}


def format_rsi_reentry_line(status: Optional[RsiReentryStatus]) -> Optional[str]:
    """回傳供 `/x` 面板顯示的單行繁體中文摘要（無 ANSI 色碼，資料全缺時回傳 None）。"""
    if status is None:
        return None
    if (
        status.weekly_state == WEEKLY_NO_DATA
        and status.daily_reentry_state == REENTRY_NO_DATA
    ):
        return None

    if status.weekly_rsi is None or status.weekly_state == WEEKLY_NO_DATA:
        weekly_part = "週線 RSI(14): 資料不足"
    else:
        badge = _WEEKLY_BADGE.get(status.weekly_state)
        if badge:
            weekly_part = f"週線 RSI(14): {status.weekly_rsi:.1f} ({badge})"
        else:
            weekly_part = f"週線 RSI(14): {status.weekly_rsi:.1f}"

    reentry_label = _REENTRY_LABEL.get(status.daily_reentry_state, "資料不足")
    return f"{weekly_part} │ 日線重返: {reentry_label}"
