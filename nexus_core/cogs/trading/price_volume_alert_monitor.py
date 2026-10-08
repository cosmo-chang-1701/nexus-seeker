"""
cogs/trading/price_volume_alert_monitor.py

個股 15 分鐘價量突破警報背景排程器 (盤中於 :02/:17/:32/:47 執行)。
"""

import asyncio
import logging
from datetime import datetime, time
from typing import Any, Dict, List, Optional

from discord.ext import commands, tasks

import database
import market_time
from services.notification_dispatcher import notify
from services.notification_dispatch_recorder import (
    DispatchRecord,
    flush_dispatch_records,
)
from database.price_volume_watch import (
    PriceVolumeWatch,
    WatchDirection,
    get_all_watches,
)
from market_analysis.price_volume_alert import (
    Confirmed15mBar,
    evaluate_watch_trigger,
    get_confirmed_15m_bar,
)
from cogs.embed_builders.alert_embeds import create_price_volume_alert_embed

logger = logging.getLogger(__name__)

# 觸發時間點：每小時 :02/:17/:32/:47（ET）。
# 為什麼：15m K 棒於 :00/:15/:30/:45 收盤，Yahoo 需約 1~2 分鐘定案，
# 配合 history_cache_expiry 的 bar 對齊快取（收盤 + 60 秒寬限），:02 起即可讀到
# 已定案的 K 棒；同時與 :00 巡邏、:05 持倉監控錯開，避免同一瞬間搶 Yahoo 預算。
_pv_alert_times: list[time] = [
    time(hour=h, minute=m, tzinfo=market_time.ny_tz)
    for h in range(24)
    for m in (2, 17, 32, 47)
]


class PriceVolumeAlertMonitorCog(commands.Cog, name="PriceVolumeAlertMonitorCog"):
    """個股 15 分鐘價量突破警報背景排程器。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.price_volume_alert_monitor.start()

    async def cog_unload(self) -> None:
        self.price_volume_alert_monitor.cancel()

    @tasks.loop(time=_pv_alert_times)
    async def price_volume_alert_monitor(self) -> None:
        """價量突破監控主循環 (:02/:17/:32/:47 ET)，僅於盤中執行。"""
        if not getattr(self.bot, "_is_leader_instance", True):
            return
        if not market_time.is_market_open():
            return

        try:
            await self._evaluate_price_volume_alerts()
        except Exception as e:
            logger.error(f"📊 [價量監測] 執行失敗: {e}", exc_info=True)
        finally:
            await flush_dispatch_records()

    @price_volume_alert_monitor.before_loop
    async def before_price_volume_alert_monitor(self) -> None:
        await self.bot.wait_until_ready()
        logger.info(
            "📊 個股 15 分鐘價量突破警報監控器已啟動，盤中於 :02/:17/:32/:47 (ET) 執行。"
        )

    async def _evaluate_price_volume_alerts(self) -> None:
        """評估所有使用者的價量監測設定並觸發警報。"""
        all_watches: List[PriceVolumeWatch] = get_all_watches()
        if not all_watches:
            return

        unique_symbols: set[str] = {w.symbol for w in all_watches}
        bar_cache: Dict[str, Optional[Confirmed15mBar]] = {}
        sem = asyncio.Semaphore(3)

        async def _fetch_one_bar(sym: str) -> tuple[str, Optional[Confirmed15mBar]]:
            async with sem:
                try:
                    bar = await get_confirmed_15m_bar(sym)
                    return sym, bar
                except Exception as ex:
                    logger.error(f"Failed to get confirmed 15m bar for {sym}: {ex}")
                    return sym, None

        fetched_bars = await asyncio.gather(
            *[_fetch_one_bar(s) for s in sorted(unique_symbols)]
        )
        for s, bar in fetched_bars:
            bar_cache[s] = bar

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
                # 向上突破以「1 單位進場」、向下跌破以「持倉出場」衡量照做的效果
                await notify(
                    self.bot,
                    watch.user_id,
                    "alpha_price_volume_watch",
                    embed=embed,
                    record=DispatchRecord(
                        symbol=watch.symbol,
                        signal_kind=(
                            "ENTRY"
                            if watch.direction == WatchDirection.ABOVE
                            else "EXIT"
                        ),
                        scenario="PRICE_VOLUME_BREAKOUT",
                        action=watch.direction.value.upper(),
                        price=bar.close,
                    ),
                )
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
