from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from market_analysis.option_guidance import is_spread_illiquid
from market_analysis.sentiment.history_storage import get_indicator_percentile

from . import logger
from ._shared import format_cash_impact
from .constants import (
    _ANTI_WASHOUT_BASE_ATR_MULT,
    _ANTI_WASHOUT_EXTREME_ATR_MULT,
    _BEAR_CALL_SPREAD_WING_ATR_MULT,
    _BEAR_CALL_SPREAD_WING_FALLBACK_PCT,
    _BUYER_LOCKOUT_IVR_THRESHOLD,
    _DEFAULT_MAX_ALLOCATION_PCT,
    _EUPHORIA_CAPITAL_SPLIT_PRIMARY,
    _EUPHORIA_CAPITAL_SPLIT_RESIDUAL,
    _EUPHORIA_SKEW_PERCENTILE,
    _EXHAUSTION_SKEW_PERCENTILE,
    _FALLBACK_TARGET_PRICE_ESTIMATE,
    _FORCED_SETTLEMENT_ROLL_MAX_DTE,
    _FORCED_SETTLEMENT_ROLL_MIN_DTE,
    _HOLDING_DTE_FORCED_SETTLEMENT_THRESHOLD,
    _IV_BUBBLE_THRESHOLD,
    _PROFIT_UNLOCK_TOLERANCE,
    _TRAILING_STOP_ATR_MULT,
    _TRAILING_STOP_SPOT_FLOOR_PCT,
)
from .models import RolloverInstruction, RolloverScenario
from .structural_signals import _resolve_canonical_anchor_base, evaluate_option_dte_tier


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

        # 機制 2: 1.5x ATR 防護墊片
        if anchor_base > 0:
            raw_stop_loss = anchor_base - (_ANTI_WASHOUT_BASE_ATR_MULT * atr_15m)
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
        is_take_profit: bool,
        stop_loss: float,
        anchor_base: float,
        extreme_stop_loss: float = 0.0,
    ) -> Tuple[str, str, str, str, bool]:
        """
        灰階思考量化裁決 (決策矩陣 - 雙軌裁決機制 Dual-Track Exit)。
        回傳 (final_action, final_target, options_strategy, system_conflict_note,
        is_extreme_tick_breach)。is_extreme_tick_breach 供呼叫端判斷是否需要將
        呈現層升級為最高急迫性樣式（見 rollover_embeds.py 的立即人工執行標記）。
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

        # 軌道二：極端瞬時停損 (Extreme Tick Breach)。無論 SPOT 或 OPTIONS，
        # 現價貫穿即立即觸發，無視 15m 實體收盤等待 (SPOT 常態停損仍維持
        # 15m 收盤確認，僅此極端檔位額外賦予 SPOT 即時熔斷能力；OPTIONS
        # 本已有即時熔斷，此檔位對其而言是純粹的向下相容 backstop)。優先權
        # 高於 is_options_fast_exit/is_15m_close_broken，僅次於獲利了結——
        # `not is_take_profit` 守衛是這個「僅次於」的必要條件，而非裝飾：
        # 若省略，即使 is_take_profit 分支才是實際決定 final_action/敘事的
        # 分支，這裡仍會回傳原始的價格穿透判定，導致下游 (呼叫端組裝
        # extreme_breach_detail_block、rollover_embeds.py 的立即人工執行紅色
        # 急迫樣式覆蓋) 誤把一則平靜的「🎯 獲利解鎖達成」通知，套上「🆘 立即
        # 人工執行」的緊急標題與極端熔斷詳情欄位——兩者敘事互相矛盾。真實
        # 案例：TSLA 現價同時站上 Call Wall (獲利解鎖) 且跌破以 GEX Support
        # Wall 算出的極端熔斷線，過去在此處會回傳 True，讓 embed 呈現「獲利
        # 了結」內文配「立即人工執行」急迫標題的錯亂訊息。
        is_extreme_tick_breach = not is_take_profit and (
            spot < extreme_stop_loss if (extreme_stop_loss > 0 and spot > 0) else False
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
        elif is_extreme_tick_breach:
            final_action = "LIQUIDATE"
            final_target = target if target else "VOO"
            system_conflict_note = (
                f"🆘 **極端瞬時停損觸發**：標的現價 (${spot:.2f}) 貫穿極端防守位 "
                f"(${extreme_stop_loss:.2f} = ${anchor_base:.2f} - "
                f"{_ANTI_WASHOUT_EXTREME_ATR_MULT}× ATR_15m)，無視 15m 實體收盤"
                f"等待，立即市價平倉轉入 {final_target}（現貨與期權皆適用的"
                "最後防線，阻斷突發黑天鵝與流動性真空滑步）。"
            )
            options_strategy = f"100% LIQUIDATE / STC (極端瞬時停損轉入 {final_target})"
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

        return (
            final_action,
            final_target,
            options_strategy,
            system_conflict_note,
            is_extreme_tick_breach,
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

        (
            final_action,
            final_target,
            options_strategy,
            system_conflict_note,
            is_extreme_tick_breach,
        ) = self._apply_decision_matrix(
            symbol=symbol,
            metrics=metrics,
            requested_action=requested_action,
            target=target,
            asset_class=asset_class,
            is_take_profit=is_take_profit,
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
     *(避開真空區，依據公式：`Stop = ${anchor_base:.2f} - (1.5 × ATR_15m) = ${stop_loss_str}`)*
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


async def _build_euphoria_primary_liquidation_instruction(
    engine: Any,
    symbol: str,
    metrics: dict,
    asset_class: str,
    quantity: float,
    current_value: float,
    user_orders: list,
    next_target: str,
) -> RolloverInstruction:
    """極端亢奮區雙軌機制 (Bear Call Spread 反向收租 / Trailing Stop 移動止盈)
    共用的 90% 主要轉倉腳：兩個分支對這 90% 部位的處理完全相同 (LIQUIDATE 90%
    進 next_target)，僅剩餘 10% 殘留腳的後續處理不同，故僅此 90% 部分可安全
    抽取為共用函式，避免兩處分支各自維護逐字相同的 34 行區塊。"""
    report_90 = await engine._generate_rule_based_rebalance_report(
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
    return {
        "symbol": symbol,
        "action": report_90["final_action"],
        "sell_ratio": _EUPHORIA_CAPITAL_SPLIT_PRIMARY
        if report_90["final_action"] == "LIQUIDATE"
        else (0.5 if report_90["final_action"] == "REDUCE" else 0.0),
        "target_core": report_90["final_target"],
        "reason": report_90["markdown_report"],
        "suggested_strategy": report_90["options_strategy"],
        "scenario": RolloverScenario.SATELLITE_REBALANCE.value,
        "is_manual_override_required": False,
        "trigger_condition_text": report_90["trigger_condition_report"],
        "cash_impact": report_90["cash_impact"],
        "limit_price": report_90["limit_price"],
        "extreme_stop_loss": report_90.get("extreme_stop_loss"),
        "is_extreme_tick_breach": report_90.get("is_extreme_tick_breach", False),
        "extreme_breach_detail_block": report_90.get("extreme_breach_detail_block"),
        "instrument_type": asset_class,
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
                "acquired_at": acquired_at,
                "iv_term_structure_status": iv_term_structure_status,
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
                    # DTE 三態狀態機：開立全新 Bear Call Spread 屬於「開立全新
                    # 選擇權結構」(NEW_OPPORTUNITY)，1<dte<7 的 LOCKOUT_SKIP 需
                    # 封鎖此分支，落入下方 Trailing Stop 分支 (純風控延伸，非
                    # 新開倉，不受影響)。SPOT 持倉 dte 恆為 99，不受影響。
                    is_new_entry_allowed = (
                        evaluate_option_dte_tier(dte, "NEW_OPPORTUNITY")
                        == "NORMAL_EXECUTION"
                    )

                    if (
                        user_ctx.can_trade_spreads
                        and is_exhaustion_confirmed
                        and is_new_entry_allowed
                    ):
                        # 90/10 權限資金拆分 - 衰竭確認，建立 Bear Call Spread 反向收租
                        # 90% 轉入新標的
                        rebalance_instructions.append(
                            await _build_euphoria_primary_liquidation_instruction(
                                engine,
                                symbol,
                                metrics,
                                asset_class,
                                quantity,
                                current_value,
                                user_orders,
                                next_target,
                            )
                        )
                        # 10% 留存原標的做 Bear Call Spread 反向收租 (定義完整 Long Wing)
                        short_strike = round(call_wall * 1.02, 2)
                        wing_buffer = (
                            _BEAR_CALL_SPREAD_WING_ATR_MULT * atr_15m
                            if atr_15m > 0
                            else short_strike * _BEAR_CALL_SPREAD_WING_FALLBACK_PCT
                        )
                        long_strike = round(short_strike + wing_buffer, 2)
                        spread_override_str = f"Bear Call Spread (${short_strike:.2f} Short / ${long_strike:.2f} Long Wing, 30-45 DTE)"
                        report_10 = await engine._generate_rule_based_rebalance_report(
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
                                    is_forced_settlement=False,
                                    is_same_symbol_reentry=True,
                                ),
                                "suggested_strategy": report_10["options_strategy"],
                                "is_manual_override_required": True,
                                "scenario": RolloverScenario.SATELLITE_REBALANCE.value,
                                "trigger_condition_text": report_10[
                                    "trigger_condition_report"
                                ],
                                "cash_impact": report_10["cash_impact"],
                                "limit_price": short_strike,
                                "extreme_stop_loss": report_10.get("extreme_stop_loss"),
                                "is_extreme_tick_breach": report_10.get(
                                    "is_extreme_tick_breach", False
                                ),
                                "extreme_breach_detail_block": report_10.get(
                                    "extreme_breach_detail_block"
                                ),
                                "instrument_type": asset_class,
                            }
                        )
                        continue
                    elif user_ctx.can_trade_spreads and not is_exhaustion_confirmed:
                        # 未衰竭 (多頭動能強勁或 Skew 極端狂熱)，嚴禁做空以防 Gamma Squeeze！
                        # 90% 獲利了結轉入新標的，剩餘 10% 啟動 Trailing Stop 移動止盈
                        trailing_stop_level = round(
                            max(
                                call_wall - (_TRAILING_STOP_ATR_MULT * atr_15m),
                                spot * _TRAILING_STOP_SPOT_FLOOR_PCT,
                            ),
                            2,
                        )
                        rebalance_instructions.append(
                            await _build_euphoria_primary_liquidation_instruction(
                                engine,
                                symbol,
                                metrics,
                                asset_class,
                                quantity,
                                current_value,
                                user_orders,
                                next_target,
                            )
                        )
                        report_10 = await engine._generate_rule_based_rebalance_report(
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
                                "limit_price": trailing_stop_level,
                                "extreme_stop_loss": report_10.get("extreme_stop_loss"),
                                "is_extreme_tick_breach": report_10.get(
                                    "is_extreme_tick_breach", False
                                ),
                                "extreme_breach_detail_block": report_10.get(
                                    "extreme_breach_detail_block"
                                ),
                                "instrument_type": asset_class,
                            }
                        )
                        continue

                # 一般清倉 / 灰階判定
                report = await engine._generate_rule_based_rebalance_report(
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

                default_sell_ratio = (
                    1.0
                    if report["final_action"] == "LIQUIDATE"
                    else (0.5 if report["final_action"] == "REDUCE" else 0.0)
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
