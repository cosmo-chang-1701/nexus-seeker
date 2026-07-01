import yfinance as yf
import pandas as pd
import numpy as np
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


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
        if df.empty:
            return None

        # 確保只有最後 days 的資料 (1天約 7 根 1h K 線)
        rows_to_keep = min(len(df), days * 7)
        df = df.tail(rows_to_keep).copy()

        if df.empty:
            return None

        num_bins = 50
        min_price = df["Low"].min()
        max_price = df["High"].max()

        if min_price == max_price:
            return {
                "hvn": round(float(min_price), 2),
                "lvn": round(float(min_price), 2),
            }

        bins = np.linspace(min_price, max_price, num_bins + 1)

        df["Typical"] = (df["High"] + df["Low"] + df["Close"]) / 3.0
        df["Bin"] = pd.cut(df["Typical"], bins=bins, labels=False, include_lowest=True)

        vol_profile = df.groupby("Bin")["Volume"].sum()

        if vol_profile.empty:
            return None

        hvn_bin = vol_profile.idxmax()
        lvn_bin = vol_profile.idxmin()

        bin_width = (max_price - min_price) / num_bins
        hvn_price = min_price + (hvn_bin + 0.5) * bin_width
        lvn_price = min_price + (lvn_bin + 0.5) * bin_width

        return {"hvn": round(float(hvn_price), 2), "lvn": round(float(lvn_price), 2)}
    except Exception as e:
        logger.warning(f"Failed to calculate volume profile for {symbol}: {e}")
        return None
