import discord
from typing import Optional
from discord.ext import commands, tasks
from datetime import timezone, datetime, timedelta
import logging
import asyncio
import math
import yfinance as yf

import database
from market_time import (
    get_next_market_target_time,
    get_sleep_seconds,
    is_market_open,
)
from database.watchlist import get_all_watchlist
from services.market_data_service import (
    get_history_df,
    get_quote,
    get_macro_environment,
    get_vix_term_structure,
)
from services.llm_service import generate_analyst_report
from services.news_service import fetch_recent_news
from services.reddit_service import get_reddit_context
from market_analysis.psq_engine import analyze_psq
from market_analysis.hedging import analyze_hedge_performance
from market_analysis.sentiment_engine import SentimentEngine
from market_analysis.intraday_pipeline import evaluate_watchlist_symbol
from services import market_data_service
from config import get_vix_tier
from cogs.embed_builder import (
    create_ai_analysis_embed,
    create_earnings_report_embed,
    create_intraday_execution_guide_embed,
    create_sector_flow_report_embed,
    split_embed_by_fields,
)
import httpx

logger = logging.getLogger(__name__)

SECTORS = {
    "XLK": "Technology",
    "XLV": "Healthcare",
    "XLF": "Financials",
    "XLY": "Consumer Discretionary",
    "XLC": "Communication Services",
    "XLI": "Industrials",
    "XLP": "Consumer Staples",
    "XLE": "Energy",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLRE": "Real Estate",
}


class AnalystAgent(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 啟動動態排程任務
        self.pre_market_loop.start()
        # self.intra_day_loop.start()  # 已整合至 trading.py 的 intraday_decision_scan
        self.post_market_loop.start()

    def cog_unload(self):
        self.pre_market_loop.cancel()
        # self.intra_day_loop.cancel()
        self.post_market_loop.cancel()

    # ==========================================
    # 🚀 1. 盤前總覽：開盤前 30 分鐘啟動
    # ==========================================
    @tasks.loop(count=1)
    async def pre_market_loop(self):
        await self.bot.wait_until_ready()
        while True:
            target = get_next_market_target_time("open", offset_minutes=-30)
            sleep_secs = get_sleep_seconds(target)

            logger.info(
                f"🤖 [Analyst Pre-Market] 下次執行時間: {target} (倒數 {sleep_secs/3600:.2f} 小時)"
            )
            await asyncio.sleep(sleep_secs)

            try:
                if not getattr(self.bot, "_is_leader_instance", True):
                    await asyncio.sleep(30)
                    continue

                logger.info("🤖 [Analyst Pre-Market] 啟動盤前綜合宏觀與自選股報告...")
                await self.dispatch_pre_market_briefing()
            except Exception as e:
                logger.error(f"Analyst Pre-Market loop error: {e}")

            await asyncio.sleep(60)  # 避免在同一秒重複觸發

    # ==========================================
    # 🚀 2. 盤中監測：每 30 分鐘心跳掃描 (僅開盤時)
    # ==========================================
    @tasks.loop(count=1)
    async def intra_day_loop(self):
        await self.bot.wait_until_ready()
        while True:
            if is_market_open():
                if not getattr(self.bot, "_is_leader_instance", True):
                    await asyncio.sleep(30)
                    continue

                logger.info(
                    "🤖 [Analyst Intra-Day] 偵測到開盤，執行 30 分鐘心跳掃描 (Active Execution Guide)..."
                )
                try:
                    await self.dispatch_intraday_guide()
                except Exception as e:
                    logger.error(f"Analyst Intra-Day loop error: {e}")

                # Reduce frequency to every 120 minutes (2 hours)
                await asyncio.sleep(120 * 60)
            else:
                target = get_next_market_target_time("open", offset_minutes=0)
                sleep_secs = get_sleep_seconds(target)
                logger.info(
                    f"🤖 [Analyst Intra-Day] 市場休市。下次開盤心跳: {target} (倒數 {sleep_secs/3600:.2f} 小時)"
                )
                await asyncio.sleep(min(sleep_secs, 3600))  # 最多睡一小時再檢查一次

    async def dispatch_report(
        self, report_content: discord.Embed, notification_key: str = None
    ):
        """
        將報告發送給所有啟用了 Analyst Agent 的用戶。
        """
        import database

        report_embeds = split_embed_by_fields(report_content)

        user_ids = database.get_all_user_ids()
        dispatched_count = 0
        for uid in user_ids:
            if notification_key and not database.is_notification_enabled(
                uid, notification_key
            ):
                logger.info(
                    f"使用者 {uid} 已關閉 {notification_key} 訂閱，略過本次推送。"
                )
                continue
            try:
                for embed in report_embeds:
                    await self.bot.queue_dm(uid, embed=embed)
                    dispatched_count += 1
            except Exception as e:
                logger.error(f"Failed to dispatch report to {uid}: {e}")
        logger.info(f"Dispatched report to {dispatched_count} users.")

    async def dispatch_intraday_guide(self):
        """
        Active, risk-aware execution guide.
        Replaces the old passive intra-day reports.
        """
        import psutil
        from market_analysis.portfolio import refresh_portfolio_greeks
        from services.asset_manager import AssetManager
        from models.asset import ContextType, TradeMetadata, HoldingMetadata
        from market_analysis.risk_engine import calculate_vega_adjusted_delta
        from market_analysis.pro_management import calculate_financial_runway
        from market_analysis.sentiment_engine import SentimentEngine

        # 0. 系統健康檢查 (Memory Safety Gate)
        mem = psutil.virtual_memory()
        if mem.percent > 85.0:
            logger.warning(
                "🚨 [Memory Gate] RAM usage > 85%, deferring heavy intraday analysis."
            )
            # Still send a basic health alert instead of skipping entirely
            is_memory_gated = True
        else:
            is_memory_gated = False

        # 1. Macro Data & VIX Ladder
        macro_data = await self._fetch_macro_data()
        vix = macro_data.get("vix", 18.0)
        vix_tier = get_vix_tier(vix)
        vix_level_name = vix_tier.get("name", "Unknown")

        # Determine Phase
        from market_time import ny_tz

        now_ny = datetime.now(ny_tz)
        hour = now_ny.hour
        if hour < 11:
            phase = "A"
            phase_name = "流動性與開盤波動 (Phase A)"
        elif 11 <= hour < 14:
            phase = "B"
            phase_name = "深度研究與板塊輪動 (Phase B)"
        else:
            phase = "C"
            phase_name = "投資組合對沖與收尾 (Phase C)"

        # Get Polymarket Whale Intent (Spy/Market general)
        poly_intent = "無顯著巨鯨活動"
        if not is_memory_gated and hasattr(self.bot, "polymarket_service"):
            try:
                markets = self.bot.polymarket_service.get_active_markets(limit=3)
                if markets:
                    poly_intent = f"焦點: {markets[0].get('question', '')[:30]}..."
            except Exception:
                pass

        # Get General SPY Skew
        skew_data = {"skew": 0.0, "state": "N/A"}
        if not is_memory_gated:
            try:
                skew_data = await SentimentEngine.calculate_skew("SPY")
            except Exception:
                pass

        user_ids = database.get_all_user_ids()
        dispatched_count = 0

        for uid in user_ids:
            if not database.is_notification_enabled(uid, "intraday_decision_scan"):
                continue
            ctx = database.get_full_user_context(uid)

            if is_memory_gated:
                embed = create_intraday_execution_guide_embed(
                    phase_name=phase_name,
                    vix=vix,
                    memory_percent=mem.percent,
                    is_memory_gated=True,
                )
                await self.bot.queue_dm(uid, embed=embed)
                dispatched_count += 1
                continue

            # 2. Portfolio Greeks & Vanna
            await refresh_portfolio_greeks(uid)
            manager = AssetManager()
            trade_assets = manager.get_assets(uid, ContextType.TRADE)
            holding_assets = manager.get_assets(uid, ContextType.HOLDING)

            total_delta = 0.0
            total_vanna = 0.0
            for a in trade_assets:
                t_meta = TradeMetadata(**a.metadata)
                total_delta += t_meta.weighted_delta
                total_vanna += t_meta.vanna
            for a in holding_assets:
                h_meta = HoldingMetadata(**a.metadata)
                total_delta += h_meta.weighted_delta

            # Vanna-Adjusted Delta (Assuming 10% IV shock for stress test)
            adj_delta = calculate_vega_adjusted_delta(total_delta, total_vanna, 0.10)

            greeks_status = (
                f"Δ: `{total_delta:.2f}` | 隱含 Δ (Vanna): `{adj_delta:.2f}`"
            )
            # 3. Financial Health
            runway_days = calculate_financial_runway(
                ctx.cash_reserve, ctx.monthly_expense, ctx.total_theta
            )
            theta_cov = (
                (ctx.total_theta * 30 / ctx.monthly_expense * 100)
                if ctx.monthly_expense > 0
                else 0.0
            )

            # 4. Active Signal
            active_signal_content = ""
            hedge_suggest = "無需緊急對沖 (Hold)"
            if abs(adj_delta) > (ctx.capital / 1000) * 0.1:  # Simple threshold logic
                action = "BUY" if adj_delta < 0 else "SELL"
                # 🚀 Task 1 Fix: SPY 每股 Delta 為 1.0，而非 0.5 (後者常規用於平價選擇權)
                # 原本使用 0.5 導致對沖口數翻倍 (166.94 -> 333)
                qty = max(1, int(round(abs(adj_delta))))
                hedge_suggest = (
                    f"建議 {action} {qty} 單位 SPY 對沖 Delta 偏離 (`/settle_hedge`)\n"
                    f"> (稽核詳情: adj_delta={adj_delta:.2f}, capital=${ctx.capital:,.0f})"
                )
            elif vix_tier.get("multiplier", 1.0) < 0.5:
                hedge_suggest = "VIX 過高，建議啟動尾部風險防禦"

            if phase == "A":
                active_signal_content = f"**早盤流動性:** 觀察 VIX 變化與日內開盤跳空缺口。\n**對沖建議:** {hedge_suggest}"
            elif phase == "B":
                active_signal_content = f"**情緒/巨鯨:** Skew `{skew_data.get('skew', 0.0)}`, {poly_intent}\n**板塊輪動:** 關注科技與金融板塊資金流向。"
            elif phase == "C":
                active_signal_content = f"**尾盤收斂:** 檢視 Vanna-Adjusted Delta 是否過高。\n**強制對沖建議:** {hedge_suggest}"

            # 5. System Health
            import services.market_data_service as mds

            sma_count = len(mds._sma_cache)
            ema_count = len(mds._ema_cache)
            embed = create_intraday_execution_guide_embed(
                phase_name=phase_name,
                vix=vix,
                memory_percent=mem.percent,
                is_memory_gated=False,
                vix_level_name=vix_level_name,
                greeks_status=greeks_status,
                runway_days=runway_days,
                theta_cov=theta_cov,
                active_signal_content=active_signal_content,
                sma_cache_size=sma_count,
                ema_cache_size=ema_count,
            )
            await self.bot.queue_dm(uid, embed=embed)
            dispatched_count += 1

        logger.info(
            f"Dispatched Intra-day Execution Guide to {dispatched_count} users."
        )

    # ==========================================
    # 🚀 3. 盤後策略：收盤後 15 分鐘啟動
    # ==========================================
    @tasks.loop(count=1)
    async def post_market_loop(self):
        await self.bot.wait_until_ready()
        while True:
            target = get_next_market_target_time("close", offset_minutes=15)
            sleep_secs = get_sleep_seconds(target)

            logger.info(
                f"🤖 [Analyst Post-Market] 下次執行時間: {target} (倒數 {sleep_secs/3600:.2f} 小時)"
            )
            await asyncio.sleep(sleep_secs)

            try:
                if not getattr(self.bot, "_is_leader_instance", True):
                    await asyncio.sleep(30)
                    continue

                logger.info(
                    "🤖 [Analyst Post-Market] 啟動盤後綜合風險與 AI 策略報告流程..."
                )
                await self.dispatch_post_market_intelligence()
            except Exception as e:
                logger.error(f"Analyst Post-Market loop error: {e}")

            await asyncio.sleep(60)

    async def _fetch_macro_data(self):
        """Helper to fetch general macro proxies."""

        def fetch():
            # Quick fetch of some proxies: VIX, DXY, TNX (10-yr yield), IRX (13-week bill as proxy or ^IRX)
            # DXY fallback: 'DX-Y.NYB' or 'UUP' (ETF)
            # For 2Y yield, we can use ^IRX or just ^TYX for 30Y and interpolate,
            # but usually ^IRX is 13W. Let's try ^VIX, DX-Y.NYB, ^TNX, ^IRX
            tickers = yf.Tickers("^VIX DX-Y.NYB ^TNX ^IRX")
            hist = tickers.history(period="2d")
            return hist

        try:
            hist = await asyncio.to_thread(fetch)
            if not hist.empty and len(hist) >= 2:
                # Current Close
                vix = float(hist["Close"]["^VIX"].iloc[-1])
                dxy = float(hist["Close"]["DX-Y.NYB"].iloc[-1])
                tnx = float(hist["Close"]["^TNX"].iloc[-1])

                # Previous Close
                vix_prev = float(hist["Close"]["^VIX"].iloc[-2])
                tnx_prev = float(hist["Close"]["^TNX"].iloc[-2])

                # Check for NaN and fallback
                if math.isnan(vix):
                    vix = float("nan")
                if math.isnan(vix_prev):
                    vix_prev = vix

                vix_change = (
                    vix - vix_prev
                    if not math.isnan(vix) and not math.isnan(vix_prev)
                    else 0.0
                )
                tnx_change_bps = (
                    (tnx - tnx_prev) * 100
                    if not (math.isnan(tnx) or math.isnan(tnx_prev))
                    else 0.0
                )

                # Mock US2Y as TNX - 0.2 if IRX is too low, or use a better proxy if available
                us2y = tnx - 0.2 if not math.isnan(tnx) else 0.0  # Fallback

                return {
                    "vix": round(vix, 2),
                    "vix_change": round(vix_change, 2),
                    "dxy": round(dxy, 2) if not math.isnan(dxy) else 0.0,
                    "tnx": round(tnx, 2) if not math.isnan(tnx) else 0.0,
                    "tnx_change_bps": round(tnx_change_bps, 1),
                    "us2y": round(us2y, 2),
                }
            elif not hist.empty:
                # Only 1 day of data
                vix = float(hist["Close"]["^VIX"].iloc[-1])
                dxy = float(hist["Close"]["DX-Y.NYB"].iloc[-1])
                tnx = float(hist["Close"]["^TNX"].iloc[-1])

                vix = float("nan") if math.isnan(vix) else vix
                dxy = 0.0 if math.isnan(dxy) else dxy
                tnx = 0.0 if math.isnan(tnx) else tnx

                return {
                    "vix": vix,
                    "vix_change": 0.0,
                    "dxy": dxy,
                    "tnx": tnx,
                    "tnx_change_bps": 0.0,
                    "us2y": tnx - 0.2,
                }
        except Exception as e:
            logger.warning(f"Failed to fetch macro proxies: {e}")
        return {
            "vix": 0.0,
            "vix_change": 0.0,
            "dxy": 0.0,
            "tnx": 0.0,
            "tnx_change_bps": 0.0,
            "us2y": 0.0,
        }

    def _get_tw_time_str(self) -> str:
        """動態生成台灣時間 (UTC+8) 的當下時間標籤"""
        now_tw = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
        return now_tw.strftime("[%H:%M UTC+8]")

    async def run_macro_scan(self):
        macro_data = await self._fetch_macro_data()

        # 處理資料格式兼容 (若為舊版 tuple 則轉為新版 dict 預設值)
        if isinstance(macro_data, tuple):
            vix, dxy, tnx = macro_data
            macro_data = {
                "vix": vix,
                "vix_change": 0.0,
                "dxy": dxy,
                "tnx": tnx,
                "tnx_change_bps": 0.0,
                "us2y": tnx - 0.2,
            }

        vix = macro_data.get("vix", 0.0)
        vix_change = macro_data.get("vix_change", 0.0)
        dxy = macro_data.get("dxy", 0.0)
        tnx = macro_data.get("tnx", 0.0)
        tnx_change_bps = macro_data.get("tnx_change_bps", 0.0)
        us2y = macro_data.get("us2y", 0.0)

        # 1. 計算利差
        spread = tnx - us2y

        # 2. 多因子告警判定
        alerts = []
        if spread < -0.2:
            alerts.append(
                "殖利率曲線深度倒掛。市場反映中長期經濟衰退預期，建議關注防禦型資產"
            )
        if -0.1 <= spread <= 0.2 and tnx_change_bps < 0:
            alerts.append(
                "殖利率曲線接近解除倒掛 (陡峭化)。歷史經驗顯示，倒掛解除初期往往伴隨市場波動加劇，請留意衰退交易發酵"
            )
        if tnx > 4.5 and tnx_change_bps > 8:
            alerts.append(
                "10 年期殖利率突破 4.5% 且短期急升。建議盤中降低對高 Beta / 估值敏感型成長股的曝險"
            )
        if vix > 20 and vix_change > 2.0:
            alerts.append("恐慌指數急遽上升，市場避險情緒發酵，注意流動性風險")
        if dxy > 105:
            alerts.append("美元指數處於強勢區間，可能壓抑跨國企業獲利與大宗商品表現")

        # 3. 建立 Embed 報告 (美化格式)
        from cogs.embed_builder import create_macro_scan_embed

        return create_macro_scan_embed(macro_data, alerts)

    async def run_premarket_earnings(self):
        time_str = self._get_tw_time_str()
        try:
            from services.calendar_service import calendar_service
            from market_time import ny_tz

            # 獲取所有觀察名單與持倉標的
            watchlist = get_all_watchlist()
            portfolio = database.get_all_portfolio()
            symbols = list(
                set([row[1] for row in watchlist] + [row[2] for row in portfolio])
            )

            # 獲取所有標的的財報日曆以進行過濾與排序
            earnings_map = await calendar_service.get_symbol_earnings_batch(symbols)

            today = datetime.now(ny_tz).date()
            valid_earnings = []
            for sym, info in earnings_map.items():
                if info is not None:
                    try:
                        e_date = datetime.strptime(info.date, "%Y-%m-%d").date()
                        days_left = (e_date - today).days
                        # 篩選未來 14 天內的財報事件（與預警雷達保持一致）
                        if 0 <= days_left <= 14:
                            valid_earnings.append(
                                {
                                    "symbol": sym,
                                    "date": info.date,
                                    "days_left": days_left,
                                }
                            )
                    except Exception as ve:
                        logger.warning(f"Error parsing earnings date for {sym}: {ve}")

            # 依據距離天數升序排序 (越近的排越前面)
            valid_earnings.sort(key=lambda x: x["days_left"])

            # 建立符合原 Embed/LLM 要求的結構，限制最多前 10 個，並進行緊迫度分級優化
            top_earnings = valid_earnings[:10]

            # 緊迫度分級：僅對 2 天內發布財報的標的執行深度掃描與 PCR 計算，其餘使用輕量默認值
            deep_scan_symbols = [
                item["symbol"] for item in top_earnings if item["days_left"] <= 2
            ]
            light_scan_symbols = [
                item["symbol"] for item in top_earnings if item["days_left"] > 2
            ]

            # 控制最大併發數，避免觸發 Finnhub / yfinance 的 Rate Limit
            sem = asyncio.Semaphore(3)

            async def deep_scan_symbol(sym):
                async with sem:
                    # evaluate_watchlist_symbol、calculate_pcr 與 get_company_profile 併行執行
                    eval_task = evaluate_watchlist_symbol(sym)
                    pcr_task = SentimentEngine.calculate_pcr(sym)
                    profile_task = market_data_service.get_company_profile(sym)
                    return await asyncio.gather(
                        eval_task, pcr_task, profile_task, return_exceptions=True
                    )

            async def light_scan_symbol(sym):
                async with sem:
                    # 輕量級僅獲取公司行業板塊資訊
                    profile_res = await market_data_service.get_company_profile(sym)
                    return profile_res

            # 執行非同步任務
            deep_tasks = [deep_scan_symbol(sym) for sym in deep_scan_symbols]
            deep_results_list = await asyncio.gather(
                *deep_tasks, return_exceptions=True
            )
            deep_results_map = {}
            for sym, res in zip(deep_scan_symbols, deep_results_list):
                if isinstance(res, Exception) or not isinstance(res, (list, tuple)):
                    deep_results_map[sym] = (None, {"pcr": 0.0, "state": "ERROR"}, {})
                else:
                    eval_res = res[0] if not isinstance(res[0], Exception) else None
                    pcr_res = (
                        res[1]
                        if not isinstance(res[1], Exception)
                        else {"pcr": 0.0, "state": "ERROR"}
                    )
                    prof_res = res[2] if not isinstance(res[2], Exception) else {}
                    deep_results_map[sym] = (eval_res, pcr_res, prof_res)

            light_tasks = [light_scan_symbol(sym) for sym in light_scan_symbols]
            light_results_list = await asyncio.gather(
                *light_tasks, return_exceptions=True
            )
            light_results_map = {}
            for sym, res in zip(light_scan_symbols, light_results_list):
                if isinstance(res, Exception):
                    light_results_map[sym] = {}
                else:
                    light_results_map[sym] = res

            earnings_data = {}
            for item in top_earnings:
                sym = item["symbol"]
                date = item["date"]
                days_left = item["days_left"]

                # 建立基本量化上下文，修剪不必要欄位以減少 LLM Token 成本
                metrics_payload = {
                    "date": date,
                    "days_left": days_left,
                    "current_price": 0.0,
                    "rsi_14": 50.0,
                    "pe_ratio": None,
                    "bias_ma20": 0.0,
                    "iv_rank": 0.0,
                    "option_skew": 0.0,
                    "pcr": 0.0,
                    "sector": "Unknown",
                }

                if sym in deep_results_map:
                    eval_res, pcr_res, prof_res = deep_results_map[sym]
                    metrics_payload.update(
                        {
                            "pcr": pcr_res.get("pcr", 0.0),
                            "sector": prof_res.get("finnhubIndustry", "Unknown"),
                        }
                    )
                    if eval_res is not None and eval_res.metrics is not None:
                        m = eval_res.metrics
                        metrics_payload.update(
                            {
                                "current_price": m.current_price,
                                "rsi_14": m.rsi_14,
                                "pe_ratio": m.pe_ratio,
                                "bias_ma20": getattr(
                                    m,
                                    "bias_ma20",
                                    (m.current_price / m.ma20 - 1.0) if m.ma20 else 0.0,
                                ),
                                "iv_rank": m.iv_rank,
                                "option_skew": m.option_skew,
                            }
                        )
                elif sym in light_results_map:
                    prof_res = light_results_map[sym]
                    metrics_payload.update(
                        {
                            "sector": prof_res.get("finnhubIndustry", "Unknown"),
                        }
                    )

                earnings_data[sym] = [metrics_payload]

            # 並行獲取即將發布財報標的之新聞與 Reddit 情緒 (最多取前 2 個，以緊迫度排序)
            upcoming_symbols = [item["symbol"] for item in valid_earnings[:2]]
            sentiment_data = {}
            if upcoming_symbols:
                news_tasks = [fetch_recent_news(sym) for sym in upcoming_symbols]
                if database.any_user_local_tunnel_enabled():
                    reddit_tasks = [get_reddit_context(sym) for sym in upcoming_symbols]
                else:
                    reddit_tasks = []

                news_results = await asyncio.gather(*news_tasks, return_exceptions=True)
                reddit_results = (
                    await asyncio.gather(*reddit_tasks, return_exceptions=True)
                    if reddit_tasks
                    else ["本地 Tunnel 已關閉，略過 Reddit 情緒。"]
                    * len(upcoming_symbols)
                )

                for i, sym in enumerate(upcoming_symbols):
                    sentiment_data[sym] = {
                        "news": news_results[i]
                        if not isinstance(news_results[i], Exception)
                        else "無法獲取",
                        "reddit_sentiment": reddit_results[i]
                        if not isinstance(reddit_results[i], Exception)
                        else "無法獲取",
                    }

            raw_data = {
                "analyzed_symbols": len(symbols),
                "upcoming_earnings": earnings_data,
                "earnings_sentiment_scan": sentiment_data,
                "note": "IV and VRP are evaluated dynamically based on recent price action. Proximity sorted (max 10).",
            }

            report_type = f"{time_str} 盤前財報與估值調整"
            report_content = await generate_analyst_report(report_type, raw_data)

            return create_earnings_report_embed(report_type, report_content, raw_data)
        except Exception as e:
            logger.error(f"run_premarket_earnings error: {e}")
            return create_ai_analysis_embed(
                f"**{time_str} 盤前財報與估值調整**\n--------------------------------------------------\n系統分析發生錯誤: {e}",
                title="📊 Nexus Seeker 盤前財報與估值調整",
            )

    async def run_market_open_liquidity(self):
        time_str = self._get_tw_time_str()
        try:
            # 選擇一些高 Beta 指標或大盤
            symbols = ["SPY", "QQQ", "IWM"]
            liquidity_data = {}
            for sym in symbols:
                df = await get_history_df(sym, period="1mo")
                if not df.empty:
                    psq_result = analyze_psq(
                        df, vix_spot=18.0
                    )  # 預設 VIX 或從 macro 取得
                    liquidity_data[sym] = {
                        "psq_score": psq_result.psq_score if psq_result else 0.0,
                        "label": psq_result.label if psq_result else "NEUTRAL",
                        "last_price": float(df["Close"].iloc[-1]),
                        "volume": int(df["Volume"].iloc[-1]),
                    }

            raw_data = {
                "monitored_indices": liquidity_data,
                "liquidity_filter_active": True,
            }

            report_type = f"{time_str} 開盤與流動性執行監控"
            report = await generate_analyst_report(report_type, raw_data)
            return report
        except Exception as e:
            logger.error(f"run_market_open_liquidity error: {e}")
            return f"**{time_str} 開盤與流動性執行監控**\n--------------------------------------------------\n系統分析發生錯誤: {e}"

    async def run_deep_research(self):
        time_str = self._get_tw_time_str()
        try:
            # 總經板塊分析
            sectors = {
                "Semiconductors": "SMH",
                "Technology": "XLK",
                "Financials": "XLF",
            }
            research_data = {}

            # 並行獲取價格歷史、新聞與 Reddit 資訊
            hist_tasks = [get_history_df(sym, period="3mo") for sym in sectors.values()]
            news_tasks = [fetch_recent_news(sym) for sym in sectors.values()]
            reddit_tasks = (
                [get_reddit_context(sym) for sym in sectors.values()]
                if database.any_user_local_tunnel_enabled()
                else []
            )

            hist_results = await asyncio.gather(*hist_tasks, return_exceptions=True)
            news_results = await asyncio.gather(*news_tasks, return_exceptions=True)
            reddit_results = (
                await asyncio.gather(*reddit_tasks, return_exceptions=True)
                if reddit_tasks
                else ["本地 Tunnel 已關閉，略過 Reddit 情緒。"] * len(sectors)
            )

            for i, (name, sym) in enumerate(sectors.items()):
                df = hist_results[i]
                if not isinstance(df, Exception) and not df.empty:
                    pct_change = (
                        (df["Close"].iloc[-1] - df["Close"].iloc[0])
                        / df["Close"].iloc[0]
                        * 100
                    )
                else:
                    pct_change = 0.0

                research_data[name] = {
                    "symbol": sym,
                    "quarterly_performance_pct": round(pct_change, 2),
                    "news": news_results[i]
                    if not isinstance(news_results[i], Exception)
                    else "無法獲取",
                    "reddit_sentiment": reddit_results[i]
                    if not isinstance(reddit_results[i], Exception)
                    else "無法獲取",
                }

            raw_data = {
                "sector_analysis": research_data,
                "capex_and_dso_status": "No cyclic oversupply detected based on price momentum proxy.",
            }

            report_type = f"{time_str} 深度研究與特定板塊分析"
            report = await generate_analyst_report(report_type, raw_data)
            return report
        except Exception as e:
            logger.error(f"run_deep_research error: {e}")
            return f"**{time_str} 深度研究與特定板塊分析**\n--------------------------------------------------\n系統分析發生錯誤: {e}"

    async def run_portfolio_hedging(self):
        time_str = self._get_tw_time_str()
        try:
            user_ids = database.get_all_user_ids()
            system_hedge_status = []

            for uid in user_ids[:5]:  # 取樣前 5 名活躍用戶以避免過度計算
                perf = await analyze_hedge_performance(uid)
                system_hedge_status.append(perf)

            avg_hedge = sum(p["hedge_ratio"] for p in system_hedge_status) / max(
                len(system_hedge_status), 1
            )
            avg_eff = sum(p["effectiveness"] for p in system_hedge_status) / max(
                len(system_hedge_status), 1
            )

            raw_data = {
                "users_analyzed": len(system_hedge_status),
                "avg_hedge_ratio": round(avg_hedge, 4),
                "avg_effectiveness": round(avg_eff, 4),
                "note": "Gamma levels and SPY Delta hedge requirements evaluated.",
            }

            report_type = f"{time_str} 投資組合再平衡與避險策略"
            report = await generate_analyst_report(report_type, raw_data)
            return report
        except Exception as e:
            logger.error(f"run_portfolio_hedging error: {e}")
            return f"**{time_str} 投資組合再平衡與避險策略**\n--------------------------------------------------\n系統分析發生錯誤: {e}"

    async def run_postmarket_summary(self):
        time_str = self._get_tw_time_str()
        try:
            # 1. 獲取宏觀環境
            macro = await get_macro_environment()
            vix = macro.get("vix", 18.0)
            vix_tier = get_vix_tier(vix)

            # 2. 彙整核心用戶數據 (取前 5 名作為樣本)
            user_ids = database.get_all_user_ids()
            total_net_pnl = 0
            total_alpha = 0
            total_hedge = 0
            total_theta = 0
            total_delta = 0
            total_capital = 0
            total_runway = 0
            active_users = 0

            for uid in user_ids[:5]:
                # 績效歸因
                perf = await analyze_hedge_performance(uid)
                total_net_pnl += perf.get("net_pnl", 0)
                total_alpha += perf.get("alpha_contribution", 0)
                total_hedge += perf.get("hedge_contribution", 0)

                # 風險指標
                u_ctx = await asyncio.to_thread(database.get_full_user_context, uid)
                total_theta += u_ctx.total_theta
                total_delta += u_ctx.total_weighted_delta
                total_capital += u_ctx.capital

                # 計算生存跑道 (Runway)
                if u_ctx.monthly_expense > 0:
                    daily_burn = (u_ctx.monthly_expense / 30.0) - u_ctx.total_theta
                    if daily_burn <= 0:
                        runway = 9999
                    else:
                        runway = (u_ctx.cash_reserve + u_ctx.capital) / daily_burn
                    total_runway += runway

                active_users += 1

            avg_runway = total_runway / active_users if active_users > 0 else 0

            spy_data = await get_quote("SPY")
            spy_price = spy_data.get("c", 500.0)
            portfolio_heat = (
                (abs(total_delta) * spy_price / total_capital * 100)
                if total_capital > 0
                else 0
            )

            raw_data = {
                "macro_snapshot": {
                    "vix": vix,
                    "vix_tier": vix_tier,
                    "spy_price": spy_price,
                },
                "brinson_attribution_proxy": {
                    "total_net_pnl": round(total_net_pnl, 2),
                    "alpha_selection_pnl": round(total_alpha, 2),
                    "market_hedge_pnl": round(total_hedge, 2),
                },
                "aggregate_risk_metrics": {
                    "total_theta": round(total_theta, 2),
                    "total_beta_delta": round(total_delta, 2),
                    "portfolio_heat_pct": round(portfolio_heat, 2),
                    "avg_financial_runway_days": round(avg_runway, 1),
                },
                "sector_correlation": "Stable",
            }

            report_type = f"{time_str} 全系統宏觀風險總結"
            report = await generate_analyst_report(report_type, raw_data)
            return report
        except Exception as e:
            logger.error(f"run_postmarket_summary error: {e}")
            return f"**{time_str} 全系統宏觀風險總結**\n--------------------------------------------------\n系統分析發生錯誤: {e}"

    async def run_sector_flow_report(self):
        time_str = self._get_tw_time_str()
        try:
            # 1. Market Snapshot
            macro = await get_macro_environment()
            vix = macro.get("vix", 18.0)
            vix_tier = get_vix_tier(vix)
            spy_quote = await get_quote("SPY")

            # 2. Sector Rotation Data
            sector_results = []
            for symbol, name in SECTORS.items():
                try:
                    df = await get_history_df(symbol, period="1mo")
                    if df.empty:
                        continue

                    pct_change = (
                        (df["Close"].iloc[-1] - df["Close"].iloc[-2])
                        / df["Close"].iloc[-2]
                        * 100
                    )
                    vol_current = df["Volume"].iloc[-1]
                    vol_avg = df["Volume"].tail(20).mean()
                    rel_vol = vol_current / vol_avg if vol_avg > 0 else 1.0

                    try:
                        skew_data = await SentimentEngine.calculate_skew(symbol)
                    except Exception:
                        skew_data = {"skew": 0, "state": "N/A"}

                    try:
                        uoa = await SentimentEngine.detect_uoa(symbol)
                    except Exception:
                        uoa = []

                    sector_results.append(
                        {
                            "symbol": symbol,
                            "name": name,
                            "pct_change": round(pct_change, 2),
                            "rel_vol": round(rel_vol, 2),
                            "skew": skew_data.get("skew", 0),
                            "skew_state": skew_data.get("state", "N/A"),
                            "uoa_count": len(uoa),
                        }
                    )
                except Exception as e:
                    logger.error(f"Error gathering data for {symbol}: {e}")

            # 3. Polymarket Events
            poly_events = []
            try:
                GAMMA_API_BASE = "https://gamma-api.polymarket.com"
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{GAMMA_API_BASE}/markets",
                        params={"active": "true", "closed": "false", "limit": 20},
                    )
                    if resp.status_code == 200:
                        markets = resp.json()
                        for m in markets:
                            # 使用 PolymarketService 的過濾邏輯
                            if hasattr(self.bot, "polymarket_service"):
                                if not self.bot.polymarket_service._is_relevant_market(
                                    m
                                ):
                                    continue

                            poly_events.append(
                                {
                                    "question": m.get("question"),
                                    "outcome": m.get("outcomes"),
                                    "price": m.get("outcomePrices"),
                                }
                            )
                            if len(poly_events) >= 5:
                                break
            except Exception as e:
                logger.error(f"Error fetching Polymarket data: {e}")

            # 4. Max Pain
            try:
                spy_max_pain = await SentimentEngine.calculate_max_pain("SPY")
            except Exception:
                spy_max_pain = {"error": "N/A"}

            raw_data = {
                "vix": vix,
                "vix_tier_name": vix_tier.get("name", "Unknown"),
                "spy_price": spy_quote.get("c", 0),
                "sectors": sector_results,
                "poly_events": poly_events,
                "spy_max_pain": spy_max_pain,
            }

            report_type = f"{time_str} 收盤資金流向與板塊輪動報告"
            report = await generate_analyst_report(report_type, raw_data)
            return create_sector_flow_report_embed(report_type, report, raw_data)
        except Exception as e:
            logger.error(f"run_sector_flow_report error: {e}")
            return create_ai_analysis_embed(
                f"**{time_str} 收盤資金流向報告**\n--------------------------------------------------\n系統分析發生錯誤: {e}",
                title="📊 Nexus Seeker 收盤資金流向與板塊輪動報告",
            )

    async def run_next_day_strategy(self):
        time_str = self._get_tw_time_str()
        macro_data = await self._fetch_macro_data()
        vix = (
            macro_data.get("vix", 0.0)
            if isinstance(macro_data, dict)
            else macro_data[0]
        )
        tier = get_vix_tier(vix)
        tier_display = f"{tier.get('emoji', '')} {tier.get('name', 'Unknown')}"

        vix_display = f"{vix:.2f}" if not math.isnan(vix) else "N/A (Using Default)"

        # 實際動態獲取 VIX 期限結構與 SPY 偏態指數
        try:
            vts_data = await get_vix_term_structure()
            vts_ratio = vts_data.get("vts_ratio", 1.0)
            vts_state = vts_data.get("vts_state", "UNKNOWN")
            vix_front = vts_data.get("vix_front")
            vix_back = vts_data.get("vix_back")
            vts_detail = (
                f" (VIX/VIX3M: {vix_front:.2f}/{vix_back:.2f})"
                if (vix_front is not None and vix_back is not None)
                else ""
            )
            vts_display = f"{vts_ratio:.3f} ({vts_state}){vts_detail}"
        except Exception as e:
            logger.error(f"獲取 VIX 期限結構失敗: {e}")
            vts_display = "取得失敗 (Using Default)"

        try:
            skew_data = await SentimentEngine.calculate_skew("SPY")
            skew_val = skew_data.get("skew", 0.0)
            skew_state = skew_data.get("state", "N/A")
            skew_display = f"{skew_val}% ({skew_state})"
        except Exception as e:
            logger.error(f"計算 SPY Skew Index 失敗: {e}")
            skew_display = "取得失敗 (Using Default)"

        report = (
            f"**{time_str} 次日策略制定**\n"
            "--------------------------------------------------\n"
            f"**市場狀態指標：**\n"
            f"• 當前 VIX: {vix_display} ({tier_display})\n"
            f"• VIX 期限結構 (VTS): {vts_display}\n"
            f"• SPY 偏態指數 (Skew): {skew_display}\n\n"
            "**戰術建議：**\n"
        )
        if vix < 15:
            report += "⚠️ 市場處於休眠期 (Dormant)。強制拒絕所有 STO 訊號。"
        elif vix >= 35:
            report += (
                "🚨 市場處於極度恐慌 (All-In)。繞過市場政權阻尼，啟用 1/2 Kelly 覆寫。"
            )
        else:
            report += "✅ 已設定標準量化掃描參數。NRO 保證金限制正常運作。"

        return report

    async def dispatch_pre_market_briefing(self):
        # 0. 盤前呼叫 edge scraper 更新 FedWatch 數據
        try:
            from services.calendar_service import calendar_service

            await calendar_service.update_fedwatch_probability()
        except Exception as e:
            logger.warning(f"更新 FedWatch 概率失敗: {e}")

        # 1. 執行巨觀資料獲取
        macro_data = await self._fetch_macro_data()
        if isinstance(macro_data, tuple):
            vix, dxy, tnx = macro_data
            macro_data = {
                "vix": vix,
                "vix_change": 0.0,
                "dxy": dxy,
                "tnx": tnx,
                "tnx_change_bps": 0.0,
                "us2y": tnx - 0.2,
            }
        vix = macro_data.get("vix", 0.0)
        vix_change = macro_data.get("vix_change", 0.0)
        dxy = macro_data.get("dxy", 0.0)
        tnx = macro_data.get("tnx", 0.0)
        tnx_change_bps = macro_data.get("tnx_change_bps", 0.0)
        us2y = macro_data.get("us2y", 0.0)
        spread = tnx - us2y

        macro_alerts = []
        if spread < -0.2:
            macro_alerts.append(
                "殖利率曲線深度倒掛。市場反映中長期經濟衰退預期，建議關注防禦型資產"
            )
        if -0.1 <= spread <= 0.2 and tnx_change_bps < 0:
            macro_alerts.append("殖利率曲線接近解除倒掛 (陡峭化)。留意衰退交易發酵")
        if tnx > 4.5 and tnx_change_bps > 8:
            macro_alerts.append(
                "10 年期殖利率突破 4.5% 且短期急升。建議盤中降低對高 Beta / 估值敏感成長股的曝險"
            )
        if vix > 20 and vix_change > 2.0:
            macro_alerts.append("恐慌指數急遽上升，市場避險情緒發酵，注意流動性風險")
        if dxy > 105:
            macro_alerts.append(
                "美元指數處於強勢區間，可能壓抑跨國企業獲利與大宗商品表現"
            )

        # 2. 執行財報資料獲取
        warning_days = 2
        from services.trading_service import TradingService

        ts = TradingService(self.bot)
        user_earnings_data = await ts.get_pre_market_alerts_data(
            warning_days=warning_days
        )

        user_ids = database.get_all_user_ids()
        from cogs.embed_builder import build_pre_market_briefing_embed

        for uid in user_ids:
            if not database.is_notification_enabled(uid, "pre_market_briefing"):
                continue
            u_data = user_earnings_data.get(uid, {"alerts": [], "scanned_symbols": []})

            # format alert dates and ensure they are parsed properly
            formatted_alerts = []
            for alert in u_data["alerts"]:
                e_date_str = (
                    alert["earnings_date"].strftime("%Y-%m-%d")
                    if isinstance(alert["earnings_date"], datetime)
                    or hasattr(alert["earnings_date"], "strftime")
                    else str(alert["earnings_date"])
                )
                formatted_alerts.append(
                    {
                        "symbol": alert["symbol"],
                        "is_portfolio": alert["is_portfolio"],
                        "earnings_date": e_date_str,
                        "days_left": alert["days_left"],
                    }
                )

            embed = build_pre_market_briefing_embed(
                macro_data=macro_data,
                alerts=macro_alerts,
                earnings_alerts=formatted_alerts,
                scanned_symbols=u_data["scanned_symbols"],
                warning_days=warning_days,
            )
            await self.bot.queue_dm(uid, embed=embed)

            # 方案 C 逃頂窗口推演並推送宏觀警報 Embed
            try:
                fomc_embed = await self.run_fomc_escape_window_analysis(uid)
                if fomc_embed:
                    await self.bot.queue_dm(uid, embed=fomc_embed)
            except Exception as e:
                logger.error(f"推送方案 C 逃頂窗口 Embed 失敗: {e}")

    async def run_fomc_escape_window_analysis(
        self, user_id: int
    ) -> Optional[discord.Embed]:
        """依據下週 FOMC 的 FedWatch 定價機率與使用者自訂設定，動態推演反彈逃頂窗口。"""
        import sqlite3
        import config

        def get_period_label(day: int) -> str:
            if day <= 10:
                return "上旬"
            elif day <= 20:
                return "中旬"
            else:
                return "下旬"

        # 1. 取得 FedWatch 概率
        prob = 0.72  # 預設值
        is_fallback = False
        try:
            from database.cache import get_kv_cache

            fedwatch_fallback_val = get_kv_cache("macro_fedwatch_is_fallback")
            if fedwatch_fallback_val is None or int(fedwatch_fallback_val) == 1:
                is_fallback = True

            with sqlite3.connect(config.DB_NAME) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT fedwatch_probability
                    FROM economic_calendar_events
                    WHERE event LIKE '%FOMC%' OR event LIKE '%Fed Interest Rate%'
                    ORDER BY event_time ASC
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
                if row and row["fedwatch_probability"] is not None:
                    prob = row["fedwatch_probability"]
                else:
                    is_fallback = True
        except Exception as e:
            logger.warning(f"查詢 SQLite FOMC FedWatch 概率失敗: {e}")
            is_fallback = True

        # 2. 載入使用者自訂的逃頂窗口區間 (MM-DD 格式，例如 07-15, 07-31)
        from database.user_settings import get_full_user_context

        ctx = get_full_user_context(user_id)
        start_setting = ctx.escape_window_start if ctx else "07-15"
        end_setting = ctx.escape_window_end if ctx else "07-31"

        try:
            start_m, start_d = map(int, start_setting.split("-"))
            end_m, end_d = map(int, end_setting.split("-"))
        except Exception:
            start_m, start_d = 7, 15
            end_m, end_d = 7, 31

        custom_period_label = f"{start_m}月{get_period_label(start_d)}至{end_m}月{get_period_label(end_d)}"

        # 3. 根據通膨與利率概率動態微調 (提前 / 延後)
        from database.cache import get_kv_cache

        cpi_actual = get_kv_cache("macro_cpi_actual")
        cpi_expected = get_kv_cache("macro_cpi_expected")
        cpi_dev = 0.0
        if cpi_actual is not None and cpi_expected is not None:
            cpi_dev = cpi_actual - cpi_expected
        else:
            cpi_dev = get_kv_cache("macro_cpi_deviation") or 0.0

        wti = get_kv_cache("macro_wti") or 75.0
        is_inflation_high = (cpi_dev > 0.1) or (wti > 85.0)

        # 決定最終調整方向：如果通膨高，或者 FedWatch 降息/利空出盡機率高 (prob <= 0.70) -> 前移
        # 否則 (通膨未過熱且維持高利率機率高 > 0.70) -> 後推
        should_advance = is_inflation_high or (prob <= 0.70)

        if should_advance:
            shift_days = 5
            adj_start_d = start_d - shift_days
            adj_end_d = end_d - shift_days
            adj_start_m = start_m
            adj_end_m = end_m
            if adj_start_d <= 0:
                adj_start_d += 30
                adj_start_m = 12 if start_m == 1 else start_m - 1
            if adj_end_d <= 0:
                adj_end_d += 30
                adj_end_m = 12 if end_m == 1 else end_m - 1

            adjusted_start = f"{adj_start_m}月{get_period_label(adj_start_d)} (約 {adj_start_m:02d}-{adj_start_d:02d})"
            adjusted_end = f"{adj_end_m}月{get_period_label(adj_end_d)} (約 {adj_end_m:02d}-{adj_end_d:02d})"
            direction = "前移"
            if is_inflation_high:
                reason = (
                    f"由於核心 CPI/PCE 高於預期 ({cpi_dev:+.2f}%) 或 WTI 油價達 ${wti:.1f} 觸及通膨高風險閾值，"
                    f"通膨壓力上升可能導致政策收緊。系統自動將您自訂的 {custom_period_label} 反彈逃頂窗口前移 {shift_days} 個交易日，提示需提前防禦撤退。"
                )
            else:
                reason = f"由於下週 FOMC 維持高利率/加息機率僅 {prob*100:.1f}%，小於 70% 臨界值，市場預期利空出盡。系統自動將您自訂的 {custom_period_label} 反彈逃頂窗口前移 {shift_days} 個交易日，提示多頭反彈可能提前發酵，需作好提前撤退部署。"
        else:
            shift_days = 5
            adj_start_d = start_d + shift_days
            adj_end_d = end_d + shift_days
            adj_start_m = start_m
            adj_end_m = end_m
            if adj_start_d > 30:
                adj_start_d -= 30
                adj_start_m = 1 if start_m == 12 else start_m + 1
            if adj_end_d > 30:
                adj_end_d -= 30
                adj_end_m = 1 if end_m == 12 else end_m + 1

            adjusted_start = f"{adj_start_m}月{get_period_label(adj_start_d)} (約 {adj_start_m:02d}-{adj_start_d:02d})"
            adjusted_end = f"{adj_end_m}月{get_period_label(adj_end_d)} (約 {adj_end_m:02d}-{adj_end_d:02d})"
            direction = "後推"
            reason = (
                f"由於通膨與油價數據放緩 (WTI: ${wti:.1f}, CPI偏差: {cpi_dev:+.2f}%) 且下週 FOMC 維持高利率機率為 {prob*100:.1f}%，"
                f"市場流動性緊縮預期減弱。系統自動將您自訂的 {custom_period_label} 反彈逃頂窗口後推 {shift_days} 個交易日，建議延後多頭撤退計劃。"
            )

        from cogs.embed_builder import create_fomc_escape_window_embed

        return create_fomc_escape_window_embed(
            prob=prob,
            direction=direction,
            shift_days=shift_days,
            adjusted_start=adjusted_start,
            adjusted_end=adjusted_end,
            reason=reason,
            is_fallback=is_fallback,
        )

    async def gather_sector_rotation_data(self):
        macro = await get_macro_environment()
        vix = macro.get("vix", 18.0)
        vix_tier = get_vix_tier(vix)
        spy_quote = await get_quote("SPY")

        sector_results = []
        for symbol, name in SECTORS.items():
            try:
                df = await get_history_df(symbol, period="1mo")
                if df.empty:
                    continue

                pct_change = (
                    (df["Close"].iloc[-1] - df["Close"].iloc[-2])
                    / df["Close"].iloc[-2]
                    * 100
                )
                vol_current = df["Volume"].iloc[-1]
                vol_avg = df["Volume"].tail(20).mean()
                rel_vol = vol_current / vol_avg if vol_avg > 0 else 1.0

                try:
                    skew_data = await SentimentEngine.calculate_skew(symbol)
                except Exception:
                    skew_data = {"skew": 0, "state": "N/A"}

                try:
                    uoa = await SentimentEngine.detect_uoa(symbol)
                except Exception:
                    uoa = []

                sector_results.append(
                    {
                        "symbol": symbol,
                        "name": name,
                        "pct_change": round(pct_change, 2),
                        "rel_vol": round(rel_vol, 2),
                        "skew": skew_data.get("skew", 0),
                        "skew_state": skew_data.get("state", "N/A"),
                        "uoa_count": len(uoa),
                    }
                )
            except Exception as e:
                logger.error(f"Error gathering data for {symbol}: {e}")

        poly_events = []
        try:
            GAMMA_API_BASE = "https://gamma-api.polymarket.com"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{GAMMA_API_BASE}/markets",
                    params={"active": "true", "closed": "false", "limit": 20},
                )
                if resp.status_code == 200:
                    markets = resp.json()
                    for m in markets:
                        if hasattr(self.bot, "polymarket_service"):
                            if not self.bot.polymarket_service._is_relevant_market(m):
                                continue
                        poly_events.append(
                            {
                                "question": m.get("question"),
                                "outcome": m.get("outcomes"),
                                "price": m.get("outcomePrices"),
                            }
                        )
                        if len(poly_events) >= 5:
                            break
        except Exception as e:
            logger.error(f"Error fetching Polymarket data: {e}")

        try:
            spy_max_pain = await SentimentEngine.calculate_max_pain("SPY")
        except Exception:
            spy_max_pain = {"error": "N/A"}

        return {
            "vix": vix,
            "vix_tier_name": vix_tier.get("name", "Unknown"),
            "spy_price": spy_quote.get("c", 0),
            "sectors": sector_results,
            "poly_events": poly_events,
            "spy_max_pain": spy_max_pain,
        }

    async def dispatch_post_market_intelligence(self):
        # 1. Purge old cache
        try:
            purged_rows = database.purge_old_cache(days=30)
            logger.info(
                f"🧹 financials_cache 清理完成，刪除 {purged_rows} 筆 30 天前資料"
            )
        except Exception as e:
            logger.warning(f"financials_cache 清理失敗: {e}")

        # 2. Gather sector rotation data (run once)
        sector_rotation_data = await self.gather_sector_rotation_data()

        # 3. Gather portfolio risk metrics for all users (run once)
        from services.trading_service import TradingService

        ts = TradingService(self.bot)
        try:
            user_reports = await ts.get_after_market_report_data()
        except Exception as e:
            logger.error(f"Error gathering after market report data: {e}")
            user_reports = {}

        user_ids = database.get_all_user_ids()
        import psutil
        from cogs.embed_builder import build_post_market_intelligence_embed

        for uid in user_ids:
            if not database.is_notification_enabled(uid, "post_market_intelligence"):
                continue

            user_ctx = database.get_full_user_context(uid)
            if not user_ctx.enable_analyst_agent:
                continue

            u_report = user_reports.get(uid, {})
            report_lines = u_report.get("report_lines", [])
            hedge_analysis = u_report.get("hedge_analysis", {})
            survival_runway = u_report.get("survival_runway")
            if survival_runway is None:
                from market_analysis.pro_management import calculate_survival_runway

                survival_runway = calculate_survival_runway(
                    cash_reserve=user_ctx.cash_reserve,
                    monthly_expense=user_ctx.monthly_expense,
                    daily_theta=user_ctx.total_theta,
                )

            # 4. Memory Safety Gate Check
            mem = psutil.virtual_memory()
            if mem.percent > 85.0:
                logger.warning(
                    f"🚨 [Memory Gate] RAM usage ({mem.percent}%) > 85%, AI Commentary suspended for user {uid}"
                )
                ai_commentary = "⚠️ [Memory Gate] 系統記憶體使用率高於 85%，為確保系統穩定，盤後 AI 深度分析與歸因點評已暫停。"
            else:
                raw_data = {
                    "macro_snapshot": {
                        "vix": sector_rotation_data["vix"],
                        "vix_tier": sector_rotation_data["vix_tier_name"],
                        "spy_price": sector_rotation_data["spy_price"],
                    },
                    "brinson_attribution_proxy": {
                        "total_net_pnl": round(hedge_analysis.get("net_pnl", 0), 2),
                        "alpha_selection_pnl": round(
                            hedge_analysis.get("alpha_contribution", 0), 2
                        ),
                        "market_hedge_pnl": round(
                            hedge_analysis.get("hedge_contribution", 0), 2
                        ),
                    },
                    "aggregate_risk_metrics": {
                        "total_theta": round(user_ctx.total_theta, 2),
                        "total_beta_delta": round(user_ctx.total_weighted_delta, 2),
                        "portfolio_heat_pct": round(
                            (
                                abs(user_ctx.total_weighted_delta)
                                * sector_rotation_data["spy_price"]
                                / user_ctx.capital
                                * 100
                            )
                            if user_ctx.capital > 0
                            else 0,
                            2,
                        ),
                        "avg_financial_runway_days": round(
                            survival_runway if survival_runway is not None else 0, 1
                        ),
                    },
                    "sectors": sector_rotation_data["sectors"],
                    "poly_events": sector_rotation_data["poly_events"],
                    "spy_max_pain": sector_rotation_data["spy_max_pain"],
                }

                time_str = datetime.now().strftime("%Y-%m-%d")
                report_type = f"{time_str} 盤後綜合風險與 AI 策略報告"
                try:
                    ai_commentary = await generate_analyst_report(report_type, raw_data)
                except Exception as e:
                    logger.error(f"Error generating analyst report for user {uid}: {e}")
                    ai_commentary = "⚠️ 無法生成 AI 報告分析。"

            embed = build_post_market_intelligence_embed(
                report_lines=report_lines,
                hedge_analysis=hedge_analysis,
                survival_runway=survival_runway,
                sectors_data=sector_rotation_data["sectors"],
                ai_commentary=ai_commentary,
            )

            await self.bot.queue_dm(uid, embed=embed)


async def setup(bot):
    await bot.add_cog(AnalystAgent(bot))
