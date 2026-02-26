import logging

import pandas as pd
from py_vollib.black_scholes_merton.greeks.analytical import delta, theta, gamma

from config import RISK_FREE_RATE

logger = logging.getLogger(__name__)

# IV 低於此門檻視為無效資料，回傳 0.0（呼叫端以 != 0.0 過濾）
MIN_IV_THRESHOLD = 0.01


def calculate_contract_delta(row, current_price, t_years, flag, q=0.0):
    """
    計算單一選擇權合約的理論 Delta 值 (Merton 模型校正股息率 q)。

    Args:
        row: dict-like (pandas Series)，至少需包含 'impliedVolatility' 與 'strike' 欄位。
        current_price (float): 標的目前價格。
        t_years (float): 距到期的年化時間（必須 > 0）。
        flag (str): 'c' 代表 Call，'p' 代表 Put。
        q (float): 年化股息殖利率，預設 0.0。

    Returns:
        float: 理論 Delta 值。計算失敗或輸入無效時回傳 0.0。
    """
    iv = row['impliedVolatility']
    if pd.isna(iv) or iv <= MIN_IV_THRESHOLD:
        return 0.0

    if t_years <= 0:
        logger.debug("t_years <= 0 (%.6f)，跳過 Delta 計算 (strike=%.2f)", t_years, row['strike'])
        return 0.0

    try:
        return delta(flag, current_price, row['strike'], t_years, RISK_FREE_RATE, iv, q)
    except Exception as e:
        logger.debug("Delta 計算失敗 (strike=%.2f, iv=%.4f): %s", row['strike'], iv, e)
        return 0.0

def calculate_greeks(opt_type, stock_price, strike, t_years, iv, q):
    """計算單一選擇權的 Greeks (Delta, Theta, Gamma)。"""
    flag = 'c' if opt_type == 'call' else 'p'
    try:
        return {
            'delta': delta(flag, stock_price, strike, t_years, RISK_FREE_RATE, iv, q),
            'theta': theta(flag, stock_price, strike, t_years, RISK_FREE_RATE, iv, q),
            'gamma': gamma(flag, stock_price, strike, t_years, RISK_FREE_RATE, iv, q)
        }
    except:
        return {'delta': 0.0, 'theta': 0.0, 'gamma': 0.0}
