from typing import Any
import logging
import asyncio
import numpy as np
from typing import Dict, Optional
from services import market_data_service
from database.user_settings import UserContext
from services.alert_filter import TrendState, MTFResult
import database
from datetime import datetime

logger = logging.getLogger(__name__)


def evaluate_rehedge_necessity(
    u_ctx: UserContext, result: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """評估是否需要重新掛回 SPY 避險。"""
    current_price = result.get("price") or result.get("current_price", 0.0)
    ema_8 = result.get("ema_8", 0.0)
    ema_21 = result.get("ema_21", 0.0)
    current_vix = result.get("macro_vix") or result.get("vix", 18.0)
    vix_change = result.get("macro_vix_change", 0.0)
    spy_price = result.get("spy_price", 670.0)

    rehedge_reason = None
    if current_price > 0 and ema_8 > 0 and current_price < ema_8:
        rehedge_reason = "📉 價格跌破 EMA 8 (動能轉弱)"
    if current_price > 0 and ema_21 > 0 and current_price < ema_21:
        rehedge_reason = "⚠️ 價格跌破 EMA 21 (趨勢反轉)"

    if current_vix > 20:
        rehedge_reason = f"🌪️ VIX 突破 20 ({current_vix:.1f} 市場進入恐慌區)"
    if vix_change > 0.10:
        rehedge_reason = f"⚡ VIX 單日大幅飆升 ({vix_change * 100:+.1f}%)"

    if u_ctx.capital > 0:
        current_exposure_pct = (
            (u_ctx.total_weighted_delta * spy_price) / u_ctx.capital * 100
        )
        # 風險上限必須雙邊判定：早期版本只比 `> risk_limit`，導致淨空頭曝險
        # (負值) 無論多大都永遠不會觸發個人風險上限——一個 -60% 淨 Delta 的
        # 帳戶在舊邏輯下看起來完全安全。曝險的「大小」與方向無關，故取絕對值。
        if abs(current_exposure_pct) > u_ctx.risk_limit:
            _side = "淨空頭" if current_exposure_pct < 0 else "淨多頭"
            rehedge_reason = (
                f"🔥 {_side}總曝險 ({current_exposure_pct:+.1f}%) "
                f"已超過個人風險上限 ({u_ctx.risk_limit}%)"
            )
            needed_spy_qty = u_ctx.total_weighted_delta
            return {
                "action": "RE_HEDGE",
                "symbol": result.get("symbol", "SPY"),
                "reason": rehedge_reason,
                "suggested_spy_qty": round(needed_spy_qty, 2),
                "priority": "HIGH",
            }

    if rehedge_reason:
        needed_spy_qty = u_ctx.total_weighted_delta
        return {
            "action": "RE_HEDGE",
            "symbol": result.get("symbol", "SPY"),
            "reason": rehedge_reason,
            "suggested_spy_qty": round(needed_spy_qty, 2),
            "priority": "HIGH"
            if (current_price < ema_21 or current_vix > 25)
            else "NORMAL",
        }
    return None


def get_portfolio_exposure_status(
    u_ctx: UserContext, spy_price: float, delta_threshold: float = 50.0
) -> Dict[str, Any]:
    """
    計算投資組合整體的 SPY 等效 Beta-Weighted Delta 曝險與對沖指令。
    當曝險超過使用者定義的門檻 (預設 ±50 Delta) 時，生成具體行動建議。
    """
    total_delta = u_ctx.total_weighted_delta

    directive = "HOLD"
    instruction = "曝險處於安全區間內。"

    if total_delta > delta_threshold:
        directive = "REDUCE_EXPOSURE"
        # 賣出 SPY 現貨以對沖正 Delta 曝險
        shares_to_sell = total_delta
        instruction = f"建議：賣出 {abs(shares_to_sell):.1f} 股 SPY 以中和 Delta 曝險。"
    elif total_delta < -delta_threshold:
        directive = "INCREASE_EXPOSURE"
        # 買入 SPY 現貨以對沖負 Delta 曝險
        shares_to_buy = abs(total_delta)
        instruction = f"建議：買入 {abs(shares_to_buy):.1f} 股 SPY 以中和 Delta 曝險。"

    return {
        "total_beta_delta": round(total_delta, 2),
        "directive": directive,
        "instruction": instruction,
        "threshold": delta_threshold,
    }


def calculate_hedge_requirement(
    total_weighted_delta: float, spy_price: float, target_delta: float = 0.0
) -> dict:
    delta_gap = target_delta - total_weighted_delta
    spy_options_qty = round(delta_gap / (0.50 * 100))
    return {
        "delta_gap": round(delta_gap, 2),
        "spy_shares": round(delta_gap),
        "spy_options_qty": abs(spy_options_qty),
        "option_action": "BTO PUT" if delta_gap < 0 else "BTO CALL",
    }


async def get_market_regime_target(
    spy_price: float, user_capital: float
) -> tuple[float, str]:
    """系統自行判斷：當前市場環境下的理想 Beta-weighted Delta 目標。"""
    sma_200 = await market_data_service.get_sma(symbol="SPY", window=200)
    vix_quote = await market_data_service.get_quote("^VIX")
    vix_c = vix_quote.get("c") if vix_quote else None
    vix_price = float(vix_c) if vix_c is not None else 20.0

    if spy_price > (sma_200 or 0) and vix_price < 25:
        target_delta = user_capital * 0.002
        regime_desc = "Bull Market (SMA200 Up)"
    elif spy_price < (sma_200 or 9999) or vix_price > 35:
        # 空頭/崩盤警戒：目標為 0（完全中性），而非負值。
        #
        # ⚠️ 刻意不給負目標：本函式回傳的是「自主對沖引擎要把組合拉到哪裡」，
        #   它的職責是消除**非預期**的方向性曝險，不是代替使用者建立方向性
        #   觀點。若這裡給負目標，等於系統自己決定要做空。使用者的做空部位
        #   (SHORT_SIDE / Regime V) 是刻意建立的 alpha 曝險，其防守由
        #   anti_washout.py 的做空 SL/TP 矩陣負責，不歸對沖引擎管。
        target_delta = 0.0
        regime_desc = "Bear/Crash Warning (Defensive)"
    else:
        target_delta = user_capital * 0.0005
        regime_desc = "Sideways/Neutral"
    return target_delta, regime_desc


def calculate_autonomous_hedge(
    current_delta: float, target_delta: float, spy_price: float
) -> Any:
    delta_gap = target_delta - current_delta
    if abs(delta_gap) < 50:
        return None
    qty = round(abs(delta_gap) / 50)
    action = "BTO PUT" if delta_gap < 0 else "BTO CALL"
    return {"action": action, "quantity": qty}


def _sum_hedge_only_delta(u_ctx: UserContext) -> float:
    """僅加總標記為 HEDGE 的部位 Beta 加權 Delta。

    用來把「刻意做空的 alpha 曝險」與「為了中和多頭而掛上的對沖」區分開。
    兩者在 `total_weighted_delta` 裡是同一個負號，但語意完全相反：前者是
    使用者的論點，後者是保險。

    分類依據沿用 `analyze_hedge_performance` 既有的 `trade_category == "HEDGE"`
    慣例（於 `cogs/terminal/trades.py` 建倉時寫入）。

    任何資料缺失一律回傳 0.0（視為「沒有可解除的對沖」），fail-safe 方向是
    不發出解除建議——誤發的代價是平掉使用者的部位，遠高於漏發。
    """
    try:
        from database.portfolio import get_user_portfolio

        rows = get_user_portfolio(u_ctx.user_id)
    except Exception:
        return 0.0
    if not rows:
        return 0.0

    total = 0.0
    for row in rows:
        try:
            # get_user_portfolio 的 tuple 佈局：
            # (asset_id, symbol, opt_type, strike, expiry, entry_price,
            #  quantity, stock_cost, weighted_delta, theta, gamma, category)
            category = str(row[11] or "").upper()
            if category != "HEDGE":
                continue
            total += float(row[8] or 0.0)
        except (IndexError, TypeError, ValueError):
            continue
    return total


def suggest_hedge_unlock(
    u_ctx: UserContext, result: Dict[str, Any], mtf: MTFResult
) -> Optional[Dict[str, Any]]:
    if not mtf.is_aligned or mtf.confirmed_direction != TrendState.BULLISH:
        return None
    current_vix = result.get("macro_vix", 18.0)
    vix_change = result.get("macro_vix_change", 0.0)
    if current_vix >= 18 or vix_change >= 0:
        return None
    price, ema_8 = result.get("price", 0.0), result.get("ema_8", 0.0)
    if not (ema_8 > 0 and (price - ema_8) / ema_8 >= 0.015):
        return None
    if result.get("weighted_delta", 0.0) <= 0 or u_ctx.total_weighted_delta >= 0:
        return None
    # 負的組合 Delta 不必然來自對沖——也可能是使用者刻意建立的做空 alpha
    # 部位 (SHORT_SIDE / Regime V)。早期版本把「總 Delta < 0」直接等同於
    # 「有對沖可解除」，於是在多頭共振訊號出現時建議 UNLOCK_HEDGE、
    # reduce_spy_qty = |總 Delta|——實際上是在叫使用者平掉自己的做空論點。
    #
    # 只把標記為 HEDGE 的部位視為可解除的對沖曝險；扣掉 alpha 空頭之後若
    # 已無淨對沖，就不該發出解除建議。
    hedge_delta = _sum_hedge_only_delta(u_ctx)
    if hedge_delta >= 0:
        return None

    potential_delta_shift = abs(hedge_delta)
    return {
        "action": "UNLOCK_HEDGE",
        "symbol": result.get("symbol"),
        "reason": "MTF 多頭共振 + VIX 低位 + 強勢動能突破",
        "reduce_spy_qty": round(potential_delta_shift, 2),
        "new_delta": round(u_ctx.total_weighted_delta + potential_delta_shift, 2),
        "risk_note": "解除對沖將增加系統化曝險，建議設定 EMA 8 作為移動停利線",
    }


async def analyze_hedge_performance(user_id: int) -> Dict[str, Any]:
    """分析投資組合的對沖有效性與績效歸因。"""
    from database.portfolio import get_user_portfolio
    from database.virtual_trading import get_open_virtual_trades
    from market_analysis.portfolio import get_option_chain_mid_iv

    real_trades = await asyncio.to_thread(get_user_portfolio, user_id)
    virtual_trades = await asyncio.to_thread(get_open_virtual_trades, user_id)

    all_trades_normalized = []
    for t in real_trades:
        all_trades_normalized.append(
            {
                "symbol": t[1],
                "opt_type": t[2],
                "strike": t[3],
                "expiry": t[4],
                "entry_price": t[5],
                "quantity": t[6],
                "weighted_delta": t[8] if len(t) > 8 else 0.0,
                "trade_category": t[11] if len(t) > 11 else "SPECULATIVE",
            }
        )
    for t in virtual_trades:
        all_trades_normalized.append(
            {
                "symbol": t["symbol"],
                "opt_type": t["opt_type"],
                "strike": t["strike"],
                "expiry": t["expiry"],
                "entry_price": t["entry_price"],
                "quantity": t["quantity"],
                "weighted_delta": t.get("weighted_delta", 0.0),
                "trade_category": t.get("trade_category", "SPECULATIVE"),
            }
        )

    alpha_pnl, hedge_pnl, alpha_delta, hedge_delta = 0.0, 0.0, 0.0, 0.0

    for t in all_trades_normalized:
        current_price, _, _bid, _ask = await get_option_chain_mid_iv(
            t["symbol"],
            t["expiry"],
            t["strike"],
            t["opt_type"],
        )
        pnl = (
            (current_price - t["entry_price"]) * t["quantity"] * 100
            if current_price > 0
            else 0.0
        )
        delta = t["weighted_delta"]
        if t["trade_category"] == "HEDGE":
            hedge_pnl += pnl
            hedge_delta += delta
        else:
            alpha_pnl += pnl
            alpha_delta += delta

    net_pnl = alpha_pnl + hedge_pnl
    hedge_ratio = abs(hedge_delta / alpha_delta) if alpha_delta != 0 else 0.0
    effectiveness = (
        max(0.0, min(1.0, 1.0 - (abs(net_pnl) / abs(alpha_pnl))))
        if abs(alpha_pnl) > 0
        else 0.0
    )

    return {
        "net_pnl": round(net_pnl, 2),
        "alpha_contribution": round(alpha_pnl, 2),
        "hedge_contribution": round(hedge_pnl, 2),
        "hedge_ratio": round(hedge_ratio, 4),
        "effectiveness": round(effectiveness, 4),
        "status": "OVER_HEDGED"
        if hedge_ratio > 1.1
        else "UNDER_HEDGED"
        if hedge_ratio < 0.8
        else "OPTIMAL",
    }


async def calculate_daily_effectiveness(user_id: int) -> Any:
    perf = await analyze_hedge_performance(user_id)
    u_ctx = await asyncio.to_thread(database.get_full_user_context, user_id)
    await asyncio.to_thread(
        database.add_hedge_history,
        user_id=user_id,
        date=datetime.now().strftime("%Y-%m-%d"),
        alpha_pnl=perf["alpha_contribution"],
        hedge_pnl=perf["hedge_contribution"],
        effectiveness=perf["effectiveness"],
        tau_applied=u_ctx.dynamic_tau,
    )
    return perf


async def calculate_dynamic_tau(user_id: int, lookback_days: int = 7) -> float:
    history = await asyncio.to_thread(
        database.get_hedge_history, user_id, limit=lookback_days
    )
    if not history or len(history) < 3:
        return 1.0
    scores = [h["effectiveness"] for h in history]
    alpha_pnls = [h["alpha_pnl"] for h in history]
    hedge_pnls = [h["hedge_pnl"] for h in history]
    avg_effectiveness = np.average(scores, weights=np.linspace(0.5, 1.0, len(scores)))
    u_ctx = await asyncio.to_thread(database.get_full_user_context, user_id)
    new_tau = u_ctx.dynamic_tau
    if avg_effectiveness < 0.5 and sum(alpha_pnls) > 0 and sum(hedge_pnls) < 0:
        new_tau -= 0.05
    elif sum(alpha_pnls) + sum(hedge_pnls) < 0 and avg_effectiveness < 0.7:
        new_tau += 0.10
    final_tau = float(np.clip(new_tau, 0.5, 1.5))
    if final_tau != u_ctx.dynamic_tau:
        await asyncio.to_thread(
            database.upsert_user_config, user_id, dynamic_tau=final_tau
        )
    return final_tau


def get_tuned_risk_advice(user_id: int, raw_advice: Dict[str, Any]) -> Dict[str, Any]:
    # 這裡 u_ctx 獲取是同步的，因為它是在一個不方便 await 的地方被呼叫，或者是我們需要在 caller 處處理
    # 為了方便，我們維持同步獲取，或者讓 caller 傳入 u_ctx
    u_ctx = database.get_full_user_context(user_id)
    if not (raw_advice and raw_advice.get("action") == "RE_HEDGE"):
        return raw_advice
    raw_advice["suggested_spy_qty"] = round(
        raw_advice.get("suggested_spy_qty", 0) * u_ctx.dynamic_tau, 2
    )
    raw_advice["reason"] += f" (由 STHE 引擎自動校正係數: {u_ctx.dynamic_tau:.2f})"
    return raw_advice
