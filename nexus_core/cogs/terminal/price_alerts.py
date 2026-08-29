"""個股 15 分鐘價量突破警報的新增、列表與移除指令邏輯。"""

from typing import Any

import discord
from discord import app_commands

from cogs.embed_builder import create_error_embed, create_info_embed


async def price_alert_set_impl(
    interaction: discord.Interaction,
    symbol: str,
    target_price: float,
    direction: app_commands.Choice[str],
    volume_multiplier: float = 1.5,
) -> Any:
    """新增或更新一筆個股 15 分鐘價量突破監測。"""
    await interaction.response.defer(ephemeral=True)
    from database.price_volume_watch import (
        WatchDirection,
        WatchLimitExceededError,
        upsert_watch,
    )
    from pydantic import ValidationError

    try:
        watch = await upsert_watch(
            user_id=interaction.user.id,
            symbol=symbol,
            target_price=target_price,
            direction=WatchDirection(direction.value),
            volume_multiplier=volume_multiplier,
        )
        direction_label = (
            "≥ 向上突破" if watch.direction == WatchDirection.ABOVE else "≤ 向下跌破"
        )
        vol_desc = (
            "不限制 (純價格警報)"
            if watch.volume_multiplier <= 0
            else f"`{watch.volume_multiplier:.2f}x` (相對 20 根 15 分鐘均量)"
        )
        msg = (
            f"✅ **價量監測已設定**\n"
            f"• 標的: `{watch.symbol}`\n"
            f"• 條件: `15分K實體收盤價 {direction_label} ${watch.target_price:.2f}`\n"
            f"• 放量門檻: {vol_desc}\n\n"
            f"💡 盤中每 15 分鐘掃描一次，觸發時將主動私訊通知（需於 `/notif_settings` 開啟"
            f"「個股 15 分鐘價量突破警報」）。"
        )
        await interaction.followup.send(
            embed=create_info_embed(title="系統資訊", message=msg), ephemeral=True
        )
    except (WatchLimitExceededError, ValueError, ValidationError) as e:
        await interaction.followup.send(
            embed=create_error_embed(str(e), title="設定失敗"), ephemeral=True
        )


async def price_alert_list_impl(interaction: discord.Interaction) -> Any:
    """列出使用者目前所有價量突破監測設定。"""
    await interaction.response.defer(ephemeral=True)
    from database.price_volume_watch import WatchDirection, get_user_watches

    watches = get_user_watches(interaction.user.id)
    if not watches:
        await interaction.followup.send(
            embed=create_info_embed(
                title="價量監測清單", message="目前尚未設定任何個股價量突破監測。"
            ),
            ephemeral=True,
        )
        return

    lines = []
    for w in watches:
        direction_label = "≥ 突破" if w.direction == WatchDirection.ABOVE else "≤ 跌破"
        vol_desc = (
            "不限成交量"
            if w.volume_multiplier <= 0
            else f"放量 `{w.volume_multiplier:.2f}x`"
        )
        lines.append(
            f"• `{w.symbol}` {direction_label} `${w.target_price:.2f}` " f"({vol_desc})"
        )

    await interaction.followup.send(
        embed=create_info_embed(title="價量監測清單", message="\n".join(lines)),
        ephemeral=True,
    )


async def price_alert_remove_impl(interaction: discord.Interaction, symbol: str) -> Any:
    """移除使用者一筆價量突破監測設定。"""
    await interaction.response.defer(ephemeral=True)
    from database.price_volume_watch import delete_watch

    removed = await delete_watch(interaction.user.id, symbol)
    normalized_symbol = symbol.strip().upper()
    if removed:
        await interaction.followup.send(
            embed=create_info_embed(
                title="系統資訊",
                message=f"✅ 已移除 `{normalized_symbol}` 的價量監測。",
            ),
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            embed=create_error_embed(
                f"找不到 `{normalized_symbol}` 的價量監測設定。", title="移除失敗"
            ),
            ephemeral=True,
        )
