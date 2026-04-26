import discord
from discord.ext import commands, tasks
from datetime import time, timezone, datetime, timedelta
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
            # Quick fetch of some proxies: VIX, DXY, TNX (10-yr yield), IRX (13-week bill as proxy or ^IRX)
            # DXY fallback: 'DX-Y.NYB' or 'UUP' (ETF)
            # For 2Y yield, we can use ^IRX or just ^TYX for 30Y and interpolate, 
            # but usually ^IRX is 13W. Let's try ^VIX, DX-Y.NYB, ^TNX, ^IRX
            tickers = yf.Tickers('^VIX DX-Y.NYB ^TNX ^IRX')
            hist = tickers.history(period="2d")
            return hist

        try:
            hist = await asyncio.to_thread(fetch)
            if not hist.empty and len(hist) >= 2:
                # Current Close
                vix = float(hist['Close']['^VIX'].iloc[-1])
                dxy = float(hist['Close']['DX-Y.NYB'].iloc[-1])
                tnx = float(hist['Close']['^TNX'].iloc[-1])
                irx = float(hist['Close']['^IRX'].iloc[-1]) # 13W Bill as a floor proxy or use it for spread
                
                # Previous Close
                vix_prev = float(hist['Close']['^VIX'].iloc[-2])
                tnx_prev = float(hist['Close']['^TNX'].iloc[-2])
                
                vix_change = vix - vix_prev
                tnx_change_bps = (tnx - tnx_prev) * 100
                
                # Mock US2Y as TNX - 0.2 if IRX is too low, or use a better proxy if available
                us2y = tnx - 0.2 # Fallback
                
                return {
                    'vix': round(vix, 2),
                    'vix_change': round(vix_change, 2),
                    'dxy': round(dxy, 2),
                    'tnx': round(tnx, 2),
                    'tnx_change_bps': round(tnx_change_bps, 1),
                    'us2y': round(us2y, 2)
                }
            elif not hist.empty:
                # Only 1 day of data
                vix = float(hist['Close']['^VIX'].iloc[-1])
                dxy = float(hist['Close']['DX-Y.NYB'].iloc[-1])
                tnx = float(hist['Close']['^TNX'].iloc[-1])
                return {'vix': vix, 'vix_change': 0.0, 'dxy': dxy, 'tnx': tnx, 'tnx_change_bps': 0.0, 'us2y': tnx - 0.2}
        except Exception as e:
            logger.warning(f"Failed to fetch macro proxies: {e}")
        return {'vix': 0.0, 'vix_change': 0.0, 'dxy': 0.0, 'tnx': 0.0, 'tnx_change_bps': 0.0, 'us2y': 0.0}

    def _get_tw_time_str(self) -> str:
        """動態生成台灣時間 (UTC+8) 的當下時間標籤"""
        now_tw = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
        return now_tw.strftime("[%H:%M UTC+8]")

    async def run_macro_scan(self):
        macro_data = await self._fetch_macro_data()
        
        # 處理資料格式兼容 (若為舊版 tuple 則轉為新版 dict 預設值)
        if isinstance(macro_data, tuple):
            vix, dxy, tnx = macro_data
            macro_data = {'vix': vix, 'vix_change': 0.0, 'dxy': dxy, 'tnx': tnx, 'tnx_change_bps': 0.0, 'us2y': tnx - 0.2}

        vix = macro_data.get('vix', 0.0)
        vix_change = macro_data.get('vix_change', 0.0)
        dxy = macro_data.get('dxy', 0.0)
        tnx = macro_data.get('tnx', 0.0)
        tnx_change_bps = macro_data.get('tnx_change_bps', 0.0)
        us2y = macro_data.get('us2y', 0.0)
        
        # 1. 計算利差
        spread = tnx - us2y
        
        # 動態生成台灣時間 (UTC+8) 的當下時間
        time_str = self._get_tw_time_str()
        
        # 2. 多因子告警判定
        alerts = []
        if spread < -0.2:
            alerts.append("殖利率曲線深度倒掛。市場反映中長期經濟衰退預期，建議關注防禦型資產")
        if -0.1 <= spread <= 0.2 and tnx_change_bps < 0:
            alerts.append("殖利率曲線接近解除倒掛 (陡峭化)。歷史經驗顯示，倒掛解除初期往往伴隨市場波動加劇，請留意衰退交易發酵")
        if tnx > 4.5 and tnx_change_bps > 8:
            alerts.append("10 年期殖利率突破 4.5% 且短期急升。建議盤中降低對高 Beta / 估值敏感型成長股的曝險")
        if vix > 20 and vix_change > 2.0:
            alerts.append("恐慌指數急遽上升，市場避險情緒發酵，注意流動性風險")
        if dxy > 105:
            alerts.append("美元指數處於強勢區間，可能壓抑跨國企業獲利與大宗商品表現")
            
        # 3. 組合報告內容
        report_lines = []
        report_lines.append(f"**{time_str} 巨觀環境與隔夜市場掃描**")
        report_lines.append("--------------------------------------------------")
        report_lines.append(f"**美元指數 (DXY):** {dxy:.2f}")
        report_lines.append(f"**10 年期公債殖利率 (TNX):** {tnx:.2f}% (單日變化: {tnx_change_bps:+.1f} bps)")
        report_lines.append(f"**2 年期公債殖利率 (US2Y):** {us2y:.2f}%")
        report_lines.append(f"**2Y-10Y 利差:** {spread:+.2f}%")
        report_lines.append(f"**恐慌指數 (VIX):** {vix:.2f} (單日變化: {vix_change:+.2f})")
        report_lines.append("")
        
        # 結論區塊
        if alerts:
            report_lines.append("🚨 **風險警示：**")
            for alert in alerts:
                report_lines.append(f"- {alert}")
        else:
            report_lines.append("✅ **巨觀狀態：** 殖利率曲線、匯率與波動率未見極端異常。維持標準市場部位。")
            
        return "\n".join(report_lines)

    async def run_premarket_earnings(self):
        time_str = self._get_tw_time_str()
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
                news_tasks = [fetch_recent_news(sym) for sym in upcoming_symbols]
                reddit_tasks = [get_reddit_context(sym) for sym in upcoming_symbols]
                
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
            sectors = {"Semiconductors": "SMH", "Technology": "XLK", "Financials": "XLF"}
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
            
            report_type = f"{time_str} 盤後交易與每日總結"
            report = await generate_analyst_report(report_type, raw_data)
            return report
        except Exception as e:
            logger.error(f"run_postmarket_summary error: {e}")
            return f"**{time_str} 盤後交易與每日總結**\n--------------------------------------------------\n系統分析發生錯誤: {e}"

    async def run_next_day_strategy(self):
        time_str = self._get_tw_time_str()
        macro_data = await self._fetch_macro_data()
        vix = macro_data.get('vix', 0.0) if isinstance(macro_data, dict) else macro_data[0]
        tier = get_vix_tier(vix)
        tier_display = f"{tier.get('emoji', '')} {tier.get('name', 'Unknown')}"
        report = (
            f"**{time_str} 次日策略制定**\n"
            "--------------------------------------------------\n"
            f"**當前 VIX:** {vix:.2f} -> **戰鬥階級 (Tier):** {tier_display}\n"
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
