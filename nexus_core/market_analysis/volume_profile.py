import yfinance as yf
import pandas as pd
import numpy as np
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def calculate_volume_profile_from_df(
    df: pd.DataFrame, days: int = 20, is_hourly: bool = False
) -> Optional[Dict[str, float]]:
    try:
        if df.empty:
            return None

        # 如果是 1h，1天大約7根 K線。如果是 daily，就是 1根
        multiplier = 7 if is_hourly else 1
        rows_to_keep = min(len(df), days * multiplier)
        df_subset = df.tail(rows_to_keep).copy()

        if df_subset.empty:
            return None

        num_bins = 50
        min_price = df_subset["Low"].min()
        max_price = df_subset["High"].max()

        if min_price == max_price:
            return {
                "hvn": round(float(min_price), 2),
                "lvn": round(float(min_price), 2),
            }

        bins = np.linspace(min_price, max_price, num_bins + 1)

        df_subset["Typical"] = (
            df_subset["High"] + df_subset["Low"] + df_subset["Close"]
        ) / 3.0
        df_subset["Bin"] = pd.cut(
            df_subset["Typical"], bins=bins, labels=False, include_lowest=True
        )

        vol_profile = df_subset.groupby("Bin")["Volume"].sum()

        if vol_profile.empty:
            return None

        hvn_bin = vol_profile.idxmax()
        lvn_bin = vol_profile.idxmin()

        bin_width = (max_price - min_price) / num_bins
        hvn_price = min_price + (hvn_bin + 0.5) * bin_width
        lvn_price = min_price + (lvn_bin + 0.5) * bin_width

        return {"hvn": round(float(hvn_price), 2), "lvn": round(float(lvn_price), 2)}
    except Exception as e:
        logger.warning(f"Failed to calculate volume profile from df: {e}")
        return None


def calculate_volume_profile(
    symbol: str, days: int = 20, interval: str = "1h"
) -> Optional[Dict[str, float]]:
    """
    計算 Volume Profile，找出 HVN (High Volume Node, 強支撐壓力) 與 LVN (Low Volume Node, 真空區)。
    """
    try:
        symbol_yf = symbol.replace(".", "-")
        ticker = yf.Ticker(symbol_yf)

        # 1h interval is only available for the last 730 days. period="1mo" covers recent 20-22 trading days.
        df = ticker.history(period="1mo", interval=interval)
        return calculate_volume_profile_from_df(df, days, is_hourly=(interval == "1h"))
    except Exception as e:
        logger.warning(f"Failed to fetch volume profile for {symbol}: {e}")
        return None
