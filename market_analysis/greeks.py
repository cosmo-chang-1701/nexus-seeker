import pandas as pd
from py_vollib.black_scholes.greeks.analytical import delta
from config import RISK_FREE_RATE

def calculate_contract_delta(row, current_price, t_years, flag):
    """
    計算單一選擇權合約的理論 Delta 值。

    Args:
        row (pd.Series): 包含 impliedVolatility 與 strike 的資料列。
        current_price (float): 標的資產當前價格。
        t_years (float): 距離到期日的年化時間。
        flag (str): 選擇權類型 ('c' for Call, 'p' for Put)。

    Returns:
        float: 計算出的 Delta 值，若失敗或無效則回傳 0.0。
    """
    iv = row['impliedVolatility']
    if pd.isna(iv) or iv <= 0.01:
        return 0.0
    try:
        return delta(flag, current_price, row['strike'], t_years, RISK_FREE_RATE, iv)
    except Exception:
        return 0.0
