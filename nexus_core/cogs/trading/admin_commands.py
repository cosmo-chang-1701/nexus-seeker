"""
cogs/trading/admin_commands.py

[Admin] 管理員手動觸發指令：sync、force_scan、force_after_report、force_macro_update。
"""

from typing import TYPE_CHECKING, Any, Optional
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

if TYPE_CHECKING:
    from services.macro_refresh_service import MacroRefreshResult

logger = logging.getLogger(__name__)


class AdminCommandsCog(commands.Cog):
    """管理員手動觸發指令集合。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @commands.command(name="sync")
    async def sync(
        self, ctx: commands.Context[Any], spec: Optional[str] = None
    ) -> None:
        """[Admin] 手動同步 Slash Commands。

        用法:
          !sync         - 全局同步
          !sync guild   - 複製全局指令並同步至當前伺服器 (或 !sync ~)
          !sync clear   - 清除當前伺服器專屬指令並同步
        """
        if ctx.author.id != DISCORD_ADMIN_USER_ID:
            await ctx.reply(
                embed=create_error_embed(
                    "權限不足：此指令僅限管理員使用。", title="權限錯誤"
                )
            )
            logger.warning(
                f"Unauthorized sync attempt by {ctx.author.name} ({ctx.author.id})"
            )
            return

        logger.info(
            f"Admin {ctx.author.name} ({ctx.author.id}) triggered sync with spec={spec}"
        )

        try:
            if spec is None:
                synced = await ctx.bot.tree.sync()
                mode_desc = "全局"
            elif spec in ("guild", "~"):
                if ctx.guild is None:
                    await ctx.reply(
                        embed=create_error_embed(
                            "此指令必須在伺服器（Guild）中執行。", title="執行錯誤"
                        )
                    )
                    return
                ctx.bot.tree.copy_global_to(guild=ctx.guild)
                synced = await ctx.bot.tree.sync(guild=ctx.guild)
                mode_desc = spec
            elif spec == "clear":
                if ctx.guild is None:
                    await ctx.reply(
                        embed=create_error_embed(
                            "此指令必須在伺服器（Guild）中執行。", title="執行錯誤"
                        )
                    )
                    return
                ctx.bot.tree.clear_commands(guild=ctx.guild)
                synced = await ctx.bot.tree.sync(guild=ctx.guild)
                mode_desc = spec
            else:
                await ctx.reply(
                    embed=create_error_embed(
                        f"未知的同步參數 `{spec}`。可用參數: 無（全局）、`guild` 或 `~`（伺服器）、`clear`（清除伺服器指令）。",
                        title="參數錯誤",
                    )
                )
                return

            await ctx.reply(
                embed=create_info_embed(
                    "系統控制",
                    f"✅ 成功同步 {len(synced)} 個指令（{'全局' if not spec else mode_desc}）",
                )
            )
        except Exception as e:
            logger.error(f"❌ 手動同步指令失敗: {e}")
            await ctx.reply(
                embed=create_error_embed(f"手動同步指令失敗: {e}", title="系統錯誤")
            )

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
        description="[Admin] 立即手動更新大盤總經數據 (GEX、日曆、FedWatch、CPI)",
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

        from services.macro_refresh_service import refresh_macro_data

        result = await refresh_macro_data()
        await interaction.followup.send(
            embed=_build_macro_refresh_embed(result), ephemeral=True
        )


def _build_macro_refresh_embed(result: "MacroRefreshResult") -> discord.Embed:
    """將共用刷新流程的結果轉為 Discord embed（僅負責呈現）。"""
    updated_block = "\n".join(
        f"- **{step.name}**: {step.message}" for step in result.succeeded
    )
    if result.all_ok:
        return create_info_embed(
            "系統控制", f"✅ 大盤與總經數據更新成功！\n{updated_block}"
        )
    err_msg = "\n".join(
        f"- {step.name} 更新失敗：{step.message}" for step in result.failed
    )
    desc = f"⚠️ 部分大盤總經數據更新失敗：\n{err_msg}"
    if updated_block:
        desc += f"\n\n已成功更新：\n{updated_block}"
    return create_error_embed(desc, title="更新部分失敗")


async def setup(bot: Any) -> None:
    await bot.add_cog(AdminCommandsCog(bot))
