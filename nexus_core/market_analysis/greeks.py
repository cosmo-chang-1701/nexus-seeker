import logging
import math
import pandas as pd
from py_vollib.black_scholes_merton.greeks.analytical import (
    delta,
    theta,
    gamma,
    vega,
    d1,
    d2,
)

from config import RISK_FREE_RATE

logger = logging.getLogger(__name__)

# IV 低於此門檻視為無效資料，回傳 0.0（呼叫端以 != 0.0 過濾）
MIN_IV_THRESHOLD = 0.01


def calculate_vanna(flag, stock_price, strike, t_years, iv, q):
    """
    計算 Vanna (dDelta / dVol)。
    """
    try:
        d1(stock_price, strike, t_years, RISK_FREE_RATE, iv, q)
        d2_val = d2(stock_price, strike, t_years, RISK_FREE_RATE, iv, q)
        v_val = vega(flag, stock_price, strike, t_years, RISK_FREE_RATE, iv, q)

        # Vanna = - (Vega / (S * sigma * sqrt(T))) * d2
        # Textbook vega = vollib_vega * 100
        vanna_val = (
            -((v_val * 100.0) / (stock_price * iv * math.sqrt(t_years))) * d2_val
        )
        return vanna_val
    except Exception:
        return 0.0


def calculate_contract_delta(row, current_price, t_years, flag, q=0.0):
    """
    計算單一選擇權合約的理論 Delta 值 (Merton 模型校正股息率 q)。
    """
    iv = row["impliedVolatility"]
    if pd.isna(iv) or iv <= MIN_IV_THRESHOLD:
        return 0.0

    if t_years <= 0:
        logger.debug(
            "t_years <= 0 (%.6f)，跳過 Delta 計算 (strike=%.2f)", t_years, row["strike"]
        )
        return 0.0

    try:
        return delta(flag, current_price, row["strike"], t_years, RISK_FREE_RATE, iv, q)
    except Exception as e:
        logger.debug("Delta 計算失敗 (strike=%.2f, iv=%.4f): %s", row["strike"], iv, e)
        return 0.0


def calculate_greeks(opt_type, stock_price, strike, t_years, iv, q):
    """計算單一選擇權的 Greeks (Delta, Theta, Gamma, Vega, Vanna)。"""
    flag = "c" if opt_type == "call" else "p"
    try:
        if iv <= 0:
            return {"delta": 0.0, "theta": 0.0, "gamma": 0.0, "vega": 0.0, "vanna": 0.0}

        res = {
            "delta": delta(flag, stock_price, strike, t_years, RISK_FREE_RATE, iv, q),
            "theta": theta(flag, stock_price, strike, t_years, RISK_FREE_RATE, iv, q),
            "gamma": gamma(flag, stock_price, strike, t_years, RISK_FREE_RATE, iv, q),
            "vega": vega(flag, stock_price, strike, t_years, RISK_FREE_RATE, iv, q),
        }
        res["vanna"] = calculate_vanna(flag, stock_price, strike, t_years, iv, q)
        return res
    except Exception as e:
        logger.error(f"Greeks 計算發生異常 ({opt_type}, strike={strike}, iv={iv}): {e}")
        return {"delta": 0.0, "theta": 0.0, "gamma": 0.0, "vega": 0.0, "vanna": 0.0}
