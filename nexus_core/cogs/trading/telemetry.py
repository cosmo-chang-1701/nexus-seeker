"""
cogs/trading/telemetry.py

委託單 Telemetry 對齊警報排程 (盤中每 30 分鐘)。
"""

from typing import Any
import asyncio
import hashlib
import logging
from datetime import datetime

from discord.ext import tasks, commands

import database
import market_time
from services.notification_dispatcher import notify_many
from cogs.embed_builder import create_telemetry_alignment_embeds

logger = logging.getLogger(__name__)


class TelemetryMonitorCog(commands.Cog):
    """委託單 Telemetry 對齊警報獨立 Cog。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.monitor_order_telemetry_alignment_task.start()

    async def cog_unload(self) -> None:
        self.monitor_order_telemetry_alignment_task.cancel()

    @tasks.loop(minutes=30)
    async def monitor_order_telemetry_alignment_task(self) -> None:
        """盤中每 30 分鐘推播委託單 Telemetry 對齊警報（需於 /notif_settings 手動開啟）"""
        if not getattr(self.bot, "_is_leader_instance", True):
            return
        if not market_time.is_market_open():
            return

        try:
            await _dispatch_order_telemetry_alignment_alert(self.bot)
        except Exception as e:
            logger.error(f"委託單 Telemetry 對齊警報執行錯誤: {e}")

    @monitor_order_telemetry_alignment_task.before_loop
    async def before_monitor_order_telemetry_alignment_task(self) -> None:
        await self.bot.wait_until_ready()


async def _dispatch_order_telemetry_alignment_alert(bot: Any) -> None:
    """獨立的委託單 Telemetry 對齊警報發送邏輯，可供其他模組呼叫。"""
    from database.orders import get_all_active_orders
    from services.calendar_service import calendar_service
    from services.order_telemetry_service import (
        build_telemetry_alignment_items,
        resolve_holding_type_and_rows,
    )

    all_orders = await asyncio.to_thread(get_all_active_orders)
    if not all_orders:
        return

    macro_events = await calendar_service.get_high_impact_events(days=14)
    macro_event_dates: set[str] = set()
    for event in macro_events:
        try:
            event_date = datetime.fromisoformat(
                str(event.time).replace("Z", "+00:00")
            ).date()
            macro_event_dates.add(event_date.isoformat())
        except ValueError:
            continue

    orders_by_uid: dict[int, list[dict[str, Any]]] = {}
    for o in all_orders:
        uid = int(o.get("user_id") or 0)
        if uid <= 0:
            continue
        orders_by_uid.setdefault(uid, []).append(o)

    for uid, orders in orders_by_uid.items():
        if not database.is_notification_enabled(uid, "telemetry_orders"):
            continue

        user_holdings = await asyncio.to_thread(database.get_user_holdings, uid)
        user_trades = await asyncio.to_thread(database.get_user_portfolio, uid)
        holding_type, holding_map = resolve_holding_type_and_rows(
            holdings=user_holdings, trades=user_trades
        )

        alignment_items, truncated = await build_telemetry_alignment_items(
            user_id=uid,
            orders=orders,
            holding_type=holding_type,
            holding_map=holding_map,
            macro_event_dates=macro_event_dates,
        )

        filtered_items = []
        for item in alignment_items:
            suggested_price = float(item["suggested_price"])
            suggested_qty = int(item["suggested_qty"])
            current_price = float(item["current_price"])
            original_qty = int(item["original_qty"])

            if (
                abs(suggested_price - current_price) >= 0.01
                or suggested_qty != original_qty
            ):
                filtered_items.append(item)

        if not filtered_items:
            continue

        embeds = create_telemetry_alignment_embeds(
            filtered_items,
            truncated=truncated,
            include_apply_button_hint=False,
            scheduled_mode=True,
        )
        # 同一組建議（掛單 × 建議價 × 建議量）當日只推一次；過去條件成立期間
        # 每 30 分鐘重發相同內容。建議價或量一有變動就是新的簽章、正常推播。
        signature = hashlib.sha256(
            repr(
                sorted(
                    (
                        str(item.get("order_id", item.get("symbol", ""))),
                        round(float(item["suggested_price"]), 2),
                        int(item["suggested_qty"]),
                    )
                    for item in filtered_items
                )
            ).encode("utf-8")
        ).hexdigest()[:12]
        today_str = datetime.now(market_time.ny_tz).strftime("%Y%m%d")
        await notify_many(
            bot,
            uid,
            "telemetry_orders",
            embeds,
            dedup_key=f"telemetry_align_{uid}_{today_str}_{signature}",
        )


async def setup(bot: Any) -> None:
    await bot.add_cog(TelemetryMonitorCog(bot))
