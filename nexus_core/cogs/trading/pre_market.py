"""
cogs/trading/pre_market.py

盤前財報風險警報 (09:00 ET) 及盤前預熱邏輯。
"""

from typing import Any
import asyncio
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from discord.ext import tasks, commands

import database
import market_time
from services.trading_service import TradingService

ny_tz = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)


class PreMarketCog(commands.Cog):
    """盤前財報警報排程與預熱邏輯。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.trading_service = TradingService(bot)
        self.pre_market_risk_monitor.start()

    async def cog_unload(self) -> None:
        self.pre_market_risk_monitor.cancel()

    @tasks.loop(time=time(hour=8, minute=45, tzinfo=ny_tz))
    async def pre_market_risk_monitor(self) -> None:
        """08:45：盤前預熱與量化快取計算 (依使用者分發私訊/預熱快取)"""
        if not getattr(self.bot, "_is_leader_instance", True):
            return
        now_ny = datetime.now(ny_tz)
        today = now_ny.date()

        schedule = market_time.nyse_calendar.schedule(start_date=today, end_date=today)
        if schedule.empty:
            return

        logger.info("Starting pre_market_risk_monitor task.")

        # 補寫前一交易日的情緒指標日級快照：昨天 16:15 若因重啟、部署或 leader
        # 切換而漏跑，這裡以同一個冪等函式補上（已存在的列不會被覆寫）。必須在
        # 預熱之前執行——預熱會寫入今天的盤前觀測，雖然不影響昨天的時段篩選，
        # 但先補寫可讓預熱讀到的百分位母體包含昨天。
        try:
            from market_analysis.sentiment.canonical_history import (
                snapshot_trading_day,
            )

            await snapshot_trading_day(
                market_time.get_last_completed_trading_date(), source="SELF_HEAL"
            )
        except Exception as e:
            logger.error(f"情緒指標日級快照補寫失敗: {e}")

        # 投組下行風險：盤中檢查只使用快取的報酬序列、不抓歷史資料，因此在盤前
        # 先為所有有部位的使用者建好序列（重啟後的第一個交易日也能盤中檢查）
        try:
            from services.downside_risk_service import warm_downside_series

            warmed = await warm_downside_series(self.bot)
            logger.info(f"投組下行風險序列預熱完成：{warmed} 位使用者")
        except Exception as e:
            logger.error(f"投組下行風險序列預熱失敗: {e}")

        try:
            asyncio.create_task(self._pre_warm_all_targets())
        except Exception as e:
            logger.error(f"盤前掃描執行錯誤: {e}")

    @pre_market_risk_monitor.before_loop
    async def before_pre_market_risk_monitor(self) -> None:
        await self.bot.wait_until_ready()

    async def _pre_warm_all_targets(self) -> None:
        """盤前預熱所有自選標的、持倉標的及掛單標的之量化/期權快取。"""
        logger.info("🚀 [盤前預熱] 開始計算並快取所有相關標的指標...")
        symbols: set[str] = set()
        try:
            all_watch = database.get_all_watchlist()
            for _, sym, _ in all_watch:
                symbols.add(sym.upper())

            from database.holdings import get_all_holdings

            holdings = await asyncio.to_thread(get_all_holdings)
            for h in holdings:
                symbols.add(h["symbol"].upper())

            from database.orders import get_all_active_orders

            orders = await asyncio.to_thread(get_all_active_orders)
            for o in orders:
                symbols.add(o["symbol"].upper())
        except Exception as e:
            logger.error(f"預熱標的清單收集失敗: {e}")

        unique_symbols = sorted(list(symbols))
        logger.info(
            f"[盤前預熱] 收集到 {len(unique_symbols)} 個獨特標的: {unique_symbols}"
        )

        async def _warm_one(sym: Any) -> None:
            try:
                terminal_cog = self.bot.get_cog("UnifiedTerminalCog")
                if terminal_cog and hasattr(terminal_cog, "_fetch_sym_radar_data_slow"):
                    await terminal_cog._fetch_sym_radar_data_slow(sym)
                else:
                    from market_analysis.sentiment_engine import SentimentEngine

                    await SentimentEngine.fetch_and_calculate_iv_metrics(sym)
                    await SentimentEngine.calculate_max_pain(sym)
                    await SentimentEngine.detect_uoa(sym)
            except Exception as err:
                logger.warning(f"[盤前預熱] 標的 {sym} 預熱失敗: {err}")

        sem = asyncio.Semaphore(3)

        async def _throttled_warm(sym: Any) -> None:
            async with sem:
                await _warm_one(sym)

        await asyncio.gather(
            *(_throttled_warm(s) for s in unique_symbols), return_exceptions=True
        )
        logger.info("✅ [盤前預熱] 量化/期權數據快取預熱完成。")


async def setup(bot: Any) -> None:
    await bot.add_cog(PreMarketCog(bot))
