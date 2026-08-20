from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from market_analysis.option_guidance import is_spread_illiquid

from . import logger
from .constants import CORE_DEFENSE_ETF_SYMBOLS
from .models import RolloverScenario


class _MarginDefenseMixin:
    """邏輯 (4)：槓桿與保證金防禦 (Leverage & Margin Defense) 輔助方法。"""

    if TYPE_CHECKING:
        # 由 DynamicRolloverEngine（__init__.py）實際提供，此處僅供 mypy 解析
        # mixin 之間互相依賴的方法簽名，執行期不會用到這個宣告。
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
            call_wall: float = 0.0,
            hvn: float = 0.0,
        ) -> Tuple[bool, bool, float, float, float, float]: ...

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
        call_wall: float = 0.0,
        hvn: float = 0.0,
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
            call_wall=call_wall,
            hvn=hvn,
        )
        return is_structural_breakdown or is_whale_sto_block


async def evaluate_margin_defense_impl(
    engine: Any,
    get_full_user_context: Any,
    user_id: int,
    portfolio_assets: List[Dict[str, Any]],
    already_flagged_symbols: Optional[set] = None,
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

    already_flagged_symbols: 已被 Scenario 2 (機會成本轉倉) 或 Scenario 3
    (核心衛星再平衡) 標記過的標的集合，會被跳過以避免同一標的同一輪次
    收到互相矛盾的清倉指令。
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

    flagged = already_flagged_symbols or set()
    for asset in portfolio_assets:
        symbol = str(asset.get("symbol", "")).upper()
        quantity = float(asset.get("quantity", 0.0))
        if (
            asset.get("asset_class") != "SATELLITE"
            or symbol in CORE_DEFENSE_ETF_SYMBOLS
            or symbol in flagged
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

        is_no_edge = await engine._evaluate_structural_no_edge(
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
            call_wall=float(asset.get("call_wall", 0.0)),
            hvn=float(asset.get("hvn", 0.0)),
        )
        if not is_no_edge:
            continue

        is_short_option = (
            "OPT" in instrument_type or "CONTRACT" in instrument_type
        ) and quantity < 0
        sell_action = "BTC" if is_short_option else "STC"

        has_actual_deficit = total_deficit > 0.0
        if has_actual_deficit:
            target_asset = "CASH"
            buy_label = "保留現金（補足現金儲備，消除追繳風險）"
            strategy_desc = f"{sell_action} 100% 部位以保留現金 (消除保證金追繳風險)"
            dest_reason = (
                "建議平倉釋放資金保留為現金儲備，優先補足保證金缺口以消除追繳風險。"
            )
        else:
            target_asset = "BOXX"
            buy_label = "轉入 BOXX（鎖定無風險利息）"
            strategy_desc = f"{sell_action} 100% 轉倉 BOXX (鎖定無風險利息)"
            dest_reason = (
                "大盤宏觀風控紅線亮起，VOO 亦會同向下跌無法提供防禦，"
                f"建議 {sell_action} 100% 部位轉倉至 BOXX 鎖定無風險利息。"
            )

        reason_text = (
            f"🚨 **槓桿與保證金防禦 (Leverage & Margin Defense)**\n"
            f"大盤 Regime: `{regime}`\n"
            f"保證金壓力判定: {deficit_desc}\n"
            f"{symbol} 個股結構無勝率 (結構性破位 或 主力空頭封殺)。\n"
            f"{dest_reason}"
        )

        # --- 強制清倉前檢查既有委託單，避免矛盾指令或重複疊加下單 ---
        has_gtc_buy_conflict = any(
            str(o.get("symbol", "")).upper() == symbol
            and "GTC" in str(o.get("validity", "")).upper()
            and str(o.get("side", "")).upper() == "BUY"
            for o in orders
        )
        if has_gtc_buy_conflict:
            reason_text += (
                "\n⚠️ **委託單矛盾警示**：偵測到現有 GTC 買入網格委託單，"
                "與本次強制平倉建議矛盾，請先手動取消買入網格委託單。"
            )

        # --- 流動性閘門 (#7)：期權部位若帶有 bid/ask，點差過寬時附加警示 ---
        bid = float(asset.get("bid", 0.0))
        ask = float(asset.get("ask", 0.0))
        if asset_class == "OPTIONS" and is_spread_illiquid(bid, ask):
            spread_pct = (ask - bid) / ((ask + bid) / 2)
            reason_text += (
                f"\n⚠️ **流動性警告**：合約點差過寬 (Bid ${bid:.2f} / Ask ${ask:.2f}，"
                f"點差 {spread_pct:.1%})，建議採限價單並留意滑價。"
            )

        _, matching_sell_order = engine._resolve_active_order_defense(
            symbol, orders, 0.0, 0.0
        )
        action = "LIQUIDATE"
        sell_ratio, net_note = engine._net_against_existing_order(
            1.0, abs(quantity), matching_sell_order
        )
        if net_note:
            reason_text += net_note
        if sell_ratio <= 0.0:
            action = "HOLD"

        instructions.append(
            {
                "symbol": symbol,
                "action": action,
                "sell_ratio": sell_ratio,
                "target_core": target_asset,
                "reason": reason_text,
                "suggested_strategy": strategy_desc,
                "sell_action": sell_action,
                "buy_action_label": buy_label,
                "is_manual_override_required": True,
                "scenario": RolloverScenario.MARGIN_DEFENSE.value,
            }
        )

    return instructions
