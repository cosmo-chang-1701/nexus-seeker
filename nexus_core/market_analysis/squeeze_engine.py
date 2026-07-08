import numpy as np
import pandas as pd
import psutil
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


def calculate_power_squeeze(df: pd.DataFrame) -> Dict[str, Any]:
    """
    計算 PowerSqueeze 指標 (PSQ)。
    包含 Bollinger Bands (20, 2.0) 和 Keltner Channels (20, 1.5)。
    is_squeezing: BB 在 KC 內部 (BB Upper < KC Upper 且 BB Lower > KC Lower)。
    """
    fallback = {"is_squeezing": False, "momentum": 0.0, "direction": "⚪"}

    # 記憶體安全檢查 (85%)
    mem_usage = psutil.virtual_memory().percent
    if mem_usage > 85.0:
        logger.warning(
            f"[PowerSqueeze] 系統記憶體過載 ({mem_usage}%)，略過 PSQ 計算，返回預設值。"
        )
        return fallback

    if df is None or df.empty or len(df) < 24:
        return fallback

    try:
        # Bollinger Bands (20, 2.0)
        sma_20 = df["Close"].rolling(window=20).mean()
        std_20 = df["Close"].rolling(window=20).std()
        bb_upper = sma_20 + (2.0 * std_20)
        bb_lower = sma_20 - (2.0 * std_20)

        # Keltner Channels (20, 1.5)
        tr0 = abs(df["High"] - df["Low"])
        tr1 = abs(df["High"] - df["Close"].shift(1))
        tr2 = abs(df["Low"] - df["Close"].shift(1))
        tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)
        atr_20 = tr.rolling(window=20).mean()

        kc_upper = sma_20 + (1.5 * atr_20)
        kc_lower = sma_20 - (1.5 * atr_20)

        # 擠壓狀態 (BB 進入 KC 內部)
        is_squeezing_series = (bb_upper < kc_upper) & (bb_lower > kc_lower)

        # 動能 (線性迴歸斜率)
        diff = df["Close"] - sma_20

        # 計算近 4 期的線性迴歸斜率
        def linreg_slope(y):
            if len(y) < 4:
                return 0.0
            x = np.arange(len(y))
            slope, _ = np.polyfit(x, y, 1)
            return slope

        momentum_series = diff.rolling(window=4).apply(linreg_slope, raw=True)

        is_squeezing = bool(is_squeezing_series.iloc[-1])
        momentum = (
            float(momentum_series.iloc[-1])
            if not pd.isna(momentum_series.iloc[-1])
            else 0.0
        )

        if momentum > 0:
            direction = "🟢"
        elif momentum < 0:
            direction = "🔴"
        else:
            direction = "⚪"

        return {
            "is_squeezing": is_squeezing,
            "momentum": momentum,
            "direction": direction,
        }

    except Exception as e:
        logger.error(f"[PowerSqueeze] 計算時發生錯誤: {e}")
        return fallback
