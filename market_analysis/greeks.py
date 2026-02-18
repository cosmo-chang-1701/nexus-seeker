import pandas as pd
from py_vollib.black_scholes_merton.greeks.analytical import delta
from config import RISK_FREE_RATE

def calculate_contract_delta(row, current_price, t_years, flag, q=0.0):
    """
    計算單一選擇權合約的理論 Delta 值 (Merton 模型校正股息率 q)。
    """
    iv = row['impliedVolatility']
    if pd.isna(iv) or iv <= 0.01:
        return 0.0
    try:
        # 傳入第 7 個參數 q (dividend yield)
        return delta(flag, current_price, row['strike'], t_years, RISK_FREE_RATE, iv, q)
    except Exception as e:
        # print(f"Delta calculation error: {e}") # Optional logging
        return 0.0
