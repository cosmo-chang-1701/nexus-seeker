import logging
from services import market_data_service

logger = logging.getLogger(__name__)

def calculate_hedge_requirement(total_weighted_delta: float, spy_price: float, target_delta: float = 0.0) -> dict:
    """
    計算對沖需求
    公式: N = (Target_Delta - Current_Delta) * (Spot_Symbol / Spot_SPY)
    這裡我們已經在資料庫儲存了加權後的 Delta (Beta-weighted to SPY)
    """
    delta_gap = target_delta - total_weighted_delta
    
    # 1. 使用 SPY 股數對沖 (Delta 1.0)
    # 每 1 單位 Weighted Delta 代表約 1 股 SPY 的等效曝險
    spy_shares = round(delta_gap)
    
    # 2. 使用 SPY 期權對沖 (假設使用 Delta 0.50 的 ATM 合約)
    # 賣出/買入期權口數 = Delta 缺口 / (單口期權 Delta * 100)
    spy_options_qty = round(delta_gap / (0.50 * 100))

    return {
        'delta_gap': round(delta_gap, 2),
        'spy_shares': spy_shares,
        'spy_options_qty': abs(spy_options_qty),
        'option_action': "BTO PUT" if delta_gap < 0 else "BTO CALL"
    }

def get_market_regime_target(spy_price: float, user_capital: float) -> tuple[float, str]:
    """
    系統自行判斷：當前市場環境下的理想 Beta-weighted Delta 目標
    """
    # 1. 取得大盤數據 (假設我們已經有獲取均線的工具)
    sma_200 = market_data_service.get_sma(symbol="SPY", window=200)
    vix_quote = market_data_service.get_quote("^VIX")
    vix_price = vix_quote.get('c', 20.0)

    # 2. 判斷邏輯 (Decision Matrix)
    if spy_price > sma_200 and vix_price < 25:
        # 【強勢多頭】容許正曝險，目標設定為總資產的 0.2% Delta
        # 例如 50k 資金，目標 Delta = 100 (相當於持倉 100 股 SPY 的曝險)
        target_delta = (user_capital * 0.002)
        regime_desc = "Bull Market (SMA200 Up)"
    elif spy_price < sma_200 or vix_price > 35:
        # 【崩盤風險/空頭】強制 Delta 中性，目標 0
        target_delta = 0.0
        regime_desc = "Bear/Crash Warning (Defensive)"
    else:
        # 【震盪市】保守偏多，目標 0.05% Delta
        target_delta = (user_capital * 0.0005)
        regime_desc = "Sideways/Neutral"

    return target_delta, regime_desc

def calculate_autonomous_hedge(current_delta: float, target_delta: float, spy_price: float):
    """
    根據目標缺口計算具體對沖行動
    """
    delta_gap = target_delta - current_delta
    
    # 設定觸發門檻 (避免頻繁對沖小波動，例如缺口 > 50 才行動)
    if abs(delta_gap) < 50:
        return None

    # 計算 SPY ATM 期權口數 (假設 Delta 為 0.5)
    qty = round(abs(delta_gap) / 50) 
    action = "BTO PUT" if delta_gap < 0 else "BTO CALL"
    
    return {
        'gap': round(delta_gap, 2),
        'action': f"{action} {qty} 口 SPY" if qty > 0 else None
    }
