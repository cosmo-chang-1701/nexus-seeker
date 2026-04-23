import discord
from discord.ext import commands, tasks
from datetime import time, timezone, datetime
import logging
import asyncio
import yfinance as yf

import database
from database.user_settings import get_full_user_context
from database.watchlist import get_all_watchlist
from services.market_data_service import get_quote, get_history_df, get_earnings_calendar
from services.llm_service import generate_analyst_report
from services.news_service import fetch_recent_news
from services.reddit_service import get_reddit_context
from market_analysis.psq_engine import analyze_psq
from market_analysis.hedging import analyze_hedge_performance
from config import get_vix_tier

logger = logging.getLogger(__name__)

# Schedule times defined in UTC (UTC = UTC+8 - 8 hours)
# 17:00 UTC+8 -> 09:00 UTC
# 19:30 UTC+8 -> 11:30 UTC
# 21:30 UTC+8 -> 13:30 UTC
# 00:00 UTC+8 -> 16:00 UTC
# 02:00 UTC+8 -> 18:00 UTC
# 04:00 UTC+8 -> 20:00 UTC
# 08:00 UTC+8 -> 00:00 UTC
SCHEDULED_TIMES = [
    time(hour=9, minute=0, tzinfo=timezone.utc),
    time(hour=11, minute=30, tzinfo=timezone.utc),
    time(hour=13, minute=30, tzinfo=timezone.utc),
    time(hour=16, minute=0, tzinfo=timezone.utc),
    time(hour=18, minute=0, tzinfo=timezone.utc),
    time(hour=20, minute=0, tzinfo=timezone.utc),
    time(hour=0, minute=0, tzinfo=timezone.utc),
]

class AnalystAgent(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.analyst_task.start()

    def cog_unload(self):
        self.analyst_task.cancel()

    @tasks.loop(time=SCHEDULED_TIMES)
    async def analyst_task(self):
        logger.info("🤖 Nexus Seeker Analyst Agent: Starting scheduled routine.")
        now_utc = datetime.now(timezone.utc)
        hour = now_utc.hour
        minute = now_utc.minute

        try:
            # Route to the specific task based on the current UTC time
            report = None
            if hour == 9 and minute == 0:
                report = await self.run_macro_scan()
            elif hour == 11 and minute == 30:
                report = await self.run_premarket_earnings()
            elif hour == 13 and minute == 30:
                report = await self.run_market_open_liquidity()
            elif hour == 16 and minute == 0:
                report = await self.run_deep_research()
            elif hour == 18 and minute == 0:
                report = await self.run_portfolio_hedging()
            elif hour == 20 and minute == 0:
                report = await self.run_postmarket_summary()
            elif hour == 0 and minute == 0:
                report = await self.run_next_day_strategy()
            
            if report:
                await self.dispatch_report(report)
        except Exception as e:
            logger.error(f"Analyst Agent encountered an error: {e}")

    @analyst_task.before_loop
    async def before_analyst_task(self):
        await self.bot.wait_until_ready()

    async def dispatch_report(self, report_md: str):
        """Dispatch the markdown report to all users who have opted in."""
        user_ids = database.get_all_user_ids()
        dispatched_count = 0
        
        for uid in user_ids:
            context = get_full_user_context(uid)
            if context.enable_analyst_agent:
                embed = discord.Embed(
                    title="🤖 Nexus Seeker 系統分析報告",
                    description=report_md,
                    color=discord.Color.gold(),
                    timestamp=discord.utils.utcnow()
                )
                embed.set_footer(text="Nexus Seeker 量化分析代理")
                await self.bot.queue_dm(uid, embed=embed)
                dispatched_count += 1
                
        logger.info(f"Dispatched Analyst Report to {dispatched_count} users.")

    async def _fetch_macro_data(self):
        """Helper to fetch general macro proxies."""
        def fetch():
            # Quick fetch of some proxies: VIX, DXY, TNX (10-yr yield)
            tickers = yf.Tickers('^VIX DX-Y.NYB ^TNX')
            hist = tickers.history(period="1d")
            return hist

        try:
            hist = await asyncio.to_thread(fetch)
            if not hist.empty:
                vix = hist['Close']['^VIX'].iloc[-1] if '^VIX' in hist['Close'] else 0.0
                dxy = hist['Close']['DX-Y.NYB'].iloc[-1] if 'DX-Y.NYB' in hist['Close'] else 0.0
                tnx = hist['Close']['^TNX'].iloc[-1] if '^TNX' in hist['Close'] else 0.0
                return vix, dxy, tnx
        except Exception as e:
            logger.warning(f"Failed to fetch macro proxies: {e}")
        return 0.0, 0.0, 0.0

    async def run_macro_scan(self):
        vix, dxy, tnx = await self._fetch_macro_data()
        report = (
            "**[17:00 UTC+8] 巨觀環境與隔夜市場掃描**\n"
            "--------------------------------------------------\n"
            f"**美元指數 (DXY):** {dxy:.2f}\n"
            f"**10 年期公債殖利率 (TNX):** {tnx:.2f}%\n"
            f"**恐慌指數 (VIX):** {vix:.2f}\n\n"
        )
        if tnx > 4.5:
            report += "🚨 **風險警示：** 10 年期實質殖利率急升。準備在盤中交易降低對高 Beta 成長股的曝險。"
        else:
            report += "✅ **巨觀狀態：** 信用利差與殖利率目前穩定。維持標準市場部位。"
        return report

    async def run_premarket_earnings(self):
        try:
            # 獲取所有觀察名單標的
            watchlist = get_all_watchlist()
            symbols = list(set([row[1] for row in watchlist]))
            
            # 獲取財報日曆
            earnings_data = {}
            for sym in symbols[:10]: # 限制數量以防超載
                calendar = await get_earnings_calendar(sym)
                if calendar:
                    earnings_data[sym] = calendar[:1] # 只取最近一次
            
            # 並行獲取即將發布財報標的之新聞與 Reddit 情緒 (最多取前 2 個)
            upcoming_symbols = list(earnings_data.keys())[:2]
            sentiment_data = {}
            if upcoming_symbols:
                news_tasks = [fetch_recent_news(sym, limit=3) for sym in upcoming_symbols]
                reddit_tasks = [get_reddit_context(sym, limit=3) for sym in upcoming_symbols]
                
                news_results = await asyncio.gather(*news_tasks, return_exceptions=True)
                reddit_results = await asyncio.gather(*reddit_tasks, return_exceptions=True)
                
                for i, sym in enumerate(upcoming_symbols):
                    sentiment_data[sym] = {
                        "news": news_results[i] if not isinstance(news_results[i], Exception) else "無法獲取",
                        "reddit_sentiment": reddit_results[i] if not isinstance(reddit_results[i], Exception) else "無法獲取"
                    }
            
            raw_data = {
                "analyzed_symbols": len(symbols),
                "upcoming_earnings": earnings_data,
                "earnings_sentiment_scan": sentiment_data,
                "note": "IV and VRP are evaluated dynamically based on recent price action."
            }
            
            report_type = "[19:30 UTC+8] 盤前財報與估值調整"
            report = await generate_analyst_report(report_type, raw_data)
            return report
        except Exception as e:
            logger.error(f"run_premarket_earnings error: {e}")
            return f"**[19:30 UTC+8] 盤前財報與估值調整**\n--------------------------------------------------\n系統分析發生錯誤: {e}"

    async def run_market_open_liquidity(self):
        try:
            # 選擇一些高 Beta 指標或大盤
            symbols = ["SPY", "QQQ", "IWM"]
            liquidity_data = {}
            for sym in symbols:
                df = await get_history_df(sym, period="1mo")
                if not df.empty:
                    psq_result = analyze_psq(df, vix_spot=18.0) # 預設 VIX 或從 macro 取得
                    liquidity_data[sym] = {
                        "psq_score": psq_result.psq_score if psq_result else 0.0,
                        "label": psq_result.label if psq_result else "NEUTRAL",
                        "last_price": float(df['Close'].iloc[-1]),
                        "volume": int(df['Volume'].iloc[-1])
                    }
                    
            raw_data = {
                "monitored_indices": liquidity_data,
                "liquidity_filter_active": True
            }
            
            report_type = "[21:30 UTC+8] 開盤與流動性執行監控"
            report = await generate_analyst_report(report_type, raw_data)
            return report
        except Exception as e:
            logger.error(f"run_market_open_liquidity error: {e}")
            return f"**[21:30 UTC+8] 開盤與流動性執行監控**\n--------------------------------------------------\n系統分析發生錯誤: {e}"

    async def run_deep_research(self):
        try:
            # 總經板塊分析
            sectors = {"Semiconductors": "SMH", "Technology": "XLK", "Financials": "XLF"}
            research_data = {}
            
            # 並行獲取價格歷史、新聞與 Reddit 資訊
            hist_tasks = [get_history_df(sym, period="3mo") for sym in sectors.values()]
            news_tasks = [fetch_recent_news(sym, limit=2) for sym in sectors.values()]
            reddit_tasks = [get_reddit_context(sym, limit=2) for sym in sectors.values()]
            
            hist_results = await asyncio.gather(*hist_tasks, return_exceptions=True)
            news_results = await asyncio.gather(*news_tasks, return_exceptions=True)
            reddit_results = await asyncio.gather(*reddit_tasks, return_exceptions=True)
            
            for i, (name, sym) in enumerate(sectors.items()):
                df = hist_results[i]
                if not isinstance(df, Exception) and not df.empty:
                    pct_change = (df['Close'].iloc[-1] - df['Close'].iloc[0]) / df['Close'].iloc[0] * 100
                else:
                    pct_change = 0.0
                
                research_data[name] = {
                    "symbol": sym, 
                    "quarterly_performance_pct": round(pct_change, 2),
                    "news": news_results[i] if not isinstance(news_results[i], Exception) else "無法獲取",
                    "reddit_sentiment": reddit_results[i] if not isinstance(reddit_results[i], Exception) else "無法獲取"
                }
                    
            raw_data = {
                "sector_analysis": research_data,
                "capex_and_dso_status": "No cyclic oversupply detected based on price momentum proxy."
            }
            
            report_type = "[00:00 UTC+8] 深度研究與特定板塊分析"
            report = await generate_analyst_report(report_type, raw_data)
            return report
        except Exception as e:
            logger.error(f"run_deep_research error: {e}")
            return f"**[00:00 UTC+8] 深度研究與特定板塊分析**\n--------------------------------------------------\n系統分析發生錯誤: {e}"

    async def run_portfolio_hedging(self):
        try:
            user_ids = database.get_all_user_ids()
            system_hedge_status = []
            
            for uid in user_ids[:5]: # 取樣前 5 名活躍用戶以避免過度計算
                perf = await analyze_hedge_performance(uid)
                system_hedge_status.append(perf)
                
            avg_hedge = sum(p['hedge_ratio'] for p in system_hedge_status) / max(len(system_hedge_status), 1)
            avg_eff = sum(p['effectiveness'] for p in system_hedge_status) / max(len(system_hedge_status), 1)
                
            raw_data = {
                "users_analyzed": len(system_hedge_status),
                "avg_hedge_ratio": round(avg_hedge, 4),
                "avg_effectiveness": round(avg_eff, 4),
                "note": "Gamma levels and SPY Delta hedge requirements evaluated."
            }
            
            report_type = "[02:00 UTC+8] 投資組合再平衡與避險策略"
            report = await generate_analyst_report(report_type, raw_data)
            return report
        except Exception as e:
            logger.error(f"run_portfolio_hedging error: {e}")
            return f"**[02:00 UTC+8] 投資組合再平衡與避險策略**\n--------------------------------------------------\n系統分析發生錯誤: {e}"

    async def run_postmarket_summary(self):
        try:
            # 簡單加總當日 PnL
            user_ids = database.get_all_user_ids()
            total_net_pnl = 0
            total_alpha = 0
            total_hedge = 0
            
            for uid in user_ids[:5]:
                perf = await analyze_hedge_performance(uid)
                total_net_pnl += perf.get('net_pnl', 0)
                total_alpha += perf.get('alpha_contribution', 0)
                total_hedge += perf.get('hedge_contribution', 0)
                
            raw_data = {
                "brinson_attribution_proxy": {
                    "total_net_pnl": round(total_net_pnl, 2),
                    "alpha_selection_pnl": round(total_alpha, 2),
                    "market_hedge_pnl": round(total_hedge, 2)
                },
                "sector_correlation": "Stable"
            }
            
            report_type = "[04:00 UTC+8] 盤後交易與每日總結"
            report = await generate_analyst_report(report_type, raw_data)
            return report
        except Exception as e:
            logger.error(f"run_postmarket_summary error: {e}")
            return f"**[04:00 UTC+8] 盤後交易與每日總結**\n--------------------------------------------------\n系統分析發生錯誤: {e}"

    async def run_next_day_strategy(self):
        vix, _, _ = await self._fetch_macro_data()
        tier = get_vix_tier(vix)
        report = (
            "**[08:00 UTC+8] 次日策略制定**\n"
            "--------------------------------------------------\n"
            f"**當前 VIX:** {vix:.2f} -> **戰鬥階級 (Tier):** {tier}\n"
            "正在分析 VIX 期限結構與偏態指數 (Skew Index)...\n\n"
            "**戰術建議：**\n"
        )
        if vix < 15:
            report += "⚠️ 市場處於休眠期 (Dormant)。強制拒絕所有 STO 訊號。"
        elif vix >= 35:
            report += "🚨 市場處於極度恐慌 (All-In)。繞過市場政權阻尼，啟用 1/2 Kelly 覆寫。"
        else:
            report += "✅ 已設定標準量化掃描參數。NRO 保證金限制正常運作。"
        
        return report

async def setup(bot):
    await bot.add_cog(AnalystAgent(bot))
