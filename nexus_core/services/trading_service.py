import asyncio
import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Dict, List, Any, Optional, Tuple, Set, TypedDict

import database
import market_math
from config import get_vix_tier
from market_analysis import portfolio, hedging
from market_analysis.gap_analysis import GapAnalyzer
from market_analysis.pro_management import simulate_cc_transition
from market_analysis.ghost_trader import GhostTrader
from market_analysis.risk_engine import optimize_position_risk
from services import market_data_service, news_service, llm_service
from market_analysis.ddp_inspector import DDPInspector
from market_analysis.volatility_inspector import VolatilityInspector
from services.execution_router import ExecutionRouter
from models.execution import MarketCondition

logger = logging.getLogger(__name__)
ny_tz = ZoneInfo("America/New_York")


class EarningsAlert(TypedDict):
    symbol: str
    is_portfolio: bool
    earnings_date: date
    days_left: int


class TradingService:
    """
    提供核心交易業務邏輯，將 Discord 機器人的介面與底層計算/資料處理分離。
    """

    def __init__(self, bot):
        self.bot = bot
        self.vtr_engine = GhostTrader()
        self.ddp_inspector = DDPInspector(bot)
        self.vol_inspector = VolatilityInspector(bot)
        self.execution_router = ExecutionRouter()

    async def run_ddp_scan(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """執行 Davis Double Play (DDP) 掃描"""
        return await self.ddp_inspector.run_scan(symbols)

    async def run_iv_opportunity_scan(
        self, symbols: List[str], user_id: int
    ) -> List[Dict[str, Any]]:
        """執行波動率優勢掃描 (IV Opportunity)"""
        return await self.vol_inspector.run_scan(symbols, user_id)

    async def get_execution_decision(
        self, symbol: str, stock_cost: float = 0.0
    ) -> Optional[Any]:
        """
        獲取標的的執行決策 (SHIELD/SPEAR/STANDBY)。
        整合市場數據並調用 ExecutionRouter。
        """
        try:
            # 1. 獲取核心市場指標
            macro = await market_data_service.get_macro_environment()
            df_hist_1d = await market_data_service.get_history_df(
                symbol, period="60d", interval="1d"
            )

            if df_hist_1d.empty:
                return None

            # 計算 MA20 與 ATR
            import pandas_ta as ta

            df_hist_1d["SMA20"] = ta.sma(df_hist_1d["Close"], length=20)
            df_hist_1d["ATR14"] = ta.atr(
                df_hist_1d["High"], df_hist_1d["Low"], df_hist_1d["Close"], length=14
            )
            df_hist_1d["RSI14"] = ta.rsi(df_hist_1d["Close"], length=14)

            last_row = df_hist_1d.iloc[-1]
            price = last_row["Close"]
            ma20 = last_row["SMA20"]
            atr = last_row["ATR14"]
            rsi = last_row["RSI14"]

            # 獲取 Skew 與 UOA (這裡簡化，實戰中可從 SentimentEngine 獲取)
            from market_analysis.sentiment_engine import SentimentEngine

            skew_res = await SentimentEngine.calculate_skew(symbol)
            skew_val = skew_res.get("skew", 0.0) / 100.0  # 轉為小數

            # 偵測 UOA
            from market_data_service import get_option_uoa

            uoa_list = await get_option_uoa(symbol)
            uoa_detected = len(uoa_list) > 0

            # 2. 構建 MarketCondition
            condition = MarketCondition(
                vix=macro.get("vix", 18.0),
                skew_percent=skew_val,
                asset_price=price,
                ma20=ma20,
                atr_14=atr,
                rsi_14=rsi,
                uoa_detected=uoa_detected,
            )

            # 3. 調用 Router
            return self.execution_router.evaluate_market(condition)
        except Exception as e:
            logger.error(f"獲獲取執行決策失敗 for {symbol}: {e}")
            return None

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

        for a in assets:
            m = a.metadata
            sym = a.symbol
            opt_type = m.get("opt_type")
            strike = m.get("strike")
            expiry = m.get("expiry")
            entry_price = m.get("entry_price") or a.entry_price or 0.0
            quantity = m.get("quantity", 0)

            mid, _ = await asyncio.to_thread(
                get_option_chain_mid_iv, sym, expiry, strike, opt_type
            )

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

    async def get_pre_market_alerts_data(
        self, warning_days: int
    ) -> Dict[int, Dict[str, Any]]:
        """
        取得盤前財報警報數據。
        """
        from services.calendar_service import calendar_service

        today = datetime.now(ny_tz).date()
        all_portfolios = database.get_all_portfolio()
        all_watchlists = database.get_all_watchlist()

        user_symbols: Dict[int, Dict[str, Set[str]]] = {}
        unique_symbols = set()

        for row in all_portfolios:
            uid, sym = row[0], row[2]
            user_symbols.setdefault(uid, {"port": set(), "watch": set()})["port"].add(
                sym
            )
            unique_symbols.add(sym)

        for row in all_watchlists:
            uid, sym = row[0], row[1]
            user_symbols.setdefault(uid, {"port": set(), "watch": set()})["watch"].add(
                sym
            )
            unique_symbols.add(sym)

        earnings_infos = await calendar_service.get_symbol_earnings_batch(
            list(unique_symbols)
        )
        earnings_cache: Dict[str, date] = {}
        for sym, earnings_info in earnings_infos.items():
            if earnings_info is None:
                continue
            e_date = datetime.strptime(earnings_info.date, "%Y-%m-%d").date()
            earnings_cache[sym] = e_date

        results = {}
        for uid, symbols_data in user_symbols.items():
            alerts: List[EarningsAlert] = []
            combined_symbols = symbols_data["port"].union(symbols_data["watch"])

            for sym in combined_symbols:
                cached_earnings_date: date | None = earnings_cache.get(sym)
                if cached_earnings_date:
                    days_left = (cached_earnings_date - today).days
                    if 0 <= days_left <= warning_days:
                        item: EarningsAlert = {
                            "symbol": sym,
                            "is_portfolio": sym in symbols_data["port"],
                            "earnings_date": cached_earnings_date,
                            "days_left": days_left,
                        }
                        alerts.append(item)

            # 🚀 根據距離財報天數升冪排序 (0天優先)
            alerts.sort(key=lambda x: x["days_left"])

            results[uid] = {
                "alerts": alerts,
                "scanned_symbols": sorted(combined_symbols),
            }
        return results

    def _validate_trade_pipeline(
        self, user_context: Any, data: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """
        4-Stage Validation Pipeline: Macro -> Alpha -> Risk -> Financials.
        """
        strategy = data.get("strategy", "")

        # --- Stage 1: Macro (VIX Battle Ladder & Regime) ---
        vix_allow = data.get("vix_allow_signal", True)
        if "STO" in strategy and not vix_allow:
            return (
                False,
                f"MACRO_REJECT: VIX {data.get('vix_spot'):.1f} tier '{data.get('vix_tier_name')}' restricts STO entry.",
            )

        # --- Stage 2: Alpha (AROC & Signal Strength) ---
        aroc = data.get("aroc", 0.0)
        if "STO" in strategy and aroc < 15.0:
            return False, f"STO 訊號遭攔截：低於 15% AROC 閾值 (目前: {aroc:.1f}%)"
        if "BTO" in strategy and aroc < 30.0:
            return False, f"ALPHA_REJECT: BTO AROC {aroc:.1f}% < 30.0% 閾值。"

        # --- Stage 3: Risk (NRO & Kelly Sizing) ---
        if data.get("safe_qty", 0) <= 0:
            return (
                False,
                "RISK_REJECT: NRO optimization determined zero safe quantity (Risk budget exceeded).",
            )

        # --- Stage 4: Financials (Runway & Survival) ---
        # If runway < 180 days, reject any non-hedging trades that increase margin
        # (Actual implementation will use the runway helper in Phase 4)
        pass

        return True, "APPROVED"

    async def run_market_scan(
        self, is_auto: bool = True, triggered_by_id: Optional[int] = None
    ) -> Dict[int, List[Dict[str, Any]]]:
        """
        執行全站市場掃描 (整合 EMA, Macro Stress Matrix 與 VIX/Oil 監控)
        """
        all_watchlists = database.get_all_watchlist()
        if not all_watchlists:
            return {}

        # 🚀 獲取所有用戶的現貨持倉，用於動態帶入成本
        from database.holdings import get_all_holdings

        all_holdings = await asyncio.to_thread(get_all_holdings)
        holding_map = {(h["user_id"], h["symbol"]): h["avg_cost"] for h in all_holdings}

        from market_analysis.risk_engine import MacroContext

        # 1. 🚀 獲取全域基準資料
        try:
            spy_task = market_data_service.get_spy_history_df("1y")
            macro_task = market_data_service.get_macro_environment()
            df_spy, macro_raw = await asyncio.gather(spy_task, macro_task)
            spy_price = df_spy["Close"].iloc[-1] if not df_spy.empty else 670.0
            vix_spot = macro_raw.get("vix", 18.0)
            macro_data = MacroContext(
                vix=vix_spot,
                oil_price=macro_raw.get("oil", 75.0),
                vix_change=macro_raw.get("vix_change", 0.0),
            )
        except Exception:
            df_spy, spy_price = None, 670.0
            vix_spot, macro_data = (
                18.0,
                MacroContext(vix=18.0, oil_price=85.0, vix_change=0.0),
            )

        vix_tier = get_vix_tier(vix_spot)

        # 2. 提取不重複標的進行「併行批次掃描」
        # 標的聚合鍵：(代號, 成本, 是否用LLM)
        scan_targets = []
        for uid, sym, use_llm in all_watchlists:
            cost = holding_map.get((uid, sym), 0.0)
            scan_targets.append((sym, cost, use_llm))

        unique_targets = list(set(scan_targets))

        async def _scan_single_target(target):
            sym, stock_cost, use_llm = target
            # ... (rest of scan logic)
            try:
                # analyze_symbol 已經是 async，若沒有 Option 訊號，res 會是 None
                res = await market_math.analyze_symbol(
                    sym, stock_cost, df_spy, spy_price, vix_spot=vix_spot
                )
                is_option_valid = bool(res)
                if not res:
                    res = {"symbol": sym, "stock_cost": stock_cost, "strategy": ""}

                res["is_option_valid"] = is_option_valid

                # 🚀 新增 Gap & Fill 跳空分析 (僅在開盤初期 2 小時內執行更精確，但這裡常態掃描)
                try:
                    df_gap = await market_data_service.get_history_df(
                        sym, period="5d", interval="1d"
                    )
                    if not df_gap.empty and len(df_gap) >= 2:
                        gap_status = GapAnalyzer.analyze_gap(df_gap)
                        if gap_status:
                            res["gap_status"] = gap_status
                except Exception as gap_e:
                    logger.warning(f"Gap 分析失敗 for {sym}: {gap_e}")

                # 🚀 新增 EMA 訊號偵測 (Crossover & Test)
                # 為確保 EMA 準確性，獲取至少 60 天歷史數據 (1-Hour 時框作為小週期觸發)
                df_hist_1h = await market_data_service.get_history_df(
                    sym, period="60d", interval="1h"
                )
                if not df_hist_1h.empty:
                    ema_8_sig = market_math.detect_ema_signals(df_hist_1h, window=8)
                    ema_21_sig = market_math.detect_ema_signals(df_hist_1h, window=21)

                    # 整合訊號至結果字典
                    res["ema_signals"] = [sig for sig in [ema_8_sig, ema_21_sig] if sig]

                    # 如果有 EMA 訊號，強制標註為「高價值追蹤」
                    if res["ema_signals"]:
                        res["is_priority_alert"] = True

                # 🚀 新增 PowerSqueeze 掃描 (使用日 K)
                df_hist_1d = await market_data_service.get_history_df(
                    sym, period="1y", interval="1d"
                )
                from market_analysis.psq_engine import analyze_psq

                psq_result = analyze_psq(df_hist_1d, vix_spot=vix_spot)
                if psq_result:
                    res["psq_result"] = psq_result
                    # Ensure price is available for PSQ reports

                # 🚀 整合核心：Execution Router 執行決策 (SDDM)
                try:
                    import pandas_ta as ta

                    # 使用 df_hist_1d (日 K) 計算指標
                    if not df_hist_1d.empty:
                        df_hist_1d["SMA20"] = ta.sma(df_hist_1d["Close"], length=20)
                        df_hist_1d["ATR14"] = ta.atr(
                            df_hist_1d["High"],
                            df_hist_1d["Low"],
                            df_hist_1d["Close"],
                            length=14,
                        )
                        df_hist_1d["RSI14"] = ta.rsi(df_hist_1d["Close"], length=14)

                        last_row = df_hist_1d.iloc[-1]

                        from market_analysis.sentiment_engine import SentimentEngine

                        skew_res = await SentimentEngine.calculate_skew(sym)
                        skew_val = skew_res.get("skew", 0.0) / 100.0

                        uoa_detected = bool(
                            res.get("uoa_list")
                        )  # 這裡假設 analyze_symbol 已處理 uoa_list

                        condition = MarketCondition(
                            vix=vix_spot,
                            skew_percent=skew_val,
                            asset_price=last_row["Close"],
                            ma20=last_row["SMA20"],
                            atr_14=last_row["ATR14"],
                            rsi_14=last_row["RSI14"],
                            uoa_detected=uoa_detected,
                        )
                        res["execution_decision"] = (
                            self.execution_router.evaluate_market(condition)
                        )
                except Exception as ex_router_e:
                    logger.warning(f"ExecutionRouter 評估失敗 for {sym}: {ex_router_e}")
                    if not res.get("price") or res.get("price") <= 0:
                        res["price"] = (
                            df_hist_1d["Close"].iloc[-1]
                            if not df_hist_1d.empty
                            else 0.0
                        )

                has_psq_signal = False
                if psq_result and (
                    getattr(psq_result, "is_breakout_long", False)
                    or psq_result.is_near_support
                ):
                    has_psq_signal = True

                # 語意風控判定: 只在有任何訊號觸發時執行以節省成本
                if is_option_valid or has_psq_signal:
                    # 併行獲取新聞 (Finnhub) 與 Reddit (從 KV 快取讀取)
                    news_task = news_service.fetch_recent_news(sym)

                    from database.cache import get_kv_cache

                    reddit_text = (
                        get_kv_cache(f"reddit_sentiment_{sym}")
                        or "暫無快取情緒資料 (等待每日更新)。"
                    )

                    news_text = await news_task

                    if use_llm:
                        strategy_text = res.get("strategy", "PowerSqueeze Trigger")
                        ai_verdict = await llm_service.evaluate_trade_risk(
                            sym, strategy_text, news_text, reddit_text
                        )
                        res["ai_decision"] = ai_verdict.get("decision", "APPROVE")
                        res["ai_reasoning"] = ai_verdict.get("reasoning", "無資料")
                    else:
                        res["ai_decision"] = "SKIP"
                        res["ai_reasoning"] = "未啟用 LLM 語意風控"

                    res.update({"news_text": news_text, "reddit_text": reddit_text})

                return target, res
            except Exception as e:
                logger.error(f"掃描標的 {sym} 失敗: {e}")
                return target, None

        # 🚀 併行執行所有標的分量 (分批執行以防止觸發 API Rate Limit)
        results_list = []
        batch_size = 10
        for i in range(0, len(unique_targets), batch_size):
            chunk = unique_targets[i : i + batch_size]
            tasks = [_scan_single_target(t) for t in chunk]
            batch_results = await asyncio.gather(*tasks)
            results_list.extend(batch_results)
            if i + batch_size < len(unique_targets):
                await asyncio.sleep(0.5)  # 給 API 一點緩衝，並釋放池空間

        scan_results = {target: res for target, res in results_list if res is not None}

        if not scan_results:
            return {}

        # 3. 準備使用者分發與「個人化 NRO 優化」
        user_alerts_results = {}
        user_watchlists: Dict[int, List[Tuple[str, float, bool]]] = {}
        for uid, sym, stock_cost, use_llm in all_watchlists:
            user_watchlists.setdefault(uid, []).append((sym, stock_cost, use_llm))

        for uid, watchlist_items in user_watchlists.items():
            valid_user_alerts = []

            # 獲取該使用者的動態風險參數與目前持倉統計
            # 🚀 [Resource Isolation] 確保 Greeks 數據最新，避免使用舊 Delta 判斷避險
            await portfolio.refresh_portfolio_greeks(uid)

            user_context = database.get_full_user_context(uid)
            user_capital = user_context.capital
            current_total_delta = user_context.total_weighted_delta

            for sym, stock_cost, use_llm in watchlist_items:
                if (sym, stock_cost, use_llm) in scan_results:
                    base_data = scan_results[(sym, stock_cost, use_llm)].copy()
                    base_data["uid"] = uid
                    base_data["spy_price"] = spy_price
                    base_data["macro_vix"] = macro_data.vix
                    base_data["macro_vix_change"] = macro_data.vix_change
                    base_data["macro_oil"] = macro_data.oil_price
                    # VIX 戰情階梯狀態注入 (供 UI 層渲染)
                    base_data["vix_spot"] = vix_spot
                    base_data["vix_battle_status"] = {
                        "name": vix_tier.get("name", "N/A"),
                        "emoji": vix_tier.get("emoji", ""),
                        "color_hex": vix_tier.get("color_hex", 0x808080),
                        "vix_spot": vix_spot,
                        "sto_delta_cap": vix_tier.get("sto_delta_cap", 0.0),
                        "sizing_multiplier": vix_tier.get("sizing_multiplier", 1.0),
                    }

                    is_option_valid = base_data.get("is_option_valid", False)
                    psq_result = base_data.get("psq_result")
                    has_psq_signal = psq_result and (
                        getattr(psq_result, "is_breakout_long", False)
                        or psq_result.is_near_support
                    )

                    if not is_option_valid and not has_psq_signal:
                        continue  # 此標的沒有任何觸發訊號

                    # === 1. 選擇權策略分支 ===
                    if user_context.option_alert_mode != 0 and is_option_valid:
                        opt_data = base_data.copy()
                        opt_data["alert_type"] = "OPTION"

                        # 🚀 整合核心：期權情緒掃描
                        from market_analysis.sentiment_engine import SentimentEngine

                        skew_data = await SentimentEngine.calculate_skew(sym)
                        pcr_data = await SentimentEngine.calculate_pcr(sym)
                        pcr_val = pcr_data.get("pcr", 0.8)
                        skew_val = skew_data.get("skew", 0.0)

                        # 🚀 整合核心：注入宏觀背景與日曆事件進行風險優化
                        from services.calendar_service import calendar_service

                        earnings_info = await calendar_service.get_symbol_earnings(sym)
                        tte_hours = earnings_info.tte_hours if earnings_info else None

                        strategy = opt_data.get("strategy", "")
                        opt_res = optimize_position_risk(
                            current_delta=current_total_delta,
                            unit_weighted_delta=opt_data.get("weighted_delta", 0.0),
                            user_capital=user_capital,
                            spy_price=spy_price,
                            stock_iv=opt_data.get("iv", 0.15),
                            strategy=strategy,
                            macro_data=macro_data,
                            risk_limit=user_context.risk_limit,
                            vix_spot=vix_spot,
                            pcr=pcr_val,
                            skew=skew_val,
                            event_tte_hours=tte_hours,
                        )
                        safe_qty = opt_res.suggested_contracts
                        hedge_spy = opt_res.suggested_hedge_spy

                        if opt_res.warnings:
                            opt_data["nro_warnings"] = opt_res.warnings

                        # 模擬成交後的衝擊
                        side_multiplier = -1 if "STO" in strategy else 1
                        new_trade_impact = (
                            opt_data.get("weighted_delta", 0.0)
                            * side_multiplier
                            * safe_qty
                        )
                        projected_total_delta = current_total_delta + new_trade_impact
                        projected_exposure_pct = (
                            (projected_total_delta * spy_price / user_capital) * 100
                            if user_capital > 0
                            else 0.0
                        )

                        opt_data.update(
                            {
                                "safe_qty": safe_qty,
                                "hedge_spy": hedge_spy,
                                "projected_exposure_pct": round(
                                    projected_exposure_pct, 2
                                ),
                                "pcr": pcr_val,
                                "skew": skew_val,
                                "risk_limit": user_context.risk_limit,
                            }
                        )

                        # 🚀 執行集中化決策管線 (Stage 1-4)
                        is_approved, reason = self._validate_trade_pipeline(
                            user_context, opt_data
                        )
                        if not is_approved:
                            logger.info(
                                f"🚫 [Pipeline Reject] {sym} {strategy}: {reason}"
                            )
                            continue

                        # 🚀 對沖解除建議 (Hedge Unlocking)
                        ema_signals = opt_data.get("ema_signals", [])
                        for sig in ema_signals:
                            if (
                                sig.get("type") == "CROSSOVER"
                                and sig.get("direction") == "BULLISH"
                            ):
                                from services.alert_filter import validate_mtf_trend

                                mtf = await validate_mtf_trend(sym, sig)
                                unlock_advice = hedging.suggest_hedge_unlock(
                                    user_context, opt_data, mtf
                                )
                                if unlock_advice:
                                    opt_data["hedge_unlock"] = unlock_advice
                                break

                        # 🚀 自動回補避險 (Auto Re-Hedging)
                        now_ts = int(time.time())
                        if now_ts - user_context.last_rehedge_alert_time > 3600:
                            rehedge_advice = hedging.evaluate_rehedge_necessity(
                                user_context, opt_data
                            )
                            if rehedge_advice:
                                rehedge_advice = hedging.get_tuned_risk_advice(
                                    uid, rehedge_advice
                                )
                                opt_data["rehedge_info"] = rehedge_advice
                                database.upsert_user_config(
                                    uid, last_rehedge_alert_time=now_ts
                                )
                                user_context.last_rehedge_alert_time = now_ts

                        valid_user_alerts.append(opt_data)

                    # === 2. PSQ 戰情分支 ===
                    if user_context.enable_psq_watchlist and has_psq_signal:
                        psq_data = base_data.copy()
                        psq_data["alert_type"] = "PSQ"
                        valid_user_alerts.append(psq_data)

            if valid_user_alerts:
                user_alerts_results[uid] = valid_user_alerts

        return user_alerts_results

    async def execute_vtr_auto_entry(self, data: Dict[str, Any]):
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

            # 2. 檢查各部位 Profit Lock (DITM)
            # row: (symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost, weighted_delta, theta, gamma, trade_category)
            for row in rows:
                sym, opt_t, strike, exp, entry, qty, cost, w_delta, theta, gamma, *_ = (
                    row
                )

                # 僅針對買方 (quantity > 0)
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
                user_capital = user_ctx.capital
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
