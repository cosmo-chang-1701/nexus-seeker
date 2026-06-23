"""Discord Cog for order management slash commands.

Refactored: Modal/View UI components moved to cogs/order_modals.py and cogs/order_views.py.
Business logic moved to services/order_telemetry_service.py.
This module retains only the Cog class (slash command routing) and backward-compatible re-exports.
"""

import discord
from discord import app_commands
from discord.ext import commands
import logging
import asyncio
from cogs.embed_builder import (
    create_info_embed,
    create_error_embed,
)
from cogs.order_modals import CancelOrderModal, EditOrderModal
from cogs.order_views import ApplyTelemetryView, OrderSetupView
from database.orders import (
    add_active_order,
    get_user_active_orders,
    delete_active_order,
    update_active_order_price,
)

logger = logging.getLogger(__name__)


# ==========================================
# Discord Cog 模組
# ==========================================
class OrderUICog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="order_panel", description="喚起交易委託單設定面板")
    async def order_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embed = create_info_embed(
            title="📥 交易委託單設定面版",
            message="請由下方下拉選單中選擇您要建立的**訂單類型**，系統將自動彈出專屬設定表單。",
        )
        view = OrderSetupView()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="list_orders",
        description="列出目前所有活躍的待成交委託單（可用下拉篩選）",
    )
    @app_commands.describe(
        symbol="標的 (Ticker) 篩選，例如: AAPL（預設：全部）",
        order_type="訂單類型（預設：全部）",
        side="委託方向（預設：全部）",
        validity="有效期限（預設：全部）",
        condition="委託條件（預設：全部）",
    )
    @app_commands.choices(
        order_type=[
            app_commands.Choice(name="全部 (ALL)", value="ALL"),
            app_commands.Choice(name="市價單 (MARKET)", value="MARKET"),
            app_commands.Choice(name="限價單 (LIMIT)", value="LIMIT"),
            app_commands.Choice(name="停損單 (STOP)", value="STOP"),
            app_commands.Choice(name="停損限價單 (STOP_LIMIT)", value="STOP_LIMIT"),
            app_commands.Choice(
                name="追蹤停損單 USD (TRAILING_STOP_USD)", value="TRAILING_STOP_USD"
            ),
            app_commands.Choice(
                name="追蹤停損單 PCT (TRAILING_STOP_PCT)", value="TRAILING_STOP_PCT"
            ),
        ],
        side=[
            app_commands.Choice(name="全部 (ALL)", value="ALL"),
            app_commands.Choice(name="買入 (BUY)", value="BUY"),
            app_commands.Choice(name="賣出 (SELL)", value="SELL"),
        ],
        validity=[
            app_commands.Choice(name="全部 (ALL)", value="ALL"),
            app_commands.Choice(name="當日有效 (DAY)", value="DAY"),
            app_commands.Choice(name="盤前+當日+盤後 (EXT_DAY)", value="EXT_DAY"),
            app_commands.Choice(name="夜盤 (NIGHT)", value="NIGHT"),
            app_commands.Choice(name="90天長期有效 (GTC_90)", value="GTC_90"),
        ],
        condition=[
            app_commands.Choice(name="全部 (ALL)", value="ALL"),
            app_commands.Choice(name="市價 (無條件)", value="MARKET_DEFAULT"),
            app_commands.Choice(name="含限價 (Limit)", value="HAS_LIMIT"),
            app_commands.Choice(name="含停損 (Stop)", value="HAS_STOP"),
            app_commands.Choice(name="含追蹤 (Trailing)", value="HAS_TRAIL"),
        ],
    )
    async def list_orders(
        self,
        interaction: discord.Interaction,
        symbol: str | None = None,
        order_type: str | None = None,
        side: str | None = None,
        validity: str | None = None,
        condition: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        orders = await asyncio.to_thread(get_user_active_orders, interaction.user.id)
        if not orders:
            embed = create_info_embed(
                title="📋 待成交委託單列表",
                message="您目前沒有任何活躍的待成交委託單。可以使用 `/order_panel` 喚起面板新增。",
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Normalize filters (optional → default ALL)
        symbol_v = (symbol or "ALL").strip().upper()
        order_type_v = (order_type or "ALL").strip().upper()
        side_v = (side or "ALL").strip().upper()
        validity_v = (validity or "ALL").strip().upper()
        condition_v = (condition or "ALL").strip().upper()

        def _match_condition(o: dict) -> bool:
            if condition_v == "ALL":
                return True

            ot = str(o.get("order_type") or "").upper()

            if condition_v == "MARKET_DEFAULT":
                return ot == "MARKET"
            if condition_v == "HAS_LIMIT":
                return ot in ("LIMIT", "STOP_LIMIT")
            if condition_v == "HAS_STOP":
                return ot in ("STOP", "STOP_LIMIT")
            if condition_v == "HAS_TRAIL":
                return ot in ("TRAILING_STOP_USD", "TRAILING_STOP_PCT")

            return True

        filtered = []
        for o in orders:
            ot = str(o.get("order_type") or "").upper()
            sd = str(o.get("side") or "BUY").upper()
            vd = str(o.get("validity") or "").upper()

            if symbol_v != "ALL" and str(o.get("symbol") or "").upper() != symbol_v:
                continue
            if order_type_v != "ALL" and ot != order_type_v:
                continue
            if side_v != "ALL" and sd != side_v:
                continue
            if validity_v != "ALL" and vd != validity_v:
                continue
            if not _match_condition(o):
                continue

            filtered.append(o)

        if not filtered:
            embed = create_info_embed(
                title="📋 待成交委託單列表",
                message=(
                    "您目前有待成交委託單，但 **沒有任何訂單符合篩選條件**。\n"
                    "建議：將下拉篩選改回 `全部 (ALL)` 後再試一次。"
                ),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # 清單式：整合多筆委託單於單一訊息中；若內容超限，會自動拆成多則訊息接續。
        # 每則訊息底部統一提供「取消 / 編輯」按鈕，使用者輸入委託單 ID 即可操作。
        from cogs.embed_builder import create_active_orders_embed

        embeds = create_active_orders_embed(filtered)

        filters_applied = (
            symbol_v != "ALL"
            or order_type_v != "ALL"
            or side_v != "ALL"
            or validity_v != "ALL"
            or condition_v != "ALL"
        )
        if filters_applied:
            filter_line = (
                "篩選條件："
                f"標的=`{symbol_v}`、類型=`{order_type_v}`、方向=`{side_v}`、有效期限=`{validity_v}`、條件=`{condition_v}`\n"
            )
            for e in embeds:
                e.description = filter_line + (e.description or "")

        for embed in embeds:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="telemetry_alert",
        description="喚起半小時心跳遙測價格偏離警報（含實時對齊防線）",
    )
    async def telemetry_alert(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        orders = await asyncio.to_thread(get_user_active_orders, interaction.user.id)

        if not orders:
            await interaction.followup.send(
                embed=create_error_embed(
                    "❌ 您目前沒有任何活躍的待成交委託單，無法進行偏離度對齊分析。請先使用 `/order_panel` 建立委託。"
                ),
                ephemeral=True,
            )
            return

        from cogs.embed_builder import create_telemetry_alignment_embeds
        from services.calendar_service import calendar_service
        from services.order_telemetry_service import (
            build_telemetry_alignment_items,
            resolve_holding_type_and_rows,
        )
        from datetime import datetime
        import database

        user_holdings = await asyncio.to_thread(
            database.get_user_holdings, interaction.user.id
        )
        user_trades = await asyncio.to_thread(
            database.get_user_portfolio, interaction.user.id
        )
        holding_type, holding_map = resolve_holding_type_and_rows(
            holdings=user_holdings, trades=user_trades
        )

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

        alignment_items, truncated = await build_telemetry_alignment_items(
            user_id=interaction.user.id,
            orders=orders,
            holding_type=holding_type,
            holding_map=holding_map,
            macro_event_dates=macro_event_dates,
        )

        embeds = create_telemetry_alignment_embeds(
            alignment_items,
            truncated=truncated,
            include_apply_button_hint=True,
            scheduled_mode=False,
        )
        suggestions = {
            item["order_id"]: (item["suggested_price"], item["suggested_qty"])
            for item in alignment_items
        }
        for embed in embeds:
            await interaction.followup.send(
                embed=embed, view=ApplyTelemetryView(suggestions), ephemeral=True
            )

    @app_commands.command(
        name="add_order",
        description="直接新增一個交易委託單 (未填價格時自動套用遙測定價)",
    )
    @app_commands.describe(
        symbol="標的代碼 (例如: AAPL)",
        quantity="委託數量 (正數)",
        order_type="訂單類型 (MARKET, LIMIT, STOP, STOP_LIMIT, TRAILING_STOP_USD, TRAILING_STOP_PCT)",
        side="委託方向 (BUY 買入 / SELL 賣出)",
        validity="有效期限制 (預設 DAY) - 可選: DAY, EXT_DAY, NIGHT, GTC_90",
        price="限價/停損價/追蹤值 (可選，未填時自動呼叫遙測定價進行高安全邊際折價)",
    )
    @app_commands.choices(
        order_type=[
            app_commands.Choice(name="市價單 (MARKET)", value="MARKET"),
            app_commands.Choice(name="限價單 (LIMIT)", value="LIMIT"),
            app_commands.Choice(name="停損單 (STOP)", value="STOP"),
            app_commands.Choice(name="停損限價單 (STOP_LIMIT)", value="STOP_LIMIT"),
            app_commands.Choice(
                name="追蹤停損單 USD (TRAILING_STOP_USD)", value="TRAILING_STOP_USD"
            ),
            app_commands.Choice(
                name="追蹤停損單 PCT (TRAILING_STOP_PCT)", value="TRAILING_STOP_PCT"
            ),
        ],
        side=[
            app_commands.Choice(name="買入 (BUY)", value="BUY"),
            app_commands.Choice(name="賣出 (SELL)", value="SELL"),
        ],
        validity=[
            app_commands.Choice(name="當日有效 (DAY)", value="DAY"),
            app_commands.Choice(name="盤前+當日+盤後 (EXT_DAY)", value="EXT_DAY"),
            app_commands.Choice(name="夜盤 (NIGHT)", value="NIGHT"),
            app_commands.Choice(name="90天長期有效 (GTC_90)", value="GTC_90"),
        ],
    )
    async def add_order(
        self,
        interaction: discord.Interaction,
        symbol: str,
        quantity: float,
        order_type: str,
        side: str = "BUY",
        validity: str = "DAY",
        price: float = 0.0,
    ):
        await interaction.response.defer(ephemeral=True)

        # 1. 驗證數量（股數只能是整數）
        if quantity <= 0 or (isinstance(quantity, float) and not quantity.is_integer()):
            await interaction.followup.send(
                embed=create_error_embed(
                    "❌ 錯誤：請輸入有效的正整數作為股數（Quantity）。"
                ),
                ephemeral=True,
            )
            return

        # 2. 決定是否啟用遙測定價
        limit_val = 0.0
        stop_val = 0.0
        trailing_val = 0.0
        auto_telemetry_triggered = price <= 0.0
        symbol = symbol.strip().upper()
        telemetry_logs: list[str] = []
        quantity_int = max(1, int(quantity))
        final_qty = quantity_int

        if auto_telemetry_triggered:
            from services.order_telemetry_service import resolve_telemetry_pricing

            try:
                (
                    limit_val,
                    stop_val,
                    trailing_val,
                    final_qty,
                    telemetry_logs,
                ) = await resolve_telemetry_pricing(
                    symbol=symbol,
                    order_type=order_type,
                    base_quantity=quantity,
                )
            except ValueError as e:
                await interaction.followup.send(
                    embed=create_error_embed(f"❌ 錯誤：{e}"),
                    ephemeral=True,
                )
                return
            except Exception as e:
                logger.error(f"Telemetry pricing failed for {symbol}: {e}")
                await interaction.followup.send(
                    embed=create_error_embed(f"❌ 遙測定價失敗：{e}。請手動輸入價格。"),
                    ephemeral=True,
                )
                return

        else:
            # 手動輸入價格映射
            if order_type == "LIMIT":
                limit_val = price
            elif order_type == "STOP":
                stop_val = price
            elif order_type == "STOP_LIMIT":
                limit_val = price
                stop_val = price
            elif order_type in ("TRAILING_STOP_USD", "TRAILING_STOP_PCT"):
                trailing_val = price

        # 3. 寫入資料庫
        try:
            order_id = add_active_order(
                user_id=interaction.user.id,
                symbol=symbol,
                quantity=final_qty,
                order_type=order_type,
                validity=validity,
                side=side,
                limit_price=limit_val,
                stop_price=stop_val,
                trailing_value=trailing_val,
            )

            # 4. 回傳 Traditional Chinese Embed 成功訊息
            validity_zh = {
                "DAY": "當日有效 (DAY)",
                "EXT_DAY": "盤前 + 當日 + 盤後 (EXT_DAY)",
                "NIGHT": "夜盤 (NIGHT)",
                "GTC_90": "90 天有效 (GTC_90)",
            }.get(validity, validity)

            order_type_zh = {
                "MARKET": "市價單",
                "LIMIT": "限價單",
                "STOP": "停損價單",
                "STOP_LIMIT": "停損限價單",
                "TRAILING_STOP_USD": "追蹤停損單 ($)",
                "TRAILING_STOP_PCT": "追蹤停損單 (%)",
            }.get(order_type, order_type)

            side_zh = "買入 (BUY)" if side.upper() == "BUY" else "賣出 (SELL)"

            msg = (
                f"✅ **訂單已成功建立並進入排程**\n\n"
                f"• **委託單 ID**: `{order_id}`\n"
                f"• **標的**: `{symbol}`\n"
                f"• **類型**: `{order_type_zh}`\n"
                f"• **方向**: `{side_zh}`\n"
                f"• **數量**: `{int(final_qty)}`\n"
                f"• **有效期限**: `{validity_zh}`\n"
            )
            if order_type in ("LIMIT", "STOP_LIMIT"):
                msg += f"• **限價**: `${limit_val:.2f}`\n"
            if order_type in ("STOP", "STOP_LIMIT"):
                msg += f"• **停損價**: `${stop_val:.2f}`\n"
            if order_type == "TRAILING_STOP_USD":
                msg += f"• **追蹤停損值 ($)**: `${trailing_val:.2f}`\n"
            if order_type == "TRAILING_STOP_PCT":
                msg += f"• **追蹤停損值 (%)**: `{trailing_val:.2f}%`\n"

            if auto_telemetry_triggered:
                msg += "\n🤖 **[已自動套用遙測最佳防線價與數量優化]**"
                if telemetry_logs:
                    msg += "\n\n**遙測決策軌跡:**\n" + "\n".join(telemetry_logs[:2])

            embed = create_info_embed(
                title="訂單登錄成功",
                message=msg,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error persisting active order: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"❌ 寫入資料庫時出錯：{str(e)}"),
                ephemeral=True,
            )

    @app_commands.command(
        name="remove_order",
        description="取消指定的待成交委託單",
    )
    @app_commands.describe(order_id="要取消的委託單 ID (留空則彈出對話框輸入)")
    async def remove_order(
        self,
        interaction: discord.Interaction,
        order_id: int | None = None,
    ):
        if order_id is None:
            await interaction.response.send_modal(CancelOrderModal())
            return

        await interaction.response.defer(ephemeral=True)
        try:
            success = await asyncio.to_thread(delete_active_order, order_id)
            if success:
                embed = create_info_embed(
                    title="取消委託成功",
                    message=f"✅ **成功取消委託單**：已自資料庫中撤銷委託單 ID `{order_id}`。",
                )
            else:
                embed = create_error_embed(
                    f"❌ 錯誤：找不到委託單 ID `{order_id}`，請確認 ID 是否正確。"
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to delete order {order_id}: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"❌ 錯誤：取消失敗：{e}"),
                ephemeral=True,
            )

    @app_commands.command(
        name="edit_order",
        description="編輯指定的待成交委託單",
    )
    @app_commands.describe(
        order_id="委託單 ID (留空則彈出對話框輸入)",
        price="新限價 / 新價格 / 新追蹤值 (留空則不變更價格)",
        side="新委託方向 (BUY 買入 / SELL 賣出，留空則不變)",
    )
    @app_commands.choices(
        side=[
            app_commands.Choice(name="買入 (BUY)", value="BUY"),
            app_commands.Choice(name="賣出 (SELL)", value="SELL"),
        ]
    )
    async def edit_order(
        self,
        interaction: discord.Interaction,
        order_id: int | None = None,
        price: float | None = None,
        side: str | None = None,
    ):
        if order_id is None:
            await interaction.response.send_modal(EditOrderModal())
            return

        if price is None and side is None:
            await interaction.response.send_modal(EditOrderModal(order_id=order_id))
            return

        await interaction.response.defer(ephemeral=True)
        if price is not None and price <= 0:
            await interaction.followup.send(
                embed=create_error_embed("❌ 錯誤：請輸入有效的正數作為新價格。"),
                ephemeral=True,
            )
            return

        side_to_apply = None
        if side:
            side_to_apply = side.strip().upper()
            if side_to_apply not in ("BUY", "SELL"):
                await interaction.followup.send(
                    embed=create_error_embed("❌ 錯誤：方向請輸入 BUY 或 SELL。"),
                    ephemeral=True,
                )
                return

        try:
            success = await asyncio.to_thread(
                update_active_order_price, order_id, price, None, side_to_apply
            )
            if success:
                side_msg = (
                    f"方向更新為 `{side_to_apply}`" if side_to_apply else "方向未變更"
                )
                price_msg = (
                    f"新價格: `${price:.2f}` (或 {price:.2f}%)"
                    if price is not None
                    else "價格未變更"
                )
                embed = create_info_embed(
                    title="編輯委託單成功",
                    message=(
                        f"✅ **成功更新委託單**：委託單 ID `{order_id}`\n"
                        f"• {price_msg}\n"
                        f"• {side_msg}"
                    ),
                )
            else:
                embed = create_error_embed(
                    f"❌ 錯誤：找不到委託單 ID `{order_id}`，請確認 ID 是否正確。"
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to edit order {order_id}: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"❌ 錯誤：更新失敗：{e}"),
                ephemeral=True,
            )


async def setup(bot):
    await bot.add_cog(OrderUICog(bot))


# ==========================================
# Backward-compatible re-exports
# ==========================================
# Ensures existing imports like `from cogs.order_ui import ApplyTelemetryView`
# continue to work without modification during the transition period.
from cogs.order_modals import DynamicOrderModal  # noqa: F401, E402
from cogs.order_views import (  # noqa: F401, E402
    OrderItemView,
    ApplyTelemetryView as _ApplyTelemetryView,
    OrderSideSelect,
    OrderValiditySelect,
    OrderSetupSelect,
    OrderSetupView as _OrderSetupView,
)
