"""
cogs/trading/price_volume_alert_monitor.py

個股 15 分鐘價量突破警報背景排程器 (盤中每 15 分鐘執行一次)。
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from discord.ext import commands, tasks

import database
import market_time
from database.price_volume_watch import PriceVolumeWatch, get_all_watches
from market_analysis.price_volume_alert import (
    Confirmed15mBar,
    evaluate_watch_trigger,
    get_confirmed_15m_bar,
)
from cogs.embed_builders.alert_embeds import create_price_volume_alert_embed

logger = logging.getLogger(__name__)


class PriceVolumeAlertMonitorCog(commands.Cog, name="PriceVolumeAlertMonitorCog"):
    """個股 15 分鐘價量突破警報背景排程器。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.price_volume_alert_monitor.start()

    async def cog_unload(self) -> None:
        self.price_volume_alert_monitor.cancel()

    @tasks.loop(minutes=15)
    async def price_volume_alert_monitor(self) -> None:
        """每 15 分鐘價量突破監控主循環，僅於盤中執行。"""
        if not getattr(self.bot, "_is_leader_instance", True):
            return
        if not market_time.is_market_open():
            return

        try:
            await self._evaluate_price_volume_alerts()
        except Exception as e:
            logger.error(f"📊 [價量監測] 執行失敗: {e}", exc_info=True)

    @price_volume_alert_monitor.before_loop
    async def before_price_volume_alert_monitor(self) -> None:
        await self.bot.wait_until_ready()
        logger.info("📊 個股 15 分鐘價量突破警報監控器已啟動，盤中每 15 分鐘執行一次。")

    async def _evaluate_price_volume_alerts(self) -> None:
        """評估所有使用者的價量監測設定並觸發警報。"""
        all_watches: List[PriceVolumeWatch] = get_all_watches()
        if not all_watches:
            return

        unique_symbols = {w.symbol for w in all_watches}
        bar_cache: Dict[str, Optional[Confirmed15mBar]] = {}
        for symbol in unique_symbols:
            bar_cache[symbol] = await get_confirmed_15m_bar(symbol)

        today_str = datetime.now(market_time.ny_tz).strftime("%Y%m%d")

        for watch in all_watches:
            bar = bar_cache.get(watch.symbol)
            if bar is None:
                continue

            if not database.is_notification_enabled(
                watch.user_id, "alpha_price_volume_watch"
            ):
                continue

            if not evaluate_watch_trigger(
                bar, watch.target_price, watch.direction, watch.volume_multiplier
            ):
                continue

            cache_key = f"price_volume_alert_{watch.user_id}_{watch.symbol}_{today_str}"
            if database.get_kv_cache(cache_key):
                continue  # 每日每標的只觸發一次，避免震盪重複洗版

            try:
                embed = create_price_volume_alert_embed(watch, bar)
                await self.bot.queue_dm(watch.user_id, embed=embed)
                await database.save_kv_cache(cache_key, 1)

                logger.warning(
                    f"📊 [價量監測] 已發送 {watch.symbol} 警報給使用者 {watch.user_id} "
                    f"(收盤: ${bar.close:.2f}, 目標: ${watch.target_price:.2f}, "
                    f"方向: {watch.direction.value})"
                )
            except Exception as e:
                logger.error(
                    f"📊 [價量監測] 發送失敗 (uid: {watch.user_id}, symbol: {watch.symbol}): {e}"
                )


async def setup(bot: Any) -> None:
    await bot.add_cog(PriceVolumeAlertMonitorCog(bot))
