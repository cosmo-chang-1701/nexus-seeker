"""VTR (虛擬交易) 自動建倉、監控與對沖計算 Mixin。"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List

import database
from config import get_vix_tier
from market_analysis import hedging
from market_analysis.pro_management import simulate_cc_transition
from services import market_data_service

if TYPE_CHECKING:
    from market_analysis.ghost_trader import GhostTrader

logger = logging.getLogger(__name__)


class VtrMixin:
    if TYPE_CHECKING:
        vtr_engine: GhostTrader

    async def execute_vtr_auto_entry(self, data: Dict[str, Any]) -> Any:
        """
        執行 VTR 自動建倉。
        """
        uid = data["uid"]
        sym = data["symbol"]
        strategy = data.get("strategy", "")
        safe_qty = data.get("safe_qty", 0)

        # VIX 戰情階梯 VTR 建倉閘門
        vix_spot_val = data.get("vix_spot")
        current_vix_tier = get_vix_tier(vix_spot_val)
        if not current_vix_tier.get("vtr_entry_allowed", True):
            logger.info(
                f"[VTR] 建倉已被 VIX 階梯 '{current_vix_tier['name']}' 放行禁止，略過 {sym}"
            )
            return

        if safe_qty > 0:
            try:
                opt_t = "put" if "PUT" in strategy else "call"
                qty = -safe_qty if "STO" in strategy else safe_qty

                # 自動判定類別：Short SPY 或 BTO SPY Put 為 HEDGE
                trade_category = "SPECULATIVE"
                if sym == "SPY":
                    if qty < 0 or (opt_t == "put" and qty > 0):
                        trade_category = "HEDGE"

                await self.vtr_engine.record_virtual_entry(
                    user_id=uid,
                    symbol=sym,
                    opt_type=opt_t,
                    strike=data["strike"],
                    expiry=data["target_date"],
                    quantity=qty,
                    weighted_delta=data.get("weighted_delta", 0.0),
                    theta=data.get("theta", 0.0),
                    gamma=data.get("gamma", 0.0),
                    tags=["auto_scan"],
                    trade_category=trade_category,
                )
            except Exception as e:
                logger.error(f"VTR Entry failed: {e}")

    async def monitor_vtr_and_calculate_hedging(self) -> List[Dict[str, Any]]:
        """
        監控 VTR 持倉，執行管理與轉倉，並計算對沖建議。
        返回需要發送給使用者的結算與對沖訊息數據。
        """
        results = []
        try:
            from database.virtual_trading import (
                get_all_open_virtual_trades,
                get_virtual_trades,
            )

            before_trades = await asyncio.to_thread(get_all_open_virtual_trades)
            before_ids = {t["id"] for t in before_trades}

            # 執行管理與轉倉
            await self.vtr_engine.manage_virtual_positions()
            await self.vtr_engine.execute_virtual_roll()

            # 重新檢查交易列表
            after_trades = await asyncio.to_thread(get_all_open_virtual_trades)
            after_ids = {t["id"] for t in after_trades}
            closed_ids = before_ids - after_ids

            # 3. 找出演進候選部位 (Synthetic -> Core Equity)
            transition_candidates = await self.vtr_engine.get_transition_candidates()
            for cand in transition_candidates:
                trade = cand["trade"]
                uid = trade["user_id"]
                sym = trade["symbol"]

                quote = await market_data_service.get_quote(sym)
                stock_price = quote.get("c", 0.0) if quote else 0.0
                if stock_price == 0.0:
                    continue

                # 模擬演進邏輯
                # 假設目標 CC Strike 為 5% OTM，權利金為 2%
                target_cc_strike = round(stock_price * 1.05, 1)
                est_premium = round(stock_price * 0.02, 2)

                trans_result = simulate_cc_transition(
                    current_option_pnl=cand["pnl_usd"],
                    current_stock_price=stock_price,
                    target_cc_strike=target_cc_strike,
                    target_cc_premium=est_premium,
                )

                results.append(
                    {
                        "uid": uid,
                        "type": "TRANSITION_SUGGESTION",
                        "symbol": sym,
                        "pnl_pct": cand["pnl_pct"],
                        "pnl_usd": cand["pnl_usd"],
                        "transition_result": trans_result,
                        "stock_price": stock_price,
                    }
                )

            if closed_ids:
                # 獲獲取全站最近紀錄
                all_history = await asyncio.to_thread(get_virtual_trades, user_id=None)
                spy_quote = await market_data_service.get_quote("SPY")
                spy_price = spy_quote.get("c", 500.0) if spy_quote else 500.0

                for tid in closed_ids:
                    trade_info = next((t for t in all_history if t["id"] == tid), None)
                    if not trade_info:
                        continue

                    uid = trade_info["user_id"]
                    user_context = database.get_full_user_context(uid)
                    current_total_delta = user_context.total_weighted_delta
                    user_capital = user_context.capital

                    # 位階判斷
                    target_delta, regime = await hedging.get_market_regime_target(
                        spy_price, user_capital
                    )
                    hedge = hedging.calculate_autonomous_hedge(
                        current_total_delta, target_delta, spy_price
                    )

                    results.append(
                        {
                            "uid": uid,
                            "trade_info": trade_info,
                            "current_total_delta": current_total_delta,
                            "user_capital": user_capital,
                            "spy_price": spy_price,
                            "regime": regime,
                            "target_delta": target_delta,
                            "hedge": hedge,
                        }
                    )
        except Exception as e:
            logger.error(f"VTR monitoring service error: {e}")

        return results
