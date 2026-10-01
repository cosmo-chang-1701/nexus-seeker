from typing import Any, Dict, List


from ._shared import format_cash_impact, format_illiquidity_warning
from .constants import (
    _COVERED_CALL_MAX_DTE,
    _COVERED_CALL_MAX_LOTS,
    _COVERED_CALL_MIN_DTE,
    _COVERED_CALL_MIN_SHARES,
)
from .models import RolloverInstruction, RolloverScenario


class _CoreDeploymentMixin:
    """邏輯 (1)：核心持倉 Covered Call Overlay。

    原本的「超額配置部署」（BOXX 防禦分支與機會分支）已移除：系統以買入並持有為
    主要策略，不再替使用者把 CORE 超額部位轉入 BOXX 或換股。僅保留對 CORE 持倉
    建議賣出備兌買權收租的 evaluate_covered_call_overlay。
    """

    async def evaluate_covered_call_overlay(
        self,
        user_id: int,
        portfolio_assets: List[Dict[str, Any]],
        already_flagged_symbols: set,
    ) -> List[RolloverInstruction]:
        """
        邏輯 (1)：Covered Call Overlay (核心持倉加碼賣出備兌買權收租)。

        當總經處於正常震盪 (get_spx_capped_from_above_signal() 判定的
        regime == NORMAL)，但大盤 SPX (以可交易的 SPY 為代理標的) 受制於
        上方負 Gamma 泥淖與 STO 封頂、缺乏向上爆發力時，對任一持股數 >= 100
        股 (_COVERED_CALL_MIN_SHARES，1 口門檻) 的 CORE 持倉，額外推薦開立
        1 口 (_COVERED_CALL_MAX_LOTS) OTM Covered Call (DTE
        _COVERED_CALL_MIN_DTE~_COVERED_CALL_MAX_DTE 天，履約價下限錨定使用者
        成本線 avg_cost 與 SPY 負 Gamma 阻力區 swamp_strike 兩者較高者) 收取
        權利金，進一步壓降持倉成本。

        不要求 target_allocation_pct opt-in：只要 CORE 持倉股數達 1 口門檻即可。

        不賣出任何標的持股 (action="HOLD", sell_ratio=0.0)：這代表本分支的
        輸出天然被 portfolio_monitor.py 既有的 already_flagged_symbols 建構
        邏輯 (僅 action != "HOLD" 才計入已標記) 排除在外，可與 Scenario 4
        於同一輪次無矛盾地共存，不需要額外的互斥判斷。

        avg_cost 直接取自 asset_entry (未經 GTC 網格委託單調整的原始成本)，
        而非 trading_orchestration.calculate_new_cost_basis() 的調整版本：
        後者需要額外查詢使用者當前委託單 (每檔 CORE 持倉每輪次一次 I/O)，
        且待成交買單只會把有效成本基礎進一步拉低，故直接採用 avg_cost 作為
        履約價下限是更保守 (門檻更高) 的預設選擇。
        """
        from market_analysis.index_microstructure import (
            get_spx_capped_from_above_signal,
        )
        from market_analysis.strategy import find_lowest_strike_call_above_floor

        instructions: List[RolloverInstruction] = []

        signal = await get_spx_capped_from_above_signal()
        if not signal.get("is_capped"):
            return instructions

        swamp_strike = float(signal.get("swamp_strike", 0.0))

        for asset in portfolio_assets:
            symbol = str(asset.get("symbol", "")).upper()
            if asset.get("asset_class") != "CORE":
                continue
            # CORE 持倉恆為現貨，與其他情境的 (symbol, instrument_type) 去重鍵語意一致。
            if (symbol, "SPOT") in already_flagged_symbols:
                continue

            quantity = float(asset.get("quantity", 0.0))
            if quantity < _COVERED_CALL_MIN_SHARES:
                continue

            contracts_to_sell = min(_COVERED_CALL_MAX_LOTS, int(quantity // 100))
            if contracts_to_sell <= 0:
                continue

            avg_cost = float(asset.get("avg_cost", 0.0))
            floor_strike = max(avg_cost, swamp_strike)
            if floor_strike <= 0:
                continue

            contract = await find_lowest_strike_call_above_floor(
                symbol, floor_strike, _COVERED_CALL_MIN_DTE, _COVERED_CALL_MAX_DTE
            )
            if not contract:
                continue

            strike = float(contract.get("strike", 0.0))
            expiry = str(contract.get("expiry", ""))
            mid = float(contract.get("mid", 0.0))
            bid = float(contract.get("bid", 0.0))
            ask = float(contract.get("ask", 0.0))

            estimated_premium = contracts_to_sell * 100 * mid
            illiquidity_warning = format_illiquidity_warning(bid, ask)

            reason_text = (
                "🖋️ **核心資金部署延伸：Covered Call Overlay (賣出備兌買權收租)**\n"
                f"{symbol} 持有 {quantity:.0f} 股，成本線 ${avg_cost:.2f}"
                + (
                    f"，SPY 負 Gamma 阻力區 ${swamp_strike:.2f}"
                    if swamp_strike > 0
                    else ""
                )
                + f"，履約價下限取兩者較高 ${floor_strike:.2f}。"
                f"建議賣出 {contracts_to_sell} 口 ${strike:.2f}C ({expiry})，"
                f"預估權利金收入約 ${estimated_premium:,.0f}，"
                "賺取時間價值並壓降持倉成本。"
            )
            if illiquidity_warning:
                reason_text += illiquidity_warning

            instructions.append(
                {
                    "symbol": symbol,
                    "action": "HOLD",
                    "sell_ratio": 0.0,
                    "target_core": symbol,
                    "reason": reason_text,
                    "suggested_strategy": "Covered Call (STO)",
                    "scenario": RolloverScenario.CORE_DEPLOYMENT.value,
                    "is_manual_override_required": illiquidity_warning is not None,
                    "trigger_condition_text": signal.get("reason"),
                    "cash_impact": format_cash_impact(estimated_premium),
                    "limit_price": strike,
                    "strike": f"${strike:.2f}C",
                    "expiry": expiry,
                    "direction": "STO",
                    "is_covered_call_overlay": True,
                }
            )

        return instructions
