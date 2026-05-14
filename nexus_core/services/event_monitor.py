import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List

from services.calendar_service import calendar_service
from database.user_settings import get_all_user_ids
import market_time

logger = logging.getLogger(__name__)
ny_tz = ZoneInfo("America/New_York")


class EventMonitor:
    """
    Background monitor for NYSE Dynamic Scheduler.
    Detects upcoming high-impact events and pushes proactive alerts.
    """

    def __init__(self, bot):
        self.bot = bot

    async def check_upcoming_events(self):
        """
        Scan all users' portfolios for upcoming high-impact events.
        """
        if not market_time.is_market_open():
            # Still check even if closed, as we want proactive alerts
            pass

        user_ids = await asyncio.to_thread(get_all_user_ids)

        for uid in user_ids:
            try:
                # 1. Fetch events affecting this user
                events = await calendar_service.get_portfolio_events(uid, days=3)

                # 2. Filter for events within the next 24-72 hours that haven't been alerted
                # For simplicity, we alert if TTE < 48h
                critical_events = [e for e in events if 0 < e.tte_hours < 48.0]

                if critical_events:
                    await self._send_event_alert(uid, critical_events)

            except Exception as e:
                logger.error(f"Error checking events for user {uid}: {e}")

    async def _send_event_alert(self, user_id: int, events: List[Any]):
        """
        Send a proactive hedging alert based on upcoming events.
        """
        import discord

        embed = discord.Embed(
            title="🛡️ 【 預警：重大事件即時防護 】",
            description="偵測到您的持倉標的即將迎來重大波動事件，請留意風險對沖。",
            color=discord.Color.dark_red(),
            timestamp=datetime.now(),
        )

        for event in events:
            if event.type == "ECONOMIC":
                name = f"🔴 經濟數據: {event.event}"
                value = f"距離發布: `{event.tte_hours}` 小時 \n**NRO 指令**: 增加 Vanna 權重，縮減賣方曝險。"
            else:
                name = f"📊 財報預警: {event.symbol}"
                value = f"距離發布: `{event.tte_hours}` 小時 \n**NRO 指令**: 已啟動 IV Crush 防護機制。"

            embed.add_field(name=name, value=value, inline=False)

        embed.set_footer(text="Proactive Event Monitor | Nexus Seeker")
        await self.bot.queue_dm(user_id, embed=embed)


# Helper to start the monitor
async def start_event_monitor(bot):
    monitor = EventMonitor(bot)
    while True:
        await monitor.check_upcoming_events()
        # Check every 4 hours
        await asyncio.sleep(4 * 3600)
