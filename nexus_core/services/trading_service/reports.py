"""持倉損益、風險審計、盤後結算報告 Mixin。"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import database
import market_time
from market_analysis import portfolio, hedging
from services import market_data_service
from market_analysis.risk_engine import BETA_HISTORY_PERIOD

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
        from market_analysis.portfolio import get_option_chain_quote

        manager = AssetManager()
        assets = manager.get_assets(user_id, ContextType.TRADE)

        trades = []
        total_unrealized_pnl = 0.0
        # 期權部位按市價 (mid×100×帶號口數) 的合計；空頭為負 (負債)。報價缺失的
        # 部位不計入，另列於 missing_quote_count。
        total_option_market_value = 0.0
        missing_quote_count = 0

        # 併發批次拉取各部位期權鏈中間價 (Semaphore(3) 上限，避免逐筆序列 await 拖慢回應)
        sem = asyncio.Semaphore(3)

        async def _fetch_quote(asset: Any) -> Dict[str, Any]:
            am = asset.metadata
            async with sem:
                return await get_option_chain_quote(
                    asset.symbol, am.get("expiry"), am.get("strike"), am.get("opt_type")
                )

        quotes = await asyncio.gather(*[_fetch_quote(a) for a in assets])

        for a, q in zip(assets, quotes):
            m = a.metadata
            sym = a.symbol
            opt_type = m.get("opt_type")
            strike = m.get("strike")
            expiry = m.get("expiry")
            entry_price = m.get("entry_price") or a.entry_price or 0.0
            quantity = m.get("quantity", 0)
            mid = float(q.get("mid") or 0.0)
            source = str(q.get("source") or "MISSING")

            # 報價缺失 (mid<=0)：過去直接以 0 計算，買方顯示 -100%、賣方 +100%，
            # 並污染總損益與 NAV。現在標記缺失並排除於加總之外。
            quote_missing = mid <= 0
            unrealized_pnl: Optional[float]
            pnl_pct: Optional[float]
            if quote_missing:
                unrealized_pnl = None
                pnl_pct = None
                missing_quote_count += 1
            elif quantity < 0:
                unrealized_pnl = (entry_price - mid) * 100 * abs(quantity)
                pnl_pct = (
                    ((entry_price - mid) / entry_price) if entry_price > 0 else 0.0
                )
            else:
                unrealized_pnl = (mid - entry_price) * 100 * quantity
                pnl_pct = (
                    ((mid - entry_price) / entry_price) if entry_price > 0 else 0.0
                )

            if unrealized_pnl is not None:
                total_unrealized_pnl += unrealized_pnl
                total_option_market_value += mid * 100 * float(quantity)

            trades.append(
                {
                    "id": a.id,
                    "symbol": sym,
                    "opt_type": opt_type,
                    "strike": strike,
                    "expiry": expiry,
                    "entry_price": entry_price,
                    "current_price": None if quote_missing else mid,
                    "quote_source": source,
                    "quantity": quantity,
                    "unrealized_pnl": unrealized_pnl,
                    "pnl_pct": pnl_pct,
                }
            )

        return {
            "trades": trades,
            "total_unrealized_pnl": total_unrealized_pnl,
            "total_option_market_value": total_option_market_value,
            "missing_quote_count": missing_quote_count,
        }

    async def get_market_nav(
        self, user_id: int, cash_reserve: float, pnl_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """按市價計算帳戶淨值 (NAV)。

        NAV = 現金儲備 + Σ 現貨數量(帶號) × 即時價 + Σ 期權 mid × 100 × 口數(帶號)

        - 現貨按市價計值 (過去 /dash 用 avg_cost 成本，漲跌完全不反映)。
        - 賣方權利金：假設 cash_reserve 已包含收到的權利金與放空所得，期權
          空頭以「回補成本」(mid×100×|口數|) 列為負債。過去 NAV = 成本資本
          (含 entry×100×|口數|) + 未實現損益 ((entry−mid)×100×|口數|)，等於把
          賣方權利金計入兩次 (2×entry − mid)。
        - 報價缺失的部位不計入，回傳 missing_* 供畫面標示。
        """
        from services.asset_manager import AssetManager
        from models.asset import ContextType

        manager = AssetManager()
        holdings = await asyncio.to_thread(
            manager.get_assets, user_id, ContextType.HOLDING
        )
        sem = asyncio.Semaphore(3)

        async def _price(sym: str) -> float:
            async with sem:
                try:
                    q = await market_data_service.get_quote(sym)
                    return float(q.get("c") or 0.0) if q else 0.0
                except Exception:
                    return 0.0

        symbols = sorted({str(h.symbol).upper() for h in holdings})
        prices = dict(zip(symbols, await asyncio.gather(*[_price(s) for s in symbols])))

        spot_value = 0.0
        missing_spot: List[str] = []
        for h in holdings:
            qty = float((h.metadata or {}).get("quantity", 0.0) or 0.0)
            if qty == 0:
                continue
            px = prices.get(str(h.symbol).upper(), 0.0)
            if px <= 0:
                missing_spot.append(str(h.symbol).upper())
                continue
            spot_value += qty * px

        option_value = float(pnl_data.get("total_option_market_value", 0.0) or 0.0)
        missing_options = int(pnl_data.get("missing_quote_count", 0) or 0)
        nav = float(cash_reserve) + spot_value + option_value
        return {
            "nav": nav,
            "cash_reserve": float(cash_reserve),
            "spot_market_value": spot_value,
            "option_market_value": option_value,
            "missing_spot_symbols": missing_spot,
            "missing_option_quotes": missing_options,
            "is_complete": not missing_spot and missing_options == 0,
        }

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
        spy_raw = spy_quote.get("c") if spy_quote else None
        # SPY 未知時為 None (不再以 670 冒充)：局部 Delta 無法由 weighted_delta
        # 換算，下方 DITM 判定只依損益與 DTE。
        spy_price: Optional[float] = (
            float(spy_raw) if spy_raw is not None and float(spy_raw) > 0 else None
        )
        # Beta 需 >= 90 個交易日日線；"60d" 只有約 41 根，Beta 永遠退回 1.0。
        df_spy = await market_data_service.get_history_df("SPY", BETA_HISTORY_PERIOD)

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
                    dte = market_time.days_to_expiry_et(exp_date)

                    # 獲取標的現價以進行 Greeks 換算
                    quote = await market_data_service.get_quote(sym)
                    curr_price = quote.get("c", 0.0) if quote else 0.0
                    if curr_price <= 0:
                        continue

                    # 換算回局部合約 Delta (Local Delta)
                    # 公式：delta = w_delta / (qty * 100 * beta * (price / spy_price))
                    # 此處簡化處理，利用 w_delta 與 qty 的關係進行臨界點判定
                    # 在 NRO 模型中，若 w_delta / (qty * 100) 接近 beta * (price / spy_price)，則 local delta 趨近於 1

                    from market_analysis.portfolio import calculate_beta_strict

                    df_stock = await market_data_service.get_history_df(
                        sym, BETA_HISTORY_PERIOD
                    )
                    beta = calculate_beta_strict(df_stock, df_spy)

                    # 精確局部 Delta 估算：Beta 或 SPY 未知時無法由 weighted_delta
                    # 還原 (過去 Beta 被靜默設為 1.0、SPY 補 670)，視為未知。
                    local_delta: Optional[float] = None
                    if beta is not None and spy_price is not None:
                        denominator = qty * 100 * beta * (curr_price / spy_price)
                        if denominator != 0:
                            local_delta = abs(w_delta / denominator)

                    # Profit Lock 觸發條件：Delta >= 0.85 且 PnL > 150% 且 DTE <= 21
                    # 獲取即時 Mid 以計算 PnL；報價缺失時 PnL 未知 (不當作 0)。
                    mid, _, _bid, _ask = await portfolio.get_option_chain_mid_iv(
                        sym, exp, strike, opt_t
                    )
                    pnl_pct: Optional[float] = (
                        ((mid - entry) / entry) if mid > 0 and entry > 0 else None
                    )

                    is_ditm = local_delta is not None and local_delta >= 0.85
                    is_big_gain = pnl_pct is not None and pnl_pct > 1.5
                    if (is_ditm or is_big_gain) and dte <= 21:
                        results.append(
                            {
                                "uid": uid,
                                "type": "PROFIT_LOCK",
                                "symbol": sym,
                                "local_delta": round(local_delta, 3)
                                if local_delta is not None
                                else None,
                                "pnl_pct": round(pnl_pct * 100, 1)
                                if pnl_pct is not None
                                else None,
                                "dte": dte,
                                "reason": (
                                    f"標的 **{sym}** Delta 已達 `{local_delta:.3f}`，部位進入深價內 (DITM) 區間，凸性 (Convexity) 已消失且 Theta 衰退加劇。"
                                    if local_delta is not None
                                    else f"標的 **{sym}** 未實現獲利已達 `{(pnl_pct or 0.0) * 100:.1f}%` (Delta 因 Beta/SPY 資料不足無法換算)，建議評估鎖定獲利。"
                                ),
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

            # 提領跑道快照（16:15 ET 寫入；無快照時顯示端會標示「尚無資料」）
            from services.withdrawal_runway_service import get_runway_display

            runway, runway_stale = await get_runway_display(uid)

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
                "runway": runway,
                "runway_stale": runway_stale,
            }
        return results
