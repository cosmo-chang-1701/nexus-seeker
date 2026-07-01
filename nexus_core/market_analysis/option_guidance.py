"""
option_guidance.py — 期權策略指引與可執行期權合約計畫。

從 intraday_pipeline.py 分離，包含：
  - _watchlist_event_risk_multiplier（事件風險乘數）
  - derive_watchlist_option_guidance（策略文字描述）
  - _mid_price_from_row / _pick_watchlist_cover_leg（合約選擇工具）
  - _estimate_watchlist_contract_count（口數估算）
  - build_watchlist_option_plan（完整期權計畫建構）
"""

import logging
from typing import Any, Mapping, Optional

import pandas as pd

from models.schemas import (
    EnhancedWatchlistMetrics,
    WatchlistEventContext,
    WatchlistLegAction,
    WatchlistOptionLeg,
    WatchlistOptionPlan,
    WatchlistOptionType,
    WatchlistPremiumType,
    WatchlistTacticalPlan,
)
from market_analysis.signal_calculator import (
    _is_mock,
    _get_tactical_model,
    calculate_dynamic_trading_signals,
)

logger = logging.getLogger(__name__)


def _watchlist_event_risk_multiplier(
    event_context: WatchlistEventContext | None,
) -> float:
    if event_context is None:
        return 1.0
    multipliers = [1.0]
    if (
        event_context.earnings_tte_hours is not None
        and 0 < event_context.earnings_tte_hours <= 72.0
    ):
        multipliers.append(0.35)
    elif (
        event_context.earnings_tte_hours is not None
        and 0 < event_context.earnings_tte_hours <= 168.0
    ):
        multipliers.append(0.5)

    if (
        event_context.macro_tte_hours is not None
        and 0 < event_context.macro_tte_hours <= 24.0
    ):
        multipliers.append(0.5)
    elif (
        event_context.macro_tte_hours is not None
        and 0 < event_context.macro_tte_hours <= 48.0
    ):
        multipliers.append(0.67)

    return min(multipliers)


def derive_watchlist_option_guidance(
    metrics: EnhancedWatchlistMetrics,
    tactical: Mapping[str, Any] | WatchlistTacticalPlan,
    event_context: WatchlistEventContext | None = None,
    has_position: bool = False,
    suitable_buy_price: float | None = None,
    suitable_sell_price: float | None = None,
) -> str:
    if _is_mock(metrics) or _is_mock(tactical):
        return "Mock 策略指引：IV 穩定，適合賣方收租。"

    tactical_model = _get_tactical_model(tactical)

    # 文案與 SDDM 路由剛性同步：WAIT 狀態下禁止任何期權開倉建議
    if tactical_model.scenario == "wait" or "WAIT" in tactical_model.sddm_route.upper():
        return "價格仍在防守框架內，維持現貨 $1.00×$ 零槓桿死守，將雙手嚴格離開期權開倉鍵。"

    # Pre-market/degraded IV check: forbid all deterministic options strategies (e.g. Short Straddle/Iron Butterfly/selling premium)
    iv_source = getattr(metrics, "iv_source", "UNAVAILABLE")
    is_premarket = getattr(
        metrics, "is_premarket", False
    ) or "[盤前數據未更新]" in getattr(metrics, "option_skew_state", "")
    if iv_source in ["HV_PROXY", "UNAVAILABLE"] or is_premarket:
        return "⚠️ 盤前或波動率數據退化，為防範風險，嚴禁輸出任何確定性期權策略建議（如 Short Straddle / Iron Butterfly / 賣方收租）。請以現貨無槓桿死守為主。"

    # 金融常識約束模組 (Financial Sanity Guardrails)
    # High IV Bubble Defense: IV Rank/Percentile > 80% 時禁止單腿買方策略
    iv_rank = float(getattr(metrics, "iv_rank", 0.0) or 0.0)
    iv_percentile = float(getattr(metrics, "iv_percentile", 0.0) or 0.0)

    if iv_rank > 80.0 or iv_percentile > 80.0:
        iv_warning = (
            f"⚠️ IV 泡沫警告 (IV Rank: {iv_rank:.1f}% / IV Pctl: {iv_percentile:.1f}%)："
            f"當前隱含波動率已進入歷史高位泡沫區，波動率收縮 (IV Crush) 風險極高。"
            f"禁止觸發任何單腿買入 Put/Call 策略。"
        )
        if has_position:
            return (
                f"{iv_warning} "
                f"已持倉：建議以 Covered Call 高位收租，或以 Collar (保護性領口) 鎖住利潤，"
                f"嚴格執行 1.00x 純現貨無槓桿防守。"
            )
        else:
            return (
                f"{iv_warning} "
                f"未持倉：建議觀望等待 IV 回落，或僅以 Credit Spread (賣方價差) 小倉位收租。"
                f"禁止裸買任何單腿期權合約。"
            )

    # 確保取得適合 the 買賣點價位 (如未提供，則以默認資金參數在線計算)
    if has_position and suitable_sell_price is None:
        sig = calculate_dynamic_trading_signals(
            metrics,
            tactical_model,
            has_position=True,
            capital=100000.0,
            risk_limit=15.0,
        )
        suitable_sell_price = sig.get("suitable_sell_price", metrics.current_price)
    elif not has_position and suitable_buy_price is None:
        sig = calculate_dynamic_trading_signals(
            metrics,
            tactical_model,
            has_position=False,
            capital=100000.0,
            risk_limit=15.0,
        )
        suitable_buy_price = sig.get("suitable_buy_price", metrics.current_price)

    # 1.00x pure equity cash-backed strategy option guidance
    if has_position:
        return (
            f"已持有現貨部位，建議於阻力位 ${float(suitable_sell_price or 0.0):.2f} "
            f"建立 Covered Call (拋補看漲選擇權) 進行鎖利收租，並嚴格執行 1.00x 純現貨無槓桿防守。"
        )
    else:
        return (
            f"目前未持倉，建議於安全買點 ${float(suitable_buy_price or 0.0):.2f} "
            f"建立 Cash-Secured Put (現金擔保賣出賣權) 進行建倉收租，嚴禁任何單邊買入或價差期權策略。"
        )


def _mid_price_from_row(row: pd.Series) -> float:
    bid = float(row.get("bid", 0.0) or 0.0)
    ask = float(row.get("ask", 0.0) or 0.0)
    if bid > 0.0 and ask > 0.0:
        return round((bid + ask) / 2.0, 4)
    return round(float(row.get("lastPrice", 0.0) or 0.0), 4)


async def _pick_watchlist_cover_leg(
    symbol: str,
    expiry: str,
    opt_type: str,
    anchor_strike: float,
    direction: str,
    current_price: float,
    atr_14: float,
) -> Optional[dict[str, float | str]]:
    from services import market_data_service

    chain = await market_data_service.get_option_chain(symbol, expiry)
    if chain is None:
        return None

    contracts = chain.calls if opt_type == "call" else chain.puts
    if contracts.empty:
        return None

    width = max(round(max(atr_14, current_price * 0.03), 2), 1.0)
    target_strike = (
        anchor_strike + width if direction == "higher" else anchor_strike - width
    )

    if direction == "higher":
        candidates = contracts[contracts["strike"] > anchor_strike].copy()
    else:
        candidates = contracts[contracts["strike"] < anchor_strike].copy()
    if candidates.empty:
        return None

    idx = (candidates["strike"] - target_strike).abs().idxmin()
    leg = candidates.loc[idx]
    return {
        "strike": float(leg["strike"]),
        "expiry": expiry,
        "mid": _mid_price_from_row(leg),
        "bid": float(leg.get("bid", 0.0) or 0.0),
        "ask": float(leg.get("ask", 0.0) or 0.0),
    }


def _estimate_watchlist_contract_count(
    *,
    premium_type: str,
    estimated_net_premium: float,
    width: float,
    short_strike: float,
    capital: float,
    risk_limit: float,
    risk_budget_multiplier: float = 1.0,
) -> tuple[int, float]:
    base_budget = max(capital * min(max(risk_limit, 1.0), 15.0) / 100.0 * 0.1, 500.0)
    base_budget *= max(min(risk_budget_multiplier, 1.0), 0.2)

    if premium_type == "debit":
        risk_per_contract = max(estimated_net_premium * 100.0, 1.0)
    elif width > 0.0:
        risk_per_contract = max((width - estimated_net_premium) * 100.0, 1.0)
    else:
        risk_per_contract = max((short_strike - estimated_net_premium) * 100.0, 1.0)

    suggested = max(1, min(int(base_budget // risk_per_contract) or 1, 3))
    return suggested, round(risk_per_contract * suggested, 2)


async def build_watchlist_option_plan(
    metrics: EnhancedWatchlistMetrics,
    tactical: Mapping[str, Any] | WatchlistTacticalPlan,
    *,
    capital: float,
    risk_limit: float,
    event_context: WatchlistEventContext | None = None,
    has_position: bool = False,
) -> Optional[WatchlistOptionPlan]:
    from market_analysis.strategy import find_best_contract

    if _is_mock(metrics) or _is_mock(tactical):
        return None

    tactical_model = _get_tactical_model(tactical)

    # SDDM 文案剛性同步：WAIT 狀態下不輸出任何可執行期權合約
    if tactical_model.scenario == "wait" or "WAIT" in tactical_model.sddm_route.upper():
        return None

    # Pre-market/degraded IV check: forbid all deterministic options strategies (e.g. Short Straddle/Iron Butterfly/selling premium)
    iv_source = getattr(metrics, "iv_source", "UNAVAILABLE")
    is_premarket = getattr(
        metrics, "is_premarket", False
    ) or "[盤前數據未更新]" in getattr(metrics, "option_skew_state", "")
    if iv_source in ["HV_PROXY", "UNAVAILABLE"] or is_premarket:
        return None

    stock_action = derive_watchlist_option_guidance(
        metrics, tactical_model, event_context=event_context, has_position=has_position
    )

    iv_percentile = float(getattr(metrics, "iv_percentile", 0.0) or 0.0)
    iv_bubble = iv_percentile > 90.0

    # 單腿裸賣防禦：當 IV Rank 極端 (> 90%) 時，提示 assignment 風險
    iv_rank_val = float(getattr(metrics, "iv_rank", 0.0) or 0.0)
    naked_sell_warning = iv_rank_val > 90.0 and not has_position

    strategy_name: str | None = None
    premium_type: WatchlistPremiumType | None = None
    primary_leg: dict[str, float | str] | None = None
    hedge_leg: dict[str, float | str] | None = None
    primary_action: WatchlistLegAction = "BUY"
    chain_opt_type = "put"
    leg_opt_type: WatchlistOptionType = "PUT"
    cover_direction = "lower"
    width = 0.0

    event_lock = event_context is not None and event_context.risk_mode == "event-lock"

    if has_position:
        strategy_name = "Covered Call (拋補看漲期權 / 高位收租)"
        premium_type = "credit"
        chain_opt_type = "call"
        leg_opt_type = "CALL"
        primary_leg = await find_best_contract(metrics.symbol, "STO_CALL", 0.20, 21, 45)
        primary_action = "SELL"
    else:
        strategy_name = "Cash-Secured Put"
        premium_type = "credit"
        chain_opt_type = "put"
        leg_opt_type = "PUT"
        primary_leg = await find_best_contract(metrics.symbol, "STO_PUT", -0.20, 30, 45)
        primary_action = "SELL"

    if primary_leg is None or strategy_name is None or premium_type is None:
        return None

    # IV Percentile > 90%: hard block all buyer routes (debit / BTO structures)
    if iv_bubble and premium_type == "debit":
        return None

    # Option pricing and liquidity verification (Guideline Four)
    # Formula: OTM Call Premium << |Strike - Spot|
    # Also verify contradictions where IV Rank is 0.0% but OTM Call Premium is extremely expensive
    strike_val = float(primary_leg.get("strike", 0.0))
    mid_val = float(primary_leg.get("mid", 0.0))
    is_call_leg = leg_opt_type == "CALL"
    is_otm_leg = (is_call_leg and strike_val >= metrics.current_price) or (
        not is_call_leg and strike_val <= metrics.current_price
    )

    is_illiquid = False
    if is_otm_leg:
        distance_val = abs(strike_val - metrics.current_price)
        if (
            distance_val > 0.0
            and mid_val >= distance_val * 0.7
            and metrics.iv_rank == 0.0
        ):
            is_illiquid = True

    bid_val = float(primary_leg.get("bid", 0.0))
    ask_val = float(primary_leg.get("ask", 0.0))
    if bid_val > 0 and ask_val > bid_val:
        spread_ratio = (ask_val - bid_val) / ((ask_val + bid_val) / 2)
        if spread_ratio > 0.15:
            is_illiquid = True

    if is_illiquid:
        return WatchlistOptionPlan(
            strategy_name="WAIT (期權鏈流動性不足，點差過大，拒絕路由)",
            premium_type="credit",
            estimated_net_premium=0.0,
            suggested_contracts=0,
            max_risk_amount=0.0,
            rationale="⚠️ 【期權鏈流動性不足，點差過大，拒絕路由】",
            stock_action="⚠️ 【期權鏈流動性不足，點差過大，拒絕路由】",
            legs=[],
        )

    if "Spread" in strategy_name:
        hedge_leg = await _pick_watchlist_cover_leg(
            metrics.symbol,
            str(primary_leg["expiry"]),
            chain_opt_type,
            float(primary_leg["strike"]),
            cover_direction,
            metrics.current_price,
            metrics.atr_14,
        )
        if hedge_leg is None:
            return None
        width = abs(float(primary_leg["strike"]) - float(hedge_leg["strike"]))

    legs = [
        WatchlistOptionLeg(
            action=primary_action,
            opt_type=leg_opt_type,
            strike=float(primary_leg["strike"]),
            expiry=str(primary_leg["expiry"]),
            mid_price=float(primary_leg["mid"]),
        )
    ]

    estimated_net_premium = float(primary_leg["mid"])
    if hedge_leg is not None:
        hedge_action: WatchlistLegAction = "SELL" if premium_type == "debit" else "BUY"
        estimated_net_premium = (
            max(float(primary_leg["mid"]) - float(hedge_leg["mid"]), 0.01)
            if premium_type == "debit"
            else max(float(primary_leg["mid"]) - float(hedge_leg["mid"]), 0.01)
        )
        legs.append(
            WatchlistOptionLeg(
                action=hedge_action,
                opt_type=leg_opt_type,
                strike=float(hedge_leg["strike"]),
                expiry=str(hedge_leg["expiry"]),
                mid_price=float(hedge_leg["mid"]),
            )
        )

    suggested_contracts, max_risk_amount = _estimate_watchlist_contract_count(
        premium_type=premium_type,
        estimated_net_premium=estimated_net_premium,
        width=width,
        short_strike=float(primary_leg["strike"])
        if primary_action == "SELL"
        else float(hedge_leg["strike"])
        if hedge_leg is not None and premium_type == "credit"
        else float(primary_leg["strike"]),
        capital=capital,
        risk_limit=risk_limit,
        risk_budget_multiplier=_watchlist_event_risk_multiplier(event_context),
    )

    # Macro 高衝擊事件倒數：剛性縮口數，避免事件前過度曝險
    if (
        event_context is not None
        and event_context.macro_tte_hours is not None
        and 0 < event_context.macro_tte_hours <= 24.0
    ):
        suggested_contracts = min(int(suggested_contracts), 1)

    rationale = (
        f"依據 {strategy_name} 路由，結合 IV Rank {metrics.iv_rank:.1f}%、"
        f"Skew {metrics.option_skew:+.2f}% 與當前技術位階自動選約。"
    )
    if event_context is not None and event_context.risk_mode != "normal":
        rationale = f"{rationale} {event_context.summary}"

    if naked_sell_warning:
        rationale = (
            "⚠️ IV Rank 極端高位 (>90%)：裸賣 CSP 面臨 Assignment 風險。"
            "建議改用 Bull Put Spread 定義最大虧損。 " + rationale
        )
    if iv_bubble:
        rationale = (
            "🚨 當前隱含波動率已高度泡沫化，強烈預警造市商波動率扼殺 (IV Crush) 陷阱，全面關閉買方路由。 "
            + rationale
        )
    if event_lock and "Credit" in strategy_name:
        return None
    return WatchlistOptionPlan(
        strategy_name=strategy_name,
        premium_type=premium_type,
        estimated_net_premium=round(estimated_net_premium, 4),
        suggested_contracts=suggested_contracts,
        max_risk_amount=max_risk_amount,
        rationale=rationale,
        stock_action=stock_action,
        legs=legs,
    )
