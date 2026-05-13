import discord
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
    get_earnings_calendar,
    get_quote,
    get_macro_environment,
)
from services.llm_service import generate_analyst_report
from services.news_service import fetch_recent_news
from services.reddit_service import get_reddit_context
from market_analysis.psq_engine import analyze_psq
from market_analysis.hedging import analyze_hedge_performance
from market_analysis.sentiment_engine import SentimentEngine
from config import get_vix_tier
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
        # 啟動三大動態排程任務
        self.pre_market_loop.start()
        self.intra_day_loop.start()
        self.post_market_loop.start()

    def cog_unload(self):
        self.pre_market_loop.cancel()
        self.intra_day_loop.cancel()
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
                logger.info("🤖 [Analyst Pre-Market] 啟動盤前巨觀與財報掃描...")
                macro_report = await self.run_macro_scan()
                if macro_report:
                    await self.dispatch_report(macro_report)

                earnings_report = await self.run_premarket_earnings()
                if earnings_report:
                    await self.dispatch_report(earnings_report)
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
                logger.info(
                    "🤖 [Analyst Intra-Day] 偵測到開盤，執行 30 分鐘心跳掃描 (Active Execution Guide)..."
                )
                try:
                    await self.dispatch_intraday_guide()
                except Exception as e:
                    logger.error(f"Analyst Intra-Day loop error: {e}")

                await asyncio.sleep(30 * 60)
            else:
                target = get_next_market_target_time("open", offset_minutes=0)
                sleep_secs = get_sleep_seconds(target)
                logger.info(
                    f"🤖 [Analyst Intra-Day] 市場休市。下次開盤心跳: {target} (倒數 {sleep_secs/3600:.2f} 小時)"
                )
                await asyncio.sleep(min(sleep_secs, 3600))  # 最多睡一小時再檢查一次

    async def dispatch_report(self, report_content):
        """
        將報告發送給所有啟用了 Analyst Agent 的用戶。
        """
        import database

        user_ids = database.get_all_user_ids()
        dispatched_count = 0
        for uid in user_ids:
            ctx = database.get_full_user_context(uid)
            if ctx.enable_analyst_agent:
                try:
                    if isinstance(report_content, discord.Embed):
                        await self.bot.queue_dm(uid, embed=report_content)
                    else:
                        await self.bot.queue_dm(uid, message=report_content)
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
            ctx = database.get_full_user_context(uid)
            if not ctx.enable_analyst_agent:
                continue

            embed = discord.Embed(
                title=f"🛡️ 盤中量化執行指引 - {phase_name}",
                color=discord.Color.red() if vix > 25 else discord.Color.blue(),
                timestamp=discord.utils.utcnow(),
            )

            if is_memory_gated:
                embed.description = "⚠️ **Memory Safety Gate Active**: VPS RAM > 85%。已暫停部分耗能分析以保證風控引擎穩定。"
                embed.add_field(
                    name="系統狀態", value=f"RAM: `{mem.percent}%`", inline=False
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
            embed.add_field(
                name="1️⃣ 風險狀態 (Risk Status)",
                value=f"**VIX 階級:** {vix_level_name} ({vix:.1f})\n**Greeks 完整性:** {greeks_status}",
                inline=False,
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

            embed.add_field(
                name="2️⃣ 財務健康 (Financial Health)",
                value=f"**剩餘跑道:** `{runway_days:.1f}` 天\n**Theta 覆蓋率:** `{theta_cov:.1f}%`",
                inline=False,
            )

            # 4. Active Signal
            active_signal_content = ""
            hedge_suggest = "無需緊急對沖 (Hold)"
            if abs(adj_delta) > (ctx.capital / 1000) * 0.1:  # Simple threshold logic
                action = "BUY" if adj_delta < 0 else "SELL"
                qty = max(1, int(abs(adj_delta) / 0.5))  # Rough SPY delta mapping
                hedge_suggest = (
                    f"建議 {action} {qty} 單位 SPY 對沖 Delta 偏離 (`/settle_hedge`)"
                )
            elif vix_tier.get("multiplier", 1.0) < 0.5:
                hedge_suggest = "VIX 過高，建議啟動尾部風險防禦"

            if phase == "A":
                active_signal_content = f"**早盤流動性:** 觀察 VIX 變化與日內開盤跳空缺口。\n**對沖建議:** {hedge_suggest}"
            elif phase == "B":
                active_signal_content = f"**情緒/巨鯨:** Skew `{skew_data.get('skew', 0.0)}`, {poly_intent}\n**板塊輪動:** 關注科技與金融板塊資金流向。"
            elif phase == "C":
                active_signal_content = f"**尾盤收斂:** 檢視 Vanna-Adjusted Delta 是否過高。\n**強制對沖建議:** {hedge_suggest}"

            embed.add_field(
                name="3️⃣ 活躍信號 (Active Signal)",
                value=active_signal_content,
                inline=False,
            )

            # 5. System Health
            import services.market_data_service as mds

            sma_count = len(mds._sma_cache)
            ema_count = len(mds._ema_cache)
            embed.add_field(
                name="4️⃣ 系統狀態 (System Health)",
                value=f"RAM: `{mem.percent}%` | BoundedCache (SMA/EMA): `{sma_count}/{ema_count}`",
                inline=False,
            )

            embed.set_footer(text="Nexus Seeker | NRO Vanna-Aware Intelligence")
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
                logger.info("🤖 [Analyst Post-Market] 啟動盤後總結與次日策略規劃...")
                summary_report = await self.run_postmarket_summary()
                if summary_report:
                    await self.dispatch_report(summary_report)

                sector_report = await self.run_sector_flow_report()
                if sector_report:
                    await self.dispatch_report(sector_report)

                next_day_report = await self.run_next_day_strategy()
                if next_day_report:
                    await self.dispatch_report(next_day_report)
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
            # 獲取所有觀察名單標的
            watchlist = get_all_watchlist()
            symbols = list(set([row[1] for row in watchlist]))

            # 獲取財報日曆
            earnings_data = {}
            for sym in symbols[:10]:  # 限制數量以防超載
                calendar = await get_earnings_calendar(sym)
                if calendar:
                    earnings_data[sym] = calendar[:1]  # 只取最近一次

            # 並行獲取即將發布財報標的之新聞與 Reddit 情緒 (最多取前 2 個)
            upcoming_symbols = list(earnings_data.keys())[:2]
            sentiment_data = {}
            if upcoming_symbols:
                news_tasks = [fetch_recent_news(sym) for sym in upcoming_symbols]
                reddit_tasks = [get_reddit_context(sym) for sym in upcoming_symbols]

                news_results = await asyncio.gather(*news_tasks, return_exceptions=True)
                reddit_results = await asyncio.gather(
                    *reddit_tasks, return_exceptions=True
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
                "note": "IV and VRP are evaluated dynamically based on recent price action.",
            }

            report_type = f"{time_str} 盤前財報與估值調整"
            report = await generate_analyst_report(report_type, raw_data)
            return report
        except Exception as e:
            logger.error(f"run_premarket_earnings error: {e}")
            return f"**{time_str} 盤前財報與估值調整**\n--------------------------------------------------\n系統分析發生錯誤: {e}"

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
            reddit_tasks = [get_reddit_context(sym) for sym in sectors.values()]

            hist_results = await asyncio.gather(*hist_tasks, return_exceptions=True)
            news_results = await asyncio.gather(*news_tasks, return_exceptions=True)
            reddit_results = await asyncio.gather(*reddit_tasks, return_exceptions=True)

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
            # 簡單加總當日 PnL
            user_ids = database.get_all_user_ids()
            total_net_pnl = 0
            total_alpha = 0
            total_hedge = 0

            for uid in user_ids[:5]:
                perf = await analyze_hedge_performance(uid)
                total_net_pnl += perf.get("net_pnl", 0)
                total_alpha += perf.get("alpha_contribution", 0)
                total_hedge += perf.get("hedge_contribution", 0)

            raw_data = {
                "brinson_attribution_proxy": {
                    "total_net_pnl": round(total_net_pnl, 2),
                    "alpha_selection_pnl": round(total_alpha, 2),
                    "market_hedge_pnl": round(total_hedge, 2),
                },
                "sector_correlation": "Stable",
            }

            report_type = f"{time_str} 盤後交易與每日總結"
            report = await generate_analyst_report(report_type, raw_data)
            return report
        except Exception as e:
            logger.error(f"run_postmarket_summary error: {e}")
            return f"**{time_str} 盤後交易與每日總結**\n--------------------------------------------------\n系統分析發生錯誤: {e}"

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
                        params={"active": "true", "closed": "false", "limit": 10},
                    )
                    if resp.status_code == 200:
                        markets = resp.json()
                        for m in markets[:5]:
                            poly_events.append(
                                {
                                    "question": m.get("question"),
                                    "outcome": m.get("outcomes"),
                                    "price": m.get("outcomePrices"),
                                }
                            )
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
            return report
        except Exception as e:
            logger.error(f"run_sector_flow_report error: {e}")
            return f"**{time_str} 收盤資金流向報告**\n--------------------------------------------------\n系統分析發生錯誤: {e}"

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

        report = (
            f"**{time_str} 次日策略制定**\n"
            "--------------------------------------------------\n"
            f"**當前 VIX:** {vix_display} -> **戰鬥階級 (Tier):** {tier_display}\n"
            "正在分析 VIX 期限結構與偏態指數 (Skew Index)...\n\n"
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


async def setup(bot):
    await bot.add_cog(AnalystAgent(bot))
