import logging
import time
from typing import Dict, Any, Optional
from services import market_data_service
from database.user_settings import UserContext
from services.alert_filter import TrendState, MTFResult
import database

logger = logging.getLogger(__name__)

def evaluate_rehedge_necessity(u_ctx: UserContext, result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    評估是否需要重新掛回 SPY 避險。
    
    監控維度：
    1. 技術面破位: Price < EMA 8 或 EMA 21
    2. 宏觀恐慌: VIX > 20 或 VIX 單日變動 > 10%
    3. 曝險超標: Portfolio Delta / Capital > User Risk Limit
    """
    # 1. 獲取當前市場狀態 (優先使用 result 中的數據，若無則給予預設值)
    current_price = result.get('price') or result.get('current_price', 0.0)
    ema_8 = result.get('ema_8', 0.0)
    ema_21 = result.get('ema_21', 0.0)
    
    # 宏觀數據 (VIX)
    current_vix = result.get('macro_vix') or result.get('vix', 18.0)
    vix_change = result.get('macro_vix_change', 0.0)
    
    # 基準 SPY 價格 (計算曝險用)
    spy_price = result.get('spy_price', 670.0)
    
    rehedge_reason = None

    # 2. 條件判定：技術面失守
    # 只有在有數據的情況下才判斷
    if current_price > 0 and ema_8 > 0 and current_price < ema_8:
        rehedge_reason = "📉 價格跌破 EMA 8 (動能轉弱)"
    if current_price > 0 and ema_21 > 0 and current_price < ema_21:
        rehedge_reason = "⚠️ 價格跌破 EMA 21 (趨勢反轉)"

    # 3. 條件判定：環境惡化 (宏觀恐慌)
    if current_vix > 20:
        rehedge_reason = f"🌪️ VIX 突破 20 ({current_vix:.1f} 市場進入恐慌區)"
    if vix_change > 0.10: # 單日變動 > 10%
        rehedge_reason = f"⚡ VIX 單日大幅飆升 ({vix_change*100:+.1f}%)"

    # 4. 條件判定：Delta 曝險過度
    # 公式: (Total Delta * SPY Price) / Capital * 100
    if u_ctx.capital > 0:
        current_exposure_pct = (u_ctx.total_weighted_delta * spy_price) / u_ctx.capital * 100
        if current_exposure_pct > u_ctx.risk_limit_base:
            rehedge_reason = f"🔥 總曝險 ({current_exposure_pct:.1f}%) 已超過個人風險上限 ({u_ctx.risk_limit_base}%)"

    if rehedge_reason:
        # 狀態鎖定 (State Lock) 邏輯移至外層調用者 (TradingService) 處理，
        # 此處僅負責邏輯判斷。
        
        # 計算回補建議：將 Delta 回調至中性或安全區所需的 SPY 股數
        # needed_spy_hedge = u_ctx.total_weighted_delta # 假設回歸 Delta 中性
        # 如果當前是 正 Delta (曝險)，回補應該是賣出 (Short) SPY
        # 股數建議為當前加權 Delta (因為 1 單位的 Weighted Delta = 1 股 SPY 曝險)
        needed_spy_qty = u_ctx.total_weighted_delta
        
        return {
            "action": "RE_HEDGE",
            "symbol": result.get('symbol', 'SPY'),
            "reason": rehedge_reason,
            "suggested_spy_qty": round(needed_spy_qty, 2),
            "priority": "HIGH" if (current_price < ema_21 or current_vix > 25) else "NORMAL"
        }
    
    return None

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

def analyze_hedge_performance(user_id: int) -> Dict[str, Any]:
    """
    分析投資組合的對沖有效性與績效歸因。
    """
    from database.portfolio import get_user_portfolio
    from database.virtual_trading import get_open_virtual_trades
    from market_analysis.portfolio import get_option_chain_mid_iv
    
    # 1. 獲取所有 OPEN 部位
    real_trades = get_user_portfolio(user_id)
    virtual_trades = get_open_virtual_trades(user_id)
    
    all_trades_normalized = []
    
    # Real trades index mapping: 
    # id(0), symbol(1), opt_type(2), strike(3), expiry(4), entry_price(5), quantity(6), stock_cost(7), 
    # weighted_delta(8), theta(9), gamma(10), trade_category(11)
    for t in real_trades:
        all_trades_normalized.append({
            'symbol': t[1],
            'opt_type': t[2],
            'strike': t[3],
            'expiry': t[4],
            'entry_price': t[5],
            'quantity': t[6],
            'weighted_delta': t[8] if len(t) > 8 else 0.0,
            'trade_category': t[11] if len(t) > 11 else 'SPECULATIVE'
        })
        
    for t in virtual_trades:
        all_trades_normalized.append({
            'symbol': t['symbol'],
            'opt_type': t['opt_type'],
            'strike': t['strike'],
            'expiry': t['expiry'],
            'entry_price': t['entry_price'],
            'quantity': t['quantity'],
            'weighted_delta': t.get('weighted_delta', 0.0),
            'trade_category': t.get('trade_category', 'SPECULATIVE')
        })

    alpha_pnl = 0.0  # 個股策略損益
    hedge_pnl = 0.0  # 對沖部位損益
    
    alpha_delta = 0.0
    hedge_delta = 0.0

    for t in all_trades_normalized:
        # 獲取當前價格以計算 PnL
        current_price, _ = get_option_chain_mid_iv(t['symbol'], t['expiry'], t['strike'], t['opt_type'])
        
        if current_price > 0:
            # PnL = (Current - Entry) * Qty * 100
            # 注意：這裡假設 quantity 正值代表 Long，負值代表 Short
            # PnL = (current - entry) * qty * 100
            pnl = (current_price - t['entry_price']) * t['quantity'] * 100
        else:
            pnl = 0.0
            
        delta = t['weighted_delta']
        
        if t['trade_category'] == 'HEDGE':
            hedge_pnl += pnl
            hedge_delta += delta
        else:
            alpha_pnl += pnl
            alpha_delta += delta

    # 2. 計算對沖比率 (Hedge Ratio)
    # 反映對沖部位抵銷了多少比例的系統性曝險
    hedge_ratio = abs(hedge_delta / alpha_delta) if alpha_delta != 0 else 0.0
    
    # 3. 淨損益與歸因
    net_pnl = alpha_pnl + hedge_pnl
    
    return {
        "net_pnl": round(net_pnl, 2),
        "alpha_contribution": round(alpha_pnl, 2),
        "hedge_contribution": round(hedge_pnl, 2),
        "hedge_ratio": round(hedge_ratio, 4),
        "status": "OVER_HEDGED" if hedge_ratio > 1.1 else "UNDER_HEDGED" if hedge_ratio < 0.8 else "OPTIMAL"
    }
