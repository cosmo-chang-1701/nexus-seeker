from typing import Any, Dict, List, Optional

from . import logger
from ._shared import (
    format_cash_impact,
    format_illiquidity_warning,
    resolve_current_value,
)
from .constants import (
    CORE_DEFENSE_ETF_SYMBOLS,
    _EUPHORIA_SKEW_PERCENTILE,
    _MACRO_TOP_ESCAPE_MIN_TIER,
    _MACRO_TOP_ESCAPE_TRIM_RATIO,
    _PROFIT_UNLOCK_TOLERANCE,
)
from .models import RolloverInstruction, RolloverScenario


class _MacroTopEscapeDefenseMixin:
    """邏輯 (6)：宏觀逃頂前瞻防禦 (Macro Top-Escape Anticipatory Defense)。

    本情境不像 Scenario 3/4 需要共用 _compute_structural_breakdown_signals 等
    輔助方法，保留此空殼類別僅為與其餘五個情境的檔案結構維持一致，供
    __init__.py 的 DynamicRolloverEngine 統一以 mixin 組裝方式繼承。
    """


def _compute_satellite_euphoria_ratio(
    portfolio_assets: List[Dict[str, Any]],
) -> Optional[float]:
    """聚合使用者衛星持倉中，個別已符合 Scenario 3 亢奮出場條件 (現貨觸及
    Call Wall 或 Skew 百分位 <= 20) 的比例，作為 evaluate_macro_top_escape_score()
    的第 5 個 (可選) 因子輸入。直接重用 anti_washout.py 完全相同的兩條判定式與
    constants.py 的既有具名常數，不新增/複製任何量化門檻。沒有任何 SATELLITE
    持倉時回傳 None (該因子不參與評分)。
    """
    satellite_assets = [
        a for a in portfolio_assets if a.get("asset_class") == "SATELLITE"
    ]
    if not satellite_assets:
        return None

    euphoria_count = 0
    for asset in satellite_assets:
        spot = float(asset.get("spot_price", 0.0))
        call_wall = float(asset.get("call_wall", 0.0))
        skew = float(asset.get("skew", 0.0))
        skew_percentile = float(asset.get("skew_percentile", 50.0))

        is_profit_unlocked = (call_wall > 0 and spot > 0) and (
            spot >= call_wall
            or abs(spot - call_wall) / call_wall < _PROFIT_UNLOCK_TOLERANCE
        )
        is_euphoria_skew = skew < 0 and skew_percentile <= _EUPHORIA_SKEW_PERCENTILE
        if is_profit_unlocked or is_euphoria_skew:
            euphoria_count += 1

    return euphoria_count / len(satellite_assets)


async def evaluate_macro_top_escape_defense_impl(
    engine: Any,
    get_full_user_context: Any,
    user_id: int,
    portfolio_assets: List[Dict[str, Any]],
    already_flagged_symbols: Optional[set] = None,
) -> List[RolloverInstruction]:
    """
    邏輯 (6): 宏觀逃頂前瞻防禦 (Macro Top-Escape Anticipatory Defense)

    觸發條件 (三道 Gate 缺一不可):
      Gate 1 — user_settings.enable_macro_top_escape_defense == True (嚴格
               opt-in，比照 Scenario 5 target_allocation_pct 的哲學：會動用戶
               資金/持倉的功能一律需要使用者明確同意，不預設開啟)。
      Gate 2 — evaluate_macro_top_escape_score() (index_microstructure.py)
               評分達 CRITICAL 分級 (VTS 逆價差 + Fear & Greed 極度貪婪 +
               FedWatch 鷹派 + 負 Gamma + 可選的衛星持倉亢奮廣度，至少 3 項
               同時觸發)。
      Gate 3 — 排除 already_flagged_symbols (已被 Scenario 2/3/4/5 標記的
               標的本輪不重複下指令，避免同一標的收到互相矛盾的建議)。

    範圍: 僅 SATELLITE 持倉 (比照 Scenario 3/4，不觸碰 CORE — CORE 超額部位
    是 Scenario 5 的職責)。

    動作: 有界防禦性減碼 _MACRO_TOP_ESCAPE_TRIM_RATIO (25%) → BOXX。刻意遠低於
    Scenario 3 的 90% 與 Scenario 4 的 100%——本情境是純粹的「領先訊號」
    (組合式機率評分)，觸發時尚無任何個股結構真正破位，假陽性風險明顯高於
    後兩者已經價格/保證金雙重確認的反應式觸發，因此只做風險曝險的部分削減，
    不強迫在可能誤判的訊號上全額出場。

    去化目的地固定為 BOXX (不設 CASH 分支)：沿用 margin_defense.py 已建立的
    論證——本情境觸發於宏觀亢奮/系統性風險環境，VOO 本身也會同向下跌，
    唯有真正的現金等價物 BOXX 才是防禦性停泊點；本情境沒有 Scenario 4 的
    保證金缺口概念，故不需要 CASH 分支。

    already_flagged_symbols: 已被 Scenario 2/3/4/5 標記過的標的集合，會被
    跳過，避免同一標的同一輪次收到互相矛盾的清倉指令。刻意排在 dispatcher
    順序最後一位 (3→2→5→4→6)——本情境是六大情境中信心度最低、最具推測性的
    觸發，絕不能搶在更確定的訊號之前對同一標的下指令。
    """
    user_ctx = get_full_user_context(user_id)
    if not user_ctx or not getattr(user_ctx, "enable_macro_top_escape_defense", False):
        return []

    from market_analysis.index_microstructure import (
        evaluate_macro_top_escape_score,
        fetch_core_macro_metrics,
        get_market_regime,
    )
    from services.market_data_service import get_vix_term_structure

    try:
        regime = await get_market_regime()
    except Exception as e:
        logger.warning(f"宏觀逃頂前瞻防禦: 取得市場 Regime 失敗: {e}")
        regime = "NORMAL"
    is_negative_gamma = regime in ("SHORT_GAMMA_CRITICAL", "SYSTEMIC_LIQUIDITY_CRISIS")

    try:
        vts_data = await get_vix_term_structure()
        vts_ratio = (
            vts_data.get("vts_ratio", 0.88) if vts_data.get("is_valid", False) else 0.88
        )
    except Exception as e:
        logger.warning(f"宏觀逃頂前瞻防禦: 取得 VTS 期限結構失敗: {e}")
        vts_ratio = 0.88

    try:
        core_metrics = await fetch_core_macro_metrics()
        fear_greed = float(core_metrics.get("fear_greed", 48.0))
    except Exception as e:
        logger.warning(f"宏觀逃頂前瞻防禦: 取得 Fear & Greed 指數失敗: {e}")
        fear_greed = 48.0

    from database.cache import get_kv_cache

    prob = get_kv_cache("macro_fedwatch_probability")

    satellite_euphoria_ratio = _compute_satellite_euphoria_ratio(portfolio_assets)

    score, tier, tier_title, factors = evaluate_macro_top_escape_score(
        vts_ratio=vts_ratio,
        fear_greed=fear_greed,
        prob=prob,
        is_negative_gamma=is_negative_gamma,
        satellite_euphoria_ratio=satellite_euphoria_ratio,
    )
    if tier != _MACRO_TOP_ESCAPE_MIN_TIER:
        return []

    try:
        from database.orders import get_user_active_orders

        orders = get_user_active_orders(user_id)
    except Exception:
        orders = []

    flagged = already_flagged_symbols or set()
    factor_lines = "\n".join(f" ├─ {name}: {val}" for name, val in factors)

    instructions: List[RolloverInstruction] = []
    for asset in portfolio_assets:
        symbol = str(asset.get("symbol", "")).upper()
        quantity = float(asset.get("quantity", 0.0))

        instrument_type = str(
            asset.get("instrument_type", asset.get("asset_type", "SPOT"))
        ).upper()
        asset_class = (
            "OPTIONS"
            if ("OPT" in instrument_type or "CONTRACT" in instrument_type)
            else "SPOT"
        )

        if (
            asset.get("asset_class") != "SATELLITE"
            or symbol in CORE_DEFENSE_ETF_SYMBOLS
            or (symbol, asset_class) in flagged
            or quantity == 0
        ):
            continue

        is_short_option = (
            "OPT" in instrument_type or "CONTRACT" in instrument_type
        ) and quantity < 0
        sell_action = "BTC" if is_short_option else "STC"
        target_asset = "BOXX"

        reason_text = (
            f"🧭 **宏觀逃頂前瞻防禦 (Macro Top-Escape Anticipatory Defense)**\n"
            f"綜合評分：{tier_title} ({score} 分)\n"
            f"{factor_lines}\n"
            f"多項宏觀領先訊號同時觸發，尚無個股結構破位確認，"
            f"先行對 {symbol} 進行 {_MACRO_TOP_ESCAPE_TRIM_RATIO:.0%} 有界防禦性減碼，"
            f"轉入 BOXX 鎖定無風險利息，保留剩餘部位靜觀後續發展。"
        )

        # --- 流動性閘門 (#7)：期權部位若帶有 bid/ask，點差過寬時附加警示 ---
        bid = float(asset.get("bid", 0.0))
        ask = float(asset.get("ask", 0.0))
        if asset_class == "OPTIONS":
            illiquidity_warning = format_illiquidity_warning(bid, ask)
            if illiquidity_warning:
                reason_text += illiquidity_warning

        _, matching_sell_order = engine._resolve_active_order_defense(
            symbol, orders, 0.0, 0.0
        )
        action = "LIQUIDATE"
        sell_ratio, net_note = engine._net_against_existing_order(
            _MACRO_TOP_ESCAPE_TRIM_RATIO, abs(quantity), matching_sell_order
        )
        if net_note:
            reason_text += net_note
        if sell_ratio <= 0.0:
            action = "HOLD"

        current_value = resolve_current_value(
            float(asset.get("current_value", 0.0)),
            abs(quantity),
            float(asset.get("spot_price", 0.0)),
        )
        recovered_cash = current_value * sell_ratio
        cash_impact = format_cash_impact(recovered_cash)

        limit_price = await engine._resolve_target_reference_price(target_asset)

        instructions.append(
            {
                "symbol": symbol,
                "action": action,
                "sell_ratio": sell_ratio,
                "target_core": target_asset,
                "reason": reason_text,
                "suggested_strategy": (
                    f"{sell_action} {_MACRO_TOP_ESCAPE_TRIM_RATIO:.0%} 部位轉倉 "
                    "BOXX (逃頂前瞻防禦)"
                ),
                "sell_action": sell_action,
                "buy_action_label": "轉入 BOXX（鎖定無風險利息）",
                "is_manual_override_required": True,
                "scenario": RolloverScenario.MACRO_TOP_ESCAPE_DEFENSE.value,
                "cash_impact": cash_impact,
                "limit_price": limit_price,
                "instrument_type": asset_class,
            }
        )

    return instructions
