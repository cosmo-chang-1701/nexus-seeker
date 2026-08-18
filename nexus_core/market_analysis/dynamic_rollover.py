from typing import Dict, Any, List, Optional, Tuple
from enum import Enum
import logging
from pydantic import BaseModel, Field
from services.llm_service import client, is_memory_safe
from config import LLM_MODEL_NAME

from market_analysis.gamma_cliff_confirmation import is_gamma_cliff_confirmed
from market_analysis.ivr_strategy_gate import is_selling_locked_by_ivr
from database.user_settings import get_full_user_context
from market_analysis.sentiment.history_storage import get_indicator_percentile

logger = logging.getLogger(__name__)


class RolloverScenario(str, Enum):
    """動態轉倉引擎四大情境的明確識別碼，供 embed 呈現層做顏色/危險等級判斷，
    避免依賴呼叫端自由文字 rollover_type 的子字串比對（該作法曾導致最危險的
    MARGIN_DEFENSE 警報無法正確標紅，詳見 rollover_embeds.py）。"""

    OPPORTUNITY_COST = "OPPORTUNITY_COST"
    SATELLITE_REBALANCE = "SATELLITE_REBALANCE"
    MARGIN_DEFENSE = "MARGIN_DEFENSE"
    FUNDAMENTAL_BROKEN = "FUNDAMENTAL_BROKEN"


# 核心防禦性 ETF 排除清單：機會成本轉倉 (_find_best_rollover_target) 與槓桿保證金
# 防禦 (evaluate_margin_defense) 共用同一份定義，避免各自維護造成分歧
# (曾發生 VXX 在部分清單中被排除、部分清單中未被排除的不一致)。
CORE_DEFENSE_ETF_SYMBOLS: frozenset[str] = frozenset(
    {"QQQ", "SPY", "VOO", "VXX", "IVV", "VTI"}
)

# _generate_rule_based_rebalance_report 在快取與現價皆無法取得目標資產參考價格時
# 使用的最終備援估計值（僅用於股數建議粗估，非交易執行依據）。
_FALLBACK_TARGET_PRICE_ESTIMATE = 500.0


class FundamentalThesisResult(BaseModel):
    # 讓模型先進行思考與文字輸出
    reasoning: str = Field(description="Step-by-step reasoning in Traditional Chinese")
    # 思考完後再給出最終判斷
    is_broken: bool = Field(
        description="True if structural thesis is broken, False if just macro/temporary"
    )
    confidence: float = Field(description="Confidence score from 0.0 to 1.0")


class DynamicRolloverEngine:
    def __init__(self) -> None:
        pass

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
        hvn = float(metrics.get("hvn", 0.0))

        if support_wall > 0:
            anchor_base = support_wall
        elif put_wall > 0 and call_wall > 0 and put_wall > call_wall:
            # 拓撲逆轉修復：較低價為做市商支撐底牆，較高價為上方阻力天花板
            anchor_base = min(put_wall, call_wall)
        elif put_wall > 0:
            anchor_base = put_wall
        else:
            anchor_base = hvn if hvn > 0 else spot

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
    ) -> Tuple[float, float, bool, float]:
        """
        防洗盤四大機制：計算精確防守位與掛單限價。
        回傳 (stop_loss, limit_price, is_01dte_expanded, dte_risk_parity_scale)。
        """
        spot = float(metrics.get("spot_price", 0.0))
        atr_15m = float(metrics.get("atr_15m", metrics.get("atr_14", 0.0)))
        lvn = float(metrics.get("lvn", 0.0))
        hvn = float(metrics.get("hvn", 0.0))
        dte = int(metrics.get("dte", 99))

        # 機制 2: 1.5x ATR 防護墊片
        if anchor_base > 0:
            raw_stop_loss = anchor_base - (1.5 * atr_15m)
        else:
            raw_stop_loss = spot * 0.96 if spot > 0 else 0.0

        base_stop_loss = raw_stop_loss

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

        # 機制 4: 末日結算容忍度 (DTE 0/1) 與風險平價口數縮放 (Risk-Parity Sizing)
        is_01dte_expanded = dte <= 1 and base_stop_loss > 0
        dte_risk_parity_scale = 1.0
        if is_01dte_expanded:
            base_stop_loss = base_stop_loss - 1.5 * atr_15m
            # Risk-Parity 縮放因子: Base Distance / (Base Distance + 1.5 * ATR_15m) = 1.5 / 3.0 = 0.5
            dte_risk_parity_scale = 0.5

        stop_loss = round(base_stop_loss, 2)
        limit_price = round(
            max(stop_loss - (0.5 * atr_15m if atr_15m > 0 else 0.6), stop_loss * 0.995),
            2,
        )
        return stop_loss, limit_price, is_01dte_expanded, dte_risk_parity_scale

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

    def _apply_decision_matrix(
        self,
        symbol: str,
        metrics: dict,
        requested_action: str,
        target: str,
        asset_class: str,
        is_take_profit: bool,
        stop_loss: float,
        anchor_base: float,
    ) -> Tuple[str, str, str, str]:
        """
        灰階思考量化裁決 (決策矩陣 - 雙軌裁決機制 Dual-Track Exit)。
        回傳 (final_action, final_target, options_strategy, system_conflict_note)。
        """
        spot = float(metrics.get("spot_price", 0.0))
        price_15m_close = float(metrics.get("price_15m_close", spot))
        sqz_mom = float(metrics.get("sqz_mom", 0.0))

        final_target = target if target else "VOO"
        final_action = requested_action
        system_conflict_note = ""

        ivr_drop = float(metrics.get("ivr_drop", metrics.get("ivr_change", 0.0)))
        is_options_fast_exit = asset_class == "OPTIONS" and (
            (spot < stop_loss if (stop_loss > 0 and spot > 0) else False)
            or (ivr_drop >= 20.0)
        )

        # 機制 3: 15 分鐘實體 K 線過濾 (非瞬時破位，針對現貨 SPOT)
        is_15m_close_broken = (
            (price_15m_close < stop_loss)
            if (stop_loss > 0 and price_15m_close > 0)
            else False
        )

        if is_take_profit:
            final_action = "LIQUIDATE"
            final_target = target
            system_conflict_note = (
                "🎯 **獲利解鎖達成**：觸及阻力目標位，按計劃獲利了結轉倉。"
            )
            options_strategy = f"100% LIQUIDATE (轉入 {final_target})"
        elif is_options_fast_exit:
            final_action = "LIQUIDATE"
            final_target = target if target else "VOO"
            system_conflict_note = (
                f"🚨 **期權雙軌快速通道觸發**：標的現價 (${spot:.2f}) 貫穿防守位 (${stop_loss:.2f}) 或 IV 驟降 (>20%)，"
                f"啟動 3-5m 快速通道平倉 (拒絕等待 15m 實體收盤以規避 Delta/Vega 雙殺)。"
            )
            options_strategy = f"100% LIQUIDATE / STC (快速平倉轉入 {final_target})"
        elif is_15m_close_broken:
            final_action = "LIQUIDATE"
            final_target = target if target else "VOO"
            system_conflict_note = (
                f"🚨 **15m 實體破位確認**：15 分鐘實體收盤價 (${price_15m_close:.2f}) 跌破防守線 (${stop_loss:.2f})，"
                f"做市商底牆徹底崩塌，負 Gamma 助跌啟動，強制啟動 100% 轉入 {final_target} 防禦。"
            )
            options_strategy = f"100% LIQUIDATE (轉入 {final_target})"
        elif requested_action == "REDUCE":
            final_action = "REDUCE"
            final_target = target
            system_conflict_note = "⚖️ **持倉比例再平衡**：衛星部位超過風險上限，執行常規部分減倉以平衡資產權重。"
            options_strategy = "REDUCE (部分獲利了結/降低持倉比重)"
        else:
            # 未跌破防守線 -> 一律維持 HOLD
            final_action = "HOLD"
            final_target = symbol
            system_conflict_note = (
                f"🛡️ **灰階量化裁決**：${anchor_base:.2f} 正 Gamma 護城河完好，"
                f"動能（SQZ MOM {sqz_mom:+.2f}）維持多頭，未觸發轉倉條件，維持現狀續抱。"
            )
            options_strategy = "HOLD (維持現狀續抱)"

        return final_action, final_target, options_strategy, system_conflict_note

    def _apply_ivr_strategy_overlay(
        self, options_strategy: str, strategy_override: str, ivr: float
    ) -> str:
        """
        IVR 策略防禦與微調。
        NOTE: strategy_override 傳入非空字串時會【完全取代】IVR 鎖定後綴邏輯 (elif，
        而非疊加)。此設計用於 Bear Call Spread / Trailing Stop 等戰術覆寫場景，
        該場景下 strategy_override 本身的文字已包含完整防守資訊，故刻意跳過 IVR 後綴。
        """
        if strategy_override:
            return strategy_override
        if is_selling_locked_by_ivr(ivr):
            return options_strategy + f" | ⚠️ IVR 極低位 ({ivr:.1f}%): 賣方策略已鎖死。"
        if ivr > 50.0:
            return options_strategy + " | 嚴禁買方 (IV 過高，規避 Gamma 陷阱)"
        return options_strategy

    def _resolve_target_reference_price(
        self, target_core_name: str, fallback_spot: float
    ) -> float:
        """
        解析轉倉目標資產的參考價格，用於估算可買入股數 (僅供文字建議粗估)。
        當目標為 VOO/SPY 等核心資產時，改用與 _calculate_ev_proxy 相同的
        market_cache 快取讀取 reference_spot_price，取代過期的硬編碼估計值；
        其餘情況維持原有 fallback 順序 (該資產自身現價 → 具名備援常數)。
        """
        if "VOO" in target_core_name or "SPY" in target_core_name:
            from database.market_cache import get_market_cache

            try:
                row = get_market_cache(target_core_name)
                if row:
                    cached_price = float(row.get("reference_spot_price") or 0.0)
                    if cached_price > 0:
                        return cached_price
            except Exception as e:
                logger.warning(
                    f"讀取 {target_core_name} market_cache 參考價格失敗: {e}"
                )

            logger.warning(
                f"{target_core_name} 快取參考價格缺失，退回備援估計值 "
                f"${_FALLBACK_TARGET_PRICE_ESTIMATE:.2f}"
            )
            return _FALLBACK_TARGET_PRICE_ESTIMATE

        return fallback_spot if fallback_spot > 0 else _FALLBACK_TARGET_PRICE_ESTIMATE

    def _estimate_cash_recovery(
        self,
        target_core_name: str,
        spot: float,
        position_shares: float,
        current_value: float,
        is_01dte_expanded: bool,
        dte_risk_parity_scale: float,
    ) -> Tuple[str, str]:
        """資金回收與目標核心資產買入預估 (結合風險平價口數縮放)。"""
        if current_value > 0:
            recovered_cash = current_value
        elif position_shares > 0 and spot > 0:
            recovered_cash = position_shares * spot
        else:
            recovered_cash = 0.0

        if recovered_cash > 0:
            cash_str = f"${recovered_cash:,.0f}"
            target_est_price = self._resolve_target_reference_price(
                target_core_name, spot
            )
            target_shares_est = int(recovered_cash / target_est_price)
            if is_01dte_expanded:
                target_shares_est = max(
                    1, int(target_shares_est * dte_risk_parity_scale)
                )
                target_shares_low = max(1, target_shares_est - 1)
                target_shares_high = max(1, target_shares_est + 1)
                shares_guidance_str = f"{target_core_name}（0/1 DTE 風險平價縮放: 約 {target_shares_low}–{target_shares_high} 股 / 削減 50% 部位）"
            else:
                target_shares_low = max(1, target_shares_est - 1)
                target_shares_high = max(1, target_shares_est + 1)
                shares_guidance_str = f"{target_core_name}（約 {target_shares_low}–{target_shares_high} 股）"
        else:
            cash_str = "全數部位資金"
            shares_guidance_str = f"{target_core_name}（全額買入）"

        return cash_str, shares_guidance_str

    def _generate_rule_based_rebalance_report(
        self,
        symbol: str,
        metrics: dict,
        requested_action: str,
        target: str = "VOO",
        strategy_override: str = "",
        asset_class: str = "SPOT",
        is_take_profit: bool = False,
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
        max_pain = float(metrics.get("max_pain", 0.0))
        is_uoa_sweep = bool(metrics.get("is_uoa_sweep", False))
        sqz_mom = float(metrics.get("sqz_mom", 0.0))
        skew = float(metrics.get("skew", 0.0))

        anchor_base, effective_res_wall = self._correct_wall_topology(metrics)
        stop_loss, limit_price, is_01dte_expanded, dte_risk_parity_scale = (
            self._compute_anti_washout_stop(anchor_base, metrics)
        )
        order_defense_str, _matching_order = self._resolve_active_order_defense(
            symbol, active_orders, stop_loss, limit_price
        )

        final_action, final_target, options_strategy, system_conflict_note = (
            self._apply_decision_matrix(
                symbol=symbol,
                metrics=metrics,
                requested_action=requested_action,
                target=target,
                asset_class=asset_class,
                is_take_profit=is_take_profit,
                stop_loss=stop_loss,
                anchor_base=anchor_base,
            )
        )

        options_strategy = self._apply_ivr_strategy_overlay(
            options_strategy, strategy_override, ivr
        )

        # 停損數值字串格式化 (嚴禁輸出 N/A)
        stop_loss_str = f"${stop_loss:.2f}"

        # 數據異常註記
        data_note = ""
        if ivr == 0.0 or spot == 0.0:
            data_note = " (⚠️ 數據失真或快取未更新，請留意風險)"

        # ━━━ 資金回收與目標核心資產買入預估 (結合風險平價口數縮放) ━━━
        target_core_name = target if target else "VOO"
        cash_str, shares_guidance_str = self._estimate_cash_recovery(
            target_core_name=target_core_name,
            spot=spot,
            position_shares=position_shares,
            current_value=current_value,
            is_01dte_expanded=is_01dte_expanded,
            dte_risk_parity_scale=dte_risk_parity_scale,
        )

        # GEX 數值描述格式化
        supp_gex = metrics.get("support_gex")
        res_gex = metrics.get("resistance_gex")
        if supp_gex is not None and supp_gex != 0:
            gex_support_desc = (
                f"{supp_gex/1e6:+.0f}M"
                if abs(supp_gex) >= 1e6
                else f"{supp_gex/1e3:+.0f}k"
            )
        else:
            gex_support_desc = "做市商強正 Gamma 支撐"

        if res_gex is not None and res_gex != 0:
            gex_res_desc = (
                f"{res_gex/1e6:+.0f}M"
                if abs(res_gex) >= 1e6
                else f"{res_gex/1e3:+.0f}k"
            )
        else:
            gex_res_desc = "做市商阻力天花板"

        dte_scale_note = ""
        if is_01dte_expanded:
            dte_scale_note = "\n   - ⚡ **0/1 DTE 風險平價口數縮放**：已啟動 3.0× ATR 緩衝墊片 (停損拉寬)，強制削減 50% 轉倉/開倉部位規模以維持 Dollar Risk 恆定。"

        dual_track_note = (
            "**3-5m 快速通道監控** (期權合約拒絕等待 15m 實體收盤以規避 Delta/Vega 雙殺)"
            if asset_class == "OPTIONS"
            else f"**15m 實體 K 線過濾** (盤中插針至 ${spot:.2f} 屬做市商正常洗盤，未跌破 ${stop_loss_str} 實體收盤前絕不手動干預)"
        )

        # 建構標準 4 段式 Markdown
        core_report = f"""
1. **盤勢定調**
   - 現價: ${spot:.2f} | IV 位階: {ivr:.1f}%{data_note}
   - 相對位置: Max Pain ${max_pain:.2f}
2. **主力意圖拆解 (UOA/GEX 微結構)**
   - 做市商護盤牆: GEX Wall: ${anchor_base:.2f} ({gex_support_desc}) (強支撐彈簧床)
   - 阻力天花板: ${effective_res_wall:.2f} ({gex_res_desc})
   - 巨鯨掃貨: {'✅ 偵測到 UOA Sweep' if is_uoa_sweep else '❌ 無明顯 UOA'}
3. **動能與擠壓狀態**
   - SQZ MOM: {sqz_mom:+.2f} | Skew: {skew:.2f} ({'多頭動能延續' if sqz_mom > 0 else '動能中性/趨緩'})
4. **具體的動態轉倉建議**
   - {system_conflict_note if system_conflict_note else '常規執行：依系統建議比例調節'}{dte_scale_note}
   - 轉倉決策: **{final_action} ({'維持現狀續抱' if final_action == 'HOLD' else '轉入 ' + final_target})**
   - 微結構判定: GEX Wall ${anchor_base:.2f} 護城河完好，阻力天花板 ${effective_res_wall:.2f}
   - 防守機制: {order_defense_str}
     *(避開真空區，依據公式：`Stop = ${anchor_base:.2f} - ({'3.0' if is_01dte_expanded else '1.5'} × ATR_15m) = ${stop_loss_str}`)*
   - 出場裁決軌道: {dual_track_note}
""".strip()

        # 🚨 動態資金輪動觸發條件：獨立拆分供 embed 呈現層放入專屬欄位，
        # 避免與其餘段落一起塞入 description 時因 4000 字元上限被截斷，
        # 導致「何時才真正轉倉」這段最關鍵的判斷依據反而消失。
        trigger_condition_report = f"""
## 🚨 動態資金輪動觸發條件（何時才真正轉倉 {target_core_name}？）
只有在以下**硬性量化條件觸發**時，才允許執行 100% 轉入 {target_core_name}：
1. **實體破位觸發**：
   - {'3-5m 快速通道跌破或 IV 崩塌' if asset_class == 'OPTIONS' else f'15 分鐘 K 線**實體收盤跌破 ${stop_loss_str}**'}，或委託單自動觸發成交。
   - **量化含義**：宣告 ${anchor_base:.2f} 做市商底牆徹底崩塌，負 Gamma 助跌啟動，價格將下探 ${max_pain:.2f} 痛點。
2. **轉倉執行動作**：
   - 回收資金約 **{cash_str}**。
   - **唯一指令**：立即市價全數買入 **{shares_guidance_str}**，使組合轉為 100% {target_core_name} 大盤防禦模式。
""".strip()

        markdown_report = f"{core_report}\n\n---\n{trigger_condition_report}"
        return {
            "final_action": final_action,
            "final_target": final_target,
            "options_strategy": options_strategy,
            "markdown_report": markdown_report.strip(),
            "trigger_condition_report": trigger_condition_report,
            "cash_impact": cash_str,
        }

    def _calculate_ev_proxy(self, symbol: str) -> float:
        """
        EV 代理值：以快取的 expected_move_upper 相對現貨的正規化上緣空間作為期望值近似。
        ev = (expected_move_upper - reference_spot_price) / reference_spot_price
        僅使用 market_cache（Cache-Aside），零額外 API 呼叫。
        is_stale 或 is_degraded 的快取視為不可信，回傳 0.0。
        """
        from database.market_cache import get_market_cache

        row = get_market_cache(symbol)
        if not row or row.get("is_stale") or row.get("is_degraded"):
            return 0.0
        spot = float(row.get("reference_spot_price") or 0.0)
        upper = float(row.get("expected_move_upper") or 0.0)
        if spot <= 0.0:
            return 0.0
        return (upper - spot) / spot

    def _find_best_rollover_target(
        self, user_id: int, exclude_symbols: Optional[set] = None
    ) -> str:
        """掃描使用者 Watchlist 與 market_cache 快取尋找下一個高 EV 衛星標的，若無則回傳 VOO"""
        from database.watchlist import get_user_watchlist

        exclude = {
            s.upper() for s in (exclude_symbols or set())
        } | CORE_DEFENSE_ETF_SYMBOLS
        try:
            watchlist = get_user_watchlist(user_id)
        except Exception as e:
            logger.error(f"取得 user {user_id} watchlist 失敗: {e}")
            return "VOO"

        best_symbol = "VOO"
        best_ev = 0.05  # 沿用原始程式碼註解中的門檻 "EV > 0.05"
        for sym, _ in watchlist:
            sym_u = str(sym).upper()
            if sym_u in exclude:
                continue
            ev = self._calculate_ev_proxy(sym_u)
            if ev > best_ev:
                best_ev = ev
                best_symbol = sym_u
        return best_symbol

    def _normalize_power_squeeze(self, psq: Dict[str, Any]) -> float:
        """
        將 analyze_psq() 產生的 PSQResult (dict 形式，如 radar cache 中的 psq_result)
        正規化為 0-100 的 PowerSqueeze 分數，供 evaluate_opportunity_cost() 使用。
        重用既有的 squeeze_level / signal_direction / momentum_color / is_breakout_long/short
        分級，而非發明新的量化門檻。
        """
        level = str(psq.get("squeeze_level", "Normal"))
        direction = str(psq.get("signal_direction", "Neutral"))
        mom_color = str(psq.get("momentum_color", "Neutral"))
        is_bullish = direction == "Long" or mom_color in ("LightBlue", "Golden")
        is_bearish = direction == "Short" or mom_color in ("Red", "DarkBlue")

        table = {
            "Release": {"neutral": 10.0, "bull": 75.0, "bear": 5.0},
            "Normal": {"neutral": 30.0, "bull": 40.0, "bear": 20.0},
            "Mid": {"neutral": 60.0, "bull": 70.0, "bear": 45.0},
            "High": {"neutral": 50.0, "bull": 90.0, "bear": 10.0},
        }
        bucket = table.get(level, table["Normal"])
        score = (
            bucket["bull"]
            if is_bullish
            else (bucket["bear"] if is_bearish else bucket["neutral"])
        )

        if psq.get("is_breakout_long"):
            score = max(score, 95.0)
        elif psq.get("is_breakout_short"):
            score = min(score, 5.0)

        return float(max(0.0, min(100.0, score)))

    async def evaluate_fundamental_thesis(
        self, symbol: str, fundamental_text: str
    ) -> Optional[FundamentalThesisResult]:
        """
        邏輯 (1): 原型假設破滅
        傳入 FastAPI 爬取的法說會或財報文本，使用 LLM 判定基本面護城河是否流失。
        """
        if not is_memory_safe():
            logger.warning("記憶體水位過高，跳過 vLLM 基本面護城河判定")
            return None

        system_prompt = (
            "You are a senior Wall Street quantitative analyst and fundamental research director.\n"
            "Your objective is to determine whether a company's long-term 'growth moat' has been lost or if its original bullish fundamental thesis is structurally broken.\n\n"
            "### 🧠 Analytical Framework (Think step-by-step before finalizing fields):\n"
            "Evaluate based on these four strict criteria:\n"
            "1. Forward Guidance: Are there significant downward revisions or withdrawal of future guidance?\n"
            "2. Margin Compression: Is there a structural contraction in gross/operating margins indicating lost pricing power?\n"
            "3. Market Share & Competition: Is there clear evidence of the company losing core market share to rivals?\n"
            "4. Core Strategy: Has management pivoted away from their primary growth engine due to failure?\n\n"
            "### ⚠️ STRICT EXCLUSION RULE (Crucial for `is_broken` decision):\n"
            "DO NOT classify the thesis as broken (is_broken = false) if the weakness is primarily driven by:\n"
            "- Cyclical / Macroeconomic headwinds (e.g., interest rates, inflation).\n"
            "- Foreign exchange (FX) fluctuations.\n"
            "- General industry downturns.\n"
            "- A minor single-quarter EPS/Revenue miss where the long-term structural advantage remains intact.\n"
            "A thesis is ONLY broken (is_broken = true) due to company-specific structural degradation (e.g., lost pricing power, technological obsolescence, permanent market share loss).\n\n"
            "### 📝 Output Field Instructions:\n"
            "You must strictly populate the required structured output fields based on the following logic:\n"
            "- `reasoning`: (CRITICAL) You must perform a Chain-of-Thought analysis here BEFORE concluding. Explicitly state the evidence extracted, categorize if the headwinds are macro (A) or structural (B), and explain how it triggers or avoids the strict exclusion rule. This field MUST be highly analytical, actionable, and written in Traditional Chinese (繁體中文).\n"
            "- `is_broken`: Set to `true` ONLY IF the thesis is structurally broken based on the exclusion rule. Otherwise, `false`.\n"
            "- `confidence`: Provide a float from 0.0 to 1.0 reflecting your confidence in this assessment based on the density and clarity of the provided text."
        )

        user_prompt = (
            f"Please analyze the following latest earnings report and conference call highlights for {symbol}.\n\n"
            f"Context:\n{fundamental_text}"
        )

        try:
            response = await client.beta.chat.completions.parse(
                model=LLM_MODEL_NAME,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {"role": "user", "content": user_prompt},
                ],
                response_format=FundamentalThesisResult,
            )
            parsed = response.choices[0].message.parsed

            # 寫入 SQLite 全域防禦閘門
            if parsed:
                from database.market_cache import save_fundamental_cache

                save_fundamental_cache(
                    symbol, parsed.is_broken, parsed.confidence, parsed.reasoning
                )

            if isinstance(parsed, FundamentalThesisResult):
                return parsed
            return None
        except Exception as e:
            logger.error(f"[{symbol}] Fundamental thesis evaluation failed: {e}")
            return None

    def evaluate_opportunity_cost(
        self,
        current_holding_symbol: str,
        current_holding_power_squeeze: float,
        current_holding_profit_pct: float,
        target_watchlist_symbol: str,
        target_power_squeeze: float,
        target_expected_value: float,
        current_holding_expected_value: float,
        target_ivr: float = 0.0,
        target_uoa_sweep: bool = False,
        target_spot: float = 0.0,
        target_put_wall: float = 0.0,
    ) -> Dict[str, Any]:
        """
        邏輯 (2): 機會成本與期望值比對 (包含勝率傾斜)
        結合 PowerSqueeze 動能指標，當持倉動能衰退且 Watchlist 具備突破條件時，
        計算期望值並給出轉倉建議。
        """
        # 假設 PowerSqueeze 指標中，數值越低代表動能越弱，越高代表突破動能強烈
        holding_momentum_decaying = current_holding_power_squeeze < 20.0
        target_breakout_ready = target_power_squeeze > 80.0

        # 期望值差距
        ev_spread = target_expected_value - current_holding_expected_value

        should_rollover = False
        rollover_ratio = 0.0
        strategy = "Buy Shares"

        if holding_momentum_decaying and target_breakout_ready and ev_spread > 0.05:
            should_rollover = True
            if current_holding_profit_pct > 0.3:
                # 獲利豐厚，可轉換 50%
                rollover_ratio = 0.5
            else:
                # 獲利一般或虧損，轉換 30% 或全轉，視風險偏好而定
                rollover_ratio = 0.3

            # ----------------------------------------------------
            # 條件二：新標的出現「極致不對稱勝率」
            # ----------------------------------------------------
            is_low_ivr = 0 < target_ivr < 30.0
            is_near_put_wall = (target_put_wall > 0 and target_spot > 0) and (
                abs(target_spot - target_put_wall) / target_put_wall <= 0.01
            )
            is_extreme_asymmetric = is_low_ivr and is_near_put_wall and target_uoa_sweep

            if is_extreme_asymmetric:
                strategy = "Shares + ITM Call"
                reason_suffix = f" (🎯 條件二極致勝率觸發: 低IVR({target_ivr:.1f}%) + 鋼鐵牆築底 + 巨鯨掃貨，強制啟動轉倉)"
            else:
                reason_suffix = ""

            # 強制優先採用極致不對稱勝率條件
            if holding_momentum_decaying and is_extreme_asymmetric:
                should_rollover = True
                rollover_ratio = 1.0  # 條件三要求 100% 滿載運算 / 不留戀
                return {
                    "should_rollover": should_rollover,
                    "rollover_ratio": rollover_ratio,
                    "strategy": strategy,
                    "reason": (
                        f"Holding {current_holding_symbol} momentum decaying (PSQ={current_holding_power_squeeze}). "
                        f"Target {target_watchlist_symbol} hit asymmetric win-rate. "
                        + reason_suffix
                    ),
                }

            return {
                "should_rollover": should_rollover,
                "rollover_ratio": rollover_ratio,
                "strategy": strategy,
                "reason": (
                    f"Holding {current_holding_symbol} momentum decaying (PSQ={current_holding_power_squeeze}). "
                    f"Target {target_watchlist_symbol} showing breakout potential (PSQ={target_power_squeeze}) "
                    f"with EV spread +{ev_spread*100:.1f}%." + reason_suffix
                ),
            }

        return {
            "should_rollover": False,
            "rollover_ratio": 0.0,
            "strategy": "N/A",
            "reason": "No action required.",
        }

    async def evaluate_opportunity_cost_for_satellites(
        self,
        user_id: int,
        portfolio_assets: List[Dict[str, Any]],
        already_flagged_symbols: set,
        candidate_symbol: str,
        candidate_radar: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        邏輯 (2) 批次橋接：對每一個尚未被 Scenario 3 標記的 SATELLITE 持倉，
        比對其 PowerSqueeze/EV 與單一預篩選候選標的 (candidate_symbol) 的機會成本，
        產生與 check_satellite_rebalancing 相同結構的 instruction dict。

        candidate_radar: 由呼叫端 (cog 層) 預先透過既有 radar 抓取機制取得的單一候選標的資料，
        純資料 dict，避免 market_analysis 層依賴 cogs。
        """
        instructions: List[Dict[str, Any]] = []
        if candidate_symbol == "VOO" or not candidate_radar:
            return instructions  # 沒有找到高 EV 候選標的，不強制轉倉

        target_psq = candidate_radar.get("psq_result", {}) or {}
        target_power_squeeze = self._normalize_power_squeeze(target_psq)
        target_expected_value = self._calculate_ev_proxy(candidate_symbol)
        target_spot = float(
            candidate_radar.get("quote", {}).get("c", 0.0)
            if candidate_radar.get("quote")
            else 0.0
        )
        target_ivr = float(
            candidate_radar.get("iv_metrics", {}).get("iv_rank", 0.0)
            if candidate_radar.get("iv_metrics")
            else 0.0
        )
        target_put_wall = (
            float(
                candidate_radar.get("gex_profile_data", {}).get("put_wall", 0.0) or 0.0
            )
            if isinstance(candidate_radar.get("gex_profile_data"), dict)
            else 0.0
        )
        target_uoa_sweep = len(candidate_radar.get("uoa", []) or []) > 0

        for asset in portfolio_assets:
            symbol = str(asset.get("symbol", "")).upper()
            if asset.get("asset_class") != "SATELLITE":
                continue
            if symbol in already_flagged_symbols or symbol == candidate_symbol:
                continue

            holding_psq = asset.get("psq_result", {}) or {}
            current_power_squeeze = self._normalize_power_squeeze(holding_psq)
            current_ev = self._calculate_ev_proxy(symbol)

            avg_cost = float(asset.get("avg_cost", 0.0))
            spot = float(asset.get("spot_price", 0.0))
            profit_pct = (spot - avg_cost) / avg_cost if avg_cost > 0 else 0.0

            result = self.evaluate_opportunity_cost(
                current_holding_symbol=symbol,
                current_holding_power_squeeze=current_power_squeeze,
                current_holding_profit_pct=profit_pct,
                target_watchlist_symbol=candidate_symbol,
                target_power_squeeze=target_power_squeeze,
                target_expected_value=target_expected_value,
                current_holding_expected_value=current_ev,
                target_ivr=target_ivr,
                target_uoa_sweep=target_uoa_sweep,
                target_spot=target_spot,
                target_put_wall=target_put_wall,
            )
            if not result["should_rollover"]:
                continue

            instructions.append(
                {
                    "symbol": symbol,
                    "action": "LIQUIDATE"
                    if result["rollover_ratio"] >= 1.0
                    else "REDUCE",
                    "sell_ratio": result["rollover_ratio"],
                    "target_core": candidate_symbol,
                    "reason": f"💡 **機會成本轉倉 (Opportunity Cost)**\n{result['reason']}",
                    "suggested_strategy": result["strategy"],
                    "scenario": RolloverScenario.OPPORTUNITY_COST.value,
                    "is_manual_override_required": False,
                }
            )
        return instructions

    async def _compute_structural_breakdown_signals(
        self,
        symbol: str,
        spot: float,
        put_wall: float,
        gamma_flip: float,
        atr_14: float,
        sqz_mom: float,
        skew: float,
        price_15m_close: float,
        gex_profile_data: Optional[Dict[str, Any]],
        asset_class: str,
    ) -> Tuple[bool, bool, float, float, float, float]:
        """
        共用結構性破位 / 主力空頭封殺訊號計算：GEX 牆掃描 + anchor_base/gamma_cliff_level
        判定，供 _evaluate_structural_no_edge (Scenario 4) 與 check_satellite_rebalancing
        (Scenario 3) 共同呼叫，避免同一段門檻邏輯需要在兩處分別維護。

        回傳 (is_structural_breakdown, is_whale_sto_block, support_wall, resistance_wall,
              support_gex, resistance_gex)。
        """
        from market_analysis.index_microstructure import classify_gex_wall

        support_wall: float = 0.0
        resistance_wall: float = 0.0
        support_gex: float = 0.0
        resistance_gex: float = 0.0
        if (
            gex_profile_data
            and "gex_profile" in gex_profile_data
            and isinstance(gex_profile_data["gex_profile"], dict)
        ):
            gex_prof = gex_profile_data["gex_profile"]
            max_positive: float = 0.0
            for _, v in gex_prof.items():
                try:
                    val = float(v)
                    if val > max_positive:
                        max_positive = val
                except Exception:
                    pass
            for k, v in gex_prof.items():
                try:
                    val = float(v)
                    strike = float(k)
                    wall_type = classify_gex_wall(
                        val, max_positive, is_heavy_otm_call=False
                    )
                    if wall_type == "SUPPORT_GEX_WALL" and strike > support_wall:
                        support_wall = strike
                        support_gex = val
                    elif (
                        wall_type == "RESISTANCE_CALL_WALL" and strike > resistance_wall
                    ):
                        resistance_wall = strike
                        resistance_gex = val
                except Exception:
                    pass

        anchor_base: float = (
            support_wall
            if support_wall > 0
            else (
                min(put_wall, gamma_flip)
                if (put_wall > 0 and gamma_flip > 0)
                else (put_wall if put_wall > 0 else gamma_flip)
            )
        )
        gamma_cliff_level: float = (
            anchor_base - (1.5 * atr_14) if anchor_base > 0 else 0.0
        )

        is_structural_breakdown_pending: bool = (
            anchor_base > 0
            and spot < anchor_base
            and (price_15m_close < gamma_cliff_level or sqz_mom <= 0)
        )

        is_structural_breakdown = False
        if asset_class == "OPTIONS":
            # 期權快速通道：現價貫穿 anchor_base 即時判定破位，拒絕等待 15m 實體收盤
            if anchor_base > 0 and spot < anchor_base:
                is_structural_breakdown = True
        else:
            if is_structural_breakdown_pending and gamma_cliff_level > 0:
                is_structural_breakdown = await is_gamma_cliff_confirmed(
                    symbol, gamma_cliff_level
                )

        is_whale_sto_block = (sqz_mom < 0.0) and (skew < -0.3)

        return (
            is_structural_breakdown,
            is_whale_sto_block,
            support_wall,
            resistance_wall,
            support_gex,
            resistance_gex,
        )

    async def _evaluate_structural_no_edge(
        self,
        symbol: str,
        spot: float,
        put_wall: float,
        gamma_flip: float,
        atr_14: float,
        sqz_mom: float,
        skew: float,
        price_15m_close: float,
        gex_profile_data: Optional[Dict[str, Any]],
        asset_class: str,
    ) -> bool:
        """
        判定該持倉是否已「結構性無勝率」(結構性破位 或 主力空頭封殺)。
        重用與 check_satellite_rebalancing 共用的 _compute_structural_breakdown_signals，
        不發明新的量化門檻，供 evaluate_margin_defense 在宏觀紅線觸發時逐一檢查每檔
        SATELLITE 持倉。
        """
        (
            is_structural_breakdown,
            is_whale_sto_block,
            _support_wall,
            _resistance_wall,
            _support_gex,
            _resistance_gex,
        ) = await self._compute_structural_breakdown_signals(
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
        )
        return is_structural_breakdown or is_whale_sto_block

    async def evaluate_margin_defense(
        self, user_id: int, portfolio_assets: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        邏輯 (4): 槓桿與保證金防禦 (Leverage & Margin Defense)
        觸發條件: 大盤 Regime 進入 SHORT_GAMMA_CRITICAL / SYSTEMIC_LIQUIDITY_CRISIS
        (大盤宏觀風控紅線亮起：GEX Flip 實質跌破、做市商轉為負 Gamma 踩踏泥淖)，
        且帳戶偵測到保證金壓力 (重用 /stress_test 的 GTC 現金赤字算法；
        若無 GTC 買單造成赤字，退化為「SATELLITE 總市值 > 現金儲備」的保守代理)。

        動作: 逐一檢查每檔 SATELLITE 持倉是否「結構性無勝率」(結構性破位 或 主力空頭封殺)。
        無勝率者強制 100% 轉倉至 BOXX 鎖定無風險利息 —— 因為大盤系統性風險發生時
        VOO 本身亦會同向下跌，唯有真正的現金等價物 BOXX 才能提供防禦；
        仍有勝率的持倉維持不動，不強制減倉。日常的機會成本轉倉與核心衛星再平衡
        (Scenario 2 / 3) 才以 VOO 作為預設閒置資金停泊區，兩者角色互不重疊。

        此系統為現貨/純現金紙上帳戶模型，沒有真實券商槓桿或維持保證金資料，
        因此以 /stress_test 既有的現金緩衝算法作為保證金壓力代理指標。
        """
        from market_analysis.index_microstructure import get_market_regime

        try:
            regime = await get_market_regime()
        except Exception as e:
            logger.error(f"取得市場 Regime 失敗: {e}")
            return []
        if regime not in ("SHORT_GAMMA_CRITICAL", "SYSTEMIC_LIQUIDITY_CRISIS"):
            return []

        # --- 保證金壓力判定 (重用 /stress_test 邏輯) ---
        from database.orders import get_user_active_orders

        try:
            orders = get_user_active_orders(user_id)
        except Exception:
            orders = []
        total_deficit = 0.0
        for o in orders:
            if (
                "GTC" in str(o.get("validity", "")).upper()
                and str(o.get("side", "")).upper() == "BUY"
            ):
                price = o.get("limit_price", 0.0) or o.get("stop_price", 0.0)
                total_deficit += price * o.get("quantity", 0.0)

        user_ctx = get_full_user_context(user_id)
        buffer = user_ctx.cash_reserve if user_ctx else 0.0

        if total_deficit > 0.0:
            is_margin_critical = total_deficit > buffer
            deficit_desc = (
                f"GTC 買單現金赤字 ${total_deficit:,.0f} vs 現金儲備 ${buffer:,.0f}"
            )
        else:
            # 退化代理：無 GTC 買單造成赤字時，以 SATELLITE 總市值 vs 現金儲備
            # 作為「高波動衛星部位超過安全緩衝」的保守保證金壓力訊號。
            satellite_value = sum(
                float(a.get("current_value", 0.0))
                for a in portfolio_assets
                if a.get("asset_class") == "SATELLITE"
            )
            is_margin_critical = satellite_value > buffer
            deficit_desc = (
                f"SATELLITE 部位總市值 ${satellite_value:,.0f} vs 現金儲備 ${buffer:,.0f} "
                "(無 GTC 赤字，採退化代理)"
            )

        if not is_margin_critical:
            return []

        # --- 逐一檢查所有 SATELLITE 持倉是否結構性無勝率 ---
        instructions: List[Dict[str, Any]] = []

        for asset in portfolio_assets:
            symbol = str(asset.get("symbol", "")).upper()
            quantity = float(asset.get("quantity", 0.0))
            if (
                asset.get("asset_class") != "SATELLITE"
                or symbol in CORE_DEFENSE_ETF_SYMBOLS
                or quantity == 0
            ):
                continue

            instrument_type = str(
                asset.get("instrument_type", asset.get("asset_type", "SPOT"))
            ).upper()
            asset_class = (
                "OPTIONS"
                if ("OPT" in instrument_type or "CONTRACT" in instrument_type)
                else "SPOT"
            )

            is_no_edge = await self._evaluate_structural_no_edge(
                symbol=symbol,
                spot=float(asset.get("spot_price", 0.0)),
                put_wall=float(asset.get("put_wall", 0.0)),
                gamma_flip=float(asset.get("gamma_flip", 0.0)),
                atr_14=float(asset.get("atr_14", 0.0)),
                sqz_mom=float(asset.get("sqz_mom", 0.0)),
                skew=float(asset.get("skew", 0.0)),
                price_15m_close=float(
                    asset.get("price_15m_close", asset.get("spot_price", 0.0))
                ),
                gex_profile_data=asset.get("gex_profile_data"),
                asset_class=asset_class,
            )
            if not is_no_edge:
                continue

            is_short_option = (
                "OPT" in instrument_type or "CONTRACT" in instrument_type
            ) and quantity < 0
            sell_action = "BTC" if is_short_option else "STC"

            instructions.append(
                {
                    "symbol": symbol,
                    "action": "LIQUIDATE",
                    "sell_ratio": 1.0,
                    "target_core": "BOXX",
                    "reason": (
                        f"🚨 **槓桿與保證金防禦 (Leverage & Margin Defense)**\n"
                        f"大盤 Regime: `{regime}`\n"
                        f"保證金壓力判定: {deficit_desc}\n"
                        f"{symbol} 個股結構無勝率 (結構性破位 或 主力空頭封殺)，"
                        f"大盤宏觀風控紅線亮起，VOO 亦會同向下跌無法提供防禦，"
                        f"建議 {sell_action} 100% 部位轉倉至 BOXX 鎖定無風險利息。"
                    ),
                    "suggested_strategy": f"{sell_action} 100% 轉倉 BOXX (鎖定無風險利息)",
                    "sell_action": sell_action,
                    "buy_action_label": "轉入 BOXX（鎖定無風險利息）",
                    "is_manual_override_required": True,
                    "scenario": RolloverScenario.MARGIN_DEFENSE.value,
                }
            )

        return instructions

    async def check_satellite_rebalancing(
        self,
        user_id: int,
        portfolio_assets: List[Dict[str, Any]],
        total_account_value: float,
    ) -> List[Dict[str, Any]]:
        """
        邏輯 (3): 核心與衛星比例再平衡 + 深度微觀結構與選擇權籌碼驅動
        包含勝率傾斜與雜訊避險等高階戰術。
        """
        rebalance_instructions: List[Dict[str, Any]] = []

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
                max_alloc: float = float(asset.get("max_allocation_pct", 0.3))

                # --- 新增：深度量化數據 (Fallback = None/0.0) ---
                spot: float = float(asset.get("spot_price", 0.0))
                call_wall: float = float(asset.get("call_wall", 0.0))
                max_pain: float = float(asset.get("max_pain", 0.0))
                ivr: float = float(asset.get("ivr", 0.0))
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
                dte: int = int(asset.get("dte", 99))
                price_15m_close: float = float(asset.get("price_15m_close", spot))
                atr_15m: float = float(asset.get("atr_15m", atr_14))

                # 計算比例
                current_alloc: float = (
                    current_value / total_account_value
                    if total_account_value > 0
                    else 0.0
                )

                asset_class = str(
                    asset.get("instrument_type", asset.get("asset_type", "SPOT"))
                ).upper()
                if "OPT" in asset_class or "CONTRACT" in asset_class:
                    asset_class = "OPTIONS"
                else:
                    asset_class = "SPOT"

                gex_profile_data = asset.get("gex_profile_data", {})

                # ----------------------------------------------------
                # 條件一：現有持倉結構劣化（護衛牆破位 / 主力物理蓋頂 / 目標區獲利解鎖完成）
                # ----------------------------------------------------
                # 1. 做市商 GEX 防線失守 (共用 _compute_structural_breakdown_signals，
                #    與 evaluate_margin_defense/_evaluate_structural_no_edge 同一份門檻邏輯)
                # 2. 主力巨量 STO 實體蓋頂
                (
                    is_structural_breakdown,
                    is_whale_sto_block,
                    support_wall,
                    resistance_wall,
                    support_gex,
                    resistance_gex,
                ) = await self._compute_structural_breakdown_signals(
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
                )

                metrics: Dict[str, Any] = {
                    "spot_price": spot,
                    "call_wall": call_wall,
                    "max_pain": max_pain,
                    "ivr": ivr,
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
                    "atr_15m": atr_15m,
                    "support_wall": support_wall,
                    "resistance_wall": resistance_wall,
                    "support_gex": support_gex,
                    "resistance_gex": resistance_gex,
                }

                # 3. 目標區獲利解鎖完成
                is_profit_unlocked = (call_wall > 0 and spot > 0) and (
                    spot >= call_wall or abs(spot - call_wall) / call_wall < 0.015
                )

                # 目標解鎖與極端亢奮 (Euphoria)
                is_euphoria_skew = skew < 0 and skew_percentile <= 20.0
                is_euphoria = is_profit_unlocked or is_euphoria_skew

                # 條件三 (部分)：擺脫高波洗籌泥淖 (IV Crush 威脅)
                is_iv_bubble = ivr > 80.0

                if (
                    is_structural_breakdown
                    or is_whale_sto_block
                    or is_euphoria
                    or is_iv_bubble
                ):
                    satellite_symbols = {
                        str(a.get("symbol", "")).upper()
                        for a in portfolio_assets
                        if a.get("asset_class") == "SATELLITE"
                    }
                    next_target = self._find_best_rollover_target(
                        user_id, exclude_symbols=satellite_symbols
                    )

                    if is_euphoria:
                        user_ctx = get_full_user_context(user_id)
                        # 雙重動能衰竭確認制：
                        # 1. 15m SQZ MOM 由正轉負 (動能拐頭)
                        # 2. Skew 脫離極端狂熱 (Percentile 回升至 30% 以上)
                        is_exhaustion_confirmed = (sqz_mom < 0.0) and (
                            skew_percentile >= 30.0
                        )

                        if user_ctx.can_trade_spreads and is_exhaustion_confirmed:
                            # 90/10 權限資金拆分 - 衰竭確認，建立 Bear Call Spread 反向收租
                            # 90% 轉入新標的
                            report_90 = self._generate_rule_based_rebalance_report(
                                symbol,
                                metrics,
                                requested_action="LIQUIDATE",
                                target=next_target,
                                asset_class=asset_class,
                                is_take_profit=True,
                                active_orders=user_orders,
                                position_shares=quantity,
                                current_value=current_value,
                            )
                            rebalance_instructions.append(
                                {
                                    "symbol": symbol,
                                    "action": report_90["final_action"],
                                    "sell_ratio": 0.9
                                    if report_90["final_action"] == "LIQUIDATE"
                                    else (
                                        0.5
                                        if report_90["final_action"] == "REDUCE"
                                        else 0.0
                                    ),
                                    "target_core": report_90["final_target"],
                                    "reason": report_90["markdown_report"],
                                    "suggested_strategy": report_90["options_strategy"],
                                    "scenario": RolloverScenario.SATELLITE_REBALANCE.value,
                                    "is_manual_override_required": False,
                                    "trigger_condition_text": report_90[
                                        "trigger_condition_report"
                                    ],
                                    "cash_impact": report_90["cash_impact"],
                                }
                            )
                            # 10% 留存原標的做 Bear Call Spread 反向收租
                            report_10 = self._generate_rule_based_rebalance_report(
                                symbol,
                                metrics,
                                requested_action="LIQUIDATE",
                                target=symbol,
                                strategy_override=f"Bear Call Spread (Short Call @ ${call_wall * 1.02:.2f})",
                                asset_class=asset_class,
                                is_take_profit=True,
                                active_orders=user_orders,
                                position_shares=quantity,
                                current_value=current_value,
                            )
                            rebalance_instructions.append(
                                {
                                    "symbol": symbol,
                                    "action": "REDUCE"
                                    if report_10["final_action"]
                                    in ["LIQUIDATE", "REDUCE"]
                                    else "HOLD",
                                    "sell_ratio": 0.1
                                    if report_10["final_action"]
                                    in ["LIQUIDATE", "REDUCE"]
                                    else 0.0,
                                    "target_core": symbol,
                                    "reason": report_10["markdown_report"]
                                    + "\n⚠️ **【動能衰竭確認】SQZ MOM 拐頭且 Skew 降溫，觸發 Bear Call Spread 反向收租 (手動防滑價)**",
                                    "suggested_strategy": report_10["options_strategy"],
                                    "is_manual_override_required": True,
                                    "scenario": RolloverScenario.SATELLITE_REBALANCE.value,
                                    "trigger_condition_text": report_10[
                                        "trigger_condition_report"
                                    ],
                                    "cash_impact": report_10["cash_impact"],
                                }
                            )
                            continue
                        elif user_ctx.can_trade_spreads and not is_exhaustion_confirmed:
                            # 未衰竭 (多頭動能強勁或 Skew 極端狂熱)，嚴禁做空以防 Gamma Squeeze！
                            # 90% 獲利了結轉入新標的，剩餘 10% 啟動 Trailing Stop 移動止盈
                            trailing_stop_level = round(
                                max(call_wall - (0.5 * atr_15m), spot * 0.98), 2
                            )
                            report_90 = self._generate_rule_based_rebalance_report(
                                symbol,
                                metrics,
                                requested_action="LIQUIDATE",
                                target=next_target,
                                asset_class=asset_class,
                                is_take_profit=True,
                                active_orders=user_orders,
                                position_shares=quantity,
                                current_value=current_value,
                            )
                            rebalance_instructions.append(
                                {
                                    "symbol": symbol,
                                    "action": report_90["final_action"],
                                    "sell_ratio": 0.9
                                    if report_90["final_action"] == "LIQUIDATE"
                                    else (
                                        0.5
                                        if report_90["final_action"] == "REDUCE"
                                        else 0.0
                                    ),
                                    "target_core": report_90["final_target"],
                                    "reason": report_90["markdown_report"],
                                    "suggested_strategy": report_90["options_strategy"],
                                    "scenario": RolloverScenario.SATELLITE_REBALANCE.value,
                                    "is_manual_override_required": False,
                                    "trigger_condition_text": report_90[
                                        "trigger_condition_report"
                                    ],
                                    "cash_impact": report_90["cash_impact"],
                                }
                            )
                            report_10 = self._generate_rule_based_rebalance_report(
                                symbol,
                                metrics,
                                requested_action="HOLD",
                                target=symbol,
                                strategy_override=f"Trailing Stop 移動止盈 (防守位: ${trailing_stop_level:.2f})",
                                asset_class=asset_class,
                                is_take_profit=False,
                                active_orders=user_orders,
                                position_shares=quantity,
                                current_value=current_value,
                            )
                            rebalance_instructions.append(
                                {
                                    "symbol": symbol,
                                    "action": "HOLD",
                                    "sell_ratio": 0.0,
                                    "target_core": symbol,
                                    "reason": report_10["markdown_report"]
                                    + f"\n🚀 **【動能延續・移動止盈】**突破 Call Wall 但動能未衰竭 (SQZ MOM {sqz_mom:+.2f} / Skew {skew_percentile:.0f}%)，嚴禁以身擋車做空！剩餘 10% 部位啟動 Trailing Stop (${trailing_stop_level:.2f}) 讓獲利奔馳。",
                                    "suggested_strategy": report_10["options_strategy"],
                                    "is_manual_override_required": False,
                                    "scenario": RolloverScenario.SATELLITE_REBALANCE.value,
                                    "trigger_condition_text": report_10[
                                        "trigger_condition_report"
                                    ],
                                    "cash_impact": report_10["cash_impact"],
                                }
                            )
                            continue

                    # 一般清倉 / 灰階判定
                    report = self._generate_rule_based_rebalance_report(
                        symbol,
                        metrics,
                        requested_action="LIQUIDATE"
                        if is_structural_breakdown
                        else "HOLD",
                        target=next_target,
                        asset_class=asset_class,
                        is_take_profit=is_euphoria,
                        active_orders=user_orders,
                        position_shares=quantity,
                        current_value=current_value,
                    )

                    rebalance_instructions.append(
                        {
                            "symbol": symbol,
                            "action": report["final_action"],
                            "sell_ratio": 1.0
                            if report["final_action"] == "LIQUIDATE"
                            else (0.5 if report["final_action"] == "REDUCE" else 0.0),
                            "target_core": report["final_target"],
                            "reason": report["markdown_report"],
                            "suggested_strategy": report["options_strategy"],
                            "scenario": RolloverScenario.SATELLITE_REBALANCE.value,
                            "is_manual_override_required": False,
                            "trigger_condition_text": report[
                                "trigger_condition_report"
                            ],
                            "cash_impact": report["cash_impact"],
                        }
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

                    report = self._generate_rule_based_rebalance_report(
                        symbol,
                        metrics,
                        requested_action="REDUCE",
                        asset_class=asset_class,
                        active_orders=user_orders,
                        position_shares=quantity,
                        current_value=current_value,
                    )

                    rebalance_instructions.append(
                        {
                            "symbol": symbol,
                            "action": report["final_action"],
                            "sell_ratio": round(sell_ratio, 2)
                            if report["final_action"] != "LIQUIDATE"
                            else 1.0,
                            "target_core": report["final_target"],
                            "reason": report["markdown_report"],
                            "suggested_strategy": report["options_strategy"],
                            "scenario": RolloverScenario.SATELLITE_REBALANCE.value,
                            "is_manual_override_required": False,
                            "trigger_condition_text": report[
                                "trigger_condition_report"
                            ],
                            "cash_impact": report["cash_impact"],
                        }
                    )

        return rebalance_instructions
