"""
cogs/trading/admin_commands.py

[Admin] 管理員手動觸發指令：force_scan、force_after_report、force_macro_update。
"""

from typing import Any
import asyncio
import logging

import discord
from discord.ext import commands
from discord import app_commands

from config import DISCORD_ADMIN_USER_ID
from cogs.embed_builder import (
    create_info_embed,
    create_error_embed,
)

logger = logging.getLogger(__name__)


class AdminCommandsCog(commands.Cog):
    """管理員手動觸發指令集合。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @app_commands.command(
        name="force_scan", description="[Admin] 立即手動執行全站掃描 (不論開盤時間)"
    )
    async def force_scan(self, interaction: discord.Interaction) -> Any:
        if not getattr(self.bot, "_is_leader_instance", True):
            await interaction.response.send_message(
                embed=create_info_embed(
                    "系統控制",
                    "⚠️ 目前此實例為 follower（藍綠部署中）。請稍候或重新觸發指令。",
                ),
                ephemeral=True,
            )
            return
        if interaction.user.id != DISCORD_ADMIN_USER_ID:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "權限不足：此指令僅限管理員使用。", title="權限錯誤"
                ),
                ephemeral=True,
            )
            logger.warning(
                f"Unauthorized force_scan attempt by {interaction.user.name} ({interaction.user.id})"
            )
            return

        logger.info(
            f"Admin {interaction.user.name} ({interaction.user.id}) triggered force_scan"
        )
        await interaction.response.send_message(
            embed=create_info_embed("系統控制", "🚀 強制啟動全站掃描中..."),
            ephemeral=True,
        )
        scan_cog = self.bot.get_cog("MarketScanCog")
        if scan_cog:
            asyncio.create_task(
                scan_cog._run_market_scan_logic(
                    is_auto=False, triggered_by=interaction.user
                )
            )
        else:
            logger.error("MarketScanCog not found, cannot execute force_scan.")

    @app_commands.command(
        name="force_after_report",
        description="[Admin] 立即手動執行盤後結算報告 (可選 dry-run)",
    )
    @app_commands.describe(dry_run="true=只做計算與建構，不發送 DM")
    async def force_after_report(
        self, interaction: discord.Interaction, dry_run: bool = True
    ) -> Any:
        if interaction.user.id != DISCORD_ADMIN_USER_ID:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "權限不足：此指令僅限管理員使用。", title="權限錯誤"
                ),
                ephemeral=True,
            )
            logger.warning(
                f"Unauthorized force_after_report attempt by {interaction.user.name} ({interaction.user.id})"
            )
            return

        mode = "DRY-RUN" if dry_run else "SEND"
        logger.info(
            f"Admin {interaction.user.name} ({interaction.user.id}) triggered force_after_report mode={mode}"
        )
        await interaction.response.send_message(
            embed=create_info_embed(
                "系統控制", f"🧪 盤後結算報告手動執行中 (`{mode}`)..."
            ),
            ephemeral=True,
        )

        after_market_cog = self.bot.get_cog("AfterMarketCog")
        if after_market_cog:
            stats = await after_market_cog._run_after_market_report_pipeline(
                dry_run=dry_run, triggered_by=interaction.user
            )
            await interaction.followup.send(
                embed=create_info_embed(
                    "盤後結算報告完成",
                    (
                        f"mode: `{mode}`\n"
                        f"users_total: `{stats['users_total']}`\n"
                        f"users_queued: `{stats['users_queued']}`\n"
                        f"users_skipped: `{stats['users_skipped']}`\n"
                        f"users_failed: `{stats['users_failed']}`"
                    ),
                ),
                ephemeral=True,
            )
        else:
            logger.error("AfterMarketCog not found, cannot execute force_after_report.")

    @app_commands.command(
        name="force_macro_update",
        description="[Admin] 立即手動更新大盤總經數據 (GEX & FedWatch)",
    )
    async def force_macro_update(self, interaction: discord.Interaction) -> Any:
        if interaction.user.id != DISCORD_ADMIN_USER_ID:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "權限不足：此指令僅限管理員使用。", title="權限錯誤"
                ),
                ephemeral=True,
            )
            logger.warning(
                f"Unauthorized force_macro_update attempt by {interaction.user.name} ({interaction.user.id})"
            )
            return

        logger.info(
            f"Admin {interaction.user.name} ({interaction.user.id}) triggered force_macro_update"
        )
        await interaction.response.defer(ephemeral=True)

        from market_analysis.index_microstructure import fetch_gex_metrics
        from services.calendar_service import calendar_service

        errors = []
        gex_info = ""

        # 1. Update GEX, Liquidity, and Dark Pool DIX
        try:
            from market_analysis.index_microstructure import fetch_liquidity_metrics
            from market_analysis.dark_pool_engine import fetch_and_cache_darkpool_dix
            import typing

            results = await asyncio.gather(
                fetch_gex_metrics(),
                fetch_liquidity_metrics(),
                fetch_and_cache_darkpool_dix(),
                return_exceptions=True,
            )
            gex_data = typing.cast(typing.Any, results[0])
            liq_data = typing.cast(typing.Any, results[1])
            _ = typing.cast(typing.Any, results[2])

            if isinstance(gex_data, Exception):
                raise gex_data

            ted_spread: float | str = "Error"
            if not isinstance(liq_data, Exception):
                assert isinstance(liq_data, dict)
                ted_spread = liq_data.get("ted_spread", 0.0)

            assert isinstance(gex_data, dict)
            gex_info = f"SPY: ${gex_data.get('spy_spot', 0.0):.2f} / Flip: {gex_data.get('gamma_flip', 0.0):.2f} / TED Spread: {ted_spread}"
        except Exception as e:
            errors.append(f"GEX & 流動性爬取失敗: {e}")

        # 2. Update FedWatch
        try:
            await calendar_service.update_fedwatch_probability()
        except Exception as e:
            errors.append(f"FedWatch 更新失敗: {e}")

        # 3. Update Macro Calendar (TradingView)
        try:
            await calendar_service.prefetch_monthly_macro_cache(
                months_ahead=1, force_fetch=True
            )
        except Exception as e:
            errors.append(f"總經日曆更新失敗: {e}")

        if errors:
            err_msg = "\n".join(errors)
            await interaction.followup.send(
                embed=create_error_embed(
                    f"⚠️ 部分大盤總經數據更新失敗：\n{err_msg}", title="更新部分失敗"
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=create_info_embed(
                    "系統控制",
                    f"✅ 大盤與總經數據更新成功！\n- **GEX**: {gex_info}\n- **FedWatch**: 已寫入資料庫\n- **Calendar**: Edge Scraper 動態抓取更新完畢",
                ),
                ephemeral=True,
            )


async def setup(bot: Any) -> None:  # type: ignore
    await bot.add_cog(AdminCommandsCog(bot))
