"""
cogs/trading/fundamental_filing_monitor.py

自動化每日 SEC 財報 (10-K/10-Q/8-K) 掃描器。

每日 08:00 ET 掃描所有使用者的現貨持倉 (HOLDING) 標的，偵測是否有新的 SEC
申報。若偵測到新申報，自動送入既有的 form-type-aware LLM 護城河判讀流程
(`DynamicRolloverEngine.evaluate_fundamental_thesis`)；只有判定「基本面假設
破滅」時才主動 DM 持有該標的的使用者，其餘結果靜默寫入 `fundamental_cache`，
避免雜訊轟炸。刻意只掃持倉、不掃 watchlist，且僅每日一次，以控制 LLM/API
成本並符合 1GB VPS 的記憶體安全要求。
"""

from typing import Any
import asyncio
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from discord.ext import tasks, commands

import database
import market_time

ny_tz = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)


class FundamentalFilingMonitorCog(commands.Cog, name="FundamentalFilingMonitorCog"):
    """每日自動化 SEC 財報掃描排程器。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.fundamental_filing_scan.start()

    async def cog_unload(self) -> None:
        self.fundamental_filing_scan.cancel()

    @tasks.loop(time=time(hour=8, minute=0, tzinfo=ny_tz))
    async def fundamental_filing_scan(self) -> None:
        """08:00 ET：自動掃描持倉標的之新 SEC 申報。"""
        if not getattr(self.bot, "_is_leader_instance", True):
            return

        now_ny = datetime.now(ny_tz)
        today = now_ny.date()
        schedule = market_time.nyse_calendar.schedule(start_date=today, end_date=today)
        if schedule.empty:
            return

        from services.llm_service import is_memory_safe

        if not is_memory_safe():
            logger.warning(
                "📜 [SEC 財報掃描] 記憶體水位過高 (RAM > 85%)，跳過本次排程。"
            )
            return

        logger.info("📜 [SEC 財報掃描] 開始每日持倉財報自動掃描...")
        try:
            await self._scan_holdings_for_new_filings()
        except Exception as e:
            logger.error(f"📜 [SEC 財報掃描] 執行失敗: {e}", exc_info=True)

    @fundamental_filing_scan.before_loop
    async def before_fundamental_filing_scan(self) -> None:
        await self.bot.wait_until_ready()
        logger.info(
            "📜 SEC 財報自動掃描器已啟動，每日 08:00 ET 執行一次 (僅限持倉標的)。"
        )

    async def _scan_holdings_for_new_filings(self) -> None:
        """收集所有使用者持倉標的，逐一比對是否有新 SEC 申報並觸發 LLM 判讀。"""
        holdings = await asyncio.to_thread(database.get_all_holdings)

        holders_by_symbol: dict[str, set[int]] = {}
        for h in holdings:
            symbol = str(h["symbol"]).upper()
            holders_by_symbol.setdefault(symbol, set()).add(int(h["user_id"]))

        if not holders_by_symbol:
            logger.info("📜 [SEC 財報掃描] 無任何使用者持倉，跳過。")
            return

        sem = asyncio.Semaphore(3)

        async def _throttled_scan(symbol: str, holder_ids: set[int]) -> None:
            async with sem:
                try:
                    await self._scan_one_symbol(symbol, holder_ids)
                except Exception as e:
                    logger.error(f"📜 [SEC 財報掃描] {symbol} 掃描失敗: {e}")

        await asyncio.gather(
            *(
                _throttled_scan(symbol, holder_ids)
                for symbol, holder_ids in holders_by_symbol.items()
            ),
            return_exceptions=True,
        )
        logger.info(
            f"✅ [SEC 財報掃描] 完成，共檢查 {len(holders_by_symbol)} 個持倉標的。"
        )

    async def _scan_one_symbol(self, symbol: str, holder_ids: set[int]) -> None:
        from services.fundamental_service import (
            get_fundamental_context,
            get_fundamental_reports_list,
        )
        from database.market_cache import (
            get_fundamental_scan_state,
            save_fundamental_scan_state,
        )
        from market_analysis.dynamic_rollover import DynamicRolloverEngine
        from cogs.embed_builders.rollover_embeds import build_fundamental_broken_embed

        reports = await get_fundamental_reports_list(symbol)
        if not reports:
            return

        # SEC EDGAR submissions API 的 "recent" 清單本身已按申報日期新到舊排序，
        # 第一筆即為最新申報。
        latest = reports[0]
        accession_number = latest.get("accession_number")
        if not accession_number:
            return

        state = await asyncio.to_thread(get_fundamental_scan_state, symbol)
        if state and state.get("last_accession_number") == accession_number:
            return  # 無新申報

        context = await get_fundamental_context(
            symbol, accession_number=accession_number
        )
        if not context or "text" not in context or not context.get("text"):
            return

        engine = DynamicRolloverEngine()
        combined_text = f"[SEC 財報段落]:\n{context['text']}\n"
        result = await engine.evaluate_fundamental_thesis(
            symbol,
            combined_text,
            form_type=context.get("form_type", ""),
            sections=context.get("sections", {}),
        )

        if result is None:
            # LLM 呼叫失敗或記憶體防禦觸發，不更新游標，讓下次排程重試。
            logger.warning(f"📜 [SEC 財報掃描] {symbol} LLM 判讀失敗，保留游標待重試。")
            return

        await asyncio.to_thread(
            save_fundamental_scan_state,
            symbol,
            accession_number,
            context.get("form_type", latest.get("form", "")),
        )

        if not result.is_broken:
            return  # 判讀通過，靜默寫入 fundamental_cache 即可，不主動打擾使用者

        source_url = context.get("source_url", "")
        source_info = f"\n\n🔗 參照資料來源: {source_url}" if source_url else ""
        embed = build_fundamental_broken_embed(symbol, result.reasoning + source_info)
        setattr(embed, "_view", f"RolloverActionView:{symbol}")

        for user_id in holder_ids:
            if not database.is_notification_enabled(
                user_id, "defense_fundamental_thesis"
            ):
                continue
            await self.bot.queue_dm(user_id, embed=embed)
            logger.warning(
                f"📜 [SEC 財報掃描] 偵測到 {symbol} 基本面假設破滅，已通知使用者 {user_id}"
            )


async def setup(bot: Any) -> None:
    await bot.add_cog(FundamentalFilingMonitorCog(bot))
