from typing import Literal
from models.execution import (
    MarketCondition,
    ExecutionDecision,
    GridParameters,
    PositionSizing,
    ExitStrategy,
)


class ExecutionRouter:
    """
    執行決策路由服務 (Gatekeeper)。
    負責根據市場狀況將交易路由至 Module A (SHIELD: 網格防禦) 或 Module B (SPEAR: 期權攻擊)。
    採用無狀態設計，優化低記憶體環境下的運行效率。
    """

    def evaluate_market(self, condition: MarketCondition) -> ExecutionDecision:
        """
        評估市場狀況並返回執行決策。

        邏輯優先級：
        1. 系統性風險偵測 (SHIELD)
        2. 異常機會偵測 (SPEAR)
        3. 中性觀望 (STANDBY)
        """
        # 計算價格偏離度 (乖離率)
        deviation = abs(condition.asset_price - condition.ma20) / condition.ma20

        # --- SHIELD 觸發條件 (Gatekeeper 邏輯) ---
        # 1. 高波動 (VIX > 25)
        # 2. 尾端風險增加 (Skew 絕對值 > 5%)
        # 3. 超買超賣導致的均線偏離 (> 10%)

        if condition.vix > 25:
            return ExecutionDecision(
                decision_type="SHIELD",
                trigger_reason=f"市場波動率過高 (VIX: {condition.vix:.2f})，啟動防禦性網格策略 (SHIELD) 以對沖風險。",
                grid_params=self._calculate_atr_grid(condition),
                exit_strategy=self._define_trailing_stop(condition, "SHIELD"),
            )

        if abs(condition.skew_percent) > 0.05:
            return ExecutionDecision(
                decision_type="SHIELD",
                trigger_reason=f"市場偏度異常 (Skew: {condition.skew_percent*100:.2f}%)，防範潛在黑天鵝事件。",
                grid_params=self._calculate_atr_grid(condition),
                exit_strategy=self._define_trailing_stop(condition, "SHIELD"),
            )

        if deviation > 0.10:
            return ExecutionDecision(
                decision_type="SHIELD",
                trigger_reason=f"價格嚴重偏離 20MA (乖離率: {deviation*100:.2f}%)，進入震盪修復網格模式。",
                grid_params=self._calculate_atr_grid(condition),
                exit_strategy=self._define_trailing_stop(condition, "SHIELD"),
            )

        # --- SPEAR 觸發條件 (Gatekeeper 邏輯) ---
        # 1. 偵測到異常期權流 (UOA)
        # 2. 且市場環境相對穩定 (未觸發 SHIELD)

        if condition.uoa_detected:
            return ExecutionDecision(
                decision_type="SPEAR",
                trigger_reason="偵測到大宗異常期權流 (UOA)，市場情緒支撐攻擊性期權策略 (SPEAR)。",
                position_sizing=self._calculate_kelly_size(condition),
                exit_strategy=self._define_trailing_stop(condition, "SPEAR"),
            )

        # --- STANDBY 默認狀態 ---
        return ExecutionDecision(
            decision_type="STANDBY",
            trigger_reason="當前市場指標處於平衡區間，無明顯技術面或情緒面觸發信號。",
        )

    def _calculate_atr_grid(self, condition: MarketCondition) -> GridParameters:
        """
        根據 ATR 計算動態網格步長。
        步長公式：(ATR_14 * 1.2) / 當前價格，最小 0.5%，最大 3%。
        """
        raw_step = (condition.atr_14 * 1.2) / condition.asset_price
        final_step = max(0.005, min(raw_step, 0.03))

        return GridParameters(
            base_price=condition.asset_price, dynamic_step_percent=final_step
        )

    def _calculate_kelly_size(self, condition: MarketCondition) -> PositionSizing:
        """
        利用凱利公式計算倉位百分比，並實施風險上限控制。
        """
        # 假設預期勝率與 RSI 相關 (RSI < 50 時勝率預期較高，適合做多 UOA)
        expected_win_rate = 0.55 if condition.rsi_14 < 50 else 0.45
        # 預期賠率 (Profit/Loss Ratio) 設為固定的 1.8
        odds = 1.8

        # 凱利公式: f* = (bp - q) / b = p - (1-p)/b
        kelly_f = expected_win_rate - (1 - expected_win_rate) / odds

        # 安全邊際控制：Half-Kelly 並封頂於 15%
        safe_percentage = max(0.0, min(kelly_f * 0.5, 0.15))

        return PositionSizing(
            kelly_percentage=safe_percentage,
            max_capital_allocation=5000.0,  # 基礎分配額
            max_theta_exposure=30.0,  # 每日最大 Theta 消耗金額
        )

    def _define_trailing_stop(
        self, condition: MarketCondition, mode: Literal["SHIELD", "SPEAR"]
    ) -> ExitStrategy:
        """
        定義出場策略與移動止損觸發點。
        """
        if mode == "SHIELD":
            # 防禦模式：價格回歸均線即視為修正完成
            return ExitStrategy(
                trailing_stop_active=True,
                trigger_price=condition.ma20,
                condition_type="MA20_BREAK",
            )
        else:
            # 攻擊模式：RSI 轉弱 (超買回落) 或跌破支撐止損
            return ExitStrategy(
                trailing_stop_active=True,
                trigger_price=condition.asset_price * 0.94,  # 6% 硬止損
                condition_type="RSI_DROP",
            )
