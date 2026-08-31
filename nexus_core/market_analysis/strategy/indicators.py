"""技術指標、策略訊號與 EMA 趨勢判定。"""

import logging
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import pandas_ta as ta  # noqa: F401

import asyncio

from config import TARGET_DELTAS
from services import market_data_service
from market_analysis.ivr_strategy_gate import (
    is_selling_locked_by_ivr,
    _ITM_CALL_MIN_DELTA,
)

logger = logging.getLogger(__name__)


def _calculate_technical_indicators(df: Any):  # type: ignore
    """計算技術指標與波動率位階"""
    try:
        if df.empty or len(df) < 50:
            return None

        df["Log_Ret"] = np.log(df["Close"] / df["Close"].shift(1))
        df["HV_20"] = df["Log_Ret"].rolling(window=20).std() * np.sqrt(252)
        hv_min = df["HV_20"].min()
        hv_max = df["HV_20"].max()
        hv_current = df["HV_20"].iloc[-1]
        if pd.isna(hv_current):
            return None
        hv_rank = (
            ((hv_current - hv_min) / (hv_max - hv_min)) * 100
            if hv_max > hv_min
            else 0.0
        )

        df.ta.rsi(length=14, append=True)
        df.ta.sma(length=20, append=True)
        df.ta.macd(fast=12, slow=26, signal=9, append=True)

        latest = df.iloc[-1]
        return {
            "price": latest.get("Close"),
            "rsi": latest.get("RSI_14"),
            "sma20": latest.get("SMA_20"),
            "macd_hist": latest.get("MACDh_12_26_9"),
            "hv_current": hv_current,
            "hv_rank": hv_rank,
        }
    except Exception as e:
        logger.error(f"指標計算錯誤: {e}")
        return None


def _determine_strategy_signal(indicators: Any, ivr: float = 0.0):  # type: ignore
    """根據技術指標決定策略"""
    price = indicators.get("price", 0.0)
    rsi = indicators.get("rsi", 50.0)
    hv_rank = indicators.get("hv_rank", 0.0)
    sma20 = indicators.get("sma20", 0.0)
    macd_hist = indicators.get("macd_hist", 0.0)

    deltas = TARGET_DELTAS if TARGET_DELTAS else {}

    # ━━━ IVR < 10% 底層賣方策略硬鎖 ━━━
    # 當 IVR 極低時，期權權利金過於廉價，賣方策略的 risk/reward 嚴重不利。
    # 鎖死所有 STO 策略，僅允許現貨或 ITM Call BTO。
    if is_selling_locked_by_ivr(ivr):
        logger.info(
            f"IVR 賣方硬鎖啟動 (IVR={ivr:.1f}%): 封鎖所有 STO 策略，僅路由 ITM Call BTO"
        )
        return "BTO_CALL", "call", _ITM_CALL_MIN_DELTA, 30, 60

    if rsi < 35 and hv_rank >= 30:
        return "STO_PUT", "put", deltas.get("STO_PUT", -0.20), 30, 45
    elif rsi > 65 and hv_rank >= 30:
        return "STO_CALL", "call", deltas.get("STO_CALL", 0.20), 30, 45
    elif price > sma20 and 50 <= rsi <= 65 and macd_hist > 0:
        if hv_rank < 50:
            return "BTO_CALL", "call", deltas.get("BTO_CALL", 0.50), 30, 60
        else:
            return "STO_PUT", "put", deltas.get("STO_PUT", -0.20), 14, 30
    elif price < sma20 and 35 <= rsi <= 50 and macd_hist < 0:
        if hv_rank < 50:
            return "BTO_PUT", "put", deltas.get("BTO_PUT", -0.50), 30, 60
        else:
            return "STO_CALL", "call", deltas.get("STO_CALL", 0.20), 14, 30
    else:
        return None, None, 0, 0, 0


async def evaluate_ema_trend(symbol: str, current_price: float) -> dict:
    """評估 EMA 8/21 趨勢狀態。"""
    ema8, ema21 = await asyncio.gather(
        market_data_service.get_ema(symbol, 8), market_data_service.get_ema(symbol, 21)
    )

    if not ema8 or not ema21:
        return {
            "trend": "UNKNOWN",
            "score": 0,
            "ema_8": 0.0,
            "ema_21": 0.0,
            "distance_from_21": 0.0,
        }

    distance_pct = (current_price - ema21) / ema21
    if current_price > ema8 > ema21:
        state = "BULLISH_STRONG"
    elif ema8 > ema21 and current_price <= ema21:
        state = "BULLISH_CORRECTION"
    elif current_price < ema8 < ema21:
        state = "BEARISH_STRONG"
    else:
        state = "NEUTRAL"

    return {
        "trend": state,
        "ema_8": ema8,
        "ema_21": ema21,
        "distance_from_21": round(distance_pct * 100, 2),
    }


def detect_ema_signals(
    df: pd.DataFrame, window: int = 21, threshold: float = 0.005
) -> Optional[Dict[str, Any]]:
    """偵測價格對 EMA 的穿透與支撐/壓力測試。"""
    if df.empty or len(df) < window + 2:
        return None
    ema_series = df["Close"].ewm(span=window, adjust=False).mean()
    p_curr, p_prev = df["Close"].iloc[-1], df["Close"].iloc[-2]
    ema_curr, ema_prev = ema_series.iloc[-1], ema_series.iloc[-2]

    signal_type, direction = None, None
    if p_prev < ema_prev and p_curr >= ema_curr:
        signal_type, direction = "CROSSOVER", "BULLISH"
    elif p_prev > ema_prev and p_curr <= ema_curr:
        signal_type, direction = "CROSSOVER", "BEARISH"
    if not signal_type:
        dist_pct = abs(p_curr - ema_curr) / ema_curr
        if dist_pct <= threshold:
            signal_type, direction = (
                "TEST",
                ("SUPPORT" if p_curr > ema_curr else "RESISTANCE"),
            )

    if signal_type:
        return {
            "window": window,
            "type": signal_type,
            "direction": direction,
            "ema_val": round(ema_curr, 2),
            "distance_pct": round((p_curr - ema_curr) / ema_curr * 100, 2),
        }
    return None
