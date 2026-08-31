"""持倉損益、風險審計、盤後結算報告 Mixin。"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List

import database
from market_analysis import portfolio, hedging
from services import market_data_service

from services.trading_service.capital import get_adjusted_user_capital

logger = logging.getLogger(__name__)


class ReportsMixin:
    async def get_portfolio_pnl(self, user_id: int) -> Dict[str, Any]:
        """
        計算實單持倉的未實現損益 (Unrealized PnL)
        回傳結構: {'trades': [...], 'total_unrealized_pnl': ...}
        """
        from services.asset_manager import AssetManager
        from models.asset import ContextType
        from market_analysis.portfolio import get_option_chain_mid_iv

        manager = AssetManager()
        assets = manager.get_assets(user_id, ContextType.TRADE)

        trades = []
        total_unrealized_pnl = 0.0

        # 併發批次拉取各部位期權鏈中間價 (Semaphore(3) 上限，避免逐筆序列 await 拖慢回應)
        sem = asyncio.Semaphore(3)

        async def _fetch_mid(asset: Any) -> float:
            am = asset.metadata
            async with sem:
                mid_price, _ = await get_option_chain_mid_iv(
                    asset.symbol, am.get("expiry"), am.get("strike"), am.get("opt_type")
                )
                return float(mid_price)

        mids = await asyncio.gather(*[_fetch_mid(a) for a in assets])

        for a, mid in zip(assets, mids):
            m = a.metadata
            sym = a.symbol
            opt_type = m.get("opt_type")
            strike = m.get("strike")
            expiry = m.get("expiry")
            entry_price = m.get("entry_price") or a.entry_price or 0.0
            quantity = m.get("quantity", 0)

            unrealized_pnl = (mid - entry_price) * 100 * quantity
            pnl_pct = ((mid - entry_price) / entry_price) if entry_price > 0 else 0.0

            if quantity < 0:
                unrealized_pnl = (entry_price - mid) * 100 * abs(quantity)
                pnl_pct = (
                    ((entry_price - mid) / entry_price) if entry_price > 0 else 0.0
                )

            total_unrealized_pnl += unrealized_pnl

            trades.append(
                {
                    "id": a.id,
                    "symbol": sym,
                    "opt_type": opt_type,
                    "strike": strike,
                    "expiry": expiry,
                    "entry_price": entry_price,
                    "current_price": mid,
                    "quantity": quantity,
                    "unrealized_pnl": unrealized_pnl,
                    "pnl_pct": pnl_pct,
                }
            )

        return {"trades": trades, "total_unrealized_pnl": total_unrealized_pnl}

    async def audit_real_portfolio_risk(self) -> List[Dict[str, Any]]:
        """
        [NRO Refinement] 審計真實持倉風險。
        偵測 DITM Profit Lock (Delta >= 0.85) 與 Gamma Fragility (Net Gamma < -20)。
        """
        all_portfolios = database.get_all_portfolio()
        if not all_portfolios:
            return []

        user_ports: Dict[int, List[Any]] = {}
        for row in all_portfolios:
            uid = row[0]
            user_ports.setdefault(uid, []).append(row[2:])

        results = []
        spy_quote = await market_data_service.get_quote("SPY")
        spy_price = spy_quote.get("c", 670.0) if spy_quote else 670.0
        df_spy = await market_data_service.get_history_df("SPY", "60d")

        for uid, rows in user_ports.items():
            user_ctx = database.get_full_user_context(uid)

            # 1. 檢查 Gamma 脆性 (Fragility Guard)
            if user_ctx.total_gamma < -20.0:
                results.append(
                    {
                        "uid": uid,
                        "type": "GAMMA_FRAGILITY",
                        "net_gamma": round(user_ctx.total_gamma, 2),
                        "threshold": -20.0,
                    }
                )

            # 1.5 檢查保證金水位與 API 連線狀態
            # [NRO Simulated] 實體券商 API (如 IBKR / Schwab) 可在此掛載 Ping 與 Margin Check
            simulated_margin_ratio = min(
                1.0, abs(user_ctx.total_weighted_delta) * 100 / (user_ctx.capital + 1)
            )
            api_disconnected = False
            if simulated_margin_ratio > 0.85 or api_disconnected:
                results.append(
                    {
                        "uid": uid,
                        "type": "MARGIN_API",
                        "ratio": simulated_margin_ratio,
                        "api_status": not api_disconnected,
                    }
                )

            # 2. 檢查各部位 Profit Lock (DITM)
            # row: (symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost, weighted_delta, theta, gamma, trade_category)
            for row in rows:
                sym, opt_t, strike, exp, entry, qty, cost, w_delta, theta, gamma, *_ = (
                    row
                )

                # 僅針對買方期權 (quantity > 0 且非現貨)
                if str(opt_t).lower() == "stock" or exp == "PERPETUAL":
                    continue

                if qty > 0 and w_delta != 0:
                    exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                    dte = (exp_date - datetime.now().date()).days

                    # 獲取標的現價以進行 Greeks 換算
                    quote = await market_data_service.get_quote(sym)
                    curr_price = quote.get("c", 0.0) if quote else 0.0
                    if curr_price <= 0:
                        continue

                    # 換算回局部合約 Delta (Local Delta)
                    # 公式：delta = w_delta / (qty * 100 * beta * (price / spy_price))
                    # 此處簡化處理，利用 w_delta 與 qty 的關係進行臨界點判定
                    # 在 NRO 模型中，若 w_delta / (qty * 100) 接近 beta * (price / spy_price)，則 local delta 趨近於 1

                    from market_analysis.portfolio import calculate_beta

                    df_stock = await market_data_service.get_history_df(sym, "60d")
                    beta = calculate_beta(df_stock, df_spy)

                    # 精確局部 Delta 估算
                    denominator = qty * 100 * beta * (curr_price / spy_price)
                    local_delta = abs(w_delta / denominator) if denominator != 0 else 0

                    # Profit Lock 觸發條件：Delta >= 0.85 且 PnL > 150% 且 DTE <= 21
                    # 獲取即時 Mid 以計算 PnL
                    mid, _ = await portfolio.get_option_chain_mid_iv(
                        sym, exp, strike, opt_t
                    )
                    pnl_pct = ((mid - entry) / entry) if mid > 0 else 0

                    if (local_delta >= 0.85 or pnl_pct > 1.5) and dte <= 21:
                        results.append(
                            {
                                "uid": uid,
                                "type": "PROFIT_LOCK",
                                "symbol": sym,
                                "local_delta": round(local_delta, 3),
                                "pnl_pct": round(pnl_pct * 100, 1),
                                "dte": dte,
                                "reason": f"標的 **{sym}** Delta 已達 `{local_delta:.3f}`，部位進入深價內 (DITM) 區間，凸性 (Convexity) 已消失且 Theta 衰退加劇。",
                            }
                        )

        return results

    async def get_after_market_report_data(self) -> Dict[int, Dict[str, Any]]:
        """
        取得盤後結算報告數據。
        """
        all_portfolios = database.get_all_portfolio()
        if not all_portfolios:
            logger.info("盤後報告略過：無任何持倉資料。")
            return {}

        user_ports: Dict[int, List[Any]] = {}
        for row in all_portfolios:
            uid = row[0]
            user_ports.setdefault(uid, []).append(row[2:])

        results = {}
        for uid, rows in user_ports.items():
            try:
                user_ctx = database.get_full_user_context(uid)
                user_capital = await get_adjusted_user_capital(uid, user_ctx.capital)
            except Exception:
                logger.exception(f"盤後報告略過：讀取使用者資產設定失敗，uid={uid}")
                continue

            try:
                # 1. 執行標準持倉報告邏輯
                report_lines = await portfolio.check_portfolio_status_logic(
                    rows, user_capital
                )
            except Exception:
                logger.exception(f"盤後報告略過：持倉報告計算失敗，uid={uid}")
                continue

            if not report_lines:
                logger.info(f"盤後報告略過：report_lines 為空，uid={uid}")
                continue

            # 🚀 [Pro Investor] 生存天數計算 (Runway Calculation) - 預設執行
            from market_analysis.pro_management import calculate_survival_runway

            survival_runway = calculate_survival_runway(
                cash_reserve=user_ctx.cash_reserve,
                monthly_expense=user_ctx.monthly_expense,
                daily_theta=user_ctx.total_theta,
            )

            try:
                # 2. 執行對沖績效分析
                hedge_analysis = await hedging.analyze_hedge_performance(uid)
            except Exception:
                logger.exception(f"盤後報告警告：對沖績效分析失敗，uid={uid}")
                hedge_analysis = {}

            if not isinstance(hedge_analysis, dict):
                logger.warning(f"盤後報告警告：hedge_analysis 不是 dict，uid={uid}")
                hedge_analysis = {}

            # STHE 自動優化屬於加值資訊，失敗不應中斷報告。
            try:
                await hedging.calculate_daily_effectiveness(uid)
            except Exception:
                logger.exception(
                    f"盤後報告警告：calculate_daily_effectiveness 失敗，uid={uid}"
                )

            try:
                new_tau = await hedging.calculate_dynamic_tau(uid)
                hedge_analysis["dynamic_tau"] = new_tau
            except Exception:
                logger.exception(f"盤後報告警告：calculate_dynamic_tau 失敗，uid={uid}")

            results[uid] = {
                "report_lines": report_lines,
                "hedge_analysis": hedge_analysis,
                "survival_runway": survival_runway,
            }
        return results
