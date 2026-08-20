from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from market_analysis.option_guidance import is_spread_illiquid
from market_analysis.sentiment.history_storage import get_indicator_percentile

from . import logger
from .constants import (
    _BEAR_CALL_SPREAD_WING_ATR_MULT,
    _BUYER_LOCKOUT_IVR_THRESHOLD,
    _DEFAULT_MAX_ALLOCATION_PCT,
    _EUPHORIA_CAPITAL_SPLIT_PRIMARY,
    _EUPHORIA_CAPITAL_SPLIT_RESIDUAL,
    _EUPHORIA_SKEW_PERCENTILE,
    _EXHAUSTION_SKEW_PERCENTILE,
    _FALLBACK_TARGET_PRICE_ESTIMATE,
    _IV_BUBBLE_THRESHOLD,
    _PROFIT_UNLOCK_TOLERANCE,
)
from .models import RolloverScenario
from .structural_signals import _resolve_canonical_anchor_base


def apply_ivr_strategy_overlay_impl(
    is_selling_locked_by_ivr: Any,
    options_strategy: str,
    strategy_override: str,
    ivr: float,
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

        # 邊界防護：機制 2 算出的基礎停損鉗制在 [spot*0.95, spot*0.98]（2%~5% 邊界），
        # 防止 anchor_base 數據異常（GEX 牆缺失/畸形）導致停損離現價過遠或過近。
        # 僅鉗制此處的「基礎值」，下方機制 1 (LVN 吸附) 與機制 4 (0/1 DTE 擴展)
        # 允許依物理流動性理由將最終停損推到此邊界之外。
        # 條件限定 base_stop_loss < spot（尚未破位）：若 anchor_base 已高於現價
        # （現價已貫穿防守牆），raw base 本身即是雙軌裁決機制用來判定「已破位」的
        # 訊號 (spot < stop_loss)，鉗制會把停損拉回現價之下、誤將破位訊號抹除，
        # 因此破位後的狀態刻意不鉗制，交由 _apply_decision_matrix 的破位判定處理。
        if spot > 0 and 0 < base_stop_loss < spot:
            base_stop_loss = max(spot * 0.95, min(spot * 0.98, base_stop_loss))

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
        self, is_01dte_expanded: bool, is_same_symbol_reentry: bool
    ) -> str:
        """稅務風險資訊性提示（純附加，不做任何攔截閘門，本系統不代為判定）。

        涵蓋兩個最有風險的既有分支：
        1. 0/1 DTE 價內短期合約平倉，可能觸發指派 (Assignment)。
        2. 同標的先賣出後又立即重新建倉 (如 Euphoria 雙軌機制留存部位開 Bear
           Call Spread)，可能落入 Wash Sale 規則範圍。
        """
        if not (is_01dte_expanded or is_same_symbol_reentry):
            return ""
        notes = []
        if is_01dte_expanded:
            notes.append("0/1 DTE 價內合約平倉可能觸發指派 (Assignment)")
        if is_same_symbol_reentry:
            notes.append("同標的近期重新建立相似曝險，請留意 Wash Sale 規則")
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
        bid = float(metrics.get("bid", 0.0))
        ask = float(metrics.get("ask", 0.0))
        # 流動性閘門 (#7)：目前僅期權持倉稽核流程有機會取得 bid/ask（預設 0.0，
        # 對近 100% 尚未接上即時期權報價的真實流量優雅降級為不判定）。實際替持倉
        # 中的期權部位取得即時 bid/ask 屬資料管線擴充，列為範圍外後續追蹤項目。
        is_illiquid_warning = asset_class == "OPTIONS" and is_spread_illiquid(bid, ask)

        anchor_base, effective_res_wall = self._correct_wall_topology(metrics)
        stop_loss, limit_price, is_01dte_expanded, dte_risk_parity_scale = (
            self._compute_anti_washout_stop(anchor_base, metrics)
        )
        order_defense_str, matching_order = self._resolve_active_order_defense(
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

        dte_scale_note = ""
        if is_01dte_expanded:
            dte_scale_note = "\n   - ⚡ **0/1 DTE 風險平價口數縮放**：已啟動 3.0× ATR 緩衝墊片 (停損拉寬)，強制削減 50% 轉倉/開倉部位規模以維持 Dollar Risk 恆定。"

        liquidity_note = ""
        if is_illiquid_warning:
            spread_pct = (ask - bid) / ((ask + bid) / 2)
            liquidity_note = (
                f"\n   - ⚠️ **流動性警告**：合約點差過寬 (Bid ${bid:.2f} / Ask ${ask:.2f}，"
                f"點差 {spread_pct:.1%})，建議採限價單並留意滑價，避免市價單重擊點差。"
            )

        tax_note = self._maybe_append_tax_risk_note(
            is_01dte_expanded=is_01dte_expanded and final_action == "LIQUIDATE",
            is_same_symbol_reentry=False,
        )

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
   - 巨鯨掃貨: {"✅ 偵測到 UOA Sweep" if is_uoa_sweep else "❌ 無明顯 UOA"}
3. **動能與擠壓狀態**
   - SQZ MOM: {sqz_mom:+.2f} | Skew: {skew:.2f} ({"多頭動能延續" if sqz_mom > 0 else "動能中性/趨緩"})
4. **具體的動態轉倉建議**
   - {system_conflict_note if system_conflict_note else "常規執行：依系統建議比例調節"}{dte_scale_note}{liquidity_note}
   - 轉倉決策: **{final_action} ({"維持現狀續抱" if final_action == "HOLD" else "轉入 " + final_target})**
   - 微結構判定: GEX Wall ${anchor_base:.2f} 護城河完好，阻力天花板 ${effective_res_wall:.2f}
   - 防守機制: {order_defense_str}
     *(避開真空區，依據公式：`Stop = ${anchor_base:.2f} - ({"3.0" if is_01dte_expanded else "1.5"} × ATR_15m) = ${stop_loss_str}`)*
   - 出場裁決軌道: {dual_track_note}
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
2. **轉倉執行動作**：
   - 回收資金約 **{cash_str}**。
   - **唯一指令**：立即市價全數買入 **{shares_guidance_str}**，使組合轉為 100% {target_core_name} 大盤防禦模式。
""".strip()

        markdown_report = f"{core_report}\n\n---\n{trigger_condition_report}{tax_note}"
        return {
            "final_action": final_action,
            "final_target": final_target,
            "options_strategy": options_strategy,
            "markdown_report": markdown_report.strip(),
            "trigger_condition_report": trigger_condition_report,
            "cash_impact": cash_str,
            "matching_order": matching_order,
            "is_illiquid_warning": is_illiquid_warning,
        }


async def check_satellite_rebalancing_impl(
    engine: Any,
    get_full_user_context: Any,
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
            max_alloc: float = float(
                asset.get("max_allocation_pct", _DEFAULT_MAX_ALLOCATION_PCT)
            )

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
                current_value / total_account_value if total_account_value > 0 else 0.0
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
                "bid": float(asset.get("bid", 0.0)),
                "ask": float(asset.get("ask", 0.0)),
            }

            # 3. 目標區獲利解鎖完成
            is_profit_unlocked = (call_wall > 0 and spot > 0) and (
                spot >= call_wall
                or abs(spot - call_wall) / call_wall < _PROFIT_UNLOCK_TOLERANCE
            )

            # 目標解鎖與極端亢奮 (Euphoria)
            is_euphoria_skew = skew < 0 and skew_percentile <= _EUPHORIA_SKEW_PERCENTILE
            is_euphoria = is_profit_unlocked or is_euphoria_skew

            # 條件三 (部分)：擺脫高波洗籌泥淖 (IV Crush 威脅)
            is_iv_bubble = ivr > _IV_BUBBLE_THRESHOLD

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
                # 機構風控鐵律：若為結構破位或空頭封殺，強制撤退回防核心資產 (VOO)，
                # 嚴禁在停損時又去追逐另一檔高波動衛星標的 (避免 Hot Potato Rotation 擴大虧損)；
                # 僅在極端亢奮獲利了結 (Euphoria) 或主動輪動時才尋找下一個高 EV 自選標的。
                if is_structural_breakdown or is_whale_sto_block:
                    next_target = "VOO"
                else:
                    next_target = engine._find_best_rollover_target(
                        user_id, exclude_symbols=satellite_symbols
                    )

                if is_euphoria:
                    user_ctx = get_full_user_context(user_id)
                    # 雙重動能衰竭確認制：
                    # 1. 15m SQZ MOM 由正轉負 (動能拐頭)
                    # 2. Skew 脫離極端狂熱 (Percentile 回升至 30% 以上)
                    is_exhaustion_confirmed = (sqz_mom < 0.0) and (
                        skew_percentile >= _EXHAUSTION_SKEW_PERCENTILE
                    )

                    if user_ctx.can_trade_spreads and is_exhaustion_confirmed:
                        # 90/10 權限資金拆分 - 衰竭確認，建立 Bear Call Spread 反向收租
                        # 90% 轉入新標的
                        report_90 = engine._generate_rule_based_rebalance_report(
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
                                "sell_ratio": _EUPHORIA_CAPITAL_SPLIT_PRIMARY
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
                        # 10% 留存原標的做 Bear Call Spread 反向收租 (定義完整 Long Wing)
                        short_strike = round(call_wall * 1.02, 2)
                        wing_buffer = (
                            _BEAR_CALL_SPREAD_WING_ATR_MULT * atr_15m
                            if atr_15m > 0
                            else short_strike * 0.05
                        )
                        long_strike = round(short_strike + wing_buffer, 2)
                        spread_override_str = f"Bear Call Spread (${short_strike:.2f} Short / ${long_strike:.2f} Long Wing, 30-45 DTE)"
                        report_10 = engine._generate_rule_based_rebalance_report(
                            symbol,
                            metrics,
                            requested_action="LIQUIDATE",
                            target=symbol,
                            strategy_override=spread_override_str,
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
                                if report_10["final_action"] in ["LIQUIDATE", "REDUCE"]
                                else "HOLD",
                                "sell_ratio": _EUPHORIA_CAPITAL_SPLIT_RESIDUAL
                                if report_10["final_action"] in ["LIQUIDATE", "REDUCE"]
                                else 0.0,
                                "target_core": symbol,
                                "reason": report_10["markdown_report"]
                                + "\n⚠️ **【動能衰竭確認】SQZ MOM 拐頭且 Skew 降溫，觸發 Bear Call Spread 反向收租 (手動防滑價)**"
                                + engine._maybe_append_tax_risk_note(
                                    is_01dte_expanded=False,
                                    is_same_symbol_reentry=True,
                                ),
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
                        report_90 = engine._generate_rule_based_rebalance_report(
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
                                "sell_ratio": _EUPHORIA_CAPITAL_SPLIT_PRIMARY
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
                        report_10 = engine._generate_rule_based_rebalance_report(
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
                report = engine._generate_rule_based_rebalance_report(
                    symbol,
                    metrics,
                    requested_action="LIQUIDATE" if is_structural_breakdown else "HOLD",
                    target=next_target,
                    asset_class=asset_class,
                    is_take_profit=is_euphoria,
                    active_orders=user_orders,
                    position_shares=quantity,
                    current_value=current_value,
                )

                net_action = report["final_action"]
                net_sell_ratio = (
                    1.0
                    if net_action == "LIQUIDATE"
                    else (0.5 if net_action == "REDUCE" else 0.0)
                )
                net_reason = report["markdown_report"]
                if net_action in ("LIQUIDATE", "REDUCE"):
                    net_sell_ratio, net_note = engine._net_against_existing_order(
                        net_sell_ratio, quantity, report.get("matching_order")
                    )
                    if net_note:
                        net_reason += net_note
                    if net_sell_ratio <= 0.0:
                        net_action = "HOLD"

                rebalance_instructions.append(
                    {
                        "symbol": symbol,
                        "action": net_action,
                        "sell_ratio": net_sell_ratio,
                        "target_core": report["final_target"],
                        "reason": net_reason,
                        "suggested_strategy": report["options_strategy"],
                        "scenario": RolloverScenario.SATELLITE_REBALANCE.value,
                        "is_manual_override_required": bool(
                            report.get("is_illiquid_warning", False)
                        ),
                        "trigger_condition_text": report["trigger_condition_report"],
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

                report = engine._generate_rule_based_rebalance_report(
                    symbol,
                    metrics,
                    requested_action="REDUCE",
                    asset_class=asset_class,
                    active_orders=user_orders,
                    position_shares=quantity,
                    current_value=current_value,
                )

                net_action = report["final_action"]
                net_sell_ratio = (
                    round(sell_ratio, 2) if net_action != "LIQUIDATE" else 1.0
                )
                net_reason = report["markdown_report"]
                if net_action in ("LIQUIDATE", "REDUCE"):
                    net_sell_ratio, net_note = engine._net_against_existing_order(
                        net_sell_ratio, quantity, report.get("matching_order")
                    )
                    if net_note:
                        net_reason += net_note
                    if net_sell_ratio <= 0.0:
                        net_action = "HOLD"

                rebalance_instructions.append(
                    {
                        "symbol": symbol,
                        "action": net_action,
                        "sell_ratio": net_sell_ratio,
                        "target_core": report["final_target"],
                        "reason": net_reason,
                        "suggested_strategy": report["options_strategy"],
                        "scenario": RolloverScenario.SATELLITE_REBALANCE.value,
                        "is_manual_override_required": bool(
                            report.get("is_illiquid_warning", False)
                        ),
                        "trigger_condition_text": report["trigger_condition_report"],
                        "cash_impact": report["cash_impact"],
                    }
                )

    return rebalance_instructions
