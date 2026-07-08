from dataclasses import dataclass
from typing import Tuple, Optional


@dataclass
class RiskInsightsContext:
    symbol: str
    current_price: float
    put_wall: float
    net_gex_status: str  # "POSITIVE_GAMMA" 或 "NEGATIVE_GAMMA_ZONE"
    term_structure: float  # 期限結構近遠月比 (例如 > 1.05 為 Backwardation)
    uoa_institutional_short_call: bool  # 是否偵測到機構賣出 Call 壓制
    iv_rank: float
    max_pain_deviation_pct: float
    can_trade_spreads: bool
    cash_reserve_protection: bool
    expected_move_lower: Optional[float] = None
    has_positive_gamma_support: bool = False
    cb_triggered: bool = False


class InsightsEngine:
    @staticmethod
    def generate_cro_insight(
        context: RiskInsightsContext,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        核心風控鐵律代碼邏輯：
        回傳 (dmp_label, status_label, suggestion_override)
        """
        # 預設行為
        dmp_label = None
        status_label = None
        suggestion = None

        can_overwrite_dmp = (
            abs(context.max_pain_deviation_pct) > 0.10
        ) or context.cb_triggered
        is_near_max_pain = abs(context.max_pain_deviation_pct) <= 0.05

        # 底牆保衛 (Narrative Trap Override)
        if context.put_wall > 0 and context.current_price > 0:
            distance = (
                context.current_price - context.put_wall
            ) / context.current_price
            if distance <= 0.02 and context.net_gex_status == "NEGATIVE_GAMMA_ZONE":
                # 強制覆蓋所有磁吸回升標籤
                return (
                    "[底牆保衛 / 嚴防破位踩踏]",
                    "🛑 底牆保衛 / 嚴防破位踩踏",
                    "STOP_ALL_BUY",
                )

        if is_near_max_pain:
            status_label = "價格接近最大痛點，維持震盪"

        # 鐵律一：左側禁區破位
        if context.current_price < context.put_wall:
            if context.has_positive_gamma_support:
                if can_overwrite_dmp:
                    dmp_label = "[底牆測試 / 強支撐共振]"
                # 不觸發極端禁區停損指引，維持原本的 status_label
            elif (
                context.expected_move_lower is not None
                and context.current_price >= context.expected_move_lower
            ):
                if can_overwrite_dmp:
                    dmp_label = "[底牆測試 / 區間震盪]"
                # 不觸發極端停損
            elif is_near_max_pain:
                if can_overwrite_dmp:
                    dmp_label = "[底牆測試 / 痛點區間]"
                # 在痛點 +- 5% 內，同步為接近痛點，而非粗暴歸入底牆破位的極端清單
            else:
                if can_overwrite_dmp:
                    dmp_label = "[底牆破位]"
                if context.cash_reserve_protection:
                    status_label = "🛑 觸發鐵律一：左側禁區 0%"
                else:
                    status_label = "🛑 觸發鐵律一：物理封印 0%"
                suggestion = "STOP_ALL_BUY"
        # 鐵律二：高位避險與獲利鎖利牆
        elif (
            context.max_pain_deviation_pct > 0.05
            and context.iv_rank > 0.80
            and context.uoa_institutional_short_call
        ):
            # UOA 偵測到大額 ITM Long Put，判定機構空頭大鱷高位砸盤風險極高。禁止追加任何多頭子彈，強制計算並輸出 Stop Limit
            status_label = f"⚖️ 高位偏離：死守 ${context.current_price * 0.95:.2f} 鎖利"
            suggestion = "STOP_LIMIT"
        elif context.term_structure > 1.05 and context.iv_rank > 0.80:
            status_label = "⚠️ 觸發鐵律二：期權單腿熔斷"
            suggestion = "NO_SINGLE_LEG"

        if not context.can_trade_spreads and suggestion != "STOP_ALL_BUY":
            suggestion = "NO_SPREAD_ALLOW_SPOT"

        return dmp_label, status_label, suggestion
