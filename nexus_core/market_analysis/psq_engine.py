import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from dataclasses import dataclass
from typing import Optional, Tuple


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
    # Green Dot：近 green_dot_lookback 根內由擠壓（任一等級）轉為解除，當前仍為
    # 解除狀態且動能 > 0。與 is_breakout_long（僅限前一根為高強度擠壓、且必須是
    # 當根解除）不同，供多時間框架擠壓進場規則使用。
    green_dot: bool = False
    green_dot_bars_ago: Optional[int] = None  # 0 = 當根解除
    # Turbo：動能柱由非淺藍翻回淺藍（動能 > 0 且重新上升）。
    turbo: bool = False
    # 最近一段連續擠壓區間的最低價（含當前仍在擠壓的區段）；從未擠壓則為 None。
    squeeze_range_low: Optional[float] = None

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


def _psq_core(
    df: pd.DataFrame, length: int, bb_mult: float, kc_mults: list
) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    """PSQ 逐根序列核心：回傳 (20SMA, 高強度擠壓, 中強度擠壓, 一般擠壓, 動能)。

    全部為因果計算（rolling／EMA／線性回歸只用當根以前的資料），`analyze_psq`
    取最後一根、`compute_psq_series` 取整段，兩者共用同一份實作。
    """
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

    # 3. Momentum (線性回歸動能)
    high_max = high.rolling(length).max()
    low_min = low.rolling(length).min()
    avg_price = (high_max + low_min) / 2.0

    momentum_source = close - (avg_price + basis) / 2.0
    momentum_value = _fast_rolling_linreg(momentum_source, length=length)

    return basis, sqz_high, sqz_mid, sqz_normal, momentum_value


def analyze_psq(
    df: pd.DataFrame,
    length: int = 20,
    bb_mult: float = 2.0,
    kc_mults: list = [1.0, 1.5, 2.0],
    near_pct: float = 1.5,
    vix_spot: float | None = None,
    green_dot_lookback: int = 1,
) -> Optional[PSQResult]:
    """
    計算 PowerSqueeze (PSQ) 量化指標 (Ultimate Edition v2 - Vectorized High Performance)。
    輸入資料為包含 'Open', 'High', 'Low', 'Close' 的 DataFrame。

    Args:
        vix_spot: VIX 即時價格。用於動能標記（OVEREXTENDED_RISK / HIGH_CONVICTION_RECOVERY）
                  以及低波環境時間框架建議。
        green_dot_lookback: Green Dot 回看根數（1 = 只認當根解除）。

    呼叫端若在盤中使用，須自行截掉尚未收盤的最後一根，否則擠壓與動能判定會
    隨每一次 tick 抖動。
    """
    if df is None or df.empty or len(df) < length * 2:
        return None

    try:
        low = df["Low"]
        basis, sqz_high, sqz_mid, sqz_normal, momentum_value = _psq_core(
            df, length, bb_mult, kc_mults
        )
        is_squeezing = sqz_normal  # 只要 BB 縮入最寬的 2.0 KC 內，即屬擠壓狀態

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

        # Green Dot：近 N 根內出現「擠壓 → 解除」的轉換，且當前仍為解除、動能 > 0。
        sq_vals = is_squeezing.to_numpy(dtype=bool)
        n_bars = len(sq_vals)
        green_dot = False
        green_dot_bars_ago: Optional[int] = None
        if n_bars >= 2 and not sq_vals[-1] and curr_mom > 0:
            lookback = max(1, int(green_dot_lookback))
            for ago in range(0, min(lookback, n_bars - 1)):
                j = n_bars - 1 - ago
                if sq_vals[j - 1] and not sq_vals[j]:
                    green_dot = True
                    green_dot_bars_ago = ago
                    break

        # Turbo：動能柱由非淺藍翻回淺藍。
        prev_diff = mom_diff.iloc[-2] if len(mom_diff) > 1 else np.nan
        prev_is_light_blue = bool(prev_mom > 0 and prev_diff > 0)
        turbo = bool(mom_color == "LightBlue" and not prev_is_light_blue)

        # 最近一段連續擠壓區間的最低價。
        squeeze_range_low: Optional[float] = None
        sq_idx = np.flatnonzero(sq_vals)
        if sq_idx.size > 0:
            end = int(sq_idx[-1])
            start = end
            while start > 0 and sq_vals[start - 1]:
                start -= 1
            squeeze_range_low = float(low.iloc[start : end + 1].min())

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
            green_dot=green_dot,
            green_dot_bars_ago=green_dot_bars_ago,
            turbo=turbo,
            squeeze_range_low=squeeze_range_low,
        )
    except Exception as e:
        import logging

        logging.getLogger(__name__).error(f"PSQ 計算發生錯誤: {e}")
        return None


def compute_psq_series(
    df: pd.DataFrame,
    length: int = 20,
    bb_mult: float = 2.0,
    kc_mults: list = [1.0, 1.5, 2.0],
    green_dot_lookback: int = 1,
) -> Optional[pd.DataFrame]:
    """逐根 PSQ 序列（離線回測用）：與 `analyze_psq` 的最後一根判定逐欄一致。

    欄位：squeeze_level、is_squeezing、momentum_value、momentum_color、green_dot、
    turbo、sma_20。全部為因果計算，第 t 列只用到第 t 根（含）以前的資料。
    """
    if df is None or df.empty or len(df) < length * 2:
        return None
    basis, sqz_high, sqz_mid, sqz_normal, mom = _psq_core(df, length, bb_mult, kc_mults)
    diff = mom.diff()
    level = np.select(
        [sqz_high.to_numpy(), sqz_mid.to_numpy(), sqz_normal.to_numpy()],
        ["High", "Mid", "Normal"],
        default="Release",
    )
    color = np.select(
        [
            (mom > 0) & (diff > 0),
            mom > 0,
            (mom < 0) & (diff < 0),
            mom < 0,
        ],
        ["LightBlue", "DarkBlue", "Red", "Golden"],
        default="Neutral",
    )
    sq = sqz_normal.astype(bool)
    released = sq.shift(1, fill_value=False) & ~sq
    lookback = max(1, int(green_dot_lookback))
    recent_release = released.rolling(lookback, min_periods=1).max().astype(bool)
    green_dot = (~sq) & (mom > 0) & recent_release
    color_s = pd.Series(color, index=df.index)
    light = color_s == "LightBlue"
    turbo = light & ~light.shift(1, fill_value=False)
    return pd.DataFrame(
        {
            "squeeze_level": level,
            "is_squeezing": sq,
            "momentum_value": mom,
            "momentum_color": color_s,
            "green_dot": green_dot,
            "turbo": turbo,
            "sma_20": basis,
        },
        index=df.index,
    )
