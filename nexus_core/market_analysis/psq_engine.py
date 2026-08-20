import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from dataclasses import dataclass
from typing import Optional


@dataclass
class PSQResult:
    squeeze_level: (
        str  # "High" (Red), "Mid" (Orange), "Normal" (Pink), "Release" (Gray)
    )
    is_squeezing: bool  # 是否處於任何形式的擠壓狀態
    momentum_value: float
    momentum_color: str  # "LightBlue", "DarkBlue", "Red", "Golden", "Neutral"
    signal_direction: str  # "Long", "Short", "Neutral"
    is_near_support: bool
    is_breakout_long: bool  # 多頭能量釋放突破
    is_breakout_short: bool  # 空頭能量釋放突破
    sma_distance_pct: float
    sma_20: float  # 20SMA 價格
    # VIX 戰情標記
    vix_momentum_label: str = "NORMAL"  # VIX 短期動能標籤

    @property
    def is_breakout_high(self) -> bool:
        return self.is_breakout_long

    @property
    def is_breakout_low(self) -> bool:
        return self.is_breakout_short


def _fast_ema(series: pd.Series, length: int) -> pd.Series:
    """TA-Lib 相容之 EMA 計算（前 length 筆以 SMA 初始化）。"""
    s = series.copy()
    sma = s.iloc[0:length].mean()
    s.iloc[: length - 1] = np.nan
    s.iloc[length - 1] = sma
    return s.ewm(span=length, adjust=False).mean()


def _fast_rolling_linreg(series: pd.Series, length: int = 20) -> pd.Series:
    """TA-Lib 相容之滑動線性回歸終點值 (Vectorized sliding window linear regression)。"""
    vals = series.to_numpy(dtype=np.float64)
    n = len(vals)
    if n < length:
        return pd.Series(np.full(n, np.nan), index=series.index)

    x = np.arange(1, length + 1, dtype=np.float64)
    x_sum = 0.5 * length * (length + 1)
    x2_sum = x_sum * (2 * length + 1) / 3.0
    divisor = length * x2_sum - x_sum * x_sum

    windows = sliding_window_view(vals, length)
    y_sum = np.sum(windows, axis=1)
    xy_sum = np.sum(windows * x, axis=1)
    m = (length * xy_sum - x_sum * y_sum) / divisor
    b = (y_sum * x2_sum - x_sum * xy_sum) / divisor
    endpoints = m * length + b

    out = np.full(n, np.nan, dtype=np.float64)
    out[length - 1 :] = endpoints
    return pd.Series(out, index=series.index)


def analyze_psq(
    df: pd.DataFrame,
    length: int = 20,
    bb_mult: float = 2.0,
    kc_mults: list = [1.0, 1.5, 2.0],
    near_pct: float = 1.5,
    vix_spot: float | None = None,
) -> Optional[PSQResult]:
    """
    計算 PowerSqueeze (PSQ) 量化指標 (Ultimate Edition v2 - Vectorized High Performance)。
    輸入資料為包含 'Open', 'High', 'Low', 'Close' 的 DataFrame。

    Args:
        vix_spot: VIX 即時價格。用於動能標記（OVEREXTENDED_RISK / HIGH_CONVICTION_RECOVERY）
                  以及低波環境時間框架建議。
    """
    if df is None or df.empty or len(df) < length * 2:
        return None

    try:
        close = df["Close"]
        high = df["High"]
        low = df["Low"]

        # 1. Bollinger Bands (20, 2)
        basis = close.rolling(length).mean()
        rolling_std = close.rolling(length).std(ddof=1)
        bb_lower = basis - bb_mult * rolling_std
        bb_upper = basis + bb_mult * rolling_std

        # 2. Keltner Channels (using TA-Lib compatible True Range + EMA)
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        tr.iloc[0] = high.iloc[0] - low.iloc[0]

        kc_basis = _fast_ema(close, length)
        band = _fast_ema(tr, length)

        kc1_lower = kc_basis - kc_mults[0] * band
        kc1_upper = kc_basis + kc_mults[0] * band
        kc2_lower = kc_basis - kc_mults[1] * band
        kc2_upper = kc_basis + kc_mults[1] * band
        kc3_lower = kc_basis - kc_mults[2] * band
        kc3_upper = kc_basis + kc_mults[2] * band

        # 判定各級別的擠壓狀態
        sqz_high = (bb_lower > kc1_lower) & (bb_upper < kc1_upper)  # 高強度 (紅)
        sqz_mid = (bb_lower > kc2_lower) & (bb_upper < kc2_upper)  # 中強度 (橘)
        sqz_normal = (bb_lower > kc3_lower) & (bb_upper < kc3_upper)  # 一般強度 (粉)

        is_squeezing = sqz_normal  # 只要 BB 縮入最寬的 2.0 KC 內，即屬擠壓狀態

        # 3. Momentum (線性回歸動能)
        high_max = high.rolling(length).max()
        low_min = low.rolling(length).min()
        avg_price = (high_max + low_min) / 2.0

        momentum_source = close - (avg_price + basis) / 2.0
        momentum_value = _fast_rolling_linreg(momentum_source, length=length)

        if momentum_value is None or momentum_value.isna().all():
            return None

        mom_diff = momentum_value.diff()

        # 4. 回調支撐判定
        # 價格與 20 SMA 的百分比距離
        sma_distance_pct = ((df["Close"] - basis) / basis) * 100
        is_near_support = sma_distance_pct.abs() <= near_pct

        # 取得最後一筆與前一筆狀態作判斷
        curr_mom = momentum_value.iloc[-1]
        prev_mom = momentum_value.iloc[-2] if len(momentum_value) > 1 else 0
        curr_diff = mom_diff.iloc[-1]

        # 判斷動能柱體顏色 (Momentum Histogram)
        if curr_mom > 0:
            mom_color = "LightBlue" if curr_diff > 0 else "DarkBlue"
        elif curr_mom < 0:
            mom_color = "Red" if curr_diff < 0 else "Golden"
        else:
            mom_color = "Neutral"

        # 判斷當前擠壓層級 (Squeeze Level)
        if sqz_high.iloc[-1]:
            squeeze_level = "High"
        elif sqz_mid.iloc[-1]:
            squeeze_level = "Mid"
        elif sqz_normal.iloc[-1]:
            squeeze_level = "Normal"
        else:
            squeeze_level = "Release"

        # 判斷基本訊號 (轉強/轉弱)
        if curr_mom > 0 and curr_mom > prev_mom:
            signal = "Long"
        elif curr_mom < 0 and curr_mom < prev_mom:
            signal = "Short"
        else:
            signal = "Neutral"

        # 判斷是否為「擠壓突破」(Breakout)
        # 前段期間處於「高強度擠壓(Red)」，當前 K 線完全解除擠壓 (Release)
        prev_sqz_high = sqz_high.iloc[-2] if len(sqz_high) > 1 else False
        curr_sqz_any = is_squeezing.iloc[-1]

        is_breakout_long = bool(prev_sqz_high and (not curr_sqz_any) and (curr_mom > 0))
        is_breakout_short = bool(
            prev_sqz_high and (not curr_sqz_any) and (curr_mom < 0)
        )

        # ---------- VIX 動能標記 (VIX Momentum Labeling) ----------
        vix_momentum_label = "NORMAL"

        if vix_spot is not None:
            # 匯入分位數邊界
            from config import VIX_QUANTILE_BOUNDS

            upper_3 = VIX_QUANTILE_BOUNDS.get("upper_3", 24.6)

            # 休兵期間的多頭訊號 → 過度延伸風險
            if vix_spot < 15.0 and signal == "Long":
                vix_momentum_label = "OVEREXTENDED_RISK"

            # 高波動期間的 Golden 柱體（空頭減速）→ 高確信反彈
            elif vix_spot > upper_3 and mom_color == "Golden":
                vix_momentum_label = "HIGH_CONVICTION_RECOVERY"

        # -----------------------------------------------------------

        return PSQResult(
            squeeze_level=squeeze_level,
            is_squeezing=bool(curr_sqz_any),
            momentum_value=float(curr_mom),
            momentum_color=mom_color,
            signal_direction=signal,
            is_near_support=bool(is_near_support.iloc[-1]),
            is_breakout_long=is_breakout_long,
            is_breakout_short=is_breakout_short,
            sma_distance_pct=float(sma_distance_pct.iloc[-1]),
            sma_20=float(basis.iloc[-1]),
            vix_momentum_label=vix_momentum_label,
        )
    except Exception as e:
        import logging

        logging.getLogger(__name__).error(f"PSQ 計算發生錯誤: {e}")
        return None
