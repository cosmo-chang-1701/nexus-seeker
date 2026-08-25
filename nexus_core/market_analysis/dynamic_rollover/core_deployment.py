from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from . import logger
from ._shared import format_cash_impact, format_illiquidity_warning
from .constants import (
    _BOXX_DEFENSE_THRESHOLD,
    _CORE_EXCESS_MIN_TRADE_PCT,
    _COVERED_CALL_MAX_DTE,
    _COVERED_CALL_MAX_LOTS,
    _COVERED_CALL_MIN_DTE,
    _COVERED_CALL_MIN_SHARES,
)
from .models import RolloverInstruction, RolloverScenario


class _CoreDeploymentMixin:
    """邏輯 (5)：核心資金部署 (Core Capital Deployment)。

    Scenario 2 (opportunity_cost.py) 與 Scenario 3 (anti_washout.py) 的持倉迴圈
    都只處理 asset_class == "SATELLITE"，CORE 持倉 (如 VOO) 永遠只被當成轉倉的
    「目的地」，從未被當成「來源」。本情境補上這個缺口：當使用者透過 /edit_holding
    在 CORE 持倉上明確設定過 target_allocation_pct，且目前配置超過該目標時，將超額
    部位部署出去。

    每個 CORE 持倉的超額資金去向由 boxx_allocation_pct (0-100，/edit_holding 選填，
    未設定時由 suggest_boxx_allocation_pct() 依當前總經數據自動評估建議值) 與
    _BOXX_DEFENSE_THRESHOLD (50.0) 比較決定：
    - >= 50：防禦分支，超額資金整筆轉入 BOXX 鎖定無風險利息，不需候選標的通過
      進場鐵律 (BOXX 是純防禦性現金替代品停泊，不該被「有沒有找到好標的」卡住)。
    - < 50：機會分支，沿用既有邏輯，需候選標的存在且經 _confirm_entry_signal
      六重鐵律確認突破，才將超額資金整筆投入候選標的。
    """

    if TYPE_CHECKING:
        # 由 DynamicRolloverEngine（__init__.py）經 _OpportunityCostMixin 實際提供，
        # 此處僅供 mypy 解析 mixin 之間互相依賴的方法簽名，執行期不會用到這個宣告。
        async def _confirm_entry_signal(
            self,
            candidate_symbol: str,
            candidate_radar: Dict[str, Any],
            target_spot: float,
        ) -> Tuple[bool, str]: ...

    async def evaluate_core_deployment(
        self,
        user_id: int,
        portfolio_assets: List[Dict[str, Any]],
        already_flagged_symbols: set,
        total_account_value: float,
        candidate_symbol: str,
        candidate_radar: Optional[Dict[str, Any]],
        precomputed_entry_confirmation: Optional[Tuple[bool, str]] = None,
    ) -> List[RolloverInstruction]:
        """
        邏輯 (5)：對每一個使用者明確設定過 target_allocation_pct 的 CORE 持倉，
        若目前配置超過該目標，將超額部位部署進單一預篩選候選標的 (candidate_symbol)，
        產生與其他情境相同結構的 instruction dict。

        candidate_symbol / candidate_radar：由呼叫端 (cog 層) 沿用 Scenario 2 已經
        算好的候選發現結果，本情境不重複掃描 watchlist，也不重複抓取 radar 資料。

        precomputed_entry_confirmation：由呼叫端沿用 Scenario 2
        (evaluate_opportunity_cost_for_satellites) 針對同一 candidate_symbol/
        candidate_radar 已經算好的 _confirm_entry_signal 結果 (is_confirmed, reason)。
        兩者的確認結果僅取決於 (candidate_symbol, candidate_radar, target_spot)，
        在同一輪次呼叫端傳入的三者皆相同，故重用安全且非僅為效能優化。若為 None
        (例如 Scenario 2 未觸及確認步驟，如 candidate_symbol 為 "VOO")，則照舊
        獨立呼叫 _confirm_entry_signal。
        """
        instructions: List[RolloverInstruction] = []
        if total_account_value <= 0.0:
            return instructions  # 無有效帳戶總值可供計算配置比例

        # 候選標的是否可用，僅決定「機會分支」是否有得選；防禦分支 (轉入 BOXX)
        # 不依賴候選標的，即使沒找到高 EV 候選標的也應照樣評估。
        has_valid_candidate = candidate_symbol != "VOO" and bool(candidate_radar)
        target_spot = 0.0
        if has_valid_candidate and candidate_radar is not None:
            target_spot = float(
                candidate_radar.get("quote", {}).get("c", 0.0)
                if candidate_radar.get("quote")
                else 0.0
            )

        # 六重鐵律確認結果與總經自動建議值同一輪次內只需計算一次，跨多個 CORE
        # 持倉共用，避免對同一候選標的/總經數據重複發送請求。若呼叫端已提供
        # Scenario 2 算好的結果，直接沿用，跳過下方的重新確認。
        candidate_entry_confirmed: Optional[bool] = None
        candidate_entry_reason: str = ""
        if precomputed_entry_confirmation is not None:
            candidate_entry_confirmed, candidate_entry_reason = (
                precomputed_entry_confirmation
            )
        boxx_auto_suggestion: Optional[float] = None

        for asset in portfolio_assets:
            symbol = str(asset.get("symbol", "")).upper()
            if asset.get("asset_class") != "CORE":
                continue
            if symbol in already_flagged_symbols or symbol == candidate_symbol:
                continue

            # 嚴格 opt-in 閘門：CORE 持倉在 portfolio_monitor.py 預設
            # max_allocation_pct=1.0（100%），target_allocation_pct 只有使用者
            # 透過 /edit_holding 明確設定過才會出現在此 dict。刻意不使用
            # asset.get("target_allocation_pct", max_alloc) 這種有預設值的
            # fallback（那是 Scenario 3 SATELLITE 修剪的行為），否則會讓所有
            # 從未表態過的滿倉 VOO 使用者被意外觸發部署。
            target_alloc = asset.get("target_allocation_pct")
            if target_alloc is None:
                continue

            current_value = float(asset.get("current_value", 0.0))
            if current_value <= 0.0:
                continue

            current_alloc = current_value / total_account_value
            excess_alloc = current_alloc - float(target_alloc)
            if excess_alloc <= _CORE_EXCESS_MIN_TRADE_PCT:
                continue  # 超額配置低於雜訊門檻，不觸發部署，避免 dust trade

            excess_value = excess_alloc * total_account_value
            sell_ratio = round(min(1.0, max(0.0, excess_value / current_value)), 4)
            if sell_ratio <= 0.0:
                continue

            recovered_cash = current_value * sell_ratio
            cash_impact = format_cash_impact(recovered_cash)

            # boxx_allocation_pct 以 0.0-1.0 fraction 儲存 (與 max/target_allocation_pct
            # 慣例一致)，換算為 0-100 尺度與 _BOXX_DEFENSE_THRESHOLD 比較。
            boxx_pct_setting_raw = asset.get("boxx_allocation_pct")
            is_user_set_boxx_pct = boxx_pct_setting_raw is not None
            if boxx_pct_setting_raw is not None:
                boxx_pct = float(boxx_pct_setting_raw) * 100.0
            else:
                if boxx_auto_suggestion is None:
                    from market_analysis.index_microstructure import (
                        suggest_boxx_allocation_pct,
                    )

                    boxx_auto_suggestion = await suggest_boxx_allocation_pct()
                boxx_pct = boxx_auto_suggestion

            if boxx_pct >= _BOXX_DEFENSE_THRESHOLD:
                # 防禦分支：不需候選標的通過進場鐵律，直接部署至 BOXX 鎖定無風險利息。
                basis_text = (
                    f"使用者設定防禦閾值 {boxx_pct:.0f}"
                    if is_user_set_boxx_pct
                    else f"依當前總經數據自動評估防禦閾值 {boxx_pct:.0f}"
                )
                reason_text = (
                    "🌱 **核心資金部署 (Core Capital Deployment)**\n"
                    f"{symbol} 目前配置 {current_alloc:.1%}，超過使用者設定之目標配置 "
                    f"{float(target_alloc):.1%}（超額 {excess_alloc:.1%}）。{basis_text} "
                    "(≥50 優先防禦)，建議將超額核心資金轉入 BOXX 鎖定無風險利息，暫緩投入新部位。"
                )
                instructions.append(
                    {
                        "symbol": symbol,
                        "action": "LIQUIDATE" if sell_ratio >= 1.0 else "REDUCE",
                        "sell_ratio": sell_ratio,
                        "target_core": "BOXX",
                        "reason": reason_text,
                        "suggested_strategy": "Buy Shares (防禦性現金替代品)",
                        "buy_action_label": "轉入 BOXX（鎖定無風險利息）",
                        "scenario": RolloverScenario.CORE_DEPLOYMENT.value,
                        "is_manual_override_required": False,
                        "cash_impact": cash_impact,
                        "limit_price": None,
                    }
                )
                continue

            # 機會分支：沿用既有邏輯，需候選標的存在且經六重鐵律確認突破。
            if not has_valid_candidate or candidate_radar is None:
                continue

            if candidate_entry_confirmed is None:
                # 防洗盤實戰策略：進場訊號六重嚴格過濾鐵律。與 Scenario 2 共用同一套
                # 把關，未通過時比照「找不到候選標的」的早退模式，靜默略過。
                (
                    candidate_entry_confirmed,
                    candidate_entry_reason,
                ) = await self._confirm_entry_signal(
                    candidate_symbol, candidate_radar, target_spot
                )
                if not candidate_entry_confirmed:
                    logger.info(
                        f"[{candidate_symbol}] 進場訊號未確認，靜默略過核心資金部署: "
                        f"{candidate_entry_reason}"
                    )
            if not candidate_entry_confirmed:
                continue

            bid = float(asset.get("bid", 0.0))
            ask = float(asset.get("ask", 0.0))
            illiquidity_warning = (
                format_illiquidity_warning(bid, ask)
                if asset.get("asset_class") == "OPTIONS"
                else None
            )
            is_illiquid_warning = illiquidity_warning is not None

            reason_text = (
                "🌱 **核心資金部署 (Core Capital Deployment)**\n"
                f"{symbol} 目前配置 {current_alloc:.1%}，超過使用者設定之目標配置 "
                f"{float(target_alloc):.1%}（超額 {excess_alloc:.1%}）。候選標的 "
                f"{candidate_symbol} 已通過進場訊號六重嚴格過濾鐵律確認突破，"
                f"建議部署部分閒置核心資金：{candidate_entry_reason}"
            )
            if illiquidity_warning:
                reason_text += illiquidity_warning

            instructions.append(
                {
                    "symbol": symbol,
                    "action": "LIQUIDATE" if sell_ratio >= 1.0 else "REDUCE",
                    "sell_ratio": sell_ratio,
                    "target_core": candidate_symbol,
                    "reason": reason_text,
                    "suggested_strategy": "Buy Shares",
                    "scenario": RolloverScenario.CORE_DEPLOYMENT.value,
                    "is_manual_override_required": is_illiquid_warning,
                    "cash_impact": cash_impact,
                    "limit_price": target_spot if target_spot > 0 else None,
                }
            )

        return instructions

    async def evaluate_covered_call_overlay(
        self,
        user_id: int,
        portfolio_assets: List[Dict[str, Any]],
        already_flagged_symbols: set,
    ) -> List[RolloverInstruction]:
        """
        邏輯 (5) 延伸：Covered Call Overlay (核心持倉加碼賣出備兌買權收租)。

        當總經處於正常震盪 (get_spx_capped_from_above_signal() 判定的
        regime == NORMAL)，但大盤 SPX (以可交易的 SPY 為代理標的) 受制於
        上方負 Gamma 泥淖與 STO 封頂、缺乏向上爆發力時，對任一持股數 >= 100
        股 (_COVERED_CALL_MIN_SHARES，1 口門檻) 的 CORE 持倉，額外推薦開立
        1 口 (_COVERED_CALL_MAX_LOTS) OTM Covered Call (DTE
        _COVERED_CALL_MIN_DTE~_COVERED_CALL_MAX_DTE 天，履約價下限錨定使用者
        成本線 avg_cost 與 SPY 負 Gamma 阻力區 swamp_strike 兩者較高者) 收取
        權利金，進一步壓降持倉成本。

        刻意獨立於 evaluate_core_deployment 之外，不巢狀其內：本分支不要求
        target_allocation_pct opt-in — 「核心衛星配置比例是否超額」與
        「要不要對既有部位加碼收租」在概念上互不相關，若安插進
        evaluate_core_deployment 既有的嚴格 opt-in 迴圈內，會讓從未透過
        /edit_holding 設定過 target_allocation_pct 的使用者永遠收不到這個
        建議，也會混淆該方法本身的 opt-in 契約說明。

        不賣出任何標的持股 (action="HOLD", sell_ratio=0.0)：這代表本分支的
        輸出天然被 portfolio_monitor.py 既有的 already_flagged_symbols 建構
        邏輯 (僅 action != "HOLD" 才計入已標記) 排除在外，可與
        evaluate_core_deployment 的其餘分支、Scenario 4 於同一輪次無矛盾地
        共存，不需要額外的互斥判斷。

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
            if symbol in already_flagged_symbols:
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
