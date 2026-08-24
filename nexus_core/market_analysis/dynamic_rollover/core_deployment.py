from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from market_analysis.option_guidance import is_spread_illiquid

from . import logger
from .constants import _BOXX_DEFENSE_THRESHOLD, _CORE_EXCESS_MIN_TRADE_PCT
from .models import RolloverScenario


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
    ) -> List[Dict[str, Any]]:
        """
        邏輯 (5)：對每一個使用者明確設定過 target_allocation_pct 的 CORE 持倉，
        若目前配置超過該目標，將超額部位部署進單一預篩選候選標的 (candidate_symbol)，
        產生與其他情境相同結構的 instruction dict。

        candidate_symbol / candidate_radar：由呼叫端 (cog 層) 沿用 Scenario 2 已經
        算好的候選發現結果，本情境不重複掃描 watchlist，也不重複抓取 radar 資料。
        """
        instructions: List[Dict[str, Any]] = []
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
        # 持倉共用，避免對同一候選標的/總經數據重複發送請求。
        candidate_entry_confirmed: Optional[bool] = None
        candidate_entry_reason: str = ""
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
            cash_impact = f"${recovered_cash:,.0f}" if recovered_cash > 0 else None

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
            is_illiquid_warning = asset.get(
                "asset_class"
            ) == "OPTIONS" and is_spread_illiquid(bid, ask)

            reason_text = (
                "🌱 **核心資金部署 (Core Capital Deployment)**\n"
                f"{symbol} 目前配置 {current_alloc:.1%}，超過使用者設定之目標配置 "
                f"{float(target_alloc):.1%}（超額 {excess_alloc:.1%}）。候選標的 "
                f"{candidate_symbol} 已通過進場訊號六重嚴格過濾鐵律確認突破，"
                f"建議部署部分閒置核心資金：{candidate_entry_reason}"
            )
            if is_illiquid_warning:
                spread_pct = (ask - bid) / ((ask + bid) / 2)
                reason_text += (
                    f"\n⚠️ **流動性警告**：合約點差過寬 (Bid ${bid:.2f} / Ask ${ask:.2f}，"
                    f"點差 {spread_pct:.1%})，建議採限價單並留意滑價。"
                )

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
