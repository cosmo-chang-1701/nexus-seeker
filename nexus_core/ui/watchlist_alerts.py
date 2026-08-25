from typing import Any
import discord
from typing import List, Callable, Awaitable, Optional
from services.asset_manager import AssetManager
from models.asset import ContextType
from database.market_cache import get_market_cache


class WatchlistAlertModal(discord.ui.Modal):
    def __init__(
        self,
        user_id: int,
        symbol: str,
        on_success_callback: Optional[
            Callable[[discord.Interaction], Awaitable[None]]
        ] = None,
    ):
        super().__init__(title=f"設定 {symbol} 價格警報")
        self.user_id = user_id
        self.symbol = symbol
        self.on_success_callback = on_success_callback

        cache = get_market_cache(symbol)
        default_price = ""
        if cache and cache.get("reference_spot_price"):
            default_price = f"{float(cache['reference_spot_price']):.2f}"

        self.target_price_input: discord.ui.TextInput = discord.ui.TextInput(
            label="目標價",
            style=discord.TextStyle.short,
            placeholder="例如：187.50",
            default=default_price,
            required=True,
            max_length=20,
        )
        self.direction_input: discord.ui.TextInput = discord.ui.TextInput(
            label="觸發方向 (above 向上突破 / below 向下跌破)",
            style=discord.TextStyle.short,
            placeholder="above",
            default="above",
            required=True,
            max_length=10,
        )
        self.volume_multiplier_input: discord.ui.TextInput = discord.ui.TextInput(
            label="放量倍數門檻 (0 代表不限制成交量)",
            style=discord.TextStyle.short,
            default="1.5",
            required=False,
            max_length=10,
        )
        self.add_item(self.target_price_input)
        self.add_item(self.direction_input)
        self.add_item(self.volume_multiplier_input)

    async def on_submit(self, interaction: discord.Interaction) -> Any:
        from database.price_volume_watch import (
            WatchDirection,
            WatchLimitExceededError,
            upsert_watch,
        )
        from pydantic import ValidationError
        from cogs.embed_builders.settings_embeds import (
            create_info_embed,
            create_error_embed,
        )

        direction_raw = self.direction_input.value.strip().lower()
        if direction_raw not in ("above", "below"):
            await interaction.response.send_message(
                embed=create_error_embed(
                    "觸發方向請輸入 `above` (向上突破) 或 `below` (向下跌破)。",
                    title="設定失敗",
                ),
                ephemeral=True,
            )
            return

        try:
            target_price = float(self.target_price_input.value.strip())
            vol_raw = self.volume_multiplier_input.value.strip()
            volume_multiplier = float(vol_raw) if vol_raw else 1.5
        except ValueError:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "目標價與放量倍數請輸入有效數字。", title="設定失敗"
                ),
                ephemeral=True,
            )
            return

        try:
            watch = await upsert_watch(
                user_id=self.user_id,
                symbol=self.symbol,
                target_price=target_price,
                direction=WatchDirection(direction_raw),
                volume_multiplier=volume_multiplier,
            )
            direction_label = (
                "≥ 向上突破"
                if watch.direction == WatchDirection.ABOVE
                else "≤ 向下跌破"
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
            await interaction.response.send_message(
                embed=create_info_embed(title="系統資訊", message=msg), ephemeral=True
            )
            if self.on_success_callback:
                await self.on_success_callback(interaction)
        except (WatchLimitExceededError, ValueError, ValidationError) as e:
            await interaction.response.send_message(
                embed=create_error_embed(str(e), title="設定失敗"), ephemeral=True
            )


class WatchlistAlertSelect(discord.ui.Select):
    def __init__(
        self,
        user_id: int,
        options: List[discord.SelectOption],
        on_success_callback: Optional[
            Callable[[discord.Interaction], Awaitable[None]]
        ] = None,
    ):
        super().__init__(
            placeholder="請選擇要設定價格警報的自選標的...",
            options=options,
            custom_id="select_watchlist_alert",
        )
        self.user_id = user_id
        self.on_success_callback = on_success_callback

    async def callback(self, interaction: discord.Interaction) -> Any:
        if interaction.data is None or not isinstance(interaction.data, dict):
            return
        select_values = interaction.data.get("values")
        if not select_values or not isinstance(select_values, list):
            return

        symbol = str(select_values[0])
        modal = WatchlistAlertModal(self.user_id, symbol, self.on_success_callback)
        await interaction.response.send_modal(modal)


class WatchlistAlertSelectView(discord.ui.View):
    def __init__(
        self,
        user_id: int,
        on_success_callback: Optional[
            Callable[[discord.Interaction], Awaitable[None]]
        ] = None,
    ):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.on_success_callback = on_success_callback

        manager = AssetManager()
        assets = manager.get_assets(user_id, ContextType.WATCH)

        if not assets:
            self.add_item(
                discord.ui.Button(label="您的自選名單目前為空", disabled=True)
            )
            return

        # Max 25 options for discord select
        options = [
            discord.SelectOption(label=a.symbol, description="設定 15 分鐘價量突破警報")
            for a in assets[:25]
        ]

        self.add_item(
            WatchlistAlertSelect(self.user_id, options, self.on_success_callback)
        )
