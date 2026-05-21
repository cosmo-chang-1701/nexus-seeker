from __future__ import annotations

from models.schemas import EnhancedWatchlistMetrics, WatchlistTacticalPlan


class WatchlistRiskController:
    """將 watchlist 技術位階轉譯為 SDDM 戰術路由。"""

    @staticmethod
    def process_metrics(metrics: EnhancedWatchlistMetrics) -> WatchlistTacticalPlan:
        dynamic_grid_step = round(metrics.atr_14 * 0.5, 2)

        if metrics.current_price <= metrics.buy_price_phase2:
            hidden_delta_risk = round(
                metrics.beta * metrics.vanna_sensitivity * 100.0, 2
            )
            hedge_allocation_shares = int(round(abs(hidden_delta_risk)))
            if hidden_delta_risk > 0:
                hedge_instruction = (
                    f"🚨 緊急對沖指令：Hidden Delta {hidden_delta_risk:+.2f}，"
                    f"建議立即放空 {hedge_allocation_shares} 股 SPY 對沖。"
                )
            elif hidden_delta_risk < 0:
                hedge_instruction = (
                    f"🚨 緊急對沖指令：Hidden Delta {hidden_delta_risk:+.2f}，"
                    f"建議立即買入 {hedge_allocation_shares} 股 SPY 對沖。"
                )
            else:
                hedge_instruction = "🚨 緊急對沖指令：Hidden Delta 接近 0，先降槓桿並觀察下一個 30 分鐘節點。"
            return WatchlistTacticalPlan(
                scenario="hard-hedge",
                sddm_route="SHIELD (網格全面防禦)",
                action_guideline=(
                    f"{hedge_instruction} 現價已跌破 Phase 2 (${metrics.buy_price_phase2:.2f})，"
                    "先執行保命對沖，再評估是否保留底倉。"
                ),
                dynamic_grid_step=dynamic_grid_step,
                hidden_delta_risk=hidden_delta_risk,
                hedge_instruction=hedge_instruction,
                hedge_allocation_shares=hedge_allocation_shares,
                alert_level="red",
            )

        if metrics.current_price <= metrics.buy_price_phase1 and metrics.iv_rank > 65.0:
            return WatchlistTacticalPlan(
                scenario="premium-harvest",
                sddm_route="SHIELD (防禦網格 - 左側權利金收集)",
                action_guideline=(
                    f"IV Rank {metrics.iv_rank:.1f}% 偏高，建議以 Phase 2 "
                    f"(${metrics.buy_price_phase2:.2f}) 為履約價建立 Cash-Secured Put，"
                    "優先收租而非直接承接現股刀口。"
                ),
                dynamic_grid_step=dynamic_grid_step,
                hidden_delta_risk=0.0,
                hedge_instruction="觀察權利金擴張，等待更深支撐再決定是否轉現股。",
                hedge_allocation_shares=0,
                alert_level="yellow",
            )

        return WatchlistTacticalPlan(
            scenario="wait",
            sddm_route="WAIT (觀望 / 待機)",
            action_guideline="價格仍在防守框架內，維持觀察並等待更佳風險報酬比。",
            dynamic_grid_step=dynamic_grid_step,
            hidden_delta_risk=0.0,
            hedge_instruction=None,
            hedge_allocation_shares=0,
            alert_level="green",
        )
