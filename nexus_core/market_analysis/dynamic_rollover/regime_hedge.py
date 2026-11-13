"""兩級體制避險引擎（regime-aware hedge）。

- 第一級：大盤 NORMAL 且有多頭現貨跌破 PutWall → PSQ（-1x）局部對沖計畫。
- 第二級：大盤 SHORT_GAMMA_CRITICAL／SYSTEMIC_LIQUIDITY_CRISIS → 結構無勝率者清倉，
  並以 SQQQ／SOXS 對沖或止損轉空；
  -3x 另須通過啟動紅線（VIX≥22、VTS>1、SPY 低於 Flip ≥1%，未知視為未達），否則凍結轉 BOXX。

每位使用者每輪最多產生一筆投資組合層級（symbol="PORTFOLIO"）指令。
本引擎只「算」：不寫 DB、不推播、不讀乾跑旗標（由呼叫端接線處理）。
"""

from dataclasses import replace
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import logger
from ._shared import format_cash_impact, sum_gtc_buy_commitment
from .constants import _FALLBACK_TARGET_PRICE_ESTIMATE, CORE_DEFENSE_ETF_SYMBOLS
from .models import RolloverInstruction, RolloverScenario
from .regime_hedge_sizing import (
    CASH_EQUIVALENT_SYMBOLS,
    INVERSE_ETF_LEVERAGE,
    HedgePlan,
    HedgePosition,
    build_tier1_plan,
    build_tier2_plan,
    minus3x_activation_block_reason,
)
from .regime_hedge_timing import (
    is_preliminary_window,
    load_earnings_dates,
    load_macro_freeze_status,
)

_NY_TZ = ZoneInfo("America/New_York")
_HEDGE_ETFS: tuple[str, ...] = ("PSQ", "SQQQ", "SOXS")


def _is_option(asset: Dict[str, Any]) -> bool:
    instrument_type = str(
        asset.get("instrument_type", asset.get("asset_type", "SPOT"))
    ).upper()
    return "OPT" in instrument_type or "CONTRACT" in instrument_type


def _is_hedgeable_long(asset: Dict[str, Any], symbol: str, flagged: set) -> bool:
    """多頭現貨且非現金等價物、非反向 ETF、未被其他情境標記。"""
    return (
        float(asset.get("quantity", 0.0) or 0.0) > 0
        and symbol not in CASH_EQUIVALENT_SYMBOLS
        and symbol not in INVERSE_ETF_LEVERAGE
        and (symbol, "SPOT") not in flagged
    )


async def _fetch_reference_prices(engine: Any) -> Dict[str, float]:
    prices: Dict[str, float] = {}
    for etf in _HEDGE_ETFS:
        try:
            price = float(await engine._resolve_target_reference_price(etf))
        except Exception as e:
            logger.warning(f"[REGIME_HEDGE] 取得 {etf} 參考價失敗: {e}")
            continue
        # 解析器取不到價時回傳備援估計值而非拋例外，視為缺失（股數待確認）
        if price > 0 and price != _FALLBACK_TARGET_PRICE_ESTIMATE:
            prices[etf] = price
    return prices


def _describe_legs(plan: HedgePlan) -> str:
    if not plan.legs:
        return "無新增反向 ETF"
    return "、".join(f"{leg.symbol} ${leg.target_value:,.0f}" for leg in plan.legs)


async def evaluate_regime_hedge_impl(
    engine: Any,
    get_full_user_context: Any,
    user_id: int,
    portfolio_assets: List[Dict[str, Any]],
    already_flagged_symbols: Optional[set] = None,
) -> List[RolloverInstruction]:
    """兩級體制避險：依大盤 regime 產生至多一筆投資組合層級指令。"""
    from market_analysis.index_microstructure import get_market_regime

    try:
        regime = await get_market_regime()
    except Exception as e:
        logger.error(f"取得市場 Regime 失敗: {e}")
        return []
    if regime not in (
        "NORMAL",
        "SHORT_GAMMA_CRITICAL",
        "SYSTEMIC_LIQUIDITY_CRISIS",
    ):
        return []

    user_ctx = get_full_user_context(user_id)
    if getattr(user_ctx, "load_failed", False) is True:
        logger.warning(
            f"[REGIME_HEDGE] 使用者 {user_id} 帳戶資料讀取失敗，略過體制避險"
        )
        return []
    cash_reserve = float(user_ctx.cash_reserve) if user_ctx else 0.0

    flagged = already_flagged_symbols or set()
    spot_assets: List[Dict[str, Any]] = [
        a for a in portfolio_assets if not _is_option(a)
    ]
    positions: List[HedgePosition] = []
    for a in spot_assets:
        sym = str(a.get("symbol", "")).upper()
        if not sym:
            continue
        positions.append(HedgePosition(sym, float(a.get("current_value", 0.0) or 0.0)))

    nav = sum(abs(p.market_value) for p in positions) + cash_reserve
    if nav <= 0:
        return []
    cash_equivalent_value = cash_reserve + sum(
        p.market_value for p in positions if p.symbol in CASH_EQUIVALENT_SYMBOLS
    )

    # 已被 GTC 買單承諾的現金不得再拿去買反向 ETF（現金帳戶無保證金）。
    from database.orders import get_user_active_orders

    try:
        orders = get_user_active_orders(user_id)
    except Exception as e:
        logger.warning(f"[REGIME_HEDGE] 讀取 GTC 委託失敗，視為無承諾: {e}")
        orders = []
    committed = sum_gtc_buy_commitment(orders)
    available = max(0.0, cash_equivalent_value - committed)

    longs = [
        a
        for a in spot_assets
        if _is_hedgeable_long(a, str(a.get("symbol", "")).upper(), flagged)
    ]

    if regime == "NORMAL":
        broken = [
            str(a.get("symbol", "")).upper()
            for a in longs
            if float(a.get("put_wall", 0.0) or 0.0) > 0
            and float(a.get("spot_price", 0.0) or 0.0) > 0
            and float(a.get("spot_price", 0.0)) < float(a.get("put_wall", 0.0))
        ]
        if not broken:
            return []
        prices = await _fetch_reference_prices(engine)
        plan = build_tier1_plan(positions, nav, broken, available, prices)
        if not plan.legs:
            return []
        tier = 1
        trigger_text = f"持股跌破 PutWall：{'、'.join(broken)}"
        strategy = f"第一級：PSQ 局部對沖 {plan.legs[0].target_value / nav:.0%} NAV"
    else:
        liquidate: List[str] = []
        for a in longs:
            symbol = str(a.get("symbol", "")).upper()
            if (
                a.get("asset_class") != "SATELLITE"
                or symbol in CORE_DEFENSE_ETF_SYMBOLS
            ):
                continue
            try:
                is_no_edge, _, _ = await engine._evaluate_structural_no_edge(
                    symbol=symbol,
                    spot=float(a.get("spot_price", 0.0) or 0.0),
                    put_wall=float(a.get("put_wall", 0.0) or 0.0),
                    gamma_flip=float(a.get("gamma_flip", 0.0) or 0.0),
                    atr_14=float(a.get("atr_14", 0.0) or 0.0),
                    sqz_mom=float(a.get("sqz_mom", 0.0) or 0.0),
                    skew=float(a.get("skew", 0.0) or 0.0),
                    price_15m_close=float(
                        a.get("price_15m_close", a.get("spot_price", 0.0)) or 0.0
                    ),
                    gex_profile_data=a.get("gex_profile_data"),
                    asset_class="SPOT",
                    call_wall=float(a.get("call_wall", 0.0) or 0.0),
                    hvn=float(a.get("hvn", 0.0) or 0.0),
                    uoa_list=a.get("uoa", []),
                )
            except Exception as e:
                logger.warning(f"[REGIME_HEDGE] {symbol} 結構無勝率判定失敗，略過: {e}")
                continue
            if is_no_edge:
                liquidate.append(symbol)

        long_symbols = sorted(
            {
                str(a.get("symbol", "")).upper()
                for a in spot_assets
                if _is_hedgeable_long(a, str(a.get("symbol", "")).upper(), set())
            }
        )
        earnings_dates: Dict[str, Optional[date]] = {}
        try:
            earnings_dates = await load_earnings_dates(long_symbols)
        except Exception as e:
            logger.warning(f"[REGIME_HEDGE] 載入財報日失敗: {e}")
        frozen_reason: Optional[str] = None
        try:
            status = await load_macro_freeze_status()
            if status.frozen:
                frozen_reason = status.reason_text()
        except Exception as e:
            logger.warning(f"[REGIME_HEDGE] 載入總經凍結狀態失敗: {e}")
            # fail-closed：讀取失敗時保守凍結新建 -3x
            frozen_reason = "總經凍結狀態讀取失敗，保守凍結新建 −3x 反向 ETF"

        # −3x 啟動紅線（fail-closed：資料不足視為未達）
        redline_reason: Optional[str]
        try:
            from market_analysis.index_microstructure import get_market_regime_inputs

            inputs = await get_market_regime_inputs()
        except Exception as e:
            logger.warning(f"[REGIME_HEDGE] 載入 −3x 啟動紅線輸入失敗: {e}")
            inputs = None
        if inputs is None:
            redline_reason = (
                "−3x 啟動紅線資料不足（VIX／VTS／Gamma Flip 無法取得），"
                "保守凍結新建 −3x 反向 ETF"
            )
        else:
            redline_reason = minus3x_activation_block_reason(
                inputs.vix, inputs.vts_ratio, inputs.spy_spot, inputs.gamma_flip
            )
        frozen_reason = (
            "；".join(r for r in (frozen_reason, redline_reason) if r) or None
        )

        prices = await _fetch_reference_prices(engine)
        today_et = datetime.now(_NY_TZ).date()
        plan = build_tier2_plan(
            positions,
            nav,
            liquidate,
            available,
            prices,
            earnings_dates,
            today_et,
            frozen_reason,
        )
        if not plan.legs and not plan.liquidate_symbols:
            return []
        tier = 2
        trigger_text = (
            f"結構無勝率清倉：{'、'.join(plan.liquidate_symbols)}"
            if plan.liquidate_symbols
            else "無清倉標的"
        )
        mode_label = "止損轉空" if plan.mode == "STOP_SHORT" else "對沖"
        strategy = f"第二級：{mode_label}（{_describe_legs(plan)}）"

    if committed > 0:
        # 被 GTC 買單承諾的現金等價物必須留在 BOXX 目標內，避免使用者誤賣。
        reserved = cash_equivalent_value - available
        note = (
            f"已預留 GTC 買單承諾 ${committed:,.0f}（BOXX 目標已含此保留額），"
            f"可動用現金等價物 ${available:,.0f}"
        )
        plan = replace(
            plan,
            boxx_target_value=plan.boxx_target_value + reserved,
            notes=plan.notes + (note,),
        )

    target_core = "/".join(leg.symbol for leg in plan.legs) or "BOXX"
    recovered = plan.liquidate_value + plan.sell_for_funding_value
    reason = (
        f"🛡️ **兩級體制避險（第{tier}級）**\n"
        f"大盤 Regime: `{regime}`；{trigger_text}；模式: {plan.mode}。"
    )
    instruction: RolloverInstruction = {
        "symbol": "PORTFOLIO",
        "action": "HEDGE_TIER1" if tier == 1 else "HEDGE_TIER2",
        "sell_ratio": 0.0,
        "target_core": target_core,
        "reason": reason,
        "scenario": RolloverScenario.REGIME_HEDGE.value,
        "instrument_type": "SPOT",
        "is_manual_override_required": True,
        "limit_price": None,
        "hedge_plan": plan,
        "hedge_tier": tier,
        "is_preliminary": is_preliminary_window(),
        "market_regime": regime,
        "suggested_strategy": strategy,
        "sell_action": "STC",
        "buy_action_label": f"買入 {target_core}" if plan.legs else "轉入 BOXX",
        "cash_impact": format_cash_impact(recovered),
    }
    return [instruction]
