"""
cogs/trading/scanner_commands.py

DDP 掃描與 IV 優勢掃描的 Discord slash commands。
"""

from typing import Any
import logging

import discord
from discord.ext import commands
from discord import app_commands

import database
from cogs.embed_builder import create_info_embed

logger = logging.getLogger(__name__)


class ScannerCommandsCog(commands.Cog):
    """DDP / IV 掃描指令集合。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        from services.trading_service import TradingService

        self.trading_service = TradingService(bot)

    @app_commands.command(
        name="ddp_scan", description="立即對觀察清單執行 Davis Double Play (DDP) 掃描"
    )
    async def ddp_scan(self, interaction: discord.Interaction) -> Any:
        await interaction.response.defer(ephemeral=False)
        all_watchlists = database.get_all_watchlist()
        symbols = sorted(list(set(row[1] for row in all_watchlists)))

        if not symbols:
            await interaction.followup.send(
                embed=create_info_embed(
                    "觀察清單", "📭 觀察清單為空，無法執行 DDP 掃描。"
                )
            )
            return

        results = await self.trading_service.run_ddp_scan(symbols)
        if not results:
            await interaction.followup.send(
                embed=create_info_embed(
                    "DDP 掃描結果",
                    "🔎 掃描完成，目前沒有符合 Davis Double Play (DDP) 條件的標的。",
                )
            )
            return

        for report in results:
            from cogs.embed_builder import create_ddp_embed

            embed = create_ddp_embed(report)
            await interaction.followup.send(embed=embed)
            await self.trading_service.ddp_inspector.record_signal(report)

    @app_commands.command(
        name="iv_scan", description="立即對觀察清單執行波動率優勢 (Cheap IV) 偵測"
    )
    async def iv_scan(self, interaction: discord.Interaction) -> Any:
        await interaction.response.defer(ephemeral=False)
        all_watchlists = database.get_all_watchlist()
        uids = sorted(list(set(row[0] for row in all_watchlists)))

        if not uids:
            await interaction.followup.send(
                embed=create_info_embed(
                    "觀察清單", "📭 觀察清單為空，無法執行 IV 掃描。"
                )
            )
            return

        found_any = False
        for uid in uids:
            user_watch = [row[1] for row in all_watchlists if row[0] == uid]
            results = await self.trading_service.run_iv_opportunity_scan(
                user_watch, uid
            )

            for report in results:
                from cogs.embed_builder import create_volatility_embed

                embed = create_volatility_embed(report)
                if interaction.user.id == uid:
                    await interaction.followup.send(embed=embed)
                else:
                    await self.bot.queue_dm(uid, embed=embed)
                found_any = True

        if not found_any:
            await interaction.followup.send(
                embed=create_info_embed(
                    "IV 掃描結果",
                    "🔎 掃描完成，目前沒有符合波動率優勢 (Cheap IV) 條件的標的。",
                )
            )


async def setup(bot: Any) -> None:  # type: ignore
    await bot.add_cog(ScannerCommandsCog(bot))
