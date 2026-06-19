"""
gamma_squeeze_engine.py — Nexus Gamma Squeeze 量化風控決策引擎。

從 intraday_pipeline.py 分離，包含 NexusGammaSqueezeEngine：
  - 四階段門檻評估（流動性、財務跑道、Kelly 倉位、Vanna 對沖）
  - 生存分析與每日 Theta 對沖覆蓋率
  - 戰術性操作路由（SPEAR / SHIELD / WAIT）
"""

import math
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from market_time import ny_tz
from market_analysis.models.trader_models import (
    TraderAccountState,
    OptionHolding,
    TickerMarketData,
    AdvancedTraderOutput,
)

logger = logging.getLogger(__name__)


class NexusGammaSqueezeEngine:
    """
    Nexus Gamma Squeeze 量化風控與決策引擎。
    管理 4 階段戰術門檻、凱利倉位配比、帳戶生存跑道與 Vanna 對沖決策。
    """

    def __init__(self, base_gate_3_threshold: float = 1000000.0):
        self.gate_3_threshold: float = base_gate_3_threshold
        self.protection_score_history: List[Dict[str, Any]] = []

    def validate_gates(
        self, data: TickerMarketData, market_phase: str
    ) -> Tuple[bool, List[str]]:
        """
        執行 4 階段戰術硬性過濾門檻。
        - Gate 1: 流動性門檻 (市值 >= 20B 且日均期權量 >= 50,000)
        - Gate 2: 事件風險 (距離財報天數 > 3 天)
        - Gate 3: 資金效率 (明日到期 OTM Call 總成交權利金 >= $1M，Phase A 調降 30%)
        - Gate 4: 跨市場驗證 (IV Rank >= 50 或期權偏斜絕對值 >= 0.05)
        """
        failed = []

        # Gate 1: Liquidity Gate
        if data.market_cap_billion < 20.0 or data.avg_option_volume < 50000:
            failed.append(
                "流動性不足門檻：市值需 >= 20B 且日均期權成交量需 >= 50,000 口"
            )

        # Gate 2: Event Risk Gate
        if data.days_until_earnings <= 3:
            failed.append(
                f"事件風險超限：距離財報公佈僅剩 {data.days_until_earnings} 天 (需 > 3 天，防範 IV Crush 陷阱)"
            )

        # Gate 3: Capital Efficiency Gate
        threshold = self.gate_3_threshold
        if market_phase == "Phase A":
            threshold *= 0.70  # 開盤前一小時 (Phase A) 門檻降低 30%

        if data.tomorrow_expiring_otm_calls_premium < threshold:
            failed.append(
                f"資金效率不足：明日到期 OTM Call 總權利金為 ${data.tomorrow_expiring_otm_calls_premium:,.2f}，低於要求門檻 ${threshold:,.2f}"
            )

        # Gate 4: Cross-Market Validation Gate
        if not (data.iv_rank >= 50.0 or abs(data.option_skew) >= 0.05):
            failed.append(
                f"跨市場驗證未達標：IV Rank 為 {data.iv_rank:.1f}，偏斜度為 {data.option_skew:.3f} (需 IV Rank >= 50 或 Skew 絕對值 >= 0.05)"
            )

        return len(failed) == 0, failed

    def analyze_ticker(
        self,
        data: TickerMarketData,
        account_state: TraderAccountState,
        options_holdings: List[OptionHolding],
        portfolio_greeks: Dict[str, float],
        market_phase: str,
        current_time: Optional[datetime] = None,
    ) -> AdvancedTraderOutput:
        """
        全功能量化決策分析，輸出 AdvancedTraderOutput。
        """
        if current_time is None:
            current_time = datetime.now(ny_tz)

        # 1. 檢查時段適用性
        is_applicable = market_phase != "Closed"

        # 2. 驗證 4 階段戰術門檻
        gates_passed, failed_gates = self.validate_gates(data, market_phase)

        # 3. SDDM 路由決策
        # - VIX >= 25.0: 強制 SHIELD 避險
        # - 未通過 4 階段門檻: SHIELD
        # - 通過且 VIX < 25: SPEAR 積極進攻
        if not is_applicable:
            sddm_route = "WAIT"
        elif not gates_passed:
            sddm_route = "SHIELD"
        elif account_state.current_vix >= 25.0:
            sddm_route = "SHIELD"
        else:
            sddm_route = "SPEAR"

        # 4. 財務跑道分析 (Financial Runway Analysis)
        daily_burn_rate = account_state.monthly_burn_rate / 30.0
        # 帳戶每日 Theta 總收益 (持倉數量 * 單口每日 Theta * 100 乘數)
        projected_theta_yield = sum(
            o.theta * o.quantity * 100 for o in options_holdings
        )

        if daily_burn_rate > 0:
            # 存活天數 = (可用儲備金 + 預計每日 Theta 收益) / 每日生活開銷
            runway_denominator = daily_burn_rate
            financial_runway_days = int(
                max(
                    0.0,
                    (account_state.cash_reserve + projected_theta_yield)
                    / runway_denominator,
                )
            )
            theta_coverage_pct = (projected_theta_yield / daily_burn_rate) * 100.0
        else:
            financial_runway_days = 9999
            theta_coverage_pct = 0.0

        # 生成生存狀態訊息
        if financial_runway_days >= 180:
            runway_status_msg = f"🟢 財務跑道極其安全 (生存跑道: {financial_runway_days} 天)，期權 Theta 每日覆蓋率達 {theta_coverage_pct:.1f}%，運營資金結構優良。"
        elif 90 <= financial_runway_days < 180:
            runway_status_msg = f"🟡 財務跑道良好 (生存跑道: {financial_runway_days} 天)，期權 Theta 每日覆蓋率為 {theta_coverage_pct:.1f}%，處於健康防守狀態。"
        elif 30 <= financial_runway_days < 90:
            runway_status_msg = f"🟠 財務跑道中等警戒 (生存跑道: {financial_runway_days} 天)，期權 Theta 每日覆蓋率為 {theta_coverage_pct:.1f}%，建議精簡持倉規模。"
        else:
            runway_status_msg = f"🔴 🚨 財務跑道極度危險！僅剩 {financial_runway_days} 天，期權 Theta 覆蓋率僅 {theta_coverage_pct:.1f}%，請立即關閉高風險部位並限制主動交易。"

        # 5. Gamma 磁吸目標價 (預估下一個整數期權行權價)
        spot = data.spot_price
        magnet_target = float(math.ceil(spot / 5.0) * 5.0)
        if abs(magnet_target - spot) < 0.01:
            magnet_target += 5.0

        # 6. 凱利公式戰力縮放 (Kelly Position Sizing)
        # 基準凱利百分比設為 0.25 (對應 55% 勝率, 1.5 盈虧比)
        base_kelly = 0.25
        vix = account_state.current_vix
        if vix < 15.0:
            kelly_position_scaling = base_kelly * 1.0  # 全力進攻 (All-in/Heavy)
        elif 15.0 <= vix < 25.0:
            kelly_position_scaling = base_kelly * 0.6  # 減速警惕 (Ready/Caution)
        else:
            kelly_position_scaling = (
                base_kelly * 0.1
            )  # 極限防守 (Dormant / 僅配置 10% 凱利權重)

        # 7. Vanna-Adjusted Delta 對沖決策 (Hidden Delta)
        # 計算現貨與波動率同步暴漲時，Vanna 帶來的非線性 Delta 漂移
        portfolio_vanna = portfolio_greeks.get("vanna", 0.0)
        beta = portfolio_greeks.get("beta", 1.0)
        # 假設盤中即時波動率波動為 +10% (0.10)
        d_vol = 0.10
        hidden_delta = portfolio_vanna * d_vol
        hidden_delta_shares = hidden_delta * 100.0  # 換算為標的股份 Delta 當量

        # 換算為 Beta 加權的 SPY/QQQ 對沖所需股數
        shares_needed = -round(hidden_delta_shares * beta)

        if abs(shares_needed) > 0:
            direction = "BUY 買入" if shares_needed > 0 else "SELL 賣出"
            vanna_hedging_instruction = f"組合 Delta 偏離！偵測到 Vanna 引起隱含 Delta 漂移 {hidden_delta_shares * beta:+.2f}。支援對沖建議：建立 [{direction} {abs(shares_needed)} 單位 SPY] 以恢復 Delta 中性。"
        else:
            vanna_hedging_instruction = (
                "組合 Delta 處於中性區間，目前無需進行 Vanna 對沖調整。"
            )

        # 8. 推薦動作
        recommended_actions = []
        if sddm_route == "SPEAR":
            recommended_actions.append(
                f"🏹 當前進入 SPEAR 進攻模組，標的 {data.ticker} 具備強大 Gamma 擠壓潛力。"
            )
            recommended_actions.append(
                f"🎯 預估上行磁吸目標價為 ${magnet_target:.2f}，建議分批建立 OTM Call。"
            )
            recommended_actions.append(
                f"📊 建議進攻合約規模限制於凱利上限 {kelly_position_scaling * 100:.1f}% 內。"
            )
        elif sddm_route == "SHIELD":
            recommended_actions.append("🛡️ 當前進入 SHIELD 避險模組，主動交易受限。")
            if not gates_passed:
                recommended_actions.append(
                    "❌ 戰術門檻未通過，不允許盲目追高。請參考未通過指標。"
                )
            if vix >= 25.0:
                recommended_actions.append(
                    f"⚠️ 市場 VIX 指數達 {vix:.2f} (高波動警戒區)，強烈建議暫停多頭部位，轉為買入尾盤保護性 Put。"
                )
            recommended_actions.append(
                "📈 請執行 Delta 中性平衡，降低整體投資組合的 Gamma 與 Vega 曝險。"
            )
        else:
            recommended_actions.append(
                "⏳ 目前市場未開盤或處於非交易時段，進入 WAIT 觀望模式。"
            )

        # 時段專屬邏輯 (Phase-specific optimization)
        if market_phase == "Phase A":
            recommended_actions.append(
                "⚡ 盤中時段 Phase A (開盤前小時)：市場定價混亂，注意滑價，流動性門檻已調降 30%。"
            )
        elif market_phase == "Phase C":
            recommended_actions.append(
                "🚨 盤中時段 Phase C (尾盤對沖)：為規避隔夜 Gamma 缺口與跳空風險，嚴格禁止新建短線 SPEAR 部位。"
            )
            if sddm_route == "SPEAR":
                recommended_actions.append(
                    "⚠️ 【尾盤 SPEAR 警戒】尾盤投機買盤強烈，若要建倉，必須搭配等比例 SPY PUT 作為隔夜安全閥！"
                )

        # 9. 風控備註
        notes = []
        if vix >= 25.0:
            notes.append(
                "當前市場恐慌指標高企 (VIX >= 25.0)，波動率期限結構轉為逆價差，防範市場系統性尾部風險。"
            )
        else:
            notes.append("當前波動率環境相對溫和，有利於低波動期權佈局。")

        if financial_runway_days <= 30:
            notes.append(
                "警告：您的財務存活跑道天數極低，禁止進行任何高槓桿或買方期權投機，優先以本金安全與獲利回收為第一要務。"
            )

        notes.append(
            "請隨時追蹤 Spot 與 IV 上漲產生的 Hidden Delta 漂移。對沖完成後，可使用 `/settle_hedge` 登錄對沖記錄。"
        )
        risk_mitigation_notes = " ".join(notes)

        return AdvancedTraderOutput(
            ticker=data.ticker,
            timestamp=current_time,
            market_phase=market_phase,
            is_applicable=is_applicable,
            failed_gates=failed_gates,
            sddm_route=sddm_route,
            financial_runway_days=financial_runway_days,
            theta_coverage_pct=theta_coverage_pct,
            runway_status_msg=runway_status_msg,
            magnet_target=magnet_target,
            recommended_actions=recommended_actions,
            vanna_hedging_instruction=vanna_hedging_instruction,
            kelly_position_scaling=kelly_position_scaling,
            risk_mitigation_notes=risk_mitigation_notes,
        )

    def run_post_market_attribution(
        self, portfolio_pnl: float, hedge_pnl: float
    ) -> Dict[str, Any]:
        """
        每日盤後 (16:30 ET) 對沖歸因與自我進化機制。
        計算對沖保護得分 (Protection Score)，反饋調節明日 Gate 3 資金效率門檻。
        """
        old_threshold = self.gate_3_threshold

        # 計算對沖防禦評分 (0-100)
        if portfolio_pnl < 0:
            # 虧損時，對沖是否有正回報？
            if hedge_pnl > 0:
                # 剛好對沖 100% 虧損得 100 分
                protection_score = min(100.0, (hedge_pnl / abs(portfolio_pnl)) * 100.0)
            else:
                protection_score = 0.0
        else:
            # 獲利時，對沖是否產生過度拖累？
            if hedge_pnl >= 0:
                protection_score = 100.0
            else:
                # 對沖虧損佔總利潤的比例，拖累越少，得分越高
                protection_score = max(
                    0.0, min(100.0, 100.0 + (hedge_pnl / portfolio_pnl) * 100.0)
                )

        # 自我進化反饋環節 (Feedback Loop)
        if protection_score >= 70.0:
            # 對沖效率高，防守強，可適度放寬進攻門檻
            self.gate_3_threshold = float(
                round(max(500000.0, self.gate_3_threshold * 0.90), 2)
            )
            evolution_msg = (
                f"🚀 盤後歸因進化成功！當前對沖防禦評分為 {protection_score:.1f}/100 (效率極佳)。"
                f"NRO 已自動調降明日 Gate 3 權利金進攻門檻 10%，新門檻為 ${self.gate_3_threshold:,.2f}，釋放進攻流動性。"
            )
        elif protection_score < 40.0:
            # 對沖效率過低，防守失效或成本過大，需收緊門檻過濾雜訊
            self.gate_3_threshold = float(
                round(min(2000000.0, self.gate_3_threshold * 1.15), 2)
            )
            evolution_msg = (
                f"⚠️ 盤後歸因進化警報！當前對沖防禦評分僅為 {protection_score:.1f}/100 (防守效率偏低或磨損過重)。"
                f"NRO 已自動調升明日 Gate 3 權利金門檻 15%，新門檻為 ${self.gate_3_threshold:,.2f}，以提升訊號品質。"
            )
        else:
            evolution_msg = (
                f"⚖️ 盤後歸因進化持平。當前對沖防禦評分為 {protection_score:.1f}/100 (符合預期區間)。"
                f"NRO 決定明日維持 Gate 3 權利金門檻為 ${self.gate_3_threshold:,.2f}。"
            )

        result = {
            "protection_score": protection_score,
            "old_threshold": old_threshold,
            "new_threshold": self.gate_3_threshold,
            "evolution_msg": evolution_msg,
        }

        self.protection_score_history.append(
            {
                "timestamp": datetime.now(ny_tz),
                "portfolio_pnl": portfolio_pnl,
                "hedge_pnl": hedge_pnl,
                "protection_score": protection_score,
                "old_threshold": old_threshold,
                "new_threshold": self.gate_3_threshold,
            }
        )

        return result
