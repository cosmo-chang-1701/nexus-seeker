from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from market_analysis.option_guidance import is_spread_illiquid
from market_analysis.sentiment.history_storage import get_indicator_percentile

from . import logger
from ._shared import format_cash_impact
from .constants import (
    _ANTI_WASHOUT_EXTREME_ATR_MULT,
    _BUYER_LOCKOUT_IVR_THRESHOLD,
    _DEFAULT_MAX_ALLOCATION_PCT,
    _FALLBACK_TARGET_PRICE_ESTIMATE,
    _FORCED_SETTLEMENT_ROLL_MAX_DTE,
    _FORCED_SETTLEMENT_ROLL_MIN_DTE,
    _HOLDING_DTE_FORCED_SETTLEMENT_THRESHOLD,
    _IV_BUBBLE_THRESHOLD,
    _MICROSTRUCTURE_SL_NET_GEX_THRESHOLD,
    _MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT,
    _MICROSTRUCTURE_SL_TRAILING_CALLWALL_PROGRESS_PCT,
    _MICROSTRUCTURE_TP1_CALLWALL_PCT,
    _MICROSTRUCTURE_TP1_RATIO,
    _MICROSTRUCTURE_TP2_RATIO,
    _MICROSTRUCTURE_TP2_WALL_BREAK_PCT,
    _MICROSTRUCTURE_TP2_WALL_MIGRATION_PCT,
    _MICROSTRUCTURE_TP3_DELTA_THRESHOLD,
    _MICROSTRUCTURE_TP3_DTE_THRESHOLD,
    _MICROSTRUCTURE_TP3_RATIO,
)
from .models import RolloverInstruction, RolloverScenario
from .structural_signals import _resolve_canonical_anchor_base, evaluate_option_dte_tier
from .transition_engine import evaluate_transition_for_position


def apply_ivr_strategy_overlay_impl(
    is_selling_locked_by_ivr: Any,
    options_strategy: str,
    strategy_override: str,
    ivr: float,
) -> str:
    """
    IVR 策略防禦與微調。
    NOTE: strategy_override 傳入非空字串時會【完全取代】IVR 鎖定後綴邏輯 (elif，
    而非疊加)，供未來需要完整自訂策略文字的戰術覆寫場景使用（該場景下
    strategy_override 本身的文字已包含完整防守資訊，故刻意跳過 IVR 後綴）。
    """
    if strategy_override:
        return strategy_override
    if is_selling_locked_by_ivr(ivr):
        return options_strategy + f" | ⚠️ IVR 極低位 ({ivr:.1f}%): 賣方策略已鎖死。"
    if ivr > _BUYER_LOCKOUT_IVR_THRESHOLD:
        return options_strategy + " | 嚴禁買方 (IV 過高，規避 Gamma 陷阱)"
    return options_strategy


class _AntiWashoutMixin:
    """邏輯 (3)：核心與衛星比例再平衡 + 防洗盤動態停損引擎 (Anti-Washout Stop Engine)。"""

    if TYPE_CHECKING:
        # 由 DynamicRolloverEngine（__init__.py）實際提供，此處僅供 mypy 解析
        # mixin 之間互相依賴的方法簽名，執行期不會用到這個宣告。
        def _apply_ivr_strategy_overlay(
            self, options_strategy: str, strategy_override: str, ivr: float
        ) -> str: ...

    def _correct_wall_topology(self, metrics: dict) -> Tuple[float, float]:
        """
        期權拓撲微結構校正：計算防守錨點 (anchor_base) 與阻力天花板 (effective_res_wall)。
        若 put_wall 與 call_wall 顛倒，或是已有 GEX 提取之 support_wall / resistance_wall，
        優先採用後者。
        """
        spot = float(metrics.get("spot_price", 0.0))
        put_wall = float(metrics.get("put_wall", 0.0))
        call_wall = float(metrics.get("call_wall", 0.0))
        support_wall = float(metrics.get("support_wall", 0.0))
        resistance_wall = float(metrics.get("resistance_wall", 0.0))
        gamma_flip = float(metrics.get("gamma_flip", 0.0))
        hvn = float(metrics.get("hvn", 0.0))

        anchor_base = _resolve_canonical_anchor_base(
            support_wall, put_wall, call_wall, gamma_flip, hvn, spot
        )

        if resistance_wall > 0:
            effective_res_wall = resistance_wall
        elif put_wall > 0 and call_wall > 0 and put_wall > call_wall:
            effective_res_wall = max(put_wall, call_wall)
        elif call_wall > 0:
            effective_res_wall = call_wall
        else:
            effective_res_wall = spot * 1.05

        return anchor_base, effective_res_wall

    def _compute_anti_washout_stop(
        self, anchor_base: float, metrics: dict
    ) -> Tuple[float, float, float]:
        """
        防洗盤機制：計算精確防守位與掛單限價。
        回傳 (stop_loss, limit_price, extreme_stop_loss)。

        注意：DTE<=1 的部位不會呼叫本函式——已由呼叫端 (check_satellite_
        rebalancing_impl) 透過 evaluate_option_dte_tier() 判定為
        EXPIRATION_SETTLEMENT_ALERT 並短路為強制結算保護指令，因此本函式
        不再需要處理 0/1 DTE 分支。
        """
        spot = float(metrics.get("spot_price", 0.0))
        atr_15m = float(metrics.get("atr_15m", 0.0))
        lvn = float(metrics.get("lvn", 0.0))
        hvn = float(metrics.get("hvn", 0.0))

        # SL-結構失效 (微觀結構出場決策矩陣)：0.5x ATR 防護墊片
        if anchor_base > 0:
            raw_stop_loss = anchor_base - (
                _MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT * atr_15m
            )
        else:
            raw_stop_loss = spot * 0.96 if spot > 0 else 0.0

        base_stop_loss = raw_stop_loss

        # 不對機制 2 算出的基礎停損做人為區間鉗制：停損點位純粹依微觀結構公式
        # (anchor_base - 1.5×ATR_15m) 輸出，避免高波動標的、或 anchor_base
        # 距現價極近/極遠時被強行推寬或縮窄。下方機制 1 (LVN 吸附) 仍可依物理
        # 流動性理由調整最終停損。

        # 機制 1: 避開 LVN 陷阱 (量價拓撲吸附演算法：絕對吸附至次級 HVN 上緣 + 0.2*ATR_15m，禁止固定 % 平移)
        if lvn > 0 and base_stop_loss > 0 and abs(base_stop_loss - lvn) / lvn <= 0.015:
            secondary_hvn = float(metrics.get("secondary_hvn", 0.0))
            target_hvn = 0.0
            if secondary_hvn > 0 and secondary_hvn < lvn:
                target_hvn = secondary_hvn
            elif hvn > 0 and hvn < lvn:
                target_hvn = hvn
            elif anchor_base > 0 and anchor_base < lvn:
                target_hvn = anchor_base

            if target_hvn > 0:
                base_stop_loss = target_hvn + (0.2 * atr_15m)
            else:
                base_stop_loss = lvn - (1.0 * atr_15m)

        # 動態調整狀態切換引擎路徑 1 已上移的保本地板：Regime 只負責「抬高
        # 地板」，實際的執行judgement 仍完全由本階梯決定 (職責邊界，見
        # transition_engine.py 模組 docstring)。取 max 而非覆寫——結構性停損
        # 若已高於保本點 (部位續漲、anchor_base 隨之上移)，應沿用較高者，
        # 保本地板只保證「不會再退回成本以下」，不會反過來把停損拉低。
        ratchet_stop = float(metrics.get("ratchet_stop", 0.0) or 0.0)
        if ratchet_stop > 0:
            base_stop_loss = max(base_stop_loss, ratchet_stop)

        stop_loss = round(base_stop_loss, 2)
        limit_price = round(
            max(stop_loss - (0.5 * atr_15m if atr_15m > 0 else 0.6), stop_loss * 0.995),
            2,
        )

        # 軌道二：極端瞬時停損 (Extreme Tick Breach)。沿用與機制 2 完全相同的
        # anchor_base/atr_15m 原始輸入，獨立於上方 base_stop_loss 的 LVN 吸附
        # 管線之外計算，純粹 anchor_base - 3.0×ATR_15m 公式，不做任何邊界
        # 修飾——見 _apply_decision_matrix 的即時 tick 觸發判定。
        if anchor_base > 0 and atr_15m > 0:
            extreme_stop_loss = round(
                anchor_base - (_ANTI_WASHOUT_EXTREME_ATR_MULT * atr_15m), 2
            )
        else:
            extreme_stop_loss = 0.0

        return (
            stop_loss,
            limit_price,
            extreme_stop_loss,
        )

    def _evaluate_microstructure_tp_ladder(
        self, metrics: dict
    ) -> Tuple[Optional[str], float, str]:
        """微觀結構出場決策矩陣 - 止盈分層 (TP1/TP2/TP3)。

        無狀態、每 15 分鐘重新評估、無「TP1 是否已執行過」的持久化狀態，故採
        「本輪最高已觸發層級」而非累加：優先序 TP3 > TP2 > TP1。回傳
        (tier_name 或 None, sell_ratio, reason_text)。
        """
        spot = float(metrics.get("spot_price", 0.0))
        call_wall = float(metrics.get("call_wall", 0.0))
        delta_raw = metrics.get("delta")
        delta: Optional[float] = float(delta_raw) if delta_raw is not None else None
        vwap_loss_with_volume = bool(metrics.get("vwap_loss_with_volume", False))
        dte = int(metrics.get("dte", 99))

        if call_wall <= 0 or spot <= 0:
            return None, 0.0, ""

        wall_break_pct = (spot - call_wall) / call_wall
        is_tp1 = spot >= call_wall * _MICROSTRUCTURE_TP1_CALLWALL_PCT
        previous_call_wall = float(metrics.get("previous_call_wall") or 0.0)
        is_wall_break = wall_break_pct >= _MICROSTRUCTURE_TP2_WALL_BREAK_PCT
        is_wall_migrated_up = (
            previous_call_wall > 0
            and call_wall
            >= previous_call_wall * (1.0 + _MICROSTRUCTURE_TP2_WALL_MIGRATION_PCT)
            and spot >= previous_call_wall  # 現價必須站穩舊阻力牆，確認突破成立
        )
        is_tp2 = is_wall_break or is_wall_migrated_up
        is_tp3_delta = (
            delta is not None and delta >= _MICROSTRUCTURE_TP3_DELTA_THRESHOLD
        )
        is_tp3_dte = 0 < dte <= _MICROSTRUCTURE_TP3_DTE_THRESHOLD
        is_tp3 = is_tp3_delta or vwap_loss_with_volume or is_tp3_dte

        if is_tp3:
            triggers = []
            if is_tp3_delta and delta is not None:
                triggers.append(
                    f"Delta {delta:.2f} >= {_MICROSTRUCTURE_TP3_DELTA_THRESHOLD}"
                )
            if vwap_loss_with_volume:
                triggers.append("15m VWAP 帶量失守")
            if is_tp3_dte:
                triggers.append(f"DTE={dte} <= {_MICROSTRUCTURE_TP3_DTE_THRESHOLD}")
            return (
                "TP3",
                _MICROSTRUCTURE_TP3_RATIO,
                f"🎯 **TP3-終局平倉**：{' 或 '.join(triggers)}，趨勢動能耗竭，"
                f"消除非線性 Theta 耗損與做市商 Pinning 釘住風險，執行 "
                f"{_MICROSTRUCTURE_TP3_RATIO:.0%} 平倉。",
            )
        if is_tp2:
            if is_wall_migrated_up:
                migration_pct = (call_wall - previous_call_wall) / previous_call_wall
                return (
                    "TP2",
                    _MICROSTRUCTURE_TP2_RATIO,
                    f"🎯 **TP2-空間擴展**：做市商阻力牆向上遷移 ${previous_call_wall:.2f} → ${call_wall:.2f} "
                    f"({migration_pct:+.1%})，現價 ${spot:.2f} 站穩舊阻力位，釋放 Gamma 空間，"
                    f"執行 {_MICROSTRUCTURE_TP2_RATIO:.0%} 平倉。",
                )
            return (
                "TP2",
                _MICROSTRUCTURE_TP2_RATIO,
                f"🎯 **TP2-空間擴展**：現價已穿越 Call Wall ${call_wall:.2f} 達 "
                f"{wall_break_pct:+.2%}（>= {_MICROSTRUCTURE_TP2_WALL_BREAK_PCT:.1%}），"
                f"釋放 Gamma Squeeze 利潤、防範滯留回洗，執行 "
                f"{_MICROSTRUCTURE_TP2_RATIO:.0%} 平倉。",
            )
        if is_tp1:
            return (
                "TP1",
                _MICROSTRUCTURE_TP1_RATIO,
                f"🎯 **TP1-阻力初探**：現價 ${spot:.2f} 已達 Call Wall ${call_wall:.2f} 的 "
                f"{_MICROSTRUCTURE_TP1_CALLWALL_PCT:.1%}，做市商多頭避險動能竭盡，"
                f"執行 {_MICROSTRUCTURE_TP1_RATIO:.0%} 平倉。",
            )
        return None, 0.0, ""

    _evaluate_microstructure_tp_tiers = _evaluate_microstructure_tp_ladder

    def _evaluate_microstructure_sl_ladder(
        self,
        metrics: dict,
        anchor_base: float,
        stop_loss: float,
        asset_class: str,
    ) -> Tuple[Optional[str], float, str, Optional[float]]:
        """微觀結構出場決策矩陣 - 止損分層 (由硬到軟依序判定)：
        SL-結構失效 > SL-狀態翻轉 > SL-主力對沖 > SL-動態保本。

        回傳 (tier_name 或 None, sell_ratio, reason_text, new_stop_level)。
        new_stop_level 僅 SL-動態保本 (HOLD，停損上移至保本點) 會有值。
        """
        spot = float(metrics.get("spot_price", 0.0))
        price_15m_close = float(metrics.get("price_15m_close", spot))
        call_wall = float(metrics.get("call_wall", 0.0))
        net_gex_raw = metrics.get("net_gex")
        net_gex = float(net_gex_raw) if net_gex_raw is not None else None
        is_whale_put_block = bool(metrics.get("is_whale_put_block", False))
        avg_cost = float(metrics.get("avg_cost", 0.0))

        # 1. SL-結構失效：OPTIONS 現價即時貫穿 / SPOT 15m 實體收盤跌破
        is_structural_break = (
            (spot > 0 and spot < stop_loss)
            if asset_class == "OPTIONS"
            else (price_15m_close > 0 and price_15m_close < stop_loss)
        ) and stop_loss > 0
        if is_structural_break:
            return (
                "SL_STRUCTURAL",
                1.0,
                f"🚨 **SL-結構失效**："
                f"{'現價即時貫穿' if asset_class == 'OPTIONS' else '15m 實體收盤跌破'} "
                f"防守線 (${stop_loss:.2f} = 錨點 ${anchor_base:.2f} - "
                f"{_MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT}×ATR_15m)，跌入負 Gamma 區，"
                "做市商加速拋售，強制 100% 平倉。",
                None,
            )

        # 2. SL-狀態翻轉：個股 Net GEX 翻轉為負。net_gex 為 None（GEX 數據缺失/
        # 未曾抓取，而非「已抓到且確認 <= 0」）時一律 fail-safe 不觸發，避免將
        # 「資料缺失」誤判為「已確認負 Gamma」而對每一筆無 GEX 資料的部位強制清倉。
        if net_gex is not None and net_gex <= _MICROSTRUCTURE_SL_NET_GEX_THRESHOLD:
            return (
                "SL_REGIME_FLIP",
                1.0,
                f"🚨 **SL-狀態翻轉**：個股 Net GEX 已翻轉為 {net_gex:+,.0f}"
                f"（<= {_MICROSTRUCTURE_SL_NET_GEX_THRESHOLD:.0f}），"
                "全鏈市場狀態轉變，做市商避險邏輯消亡，強制 100% 平倉。",
                None,
            )

        # 3. SL-主力對沖：近平值單筆 PUT BTO 大單壓制 (真實 UOA 判定，見
        # structural_signals.py::_detect_whale_put_bto_block)
        if is_whale_put_block:
            return (
                "SL_WHALE_PUT",
                1.0,
                "🚨 **SL-主力對沖**：偵測到近平值單筆 PUT BTO 大單"
                "（權利金 >= $500k 且 Vol/OI >= 1.5x），機構級大單壓制，"
                "做市商產生即時做空對沖，強制 100% 平倉。",
                None,
            )

        # 4. SL-動態保本：現價漲幅達距 Call Wall 空間之 50%，停損上移至保本點
        if call_wall > anchor_base > 0 and spot > 0:
            progress = (spot - anchor_base) / (call_wall - anchor_base)
            if progress >= _MICROSTRUCTURE_SL_TRAILING_CALLWALL_PROGRESS_PCT:
                new_stop = anchor_base if avg_cost <= 0 else max(avg_cost, anchor_base)
                approx_note = (
                    "（期權部位無單筆成本基礎資料，以結構錨點近似保本點）"
                    if avg_cost <= 0
                    else ""
                )
                return (
                    "SL_TRAILING_BREAKEVEN",
                    0.0,
                    f"🛡️ **SL-動態保本**：現價距 Call Wall 空間已達 {progress:.0%}"
                    f"（>= {_MICROSTRUCTURE_SL_TRAILING_CALLWALL_PROGRESS_PCT:.0%}），"
                    f"停損上移至保本點 ${new_stop:.2f}{approx_note}，"
                    "鎖定基礎成本，消除本金承險敞口。",
                    new_stop,
                )

        return None, 0.0, "", None

    def _resolve_active_order_defense(
        self,
        symbol: str,
        active_orders: Optional[list[dict]],
        stop_loss: float,
        limit_price: float,
    ) -> Tuple[str, Optional[dict]]:
        """委託單聯動 (Active Orders)：比對現有委託單，產生防守機制描述文字。"""
        matching_order: Optional[dict] = None
        if active_orders:
            for ord_entry in active_orders:
                if (
                    ord_entry.get("symbol", "").upper() == symbol.upper()
                    and ord_entry.get("side", "SELL").upper() == "SELL"
                ):
                    matching_order = ord_entry
                    break

        if matching_order:
            order_id = matching_order.get("id", "")
            ord_stop = float(matching_order.get("stop_price") or stop_loss)
            ord_limit = float(matching_order.get("limit_price") or limit_price)
            order_defense_str = f"**委託單 #{order_id} 有效**\n\n**停損: ${ord_stop:.2f} | 限價: ${ord_limit:.2f}**"
        else:
            order_defense_str = f"**建議設置防守委託單**\n\n**停損: ${stop_loss:.2f} | 限價: ${limit_price:.2f}**"

        return order_defense_str, matching_order

    def _net_against_existing_order(
        self,
        sell_ratio: float,
        quantity: float,
        matching_order: Optional[dict],
    ) -> Tuple[float, str]:
        """委託單淨額扣抵：避免對已被既有 SELL 委託單覆蓋的部位重複疊加下單。
        若既有委託單數量已足額覆蓋建議賣出量，淨額扣抵至 0（降級為觀察持有）；
        若僅部分覆蓋，按比例扣減 sell_ratio。回傳 (淨額後 sell_ratio, 附加說明文字)。
        """
        if not matching_order or sell_ratio <= 0.0 or quantity <= 0.0:
            return sell_ratio, ""
        try:
            order_qty = abs(float(matching_order.get("quantity", 0.0)))
        except (TypeError, ValueError):
            order_qty = 0.0
        if order_qty <= 0.0:
            return sell_ratio, ""

        requested_qty = sell_ratio * quantity
        order_id = matching_order.get("id", "")
        if order_qty >= requested_qty:
            note = (
                f"\n♻️ **委託單淨額扣抵**：既有委託單 #{order_id} "
                f"已覆蓋建議賣出數量 ({order_qty:.0f} 股 ≥ 建議 {requested_qty:.0f} 股)，"
                f"降級為觀察持有，不重複疊加下單。"
            )
            return 0.0, note

        net_qty = requested_qty - order_qty
        net_ratio = round(net_qty / quantity, 4)
        note = (
            f"\n♻️ **委託單淨額扣抵**：既有委託單 #{order_id} "
            f"已覆蓋 {order_qty:.0f} 股，建議賣出比例由 {sell_ratio:.0%} 淨額調整為 {net_ratio:.0%}。"
        )
        return net_ratio, note

    def _maybe_append_tax_risk_note(
        self,
        is_forced_settlement: bool,
        is_same_symbol_reentry: bool,
        holding_period_days: Optional[int] = None,
    ) -> str:
        """稅務風險資訊性提示（純附加，不做任何攔截閘門，本系統不代為判定）。

        涵蓋三個最有風險的既有分支：
        1. DTE<=1 強制結算保護的價內短期合約平倉，可能觸發指派 (Assignment)。
        2. 同標的先賣出後又立即重新建倉 (如 Euphoria 雙軌機制留存部位開 Bear
           Call Spread)，可能落入 Wash Sale 規則範圍。
        3. 若持倉來源標記了 acquired_at（透過 /add_holding 或 /edit_holding
           設定），提示目前屬於長期 (>365 天) 或短期 (<=365 天) 資本利得稅率
           區間，供使用者評估是否值得延後平倉以跨越長期門檻。此為單一
           acquired_at 粗略估計，非完整多批次 (Lot-based FIFO) 成本基礎追蹤。
        """
        notes = []
        if is_forced_settlement:
            notes.append("DTE<=1 強制結算保護的價內合約平倉可能觸發指派 (Assignment)")
        if is_same_symbol_reentry:
            notes.append("同標的近期重新建立相似曝險，請留意 Wash Sale 規則")
        if holding_period_days is not None:
            if holding_period_days > 365:
                notes.append(
                    f"已持有 {holding_period_days} 天 (>365)，符合長期資本利得稅率區間"
                )
            else:
                days_left = 365 - holding_period_days
                notes.append(
                    f"已持有 {holding_period_days} 天 (<=365)，屬短期資本利得稅率區間"
                    f"（距長期門檻尚餘 {days_left} 天）"
                )
        if not notes:
            return ""
        return (
            "\n⚠️ **稅務提醒**：" + "；".join(notes) + "（本系統不代為判定，僅供參考）"
        )

    def _apply_decision_matrix(
        self,
        symbol: str,
        metrics: dict,
        requested_action: str,
        target: str,
        asset_class: str,
        tp_tier_result: Tuple[Optional[str], float, str],
        sl_tier_result: Tuple[Optional[str], float, str, Optional[float]],
        stop_loss: float,
        anchor_base: float,
        extreme_stop_loss: float = 0.0,
    ) -> Tuple[str, str, str, str, bool, float, Optional[str]]:
        """
        微觀結構出場決策矩陣 - 統一裁決。優先序：
        TP 分層 (TP1/TP2/TP3) > Track 2 極端瞬時停損 (黑天鵝最後防線，不受本
        矩陣影響) > OPTIONS IV 驟降快速出場 (Vega/IV crush，矩陣未涵蓋的獨立
        保護) > SL 分層 (SL-結構失效/SL-狀態翻轉/SL-主力對沖 一律 100% 平倉；
        SL-動態保本為 HOLD + 停損上移保本點) > 常規配置超額 REDUCE > HOLD。

        回傳 (final_action, final_target, options_strategy, system_conflict_note,
        is_extreme_tick_breach, sell_ratio, fired_tier)。is_extreme_tick_breach
        供呼叫端判斷是否需要將呈現層升級為最高急迫性樣式（見 rollover_embeds.py
        的立即人工執行標記）。sell_ratio 為本次裁決實際決定的執行比例（TP1/TP2/
        TP3 為各自的部分比例，其餘 LIQUIDATE 分支恆為 1.0，SL-動態保本/HOLD 恆為
        0.0；REDUCE 分支的實際比例由呼叫端的常規配置超額運算另行決定，此處
        僅填入佔位值 0.0，不影響呼叫端行為）。fired_tier 為純附加的分層識別碼
        (供 RolloverInstruction.exit_tier 記錄用途，不影響任何裁決邏輯)。
        """
        spot = float(metrics.get("spot_price", 0.0))

        tp_tier, tp_ratio, tp_reason = tp_tier_result
        sl_tier, sl_ratio, sl_reason, sl_new_stop = sl_tier_result

        final_target = target if target else "VOO"
        final_action = requested_action
        sell_ratio = 0.0
        system_conflict_note = ""

        ivr_drop = float(metrics.get("ivr_drop", metrics.get("ivr_change", 0.0)))
        is_ivr_fast_exit = asset_class == "OPTIONS" and ivr_drop >= 20.0

        # 軌道二：極端瞬時停損 (Extreme Tick Breach)。無論 SPOT 或 OPTIONS，
        # 現價貫穿即立即觸發，無視 15m 實體收盤等待。優先權高於 IV 驟降快速
        # 出場/SL 分層，僅次於 TP 分層獲利了結——`not tp_tier` 守衛是這個
        # 「僅次於」的必要條件，而非裝飾：若省略，即使 TP 分支才是實際決定
        # final_action/敘事的分支，這裡仍會回傳原始的價格穿透判定，導致下游
        # (呼叫端組裝 extreme_breach_detail_block、rollover_embeds.py 的立即
        # 人工執行紅色急迫樣式覆蓋) 誤把一則平靜的「🎯 獲利解鎖達成」通知，
        # 套上「🆘 立即人工執行」的緊急標題與極端熔斷詳情欄位——兩者敘事互相
        # 矛盾。真實案例：TSLA 現價同時站上 Call Wall (獲利解鎖) 且跌破以 GEX
        # Support Wall 算出的極端熔斷線，過去在此處會回傳 True，讓 embed 呈現
        # 「獲利了結」內文配「立即人工執行」急迫標題的錯亂訊息。
        is_extreme_tick_breach = not tp_tier and (
            spot < extreme_stop_loss if (extreme_stop_loss > 0 and spot > 0) else False
        )

        fired_tier: Optional[str] = None

        if tp_tier:
            final_action = "LIQUIDATE"
            final_target = target
            sell_ratio = tp_ratio
            system_conflict_note = tp_reason
            options_strategy = f"{tp_ratio:.0%} LIQUIDATE (轉入 {final_target})"
            fired_tier = tp_tier
        elif is_extreme_tick_breach:
            final_action = "LIQUIDATE"
            final_target = target if target else "VOO"
            sell_ratio = 1.0
            system_conflict_note = (
                f"🆘 **極端瞬時停損觸發**：標的現價 (${spot:.2f}) 貫穿極端防守位 "
                f"(${extreme_stop_loss:.2f} = ${anchor_base:.2f} - "
                f"{_ANTI_WASHOUT_EXTREME_ATR_MULT}× ATR_15m)，無視 15m 實體收盤"
                f"等待，立即市價平倉轉入 {final_target}（現貨與期權皆適用的"
                "最後防線，阻斷突發黑天鵝與流動性真空滑步）。"
            )
            options_strategy = f"100% LIQUIDATE / STC (極端瞬時停損轉入 {final_target})"
            fired_tier = "EXTREME_TICK_BREACH"
        elif is_ivr_fast_exit:
            final_action = "LIQUIDATE"
            final_target = target if target else "VOO"
            sell_ratio = 1.0
            system_conflict_note = (
                "🚨 **期權 IV 驟降快速出場**：IV 較前次觀測驟降 (>20%)，"
                "啟動 3-5m 快速通道平倉 (拒絕等待 15m 實體收盤以規避 Delta/Vega 雙殺)。"
            )
            options_strategy = f"100% LIQUIDATE / STC (快速平倉轉入 {final_target})"
            fired_tier = "IVR_FAST_EXIT"
        elif sl_tier in ("SL_STRUCTURAL", "SL_REGIME_FLIP", "SL_WHALE_PUT"):
            final_action = "LIQUIDATE"
            final_target = target if target else "VOO"
            sell_ratio = 1.0
            system_conflict_note = sl_reason
            options_strategy = f"100% LIQUIDATE (轉入 {final_target})"
            fired_tier = sl_tier
        elif sl_tier == "SL_TRAILING_BREAKEVEN":
            final_action = "HOLD"
            final_target = symbol
            sell_ratio = 0.0
            system_conflict_note = sl_reason
            options_strategy = (
                f"移動止盈 (保本) @ ${sl_new_stop:.2f}"
                if sl_new_stop is not None
                else "移動止盈 (保本)"
            )
            fired_tier = sl_tier
        elif requested_action == "REDUCE":
            final_action = "REDUCE"
            final_target = target
            system_conflict_note = "⚖️ **持倉比例再平衡**：衛星部位超過風險上限，執行常規部分減倉以平衡資產權重。"
            options_strategy = "REDUCE (部分獲利了結/降低持倉比重)"
        else:
            # 未觸發任何 SL/TP 分層 -> 一律維持 HOLD
            final_action = "HOLD"
            final_target = symbol
            sell_ratio = 0.0
            system_conflict_note = (
                f"🛡️ **灰階量化裁決**：${anchor_base:.2f} 正 Gamma 護城河完好，"
                "未觸發微觀結構出場決策矩陣任何分層，維持現狀續抱。"
            )
            options_strategy = "HOLD (維持現狀續抱)"

        return (
            final_action,
            final_target,
            options_strategy,
            system_conflict_note,
            is_extreme_tick_breach,
            sell_ratio,
            fired_tier,
        )

    async def _resolve_target_reference_price(self, target_core_name: str) -> float:
        """
        解析轉倉目標資產的參考價格，用於估算可買入股數 (僅供文字建議粗估)。
        三層備援（與「執行試算」按鈕 RolloverActionView.btn_execute_callback
        共用同一順序）：market_cache 快取 → 即時報價 → 具名備援常數。
        市場快取（market_cache）僅涵蓋已預熱的期權 Watchlist 標的，BOXX 等
        無選擇權鏈的純現金等價 ETF 通常不會出現在該表中，因此不可只退回
        「被賣出資產自身的現價」（兩者價格通常無關，例如賣出 NVDA 轉倉 BOXX
        絕不能用 NVDA 現價估算 BOXX 股數），而是改嘗試即時報價。
        """
        from database.market_cache import get_market_cache

        try:
            row = get_market_cache(target_core_name)
            if row:
                cached_price = float(row.get("reference_spot_price") or 0.0)
                if cached_price > 0:
                    return cached_price
        except Exception as e:
            logger.warning(f"讀取 {target_core_name} market_cache 參考價格失敗: {e}")

        try:
            from services import market_data_service

            quote = await market_data_service.get_quote(target_core_name)
            live_price = float(quote.get("c") or 0.0) if quote else 0.0
            if live_price > 0:
                return live_price
        except Exception as e:
            logger.warning(f"讀取 {target_core_name} 即時報價失敗: {e}")

        logger.warning(
            f"{target_core_name} 快取與即時報價皆缺失，退回備援估計值 "
            f"${_FALLBACK_TARGET_PRICE_ESTIMATE:.2f}"
        )
        return _FALLBACK_TARGET_PRICE_ESTIMATE

    async def _estimate_cash_recovery(
        self,
        target_core_name: str,
        spot: float,
        position_shares: float,
        current_value: float,
    ) -> Tuple[str, str, float]:
        """資金回收與目標核心資產買入預估。
        回傳 (cash_str, shares_guidance_str, target_entry_price)——
        target_entry_price 為轉入目標資產的參考進場價，供呼叫端填入
        Discord Embed「建議限價 (Limit)」欄位，取代過去恆為 "Market" 的佔位字串。
        """
        target_est_price = await self._resolve_target_reference_price(target_core_name)

        if current_value > 0:
            recovered_cash = current_value
        elif position_shares > 0 and spot > 0:
            recovered_cash = position_shares * spot
        else:
            recovered_cash = 0.0

        if recovered_cash > 0:
            cash_str = f"${recovered_cash:,.0f}"
            target_shares_est = int(recovered_cash / target_est_price)
            target_shares_low = max(1, target_shares_est - 1)
            target_shares_high = max(1, target_shares_est + 1)
            shares_guidance_str = (
                f"{target_core_name}（約 {target_shares_low}–{target_shares_high} 股）"
            )
        else:
            cash_str = "全數部位資金"
            shares_guidance_str = f"{target_core_name}（全額買入）"

        return cash_str, shares_guidance_str, target_est_price

    async def _generate_rule_based_rebalance_report(
        self,
        symbol: str,
        metrics: dict,
        requested_action: str,
        target: str = "VOO",
        strategy_override: str = "",
        asset_class: str = "SPOT",
        active_orders: Optional[list[dict]] = None,
        position_shares: float = 0.0,
        current_value: float = 0.0,
    ) -> dict:
        """
        Evaluates rebalancing rules under Gray-Scale Quantitative Framework
        and generates the strict 4-part markdown report.
        Returns a dict containing the final action, target asset, and markdown string.
        """
        spot = float(metrics.get("spot_price", 0.0))
        ivr = float(metrics.get("ivr", 0.0))
        iv_term_structure_status = metrics.get("iv_term_structure_status") or "N/A"
        max_pain = float(metrics.get("max_pain", 0.0))
        is_uoa_sweep = bool(metrics.get("is_uoa_sweep", False))
        sqz_mom = float(metrics.get("sqz_mom", 0.0))
        skew = float(metrics.get("skew", 0.0))
        bid = float(metrics.get("bid", 0.0))
        ask = float(metrics.get("ask", 0.0))
        # 流動性閘門 (#7)：目前僅期權持倉稽核流程有機會取得 bid/ask（預設 0.0，
        # 對近 100% 尚未接上即時期權報價的真實流量優雅降級為不判定）。實際替持倉
        # 中的期權部位取得即時 bid/ask 屬資料管線擴充，列為範圍外後續追蹤項目。
        is_illiquid_warning = asset_class == "OPTIONS" and is_spread_illiquid(bid, ask)

        atr_15m = float(metrics.get("atr_15m", 0.0))

        anchor_base, effective_res_wall = self._correct_wall_topology(metrics)
        (
            stop_loss,
            limit_price,
            extreme_stop_loss,
        ) = self._compute_anti_washout_stop(anchor_base, metrics)
        order_defense_str, matching_order = self._resolve_active_order_defense(
            symbol, active_orders, stop_loss, limit_price
        )

        tp_tier_result = self._evaluate_microstructure_tp_ladder(metrics)
        sl_tier_result = self._evaluate_microstructure_sl_ladder(
            metrics, anchor_base, stop_loss, asset_class
        )

        (
            final_action,
            final_target,
            options_strategy,
            system_conflict_note,
            is_extreme_tick_breach,
            sell_ratio,
            fired_tier,
        ) = self._apply_decision_matrix(
            symbol=symbol,
            metrics=metrics,
            requested_action=requested_action,
            target=target,
            asset_class=asset_class,
            tp_tier_result=tp_tier_result,
            sl_tier_result=sl_tier_result,
            stop_loss=stop_loss,
            anchor_base=anchor_base,
            extreme_stop_loss=extreme_stop_loss,
        )

        options_strategy = self._apply_ivr_strategy_overlay(
            options_strategy, strategy_override, ivr
        )

        # 停損數值字串格式化 (嚴禁輸出 N/A)
        stop_loss_str = f"${stop_loss:.2f}"
        extreme_stop_loss_str = (
            f"${extreme_stop_loss:.2f}" if extreme_stop_loss > 0 else "N/A"
        )

        # 數據異常註記
        data_note = ""
        if ivr == 0.0 or spot == 0.0:
            data_note = " (⚠️ 數據失真或快取未更新，請留意風險)"

        # ━━━ 資金回收與目標核心資產買入預估 (結合風險平價口數縮放) ━━━
        target_core_name = target if target else "VOO"
        (
            cash_str,
            shares_guidance_str,
            target_entry_price,
        ) = await self._estimate_cash_recovery(
            target_core_name=target_core_name,
            spot=spot,
            position_shares=position_shares,
            current_value=current_value,
        )

        # GEX 數值描述格式化
        supp_gex = metrics.get("support_gex")
        res_gex = metrics.get("resistance_gex")
        if supp_gex is not None and supp_gex != 0:
            gex_support_desc = (
                f"{supp_gex / 1e6:+.0f}M"
                if abs(supp_gex) >= 1e6
                else f"{supp_gex / 1e3:+.0f}k"
            )
        else:
            gex_support_desc = "做市商強正 Gamma 支撐"

        if res_gex is not None and res_gex != 0:
            gex_res_desc = (
                f"{res_gex / 1e6:+.0f}M"
                if abs(res_gex) >= 1e6
                else f"{res_gex / 1e3:+.0f}k"
            )
        else:
            gex_res_desc = "做市商阻力天花板"

        liquidity_note = ""
        if is_illiquid_warning:
            spread_pct = (ask - bid) / ((ask + bid) / 2)
            liquidity_note = (
                f"\n   - ⚠️ **流動性警告**：合約點差過寬 (Bid ${bid:.2f} / Ask ${ask:.2f}，"
                f"點差 {spread_pct:.1%})，建議採限價單並留意滑價，避免市價單重擊點差。"
            )

        holding_period_days: Optional[int] = None
        if final_action in ("LIQUIDATE", "REDUCE"):
            acquired_at_str = metrics.get("acquired_at")
            if acquired_at_str:
                try:
                    from datetime import datetime

                    acquired_dt = datetime.strptime(str(acquired_at_str), "%Y-%m-%d")
                    holding_period_days = (datetime.now() - acquired_dt).days
                except (ValueError, TypeError):
                    holding_period_days = None

        tax_note = self._maybe_append_tax_risk_note(
            is_forced_settlement=False,
            is_same_symbol_reentry=False,
            holding_period_days=holding_period_days,
        )

        dual_track_note = (
            "**3-5m 快速通道監控** (期權合約拒絕等待 15m 實體收盤以規避 Delta/Vega 雙殺)"
            if asset_class == "OPTIONS"
            else f"**15m 實體 K 線過濾** (盤中插針至 ${spot:.2f} 屬做市商正常洗盤，未跌破 ${stop_loss_str} 實體收盤前絕不手動干預)"
        )
        extreme_stop_note = (
            f"🆘 **極端瞬時停損 (軌道二)**：{extreme_stop_loss_str}"
            f"（現價貫穿即立即市價平倉，無視 15m 收盤等待，全資產類別適用，"
            f"作為黑天鵝/流動性真空級別的最後防線）"
        )

        # 建構標準 4 段式 Markdown
        core_report = f"""
1. **盤勢定調**
   - 現價: ${spot:.2f} | IV 位階: {ivr:.1f}%{data_note}
   - IV 期限結構: {iv_term_structure_status}
   - 相對位置: Max Pain ${max_pain:.2f}
2. **主力意圖拆解 (UOA/GEX 微結構)**
   - 做市商護盤牆: GEX Wall: ${anchor_base:.2f} ({gex_support_desc}) (強支撐彈簧床)
   - 阻力天花板: ${effective_res_wall:.2f} ({gex_res_desc})
   - 巨鯨掃貨: {"✅ 偵測到 UOA Sweep" if is_uoa_sweep else "❌ 無明顯 UOA"}
3. **動能與擠壓狀態**
   - SQZ MOM: {sqz_mom:+.2f} | Skew: {skew:.2f} ({"多頭動能延續" if sqz_mom > 0 else "動能中性/趨緩"})
4. **具體的動態轉倉建議**
   - {system_conflict_note if system_conflict_note else "常規執行：依系統建議比例調節"}{liquidity_note}
   - 轉倉決策: **{final_action} ({"維持現狀續抱" if final_action == "HOLD" else "轉入 " + final_target})**
   - 微結構判定: GEX Wall ${anchor_base:.2f} 護城河完好，阻力天花板 ${effective_res_wall:.2f}
   - 防守機制: {order_defense_str}
     *(避開真空區，依據公式：`Stop = ${anchor_base:.2f} - ({_MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT} × ATR_15m) = ${stop_loss_str}`)*
   - 出場裁決軌道: {dual_track_note}
   - {extreme_stop_note}
""".strip()

        # 🚨 動態資金輪動觸發條件：獨立拆分供 embed 呈現層放入專屬欄位，
        # 避免與其餘段落一起塞入 description 時因 4000 字元上限被截斷，
        # 導致「何時才真正轉倉」這段最關鍵的判斷依據反而消失。
        trigger_condition_report = f"""
## 🚨 動態資金輪動觸發條件（何時才真正轉倉 {target_core_name}？）
只有在以下**硬性量化條件觸發**時，才允許執行 100% 轉入 {target_core_name}：
1. **實體破位觸發**：
   - {"3-5m 快速通道跌破或 IV 崩塌" if asset_class == "OPTIONS" else f"15 分鐘 K 線**實體收盤跌破 ${stop_loss_str}**"}，或委託單自動觸發成交。
   - **量化含義**：宣告 ${anchor_base:.2f} 做市商底牆徹底崩塌，負 Gamma 助跌啟動，價格將下探 ${max_pain:.2f} 痛點。
   - **軌道二（極端瞬時停損）**：現價貫穿 **{extreme_stop_loss_str}** 時，無視上述 15m 收盤等待，立即市價平倉（全資產類別適用）。
2. **轉倉執行動作**：
   - 回收資金約 **{cash_str}**。
   - **唯一指令**：立即市價全數買入 **{shares_guidance_str}**，使組合轉為 100% {target_core_name} 大盤防禦模式。
""".strip()

        markdown_report = f"{core_report}\n\n---\n{trigger_condition_report}{tax_note}"

        # 軌道二極端瞬時停損詳情區塊：僅在這次真的由 is_extreme_tick_breach 觸發時
        # 組裝，供呈現層 (rollover_embeds.py) 渲染為獨立的「立即人工執行」欄位。
        extreme_breach_detail_block: Optional[str] = None
        if is_extreme_tick_breach and extreme_stop_loss > 0 and spot > 0:
            penetration_pct = (extreme_stop_loss - spot) / spot * 100
            penetration_atrs = (
                (extreme_stop_loss - spot) / atr_15m if atr_15m > 0 else 0.0
            )
            extreme_breach_detail_block = f"""```ansi
🚨 【緊急風控指令：軌道二極端瞬時停損觸發】
------------------------------------------------------------
標的資產：{symbol} ({asset_class})
觸發價格：${spot:.2f}  (已穿透極端熔斷線 ${extreme_stop_loss:.2f})
做市商底牆：${anchor_base:.2f} | 15m ATR：${atr_15m:.2f}
結構破位幅度：-{penetration_pct:.2f}% (超額穿透 {penetration_atrs:.2f}x ATR)
做市商 Gamma 狀態：🔴 進入負 Gamma 踩踏區間 (追跌對沖生效中)

⚠️ 執行指引 (ACTION REQUIRED)：
系統當前處於 15 分鐘輪詢節點，市場流動性可能正處於斷崖真空。
請「立即手動至券商終端」執行市價/IOC 清倉指令，嚴禁左側抗單！
------------------------------------------------------------
```""".strip()

        return {
            "final_action": final_action,
            "final_target": final_target,
            "sell_ratio": sell_ratio,
            "exit_tier": fired_tier,
            "options_strategy": options_strategy,
            "markdown_report": markdown_report.strip(),
            "trigger_condition_report": trigger_condition_report,
            "cash_impact": cash_str,
            "matching_order": matching_order,
            "is_illiquid_warning": is_illiquid_warning,
            "extreme_stop_loss": extreme_stop_loss,
            "is_extreme_tick_breach": is_extreme_tick_breach,
            "extreme_breach_detail_block": extreme_breach_detail_block,
            # 注意：這裡刻意採用 target_entry_price（轉入目標資產的參考進場價），
            # 而非上面用於防守被賣出部位的 stop-limit `limit_price` 區域變數——
            # Discord Embed 的「建議限價 (Limit)」欄位語意上對應的是買入目標資產
            # 的委託價，兩者絕不可混用。
            "limit_price": target_entry_price,
        }


def _net_and_build_rebalance_instruction(
    engine: Any,
    symbol: str,
    quantity: float,
    report: dict,
    default_sell_ratio: float,
    asset_class: str = "SPOT",
) -> RolloverInstruction:
    """套用既有委託單淨額扣抵並組裝 instruction dict：一般清倉/灰階判定分支與
    常規比例修剪分支皆遵循「report 決定 final_action → 依情境算出預設
    sell_ratio → _net_against_existing_order 扣抵既有委託單 → 組裝 dict」的
    相同流程，僅 default_sell_ratio 的計算方式不同 (前者取決於 report 本身的
    LIQUIDATE/REDUCE 判定，後者取決於超額配置比例)，故由呼叫端各自算好
    default_sell_ratio 後傳入，其餘完全共用。"""
    net_action = report["final_action"]
    net_sell_ratio = default_sell_ratio
    net_reason = report["markdown_report"]
    if net_action in ("LIQUIDATE", "REDUCE"):
        net_sell_ratio, net_note = engine._net_against_existing_order(
            net_sell_ratio, quantity, report.get("matching_order")
        )
        if net_note:
            net_reason += net_note
        if net_sell_ratio <= 0.0:
            net_action = "HOLD"

    return {
        "symbol": symbol,
        "action": net_action,
        "sell_ratio": net_sell_ratio,
        "target_core": report["final_target"],
        "reason": net_reason,
        "suggested_strategy": report["options_strategy"],
        "scenario": RolloverScenario.SATELLITE_REBALANCE.value,
        "is_manual_override_required": bool(report.get("is_illiquid_warning", False)),
        "trigger_condition_text": report["trigger_condition_report"],
        "cash_impact": report["cash_impact"],
        "limit_price": report["limit_price"],
        "extreme_stop_loss": report.get("extreme_stop_loss"),
        "is_extreme_tick_breach": report.get("is_extreme_tick_breach", False),
        "extreme_breach_detail_block": report.get("extreme_breach_detail_block"),
        "instrument_type": asset_class,
        "exit_tier": report.get("exit_tier"),
    }


def _build_forced_settlement_instruction(
    engine: Any,
    symbol: str,
    asset_class: str,
    quantity: float,
    current_value: float,
    dte: int,
) -> RolloverInstruction:
    """DTE<=1 末日結算保護 (EXPIRATION_SETTLEMENT_ALERT)：完全略過錨點/破位
    判定與停損計算，無條件產生 LIQUIDATE 指令，轉倉至**同標的**次月主力合約
    （而非切換至其他標的），嚴禁透過擴大停損空間抗單。取代舊版「0/1 DTE
    風險平價縮放」機制（擴大停損 + 口數砍半）。"""
    sell_action = "BTC" if quantity < 0 else "STC"
    reason = (
        "🆘 **末日結算保護 (Forced Settlement Protection)**\n"
        f"{symbol} 合約 DTE={dte}（<= {_HOLDING_DTE_FORCED_SETTLEMENT_THRESHOLD}），"
        "已進入最後結算週期，無條件強制平倉，嚴禁透過擴大停損空間抗單。\n"
        f"建議轉倉至 {symbol} 次月主力合約（約 {_FORCED_SETTLEMENT_ROLL_MIN_DTE}-"
        f"{_FORCED_SETTLEMENT_ROLL_MAX_DTE} DTE 效期）。"
    )
    tax_note = engine._maybe_append_tax_risk_note(
        is_forced_settlement=True,
        is_same_symbol_reentry=False,
    )
    return {
        "symbol": symbol,
        "action": "LIQUIDATE",
        "sell_ratio": 1.0,
        "target_core": symbol,
        "reason": reason + tax_note,
        "suggested_strategy": (
            f"100% {sell_action} → 轉倉至 {symbol} 次月主力合約 "
            f"({_FORCED_SETTLEMENT_ROLL_MIN_DTE}-{_FORCED_SETTLEMENT_ROLL_MAX_DTE} DTE)"
        ),
        "sell_action": sell_action,
        "scenario": RolloverScenario.SATELLITE_REBALANCE.value,
        "is_manual_override_required": True,
        "cash_impact": format_cash_impact(abs(current_value)),
        "limit_price": None,
        "extreme_stop_loss": None,
        "is_extreme_tick_breach": False,
        "extreme_breach_detail_block": None,
        "instrument_type": asset_class,
    }


async def check_satellite_rebalancing_impl(
    engine: Any,
    get_full_user_context: Any,
    user_id: int,
    portfolio_assets: List[Dict[str, Any]],
    total_account_value: float,
) -> List[RolloverInstruction]:
    """
    邏輯 (3): 核心與衛星比例再平衡 + 深度微觀結構與選擇權籌碼驅動
    包含勝率傾斜與雜訊避險等高階戰術。
    """
    rebalance_instructions: List[RolloverInstruction] = []

    # 取得使用者待成交委託單以供防守機制關聯
    user_orders: list[dict] = []
    try:
        from database.orders import get_user_active_orders

        user_orders = get_user_active_orders(user_id)
    except Exception as e:
        logger.debug(f"無法取得 user {user_id} active_orders: {e}")
        user_orders = []

    for asset in portfolio_assets:
        if asset.get("asset_class") == "SATELLITE":
            symbol: str = str(asset.get("symbol", ""))
            current_value: float = float(asset.get("current_value", 0.0))
            quantity: float = float(asset.get("quantity", 0.0))
            max_alloc: float = float(
                asset.get("max_allocation_pct", _DEFAULT_MAX_ALLOCATION_PCT)
            )

            # --- DTE 三態狀態機閘門：提前解析 asset_class 與 dte，於深度量化
            # 資料提取與 GEX 掃描之前短路，避免對即將被結算保護接管的部位做
            # 不必要的運算。僅 OPTIONS 部位有意義；SPOT 的 dte 恆為預設值 99
            # (>=7)，天生落在 NORMAL_EXECUTION，故不需另外判斷 asset_class。 ---
            asset_class = str(
                asset.get("instrument_type", asset.get("asset_type", "SPOT"))
            ).upper()
            if "OPT" in asset_class or "CONTRACT" in asset_class:
                asset_class = "OPTIONS"
            else:
                asset_class = "SPOT"
            dte: int = int(asset.get("dte", 99))

            if asset_class == "OPTIONS":
                dte_tier = evaluate_option_dte_tier(dte, "MANAGE_EXISTING")
                if dte_tier == "EXPIRATION_SETTLEMENT_ALERT":
                    rebalance_instructions.append(
                        _build_forced_settlement_instruction(
                            engine=engine,
                            symbol=symbol,
                            asset_class=asset_class,
                            quantity=quantity,
                            current_value=current_value,
                            dte=dte,
                        )
                    )
                    continue

            # --- 新增：深度量化數據 (Fallback = None/0.0) ---
            spot: float = float(asset.get("spot_price", 0.0))
            call_wall: float = float(asset.get("call_wall", 0.0))
            max_pain: float = float(asset.get("max_pain", 0.0))
            ivr: float = float(asset.get("ivr", 0.0))
            # ⚠️ 既有缺陷修正：portfolio_monitor 早已把 ivr_drop 放進 asset
            # entry，但此處的 metrics 組裝從未讀取它，導致
            # _apply_decision_matrix 的 is_ivr_fast_exit
            # (metrics.get("ivr_drop", ...)) 恆為 0.0——AGENTS.md 記載的
            # 「OPTIONS IV 崩塌快速通道」對所有部位其實從未真正觸發過。
            ivr_drop: float = float(asset.get("ivr_drop", 0.0))
            put_wall: float = float(asset.get("put_wall", 0.0))
            is_uoa_sweep: bool = bool(asset.get("is_uoa_sweep", False))
            sqz_mom: float = float(asset.get("sqz_mom", 0.0))
            skew: float = float(asset.get("skew", 0.0))

            raw_skew_perc = asset.get("skew_percentile", None)
            skew_percentile: float
            if raw_skew_perc is not None:
                skew_percentile = float(raw_skew_perc)
            else:
                skew_percentile = get_indicator_percentile(symbol, "SKEW", skew)

            gamma_flip: float = float(asset.get("gamma_flip", 0.0))
            atr_14: float = float(asset.get("atr_14", 0.0))
            hvn: float = float(asset.get("hvn", 0.0))
            lvn: float = float(asset.get("lvn", 0.0))
            price_15m_close: float = float(asset.get("price_15m_close", spot))
            price_15m_open: float = float(asset.get("price_15m_open", spot))
            # 路徑 1 上移後持久化的保本停損地板 (未標記/未觸發者為 0.0)
            ratchet_stop: float = float(
                (asset.get("dynamic_strategy_state") or {}).get("ratchet_stop") or 0.0
            )
            atr_15m: float = float(asset.get("atr_15m", 0.0))
            acquired_at: Optional[str] = asset.get("acquired_at")
            iv_term_structure_status: Optional[str] = asset.get(
                "iv_term_structure_status"
            )

            # 計算比例
            current_alloc: float = (
                current_value / total_account_value if total_account_value > 0 else 0.0
            )

            gex_profile_data = asset.get("gex_profile_data", {})
            uoa_list = asset.get("uoa", []) or []
            # None（而非 0.0）代表 GEX 數據缺失/未曾抓取，供 SL-狀態翻轉判定
            # 明確區分「資料缺失」與「已抓到且確認 Net GEX <= 0」。
            net_gex_raw = (
                gex_profile_data.get("net_gex")
                if isinstance(gex_profile_data, dict)
                else None
            )
            net_gex: Optional[float] = (
                float(net_gex_raw) if net_gex_raw is not None else None
            )
            avg_cost: float = float(asset.get("avg_cost", 0.0))
            delta_val = asset.get("delta")
            vwap_loss_with_volume: bool = bool(
                asset.get("vwap_loss_with_volume", False)
            )
            session_vwap: float = float(asset.get("session_vwap", 0.0))
            vwap_reclaim_with_volume: bool = bool(
                asset.get("vwap_reclaim_with_volume", False)
            )

            # ----------------------------------------------------
            # 微觀結構出場決策矩陣：SL-主力對沖訊號 (真實 UOA PUT BTO 判定，取代
            # 舊版 sqz_mom/skew 動能代理) 與 GEX 牆掃描，供錨點解析與矩陣共用。
            # ----------------------------------------------------
            (
                _legacy_structural_breakdown,
                is_whale_put_block,
                support_wall,
                resistance_wall,
                support_gex,
                resistance_gex,
            ) = await engine._compute_structural_breakdown_signals(
                symbol=symbol,
                spot=spot,
                put_wall=put_wall,
                gamma_flip=gamma_flip,
                atr_14=atr_14,
                sqz_mom=sqz_mom,
                skew=skew,
                price_15m_close=price_15m_close,
                gex_profile_data=gex_profile_data,
                asset_class=asset_class,
                call_wall=call_wall,
                hvn=hvn,
                uoa_list=uoa_list,
            )

            metrics: Dict[str, Any] = {
                "spot_price": spot,
                "call_wall": call_wall,
                "max_pain": max_pain,
                "ivr": ivr,
                "ivr_drop": ivr_drop,
                "put_wall": put_wall,
                "is_uoa_sweep": is_uoa_sweep,
                "sqz_mom": sqz_mom,
                "skew": skew,
                "skew_percentile": skew_percentile,
                "gamma_flip": gamma_flip,
                "atr_14": atr_14,
                "hvn": hvn,
                "lvn": lvn,
                "dte": dte,
                "price_15m_close": price_15m_close,
                "price_15m_open": price_15m_open,
                "ratchet_stop": ratchet_stop,
                "atr_15m": atr_15m,
                "support_wall": support_wall,
                "resistance_wall": resistance_wall,
                "support_gex": support_gex,
                "resistance_gex": resistance_gex,
                "bid": float(asset.get("bid", 0.0)),
                "ask": float(asset.get("ask", 0.0)),
                "acquired_at": acquired_at,
                "iv_term_structure_status": iv_term_structure_status,
                "net_gex": net_gex,
                "avg_cost": avg_cost,
                "delta": delta_val,
                "vwap_loss_with_volume": vwap_loss_with_volume,
                "is_whale_put_block": is_whale_put_block,
                "session_vwap": session_vwap,
                "vwap_reclaim_with_volume": vwap_reclaim_with_volume,
            }

            # 微觀結構出場決策矩陣：TP 分層 (TP1/TP2/TP3) 與 SL 分層 (SL-結構
            # 失效/SL-狀態翻轉/SL-主力對沖/SL-動態保本)。兩者皆為純函式，
            # 於此處先行評估以決定是否需要進入本輪特殊評估路徑；
            # _generate_rule_based_rebalance_report 內部會再次評估以產生最終
            # 指令 (與既有 is_structural_breakdown 於外層/_apply_decision_matrix
            # 內層雙重確認的既有架構模式一致)。
            tp_tier, _tp_ratio, _tp_reason = engine._evaluate_microstructure_tp_ladder(
                metrics
            )
            anchor_base_gate, _res_wall_gate = engine._correct_wall_topology(metrics)
            stop_loss_gate, _limit_gate, extreme_gate = (
                engine._compute_anti_washout_stop(anchor_base_gate, metrics)
            )

            # 軌道二極端瞬時停損 (黑天鵝最後防線) 的觸發判定，定義與
            # _apply_decision_matrix 內部的 is_extreme_tick_breach 完全一致
            # (TP 未觸發，且現價已貫穿 anchor_base - 3.0×ATR₁₅ₘ 的極端熔斷線)。
            # 在此先行計算，唯一用途是確保它對「所有」部位通用——包含下方
            # 交由 Transition Engine 接管的已標記部位。
            is_extreme_breach_gate = (
                (not tp_tier) and extreme_gate > 0 and spot > 0 and spot < extreme_gate
            )

            # ----------------------------------------------------
            # 動態調整狀態切換引擎 (Transition Engine)：僅接管使用者透過
            # /add_trade、/add_holding 手動標記 dynamic_strategy_state
            # (entry_mode="DYNAMIC") 且未 lockout 的部位，完全取代這些部位
            # 原本會走的通用微觀結構出場決策矩陣 (SL/TP 分層)，避免同一標的
            # 出現兩組互相衝突的出場建議。未標記部位不受影響，直接落入下方
            # 既有邏輯。
            #
            # 狀態轉換引擎只負責「授予進場權限」(路徑1：停損上移保本 + 授權
            # 加碼)，**不再接管出場**。它的建議與下方出場階梯並存而非互斥：
            # 階梯對所有部位一律照跑，不因部位被標記而跳過。
            #
            # 這是刻意的職責邊界修正。先前的作法是讓 Transition Engine 完全
            # 取代已標記部位的出場矩陣，結果把「部位能否活下去」綁在進場當下
            # 貼的 entry_regime 標籤上；而該標籤從不更新，導致路徑1觸發後的
            # 部位失去所有例行停損 (詳見 transition_engine.py 模組 docstring)。
            # 現在 Regime 只做環境識別與進場閘門，部位存亡回歸獨立風控階梯，
            # 因此也不再需要為軌道二極端瞬時停損與 OPTIONS IV 崩塌快速通道
            # 各開一個例外孔。
            dynamic_state = asset.get("dynamic_strategy_state")
            if (
                dynamic_state
                and dynamic_state.get("entry_mode") == "DYNAMIC"
                and not dynamic_state.get("lockout")
            ):
                rebalance_instructions.extend(
                    await evaluate_transition_for_position(
                        engine, user_id, asset, metrics
                    )
                )

            sl_tier, _sl_ratio, _sl_reason, _sl_new_stop = (
                engine._evaluate_microstructure_sl_ladder(
                    metrics, anchor_base_gate, stop_loss_gate, asset_class
                )
            )

            # IV 泡沫防護：擺脫高波洗籌泥淖 (IV Crush 威脅)，矩陣未涵蓋的獨立保護
            is_iv_bubble = ivr > _IV_BUBBLE_THRESHOLD

            # is_extreme_breach_gate 一併納入閘門：軌道二觸發時必須確保能進入
            # 報告產生流程。目前 extreme_stop (anchor-3.0×ATR) 恆低於 Track 1
            # stop_loss (anchor-0.5×ATR)，故軌道二觸發時 sl_tier 必然也已觸發、
            # 閘門本就會通過；此處明確納入是防禦性寫法，避免未來若 SL 分層或
            # price_15m_close 語意調整後，出現「軌道二已觸發卻無任何指令產出」
            # 的破口。
            if (
                tp_tier is not None
                or sl_tier is not None
                or is_iv_bubble
                or is_extreme_breach_gate
            ):
                satellite_symbols = {
                    str(a.get("symbol", "")).upper()
                    for a in portfolio_assets
                    if a.get("asset_class") == "SATELLITE"
                }
                # 機構風控鐵律：SL 分層 (結構破位/狀態翻轉/主力對沖) 強制撤退回防
                # 核心資產 (VOO)，嚴禁在停損時又去追逐另一檔高波動衛星標的
                # (避免 Hot Potato Rotation 擴大虧損)；僅 TP 分層 (獲利了結
                # 輪動) 才尋找下一個高 EV 自選標的。
                if tp_tier is not None:
                    next_target = engine._find_best_rollover_target(
                        user_id, exclude_symbols=satellite_symbols
                    )
                else:
                    next_target = "VOO"

                report = await engine._generate_rule_based_rebalance_report(
                    symbol,
                    metrics,
                    requested_action="HOLD",
                    target=next_target,
                    asset_class=asset_class,
                    active_orders=user_orders,
                    position_shares=quantity,
                    current_value=current_value,
                )

                default_sell_ratio = report.get("sell_ratio", 0.0) or 0.0
                rebalance_instructions.append(
                    _net_and_build_rebalance_instruction(
                        engine,
                        symbol,
                        quantity,
                        report,
                        default_sell_ratio,
                        asset_class,
                    )
                )
                continue  # 已經處理，不需進行後續常規再平衡

            # ----------------------------------------------------
            # [ 常規比例控管 ]
            # ----------------------------------------------------
            if (
                max_alloc > 0.0
                and total_account_value > 0.0
                and current_alloc > max_alloc
            ):
                excess_alloc = current_alloc - asset.get(
                    "target_allocation_pct", max_alloc
                )
                excess_value = excess_alloc * total_account_value
                sell_ratio = excess_value / current_value

                report = await engine._generate_rule_based_rebalance_report(
                    symbol,
                    metrics,
                    requested_action="REDUCE",
                    asset_class=asset_class,
                    active_orders=user_orders,
                    position_shares=quantity,
                    current_value=current_value,
                )

                default_sell_ratio = (
                    round(sell_ratio, 2)
                    if report["final_action"] != "LIQUIDATE"
                    else 1.0
                )
                rebalance_instructions.append(
                    _net_and_build_rebalance_instruction(
                        engine,
                        symbol,
                        quantity,
                        report,
                        default_sell_ratio,
                        asset_class,
                    )
                )

    return rebalance_instructions
