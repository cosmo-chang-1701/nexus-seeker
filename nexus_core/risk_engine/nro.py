from __future__ import annotations

from models.schemas import EnhancedWatchlistMetrics, WatchlistTacticalPlan


class WatchlistRiskController:
    """將 watchlist 技術位階轉譯為 SDDM 戰術路由。"""

    @staticmethod
    def process_metrics(metrics: EnhancedWatchlistMetrics) -> WatchlistTacticalPlan:
        dynamic_grid_step = round(metrics.atr_14 * 0.5, 2)

        if metrics.current_price <= metrics.buy_price_phase2:
            return WatchlistTacticalPlan(
                scenario="hard-hedge",
                sddm_route="SHIELD (全面防禦中)",
                action_guideline=(
                    "現貨部位已進入 Hard-Hedge 全數出清路由，"
                    "無需執行 SPY 指數對沖，確保流動性完全回歸。"
                ),
                dynamic_grid_step=dynamic_grid_step,
                hidden_delta_risk=0.00,
                hedge_instruction=None,
                hedge_allocation_shares=0,
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
            action_guideline=(
                "價格仍在防守框架內，維持現貨 $1.00×$ 零槓桿死守，將雙手嚴格離開期權開倉鍵。"
            ),
            dynamic_grid_step=dynamic_grid_step,
            hidden_delta_risk=0.0,
            hedge_instruction=None,
            hedge_allocation_shares=0,
            alert_level="green",
        )
