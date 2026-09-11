"""
gamma_squeeze_engine.py — Nexus Gamma Squeeze 量化風控決策引擎。

從 intraday_pipeline.py 分離，包含 NexusGammaSqueezeEngine：
  - 四階段門檻評估（流動性、財務跑道、Kelly 倉位、Vanna 對沖）
  - 生存分析與每日 Theta 對沖覆蓋率
  - 戰術性操作路由（SPEAR / SHIELD / WAIT）
"""

import math
import logging
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

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
        # deque(maxlen=...) 而非 list：此 engine 為長駐單例（見
        # cogs/trading/scheduler.py::SchedulerCog.__init__），若未來
        # run_post_market_attribution() 被排入定期任務，list 會無上限持續
        # 增長；maxlen=252 約為一個交易年，超出即自動淘汰最舊項目。
        self.protection_score_history: Deque[Dict[str, Any]] = deque(maxlen=252)

    def validate_gates(
        self, data: TickerMarketData, market_phase: str
    ) -> Tuple[bool, List[str]]:
        """
        執行 4 階段戰術硬性過濾門檻。
        - Gate 1: 流動性門檻 (市值 >= 20B 且 15m 即時量比 RVOL_15m >= 1.0；Phase A 要求 >= 1.5x)
        - Gate 2: 事件風險 (距離財報天數 > 3 天)
        - Gate 3: 資金效率 (DTE>=7 且 Vol/OI>=0.8x 之價外 Call 主力成交權利金 >= $1M，Phase A 調高 30%)
        - Gate 4: 跨市場驗證 (IV Rank >= 50 或期權偏斜絕對值 >= 0.05)
        """
        failed = []

        # Gate 1: Liquidity Gate
        gate_1_fails = []
        if data.market_cap_billion < 20.0:
            gate_1_fails.append("市值需 >= 20B")

        # 優先採樣微觀結構指標 RVOL_15m (取消跨時段線性外推)
        if data.rvol_15m is not None:
            required_rvol = 1.5 if market_phase == "Phase A" else 1.0
            if data.rvol_15m < required_rvol:
                phase_a_tag = (
                    " (Phase A 開盤衝擊期要求 15m 放量 >= 1.5x SMA20)"
                    if market_phase == "Phase A"
                    else ""
                )
                gate_1_fails.append(
                    f"15m 成交量比 (RVOL_15m: {data.rvol_15m:.2f}x) 需 >= {required_rvol:.1f}x{phase_a_tag}"
                )
        else:
            # 兼容無 rvol_15m 之舊數據或測試情境
            # Phase A 時段提高 30% 門檻防範滑價與脆弱報價
            vol_threshold = 65000 if market_phase == "Phase A" else 50000
            if data.avg_option_volume < vol_threshold:
                gate_1_fails.append(f"日均期權成交量需 >= {vol_threshold:,} 口")

        if gate_1_fails:
            failed.append(f"流動性不足門檻：{' 且 '.join(gate_1_fails)}")

        # Gate 2: Event Risk Gate
        if data.days_until_earnings <= 3:
            failed.append(
                f"事件風險超限：距離財報公佈僅剩 {data.days_until_earnings} 天 (需 > 3 天，防範 IV Crush 陷阱)"
            )

        # Gate 3: Capital Efficiency Gate
        # Phase A 開盤衝擊期點差大、報價脆弱，流動性門檻調高 30%（嚴禁逆向放寬）
        threshold = self.gate_3_threshold
        if market_phase == "Phase A":
            threshold *= 1.30

        if data.tomorrow_expiring_otm_calls_premium < threshold:
            failed.append(
                f"資金效率不足：DTE>=7 主力 OTM Call 總權利金為 ${data.tomorrow_expiring_otm_calls_premium:,.2f}，低於要求門檻 ${threshold:,.2f}"
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

        # 3. 財務跑道分析 (Financial Runway Analysis) - 優先於路由決策
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
        is_runway_critical = financial_runway_days < 30
        if financial_runway_days >= 180:
            runway_status_msg = f"🟢 財務跑道極其安全 (生存跑道: {financial_runway_days} 天)，期權 Theta 每日覆蓋率達 {theta_coverage_pct:.1f}%，運營資金結構優良。"
        elif 90 <= financial_runway_days < 180:
            runway_status_msg = f"🟡 財務跑道良好 (生存跑道: {financial_runway_days} 天)，期權 Theta 每日覆蓋率為 {theta_coverage_pct:.1f}%，處於健康防守狀態。"
        elif 30 <= financial_runway_days < 90:
            runway_status_msg = f"🟠 財務跑道中等警戒 (生存跑道: {financial_runway_days} 天)，期權 Theta 每日覆蓋率為 {theta_coverage_pct:.1f}%，建議精簡持倉規模。"
        else:
            runway_status_msg = f"🔴 🚨 財務跑道極度危險！僅剩 {financial_runway_days} 天，期權 Theta 覆蓋率僅 {theta_coverage_pct:.1f}%，觸發存活熔斷，嚴禁建立期權買方部位！"

        # 4. Gamma 磁吸目標價演算與微觀結構否決 (GEX Peak & Asymmetric Payoff)
        spot = data.spot_price
        min_target_price = spot * 1.05  # 向上空間必須滿足 >= 5% 的非對稱獲利要求
        magnet_target: Optional[float] = None
        microstructure_veto = False
        veto_reasons: List[str] = []

        def _is_severe_sto_cap(strike: float) -> bool:
            if not data.physical_cap_strikes:
                return False
            for cap in data.physical_cap_strikes:
                try:
                    c_strike = float(cap.get("strike", 0.0))
                    c_ratio = float(cap.get("ratio", 0.0))
                    c_type = str(cap.get("type", "")).upper()
                    c_action = str(cap.get("action", "STO")).upper()
                    # Call STO 且 ratio > 1.0x OI
                    if (
                        abs(c_strike - strike) < 0.01
                        and c_ratio > 1.0
                        and ("STO" in c_action)
                        and ("C" in c_type or c_type == "CALL")
                    ):
                        return True
                except (ValueError, TypeError):
                    continue
            return False

        # 全鏈淨 GEX 檢查：若標的整體處於實質負 Gamma (Net GEX < 0)，做市商順向拋壓阻礙上行，一律否決
        if data.net_gex is not None and data.net_gex < 0:
            microstructure_veto = True
            veto_reasons.append(
                "微觀結構否決：標的處於負 Gamma 泥淖 (Net GEX < 0)，做市商順向拋壓阻礙上行"
            )

        # 若 Call Wall 空間不足 5% 或遭遇天量 STO 封頂，阻力牆壓頂，同樣予以否決
        if (
            not microstructure_veto
            and data.call_wall is not None
            and data.call_wall > 0
        ):
            if data.call_wall < min_target_price:
                microstructure_veto = True
                veto_reasons.append(
                    f"空間不足否決：Call Wall (${data.call_wall:.2f}) 距現價 (${spot:.2f}) 向上空間不足 5%，不滿足非對稱獲利要求"
                )
            elif _is_severe_sto_cap(data.call_wall):
                microstructure_veto = True
                veto_reasons.append(
                    f"微觀結構否決：Call Wall (${data.call_wall:.2f}) 遭遇 > 1.0x OI 之天量 STO 剛性物理封頂，主力築頂防守"
                )

        if not microstructure_veto:
            if data.gex_profile:
                # 尋找現價上方且空間 >= 5% 的實體正 Gamma 深度節點 (GEX Peak)
                candidate_peaks: List[Tuple[float, float]] = []
                for k_str, gex_val in data.gex_profile.items():
                    try:
                        k = float(k_str)
                        g = float(gex_val)
                        if k >= min_target_price and g > 0:
                            candidate_peaks.append((k, g))
                    except (ValueError, TypeError):
                        continue

                # 依正 GEX 深度降序排列
                candidate_peaks.sort(key=lambda x: x[1], reverse=True)

                target_found = False
                for cand_k, _ in candidate_peaks:
                    if _is_severe_sto_cap(cand_k):
                        continue  # 跳過遭遇天量 STO 剛性封頂的節點
                    magnet_target = cand_k
                    target_found = True
                    break

                if not target_found:
                    microstructure_veto = True
                    veto_reasons.append(
                        "微觀結構否決：現價上方無具備實體正 Gamma 深度 (GEX Peak) 且空間 >= 5% 之安全磁吸目標（候選履約價均沉澱負 Gamma 斷層或遭遇 > 1.0x OI 之天量 STO 封頂）"
                    )
            elif data.call_wall is not None and data.call_wall > 0:
                magnet_target = data.call_wall
            else:
                # 降級純代數計算：仍強制滿足 >= 5% 空間
                candidate = float(math.ceil(min_target_price / 5.0) * 5.0)
                if candidate < min_target_price:
                    candidate += 5.0
                if _is_severe_sto_cap(candidate):
                    microstructure_veto = True
                    veto_reasons.append(
                        f"微觀結構否決：目標價 (${candidate:.2f}) 遭遇 > 1.0x OI 之天量 STO 剛性物理封頂"
                    )
                else:
                    magnet_target = candidate

        # 5. SDDM 路由決策 (全局風控優先級仲裁)
        # 優先級：存活熔斷 (Runway < 30) SHIELD > 未開盤 WAIT > 微觀否決 SHIELD > 門檻未過 SHIELD > VIX>=25 SHIELD > SPEAR
        vix = account_state.current_vix
        if is_runway_critical:
            # 全局風控覆蓋機制 (Global Risk Override)：強制硬鎖，阻斷所有買方進攻
            sddm_route = "SHIELD"
        elif not is_applicable:
            sddm_route = "WAIT"
        elif microstructure_veto:
            sddm_route = "SHIELD"
            for vr in veto_reasons:
                failed_gates.append(vr)
        elif not gates_passed:
            sddm_route = "SHIELD"
        elif vix >= 25.0:
            sddm_route = "SHIELD"
        else:
            sddm_route = "SPEAR"

        # 6. 分數凱利倉位上限 (Fractional Kelly & Hard Cap)
        # 單筆方向性期權買方倉位硬性限制在 3%~5% 內；存活跑道告急時強制歸零
        if is_runway_critical:
            kelly_position_scaling = 0.0
        elif sddm_route != "SPEAR":
            kelly_position_scaling = 0.0
        else:
            max_fractional_kelly = 0.05  # 5.0% 硬上限
            if vix < 15.0:
                kelly_position_scaling = max_fractional_kelly * 1.0  # 5.0%
            elif 15.0 <= vix < 25.0:
                kelly_position_scaling = max_fractional_kelly * 0.6  # 3.0%
            else:
                kelly_position_scaling = 0.0

        # 7. Vanna-Adjusted Delta 對沖決策 (跨資產 Beta 與價格比率校準)
        portfolio_vanna = portfolio_greeks.get("vanna", 0.0)
        beta = portfolio_greeks.get("beta", 1.0)
        d_vol = 0.10
        hidden_delta = portfolio_vanna * d_vol
        hidden_delta_shares = hidden_delta * 100.0  # 標的 Delta 股數當量

        # 標的內部對沖 (首選)
        internal_shares = -round(hidden_delta_shares)

        # 跨資產 SPY 對沖 (次選)：Delta_TSLA * beta * (P_TSLA / P_SPY)
        spy_spot = portfolio_greeks.get("spy_price", 0.0)
        if spy_spot <= 0.0:
            spy_spot = 500.0  # 基準參考價
        price_ratio = (data.spot_price / spy_spot) if spy_spot > 0 else 1.0
        spy_shares_needed = -round(hidden_delta_shares * beta * price_ratio)

        if abs(internal_shares) > 0:
            int_dir = "BUY 買入" if internal_shares > 0 else "SELL 賣出"
            spy_dir = "BUY 買入" if spy_shares_needed > 0 else "SELL 賣出"
            vanna_hedging_instruction = (
                f"組合 Delta 偏離！偵測到 Vanna 引起隱含 Delta 漂移 {hidden_delta_shares:+.2f}。\n"
                f" ├─ 首選標的內部對沖：建立 [{int_dir} {abs(internal_shares)} 股 {data.ticker}] 現貨/期權平衡\n"
                f" └─ 次選跨資產 SPY 對沖 (Beta={beta:.2f}, 價格比={price_ratio:.2f})：建立 [{spy_dir} {abs(spy_shares_needed)} 單位 SPY]"
            )
        else:
            vanna_hedging_instruction = (
                "組合 Delta 處於中性區間，目前無需進行 Vanna 對沖調整。"
            )

        # 8. 波動率體制判定 (廢除靜態文字，IVR > 50% 強制高波洗盤環境)
        is_high_iv = data.iv_rank > 50.0
        notes = []
        if is_high_iv:
            iv_str = (
                f" (即時 IV: {data.realtime_iv:.1%})"
                if data.realtime_iv is not None
                else ""
            )
            notes.append(
                f"🔥 波動率體制：當前處於【高波劇烈洗盤環境】(IV Rank: {data.iv_rank:.1f}%{iv_str} > 50%)。"
                "嚴禁裸買 OTM 期權以規避劇烈波動率回縮 (IV Crush) 殺傷，進攻時應改採垂直價差 (Bull Call Spread) 或賣方保護。"
            )
        elif vix >= 25.0:
            notes.append(
                f"⚠️ 市場恐慌指標高企 (VIX: {vix:.2f} >= 25.0)，波動率期限結構轉為逆價差，防範市場系統性尾部風險。"
            )
        else:
            notes.append(
                f"當前波動率環境相對溫和 (VIX: {vix:.2f}, IV Rank: {data.iv_rank:.1f}%)，有利於低波動期權佈局。"
            )

        if is_runway_critical:
            notes.append(
                f"🚨 存活風控硬鎖：您的財務存活跑道僅剩 {financial_runway_days} 天 (< 30 天)，觸發全局熔斷！"
                "已強制阻斷所有方向性期權買方開倉訊號，嚴禁任何買方投機，請立即關閉高風險部位。"
            )
        elif financial_runway_days <= 60:
            notes.append(
                f"⚠️ 存活警戒：財務跑道剩餘 {financial_runway_days} 天，建議嚴格控管倉位並回收流動性。"
            )

        notes.append(
            "請隨時追蹤 Spot 與 IV 變化產生的 Hidden Delta 漂移。對沖完成後，可使用 `/settle_hedge` 登錄對沖記錄。"
        )
        risk_mitigation_notes = " ".join(notes)

        # 9. 推薦動作 (嚴格遵守風控優先級，硬鎖時遮蔽所有買方建議)
        recommended_actions = []
        if is_runway_critical:
            recommended_actions.append(
                f"🚨 【存活風控硬鎖 (Hard Lock)】生存跑道僅剩 {financial_runway_days} 天 (< 30 天)！"
            )
            recommended_actions.append(
                "⛔ 全局風控覆蓋機制已啟動：強制阻斷並遮蔽所有方向性期權買方開倉訊號，嚴禁建立 OTM Call！"
            )
            recommended_actions.append(
                "🛡️ 當前路由硬鎖為 SHIELD 避險模組，首要任務為關閉高風險部位、確保本金安全。"
            )
        elif sddm_route == "SPEAR":
            recommended_actions.append(
                f"🏹 當前進入 SPEAR 進攻模組，標的 {data.ticker} 具備強大 Gamma 擠壓潛力。"
            )
            target_str = f"${magnet_target:.2f}" if magnet_target is not None else "--"
            if is_high_iv:
                recommended_actions.append(
                    f"🎯 預估上行磁吸目標價為 {target_str}。因處於高波環境 (IVR > 50%)，限制裸買 OTM 期權，建議改以牛市認購價差 (Bull Call Spread) 進攻。"
                )
            else:
                recommended_actions.append(
                    f"🎯 預估上行磁吸目標價為 {target_str}，建議分批建立 OTM Call。"
                )
            recommended_actions.append(
                f"📊 建議進攻合約規模限制於分數凱利上限 {kelly_position_scaling * 100:.1f}% 內（嚴守 3%~5% 絕對硬上限）。"
            )
        elif sddm_route == "SHIELD":
            recommended_actions.append("🛡️ 當前進入 SHIELD 避險模組，主動交易受限。")
            if microstructure_veto or not gates_passed:
                for fg in failed_gates:
                    recommended_actions.append(f"❌ {fg}")
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

        # 時段專屬邏輯
        if market_phase == "Phase A":
            recommended_actions.append(
                "⚡ 盤中時段 Phase A (開盤衝擊期)：市場定價混亂、滑價風險極高，流動性門檻已調高 30%（要求 15m 實體 K 棒收盤且 Volume_15m >= 1.5x SMA20），未完成右側驗證前嚴禁盲目追單！"
            )
        elif market_phase == "Phase C":
            recommended_actions.append(
                "🚨 盤中時段 Phase C (尾盤對沖)：為規避隔夜 Gamma 缺口與跳空風險，嚴格禁止新建短線 SPEAR 部位。"
            )
            if sddm_route == "SPEAR":
                recommended_actions.append(
                    "⚠️ 【尾盤 SPEAR 警戒】尾盤投機買盤強烈，若要建倉，必須搭配等比例 SPY PUT 作為隔夜安全閥！"
                )

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
