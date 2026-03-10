from typing import Dict, Any, Optional
from services import market_data_service
from database.user_settings import UserContext
from services.alert_filter import TrendState, MTFResult

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
    

def suggest_hedge_unlock(u_ctx: UserContext, result: Dict[str, Any], mtf: MTFResult) -> Optional[Dict[str, Any]]:
    """
    評估是否建議解除 SPY 對沖 (Hedge Unlocking)。
    
    觸發條件矩陣：
    1. 趨勢共振 (MTF Aligned): 日線與小時線皆處於多頭排列。
    2. 動能強度: Price 脫離 EMA 8 比例 > 1.5%。
    3. 環境穩定: VIX < 18 且目前處於低波環境。
    4. 部位安全性: 個人 Beta-Delta > 0 (獲利貢獻者)。
    """
    # 1. 基礎條件：必須是多頭共振
    if not mtf.is_aligned or mtf.confirmed_direction != TrendState.BULLISH:
        return None
    
    # 2. 環境穩定性檢查 (VIX)
    # result 預期包含 macro_vix 與 macro_vix_change (來自 trading_service)
    current_vix = result.get('macro_vix', 18.0)
    vix_change = result.get('macro_vix_change', 0.0)
    
    # 矩陣條件：VIX < 18 且 ΔVIX < 0 (波動率收縮)
    if current_vix >= 18 or vix_change >= 0:
        return None

    # 3. 動能強度檢查 (Distance from EMA 8)
    # analyze_symbol 回傳的 ema_8 與 price
    price = result.get('price', 0.0)
    ema_8 = result.get('ema_8', 0.0)
    if ema_8 > 0:
        dist_ema8_pct = (price - ema_8) / ema_8
        if dist_ema8_pct < 0.015: # 門檻 1.5%
            return None
    else:
        return None

    # 4. 位元安全性檢查 (Individual Beta-Delta > 0)
    # 這裡指該標的的加權 Delta 是否為正 (多頭部位)
    if result.get('weighted_delta', 0.0) <= 0:
        return None

    # 5. 帳戶是否處於淨避險狀態 (Delta < 0)
    if u_ctx.total_weighted_delta >= 0:
        return None
    
    # 6. 計算建議解除的 Delta 規模 (釋放 Beta 動能)
    potential_delta_shift = abs(u_ctx.total_weighted_delta)
    
    return {
        "action": "UNLOCK_HEDGE",
        "symbol": result.get('symbol'),
        "reason": "MTF 多頭共振 + VIX 低位 + 強勢動能突破",
        "reduce_spy_qty": round(potential_delta_shift, 2),
        "new_delta": round(u_ctx.total_weighted_delta + potential_delta_shift, 2),
        "risk_note": "解除對沖將增加系統化曝險，建議設定 EMA 8 作為移動停利線"
    }
