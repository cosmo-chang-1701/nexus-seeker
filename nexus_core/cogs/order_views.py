"""Discord View and Select UI components for order management.

Extracted from cogs/order_ui.py to isolate interactive UI components
from business logic (now in services/order_telemetry_service.py)
and modal definitions (now in cogs/order_modals.py).
"""

import asyncio
import discord
import logging
from cogs.embed_builder import create_info_embed, create_error_embed
from cogs.order_modals import CancelOrderModal, EditOrderModal, DynamicOrderModal
from database.orders import get_user_active_orders

logger = logging.getLogger(__name__)


class OrderItemView(discord.ui.View):
    """單筆委託單卡片的操作按鈕（每一筆訂單底下都有獨立按鈕）。"""

    def __init__(self, order_id: int):
        super().__init__(timeout=180)
        self.order_id = int(order_id)

    @discord.ui.button(label="❌ 取消委託單", style=discord.ButtonStyle.danger)
    async def cancel_order_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        try:
            await interaction.response.send_modal(
                CancelOrderModal(order_id=self.order_id)
            )
        except Exception as e:
            logger.error(
                f"Failed to send CancelOrderModal(order_id={self.order_id}): {e}"
            )
            await interaction.followup.send(
                embed=create_error_embed(f"❌ 無法開啟取消委託視窗：{e}"),
                ephemeral=True,
            )

    @discord.ui.button(label="✏️ 編輯委託單", style=discord.ButtonStyle.primary)
    async def edit_order_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        try:
            # 撈取訂單取得 order_type
            from database.orders import get_active_order

            order = await asyncio.to_thread(get_active_order, self.order_id)
            if not order:
                await interaction.response.send_message(
                    embed=create_error_embed("❌ 找不到該委託單"), ephemeral=True
                )
                return
            await interaction.response.send_modal(
                EditOrderModal(
                    order_id=self.order_id, order_type=order.get("order_type", "")
                )
            )
        except Exception as e:
            logger.error(
                f"Failed to send EditOrderModal(order_id={self.order_id}): {e}"
            )
            await interaction.response.send_message(
                embed=create_error_embed(f"❌ 無法開啟編輯委託單視窗：{e}"),
                ephemeral=True,
            )


class ApplyTelemetryView(discord.ui.View):
    def __init__(self, suggestions: dict[int, tuple[float, int]] = None):
        super().__init__(timeout=180)
        self.suggestions = suggestions or {}

    @discord.ui.button(label="⚡ 一鍵套用遙測建議價", style=discord.ButtonStyle.success)
    async def apply_telemetry_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        import database
        from services.order_telemetry_service import (
            apply_telemetry_to_orders,
            resolve_holding_type_and_rows,
        )

        # 延遲回覆以防計算超時
        await interaction.response.defer(ephemeral=True)

        user_orders = await asyncio.to_thread(
            get_user_active_orders, interaction.user.id
        )
        if not user_orders:
            await interaction.followup.send(
                embed=create_error_embed("❌ 您目前沒有任何活躍的待成交委託單。"),
                ephemeral=True,
            )
            return

        user_holdings = await asyncio.to_thread(
            database.get_user_holdings, interaction.user.id
        )
        user_trades = await asyncio.to_thread(
            database.get_user_portfolio, interaction.user.id
        )
        holding_type, holding_map = resolve_holding_type_and_rows(
            holdings=user_holdings, trades=user_trades
        )

        updated_count, details = await apply_telemetry_to_orders(
            user_id=interaction.user.id,
            orders=user_orders,
            suggestions=self.suggestions,
            holding_type=holding_type,
            holding_map=holding_map,
        )

        if updated_count > 0:
            msg = (
                f"✅ **成功套用動態遙測建議價！**\n\n"
                f"已成功為您自動安全防禦更新 `{updated_count}` 筆待成交委託防線：\n\n"
                + "\n".join(details)
            )
            embed = create_info_embed(
                title="動態遙測對齊完成",
                message=msg,
            )
        else:
            embed = create_info_embed(
                title="遙測狀態同步完成",
                message="✅ 您的所有待成交委託單皆處於絕對安全的遙測震盪疆界內，暫無須微調。",
            )

        await interaction.followup.send(embed=embed, ephemeral=True)


# ==========================================
# 4. 前端委託單面版下拉選單
# ==========================================
class OrderSideSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="買入 (BUY)",
                value="BUY",
                description="建立買入方向委託 (例如：逢低承接 / 突破追價)",
                default=True,
            ),
            discord.SelectOption(
                label="賣出 (SELL)",
                value="SELL",
                description="建立賣出方向委託 (例如：停損 / 停利 / 減碼)",
            ),
        ]
        super().__init__(
            placeholder="選擇委託方向 (預設為買入)...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if self.view is not None:
            setattr(self.view, "selected_side", selected)

        for option in self.options:
            option.default = option.value == selected

        await interaction.response.edit_message(view=self.view)


class OrderValiditySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="當日有效 (DAY)",
                value="DAY",
                description="常規盤收盤自動失效",
                default=True,
            ),
            discord.SelectOption(
                label="盤前 + 當日 + 盤後 (EXT_DAY)",
                value="EXT_DAY",
                description="盤前、常規盤、盤後皆有效",
            ),
            discord.SelectOption(
                label="夜盤 (NIGHT)",
                value="NIGHT",
                description="夜盤時段有效",
            ),
            discord.SelectOption(
                label="90 天有效 (GTC_90)",
                value="GTC_90",
                description="90 天長期有效 (Good 'Til Cancelled)",
            ),
        ]
        super().__init__(
            placeholder="選擇有效期限 (預設為當日有效)...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if self.view is not None:
            setattr(self.view, "selected_validity", selected)

        # 更新下拉選單的 default 狀態，讓 UI 呈現已被選取的狀態
        for option in self.options:
            option.default = option.value == selected

        await interaction.response.edit_message(view=self.view)


class OrderSetupSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="市價 (MARKET)",
                value="MARKET",
                description="建立市價委託單",
            ),
            discord.SelectOption(
                label="限價 (LIMIT)",
                value="LIMIT",
                description="建立限價委託單",
            ),
            discord.SelectOption(
                label="停損價 (STOP)",
                value="STOP",
                description="建立停損價委託單",
            ),
            discord.SelectOption(
                label="停損限價 (STOP_LIMIT)",
                value="STOP_LIMIT",
                description="建立停損限價委託單",
            ),
            discord.SelectOption(
                label="追蹤停損價 $ (TRAILING_STOP_USD)",
                value="TRAILING_STOP_USD",
                description="以固定美元建立追蹤停損單",
            ),
            discord.SelectOption(
                label="追蹤停損價 % (TRAILING_STOP_PCT)",
                value="TRAILING_STOP_PCT",
                description="以百分比建立追蹤停損單",
            ),
        ]
        super().__init__(
            placeholder="選擇訂單類型...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        order_type = self.values[0]
        modal_title = {
            "MARKET": "新增市價訂單",
            "LIMIT": "新增限價訂單",
            "STOP": "新增停損價訂單",
            "STOP_LIMIT": "新增停損限價訂單",
            "TRAILING_STOP_USD": "新增追蹤停損單 ($)",
            "TRAILING_STOP_PCT": "新增追蹤停損單 (%)",
        }.get(order_type, "新增訂單")

        validity = "DAY"
        side = "BUY"
        if self.view is not None:
            validity = getattr(self.view, "selected_validity", "DAY")
            side = getattr(self.view, "selected_side", "BUY")

        modal = DynamicOrderModal(
            order_type=order_type, title=modal_title, validity_db=validity, side_db=side
        )
        await interaction.response.send_modal(modal)


class OrderSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.selected_side = "BUY"
        self.selected_validity = "DAY"
        self.add_item(OrderSideSelect())
        self.add_item(OrderValiditySelect())
        self.add_item(OrderSetupSelect())

    @discord.ui.button(
        label="🟢 限價單 (Limit)", style=discord.ButtonStyle.success, row=2
    )
    async def limit_shortcut(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        modal = DynamicOrderModal(
            order_type="LIMIT",
            title="新增限價訂單",
            validity_db=self.selected_validity,
            side_db=self.selected_side,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="停損單 (Stop)", style=discord.ButtonStyle.primary, row=2)
    async def stop_shortcut(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        modal = DynamicOrderModal(
            order_type="STOP",
            title="新增停損價訂單",
            validity_db=self.selected_validity,
            side_db=self.selected_side,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="⚡ 市價單 (Market)", style=discord.ButtonStyle.secondary, row=2
    )
    async def market_shortcut(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        modal = DynamicOrderModal(
            order_type="MARKET",
            title="新增市價訂單",
            validity_db=self.selected_validity,
            side_db=self.selected_side,
        )
        await interaction.response.send_modal(modal)
