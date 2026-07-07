"""Discord Modal UI components for order management.

Extracted from cogs/order_ui.py to isolate UI form definitions
from business logic (now in services/order_telemetry_service.py).
"""

import asyncio
from typing import Any
import discord
import logging
from cogs.embed_builder import create_info_embed, create_error_embed
from database.orders import (
    add_active_order,
    delete_active_order,
    get_active_order,
    update_active_order_fields,
)

logger = logging.getLogger(__name__)


# ==========================================
# 1. 委託單新增 Modal
# ==========================================
class DynamicOrderModal(discord.ui.Modal):
    ticker: discord.ui.TextInput
    quantity: discord.ui.TextInput
    limit_price: discord.ui.TextInput
    stop_price: discord.ui.TextInput
    trailing_value: discord.ui.TextInput

    def __init__(
        self, order_type: str, title: str, validity_db: str, side_db: str = "BUY"
    ):
        super().__init__(title=title)
        self.order_type = order_type
        self.validity_db = validity_db
        self.side_db = side_db

        # 基礎必要欄位 (所有訂單類型皆有)
        self.ticker = discord.ui.TextInput(
            label="標的 (Ticker)",
            placeholder="例如: NET",
            max_length=10,
            required=True,
        )
        self.quantity = discord.ui.TextInput(
            label="數量 (Quantity)",
            placeholder="例如: 100",
            required=True,
        )

        self.add_item(self.ticker)
        self.add_item(self.quantity)

        # 條件式動態欄位注入 (設為非必填，以利空值時自動套用遙測定價)
        if self.order_type == "LIMIT":
            self.limit_price = discord.ui.TextInput(
                label="限價 (Limit Price)",
                placeholder="例如: 85.5 (留空則自動套用遙測定價)",
                required=False,
            )
            self.add_item(self.limit_price)

        elif self.order_type == "STOP":
            self.stop_price = discord.ui.TextInput(
                label="停損價 (Stop Price)",
                placeholder="例如: 80.0 (留空則自動套用遙測定價)",
                required=False,
            )
            self.add_item(self.stop_price)

        elif self.order_type == "STOP_LIMIT":
            self.limit_price = discord.ui.TextInput(
                label="限價 (Limit Price)",
                placeholder="例如: 85.5 (留空則自動套用遙測定價)",
                required=False,
            )
            self.stop_price = discord.ui.TextInput(
                label="停損價 (Stop Price)",
                placeholder="例如: 80.0 (留空則自動套用遙測定價)",
                required=False,
            )
            self.add_item(self.limit_price)
            self.add_item(self.stop_price)

        elif self.order_type == "TRAILING_STOP_USD":
            self.trailing_value = discord.ui.TextInput(
                label="追蹤值 $ (Trailing Amount USD)",
                placeholder="例如: 5.0 (留空則自動套用遙測定價)",
                required=False,
            )
            self.add_item(self.trailing_value)

        elif self.order_type == "TRAILING_STOP_PCT":
            self.trailing_value = discord.ui.TextInput(
                label="追蹤值 % (Trailing Amount PCT)",
                placeholder="例如: 10.0 (留空則自動套用遙測定價)",
                required=False,
            )
            self.add_item(self.trailing_value)

    async def on_submit(self, interaction: discord.Interaction):
        # 1. 驗證並解析數量
        try:
            qty_str = self.quantity.value.strip()
            if "." in qty_str:
                raise ValueError("股數必須為整數")
            qty = int(qty_str)
            if qty <= 0:
                raise ValueError("數量必須大於 0")
        except Exception:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "❌ 錯誤：請輸入有效的正整數作為股數（Quantity）。",
                    title="系統錯誤",
                ),
                ephemeral=True,
            )
            return

        # 2. 驗證並解析各個條件限制價格/追蹤值
        limit_val = 0.0
        stop_val = 0.0
        trailing_val = 0.0
        auto_telemetry_triggered = False

        def is_empty_or_zero(field) -> bool:
            if not field or not getattr(field, "value", "").strip():
                return True
            try:
                val = float(field.value.strip())
                return val <= 0.0
            except ValueError:
                return False

        if self.order_type == "LIMIT":
            if is_empty_or_zero(getattr(self, "limit_price", None)):
                auto_telemetry_triggered = True
            else:
                try:
                    limit_val = float(self.limit_price.value.strip())
                    if limit_val <= 0:
                        raise ValueError()
                except Exception:
                    await interaction.response.send_message(
                        embed=create_error_embed(
                            "❌ 錯誤：請輸入有效的限價。", title="系統錯誤"
                        ),
                        ephemeral=True,
                    )
                    return

        elif self.order_type == "STOP":
            if is_empty_or_zero(getattr(self, "stop_price", None)):
                auto_telemetry_triggered = True
            else:
                try:
                    stop_val = float(self.stop_price.value.strip())
                    if stop_val <= 0:
                        raise ValueError()
                except Exception:
                    await interaction.response.send_message(
                        embed=create_error_embed(
                            "❌ 錯誤：請輸入有效的停損價。", title="系統錯誤"
                        ),
                        ephemeral=True,
                    )
                    return

        elif self.order_type == "STOP_LIMIT":
            if is_empty_or_zero(getattr(self, "limit_price", None)) or is_empty_or_zero(
                getattr(self, "stop_price", None)
            ):
                auto_telemetry_triggered = True
            else:
                try:
                    limit_val = float(self.limit_price.value.strip())
                    stop_val = float(self.stop_price.value.strip())
                    if limit_val <= 0 or stop_val <= 0:
                        raise ValueError()
                except Exception:
                    await interaction.response.send_message(
                        embed=create_error_embed(
                            "❌ 錯誤：請輸入有效的限價與停損價。", title="系統錯誤"
                        ),
                        ephemeral=True,
                    )
                    return

        elif self.order_type in ("TRAILING_STOP_USD", "TRAILING_STOP_PCT"):
            if is_empty_or_zero(getattr(self, "trailing_value", None)):
                auto_telemetry_triggered = True
            else:
                try:
                    trailing_val = float(self.trailing_value.value.strip())
                    if trailing_val <= 0:
                        raise ValueError()
                except Exception:
                    await interaction.response.send_message(
                        embed=create_error_embed(
                            "❌ 錯誤：請輸入有效的追蹤停損值。", title="系統錯誤"
                        ),
                        ephemeral=True,
                    )
                    return

        symbol = self.ticker.value.strip().upper()
        telemetry_logs: list[str] = []
        final_qty = int(qty)

        if auto_telemetry_triggered:
            # 延遲回應，避免查詢 API 時超時
            await interaction.response.defer(ephemeral=True)
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
                    order_type=self.order_type,
                    base_quantity=qty,
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

        # 3. 寫入資料庫
        try:
            order_id = add_active_order(
                user_id=interaction.user.id,
                symbol=symbol,
                quantity=final_qty,
                order_type=self.order_type,
                validity=self.validity_db,
                side=self.side_db,
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
            }.get(self.validity_db, self.validity_db)

            order_type_zh = {
                "MARKET": "市價單",
                "LIMIT": "限價單",
                "STOP": "停損價單",
                "STOP_LIMIT": "停損限價單",
                "TRAILING_STOP_USD": "追蹤停損單 ($)",
                "TRAILING_STOP_PCT": "追蹤停損單 (%)",
            }.get(self.order_type, self.order_type)

            side_zh = "買入 (BUY)" if self.side_db.upper() == "BUY" else "賣出 (SELL)"

            msg = (
                f"✅ **訂單已成功建立並進入排程**\n\n"
                f"• **委託單 ID**: `{order_id}`\n"
                f"• **標的**: `{symbol}`\n"
                f"• **類型**: `{order_type_zh}`\n"
                f"• **方向**: `{side_zh}`\n"
                f"• **數量**: `{int(final_qty)}`\n"
                f"• **有效期限**: `{validity_zh}`\n"
            )
            if self.order_type in ("LIMIT", "STOP_LIMIT"):
                msg += f"• **限價**: `${limit_val:.2f}`\n"
            if self.order_type in ("STOP", "STOP_LIMIT"):
                msg += f"• **停損價**: `${stop_val:.2f}`\n"
            if self.order_type == "TRAILING_STOP_USD":
                msg += f"• **追蹤停損值 ($)**: `${trailing_val:.2f}`\n"
            if self.order_type == "TRAILING_STOP_PCT":
                msg += f"• **追蹤停損值 (%)**: `{trailing_val:.2f}%`\n"

            if auto_telemetry_triggered:
                msg += "\n🤖 **[已自動套用遙測最佳防線價與數量優化]**"
                if telemetry_logs:
                    msg += "\n\n**遙測決策軌跡:**\n" + "\n".join(telemetry_logs[:2])

            embed = create_info_embed(
                title="訂單登錄成功",
                message=msg,
            )
            if auto_telemetry_triggered:
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error persisting active order: {e}")
            err_embed = create_error_embed(f"❌ 寫入資料庫時出錯：{str(e)}")
            if auto_telemetry_triggered:
                await interaction.followup.send(embed=err_embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=err_embed, ephemeral=True)


# ==========================================
# 2. 委託單管理交互 Modal
# ==========================================
class CancelOrderModal(discord.ui.Modal):
    order_id: discord.ui.TextInput

    def __init__(self, order_id: int | None = None):
        super().__init__(title="取消待成交委託單")
        self.order_id = discord.ui.TextInput(
            label="委託單 ID (Order ID)",
            placeholder="例如: 1",
            default=str(order_id) if order_id is not None else None,
            required=True,
        )
        self.add_item(self.order_id)

    async def on_submit(self, interaction: discord.Interaction):
        # 1. 立即延遲回應，以防止任何資料庫/網絡延遲導致的 3 秒超時「此交互失敗」
        await interaction.response.defer(ephemeral=True)

        try:
            oid = int(self.order_id.value.strip())
        except Exception:
            await interaction.followup.send(
                embed=create_error_embed("❌ 錯誤：請輸入有效的整數作為委託單 ID。"),
                ephemeral=True,
            )
            return

        try:
            # 2. 將同步的資料庫操作交給執行緒，避免阻塞事件循環
            success = await asyncio.to_thread(delete_active_order, oid)
            if success:
                embed = create_info_embed(
                    title="取消委託成功",
                    message=f"✅ **成功取消委託單**：已自資料庫中撤銷委託單 ID `{oid}`。",
                )
            else:
                embed = create_error_embed(
                    f"❌ 錯誤：找不到委託單 ID `{oid}`，請確認 ID 是否正確。"
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to delete order {oid}: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"❌ 錯誤：取消失敗：{e}"),
                ephemeral=True,
            )


class EditOrderModal(discord.ui.Modal):
    order_id: discord.ui.TextInput
    new_symbol: discord.ui.TextInput
    new_quantity: discord.ui.TextInput
    new_side: discord.ui.TextInput
    new_price: discord.ui.TextInput

    def __init__(self, order_id: int | None = None):
        super().__init__(title="編輯委託單")
        self.order_id = discord.ui.TextInput(
            label="委託單 ID (Order ID)",
            placeholder="例如: 1",
            default=str(order_id) if order_id is not None else None,
            required=True,
        )
        self.new_symbol = discord.ui.TextInput(
            label="新標的代碼",
            placeholder="例如: AAPL（留空則不變更）",
            required=False,
            max_length=10,
        )
        self.new_quantity = discord.ui.TextInput(
            label="新數量",
            placeholder="例如: 100（留空則不變更）",
            required=False,
        )
        self.new_side = discord.ui.TextInput(
            label="委託方向 (BUY/SELL)",
            placeholder="例如: BUY 或 SELL（留空則不變更）",
            required=False,
        )
        self.new_price = discord.ui.TextInput(
            label="新限價 / 新停損價 / 新追蹤值",
            placeholder="例如: 82.5（留空則不變更價格）",
            required=False,
        )
        self.add_item(self.order_id)
        self.add_item(self.new_symbol)
        self.add_item(self.new_quantity)
        self.add_item(self.new_side)
        self.add_item(self.new_price)

    async def on_submit(self, interaction: discord.Interaction):
        # 1. 立即延遲回應，以防止任何資料庫/網絡延遲導致的 3 秒超時「此交互失敗」
        await interaction.response.defer(ephemeral=True)

        try:
            oid = int(self.order_id.value.strip())
        except Exception:
            await interaction.followup.send(
                embed=create_error_embed("❌ 錯誤：請輸入有效的整數作為委託單 ID。"),
                ephemeral=True,
            )
            return

        symbol_to_apply = (
            self.new_symbol.value.strip().upper() if self.new_symbol.value else None
        )

        quantity_to_apply = None
        if self.new_quantity.value:
            qty_text = self.new_quantity.value.strip()
            try:
                if "." in qty_text:
                    raise ValueError()
                quantity_to_apply = int(qty_text)
                if quantity_to_apply <= 0:
                    raise ValueError()
            except Exception:
                await interaction.followup.send(
                    embed=create_error_embed(
                        "❌ 錯誤：請輸入有效的正整數作為新數量（或留空不變更）。"
                    ),
                    ephemeral=True,
                )
                return

        price_text = self.new_price.value.strip() if self.new_price.value else ""
        price: float | None = None
        if price_text:
            try:
                price = float(price_text)
                if price <= 0:
                    raise ValueError()
            except Exception:
                await interaction.followup.send(
                    embed=create_error_embed(
                        "❌ 錯誤：請輸入有效的正數作為新價格（或留空不變更）。"
                    ),
                    ephemeral=True,
                )
                return

        new_side = self.new_side.value.strip().upper() if self.new_side.value else ""
        side_to_apply: str | None = None
        if new_side:
            if new_side not in ("BUY", "SELL"):
                await interaction.followup.send(
                    embed=create_error_embed(
                        "❌ 錯誤：方向請輸入 BUY 或 SELL（或留空不變）。"
                    ),
                    ephemeral=True,
                )
                return
            side_to_apply = new_side

        if (
            price is None
            and side_to_apply is None
            and symbol_to_apply is None
            and quantity_to_apply is None
        ):
            await interaction.followup.send(
                embed=create_error_embed("❌ 錯誤：請至少填寫一個要變更的欄位。"),
                ephemeral=True,
            )
            return

        try:
            # 取出訂單以判斷 order_type (用以 mapping price)
            order = await asyncio.to_thread(get_active_order, oid)
            if not order:
                await interaction.followup.send(
                    embed=create_error_embed(
                        f"❌ 錯誤：找不到委託單 ID `{oid}`，請確認 ID 是否正確。"
                    ),
                    ephemeral=True,
                )
                return

            update_kwargs: dict[str, Any] = {}
            if symbol_to_apply:
                update_kwargs["symbol"] = symbol_to_apply
            if quantity_to_apply:
                update_kwargs["quantity"] = quantity_to_apply
            if side_to_apply:
                update_kwargs["side"] = side_to_apply

            if price is not None:
                o_type = order.get("order_type", "")
                if o_type in ("LIMIT", "STOP_LIMIT"):
                    update_kwargs["limit_price"] = price
                if o_type in ("STOP", "STOP_LIMIT"):
                    update_kwargs["stop_price"] = price
                if o_type in ("TRAILING_STOP_USD", "TRAILING_STOP_PCT"):
                    update_kwargs["trailing_value"] = price

            # 2. 將同步的資料庫操作交給執行緒，避免阻塞事件循環
            success = await asyncio.to_thread(
                update_active_order_fields, oid, **update_kwargs
            )
            if success:
                updates_msg = []
                if symbol_to_apply:
                    updates_msg.append(f"標的: `{symbol_to_apply}`")
                if quantity_to_apply:
                    updates_msg.append(f"數量: `{quantity_to_apply}`")
                if side_to_apply:
                    updates_msg.append(f"方向: `{side_to_apply}`")
                if price is not None:
                    updates_msg.append(f"新價格/追蹤值: `{price}`")

                embed = create_info_embed(
                    title="編輯委託單成功",
                    message=(
                        f"✅ **成功更新委託單**：委託單 ID `{oid}`\n"
                        f"• 更新項目: " + ", ".join(updates_msg)
                    ),
                )
            else:
                embed = create_error_embed(
                    f"❌ 錯誤：找不到委託單 ID `{oid}`，請確認 ID 是否正確。"
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to edit order {oid}: {e}")
            await interaction.followup.send(
                embed=create_error_embed(f"❌ 錯誤：更新失敗：{e}"),
                ephemeral=True,
            )
