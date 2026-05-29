import discord
from discord import app_commands
from discord.ext import commands
import logging
from cogs.embed_builder import create_info_embed, create_error_embed
from database.orders import (
    add_active_order,
    get_user_active_orders,
    delete_active_order,
    update_active_order_price,
)

logger = logging.getLogger(__name__)


# ==========================================
# 1. 委託單新增 Modal
# ==========================================
class DynamicOrderModal(discord.ui.Modal):
    def __init__(self, order_type: str, title: str):
        super().__init__(title=title)
        self.order_type = order_type

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
        self.validity = discord.ui.TextInput(
            label="有效期限 (Time In Force)",
            placeholder="填寫: 當日有效 / 盤前當日盤後 / 夜盤 / 90天有效",
            required=True,
        )

        self.add_item(self.ticker)
        self.add_item(self.quantity)
        self.add_item(self.validity)

        # 條件式動態欄位注入
        if self.order_type == "LIMIT":
            self.limit_price = discord.ui.TextInput(
                label="限價 (Limit Price)",
                placeholder="例如: 85.5",
                required=True,
            )
            self.add_item(self.limit_price)

        elif self.order_type == "STOP":
            self.stop_price = discord.ui.TextInput(
                label="停損價 (Stop Price)",
                placeholder="例如: 80.0",
                required=True,
            )
            self.add_item(self.stop_price)

        elif self.order_type == "STOP_LIMIT":
            self.limit_price = discord.ui.TextInput(
                label="限價 (Limit Price)",
                placeholder="例如: 85.5",
                required=True,
            )
            self.stop_price = discord.ui.TextInput(
                label="停損價 (Stop Price)",
                placeholder="例如: 80.0",
                required=True,
            )
            self.add_item(self.limit_price)
            self.add_item(self.stop_price)

        elif self.order_type == "TRAILING_STOP_USD":
            self.trailing_value = discord.ui.TextInput(
                label="追蹤值 $ (Trailing Amount USD)",
                placeholder="例如: 5.0",
                required=True,
            )
            self.add_item(self.trailing_value)

        elif self.order_type == "TRAILING_STOP_PCT":
            self.trailing_value = discord.ui.TextInput(
                label="追蹤值 % (Trailing Amount PCT)",
                placeholder="例如: 10.0",
                required=True,
            )
            self.add_item(self.trailing_value)

    async def on_submit(self, interaction: discord.Interaction):
        # 1. 驗證並解析數量
        try:
            qty_str = self.quantity.value.strip()
            if "." in qty_str:
                qty = float(qty_str)
            else:
                qty = int(qty_str)
            if qty <= 0:
                raise ValueError("數量必須大於 0")
        except Exception:
            await interaction.response.send_message(
                embed=create_error_embed("❌ 錯誤：請輸入有效的正數作為訂單數量。"),
                ephemeral=True,
            )
            return

        # 2. 驗證並解析各個條件限制價格/追蹤值
        limit_val = 0.0
        stop_val = 0.0
        trailing_val = 0.0

        if self.order_type in ("LIMIT", "STOP_LIMIT"):
            try:
                limit_val = float(self.limit_price.value.strip())
                if limit_val <= 0:
                    raise ValueError()
            except Exception:
                await interaction.response.send_message(
                    embed=create_error_embed("❌ 錯誤：請輸入有效的限價。"),
                    ephemeral=True,
                )
                return

        if self.order_type in ("STOP", "STOP_LIMIT"):
            try:
                stop_val = float(self.stop_price.value.strip())
                if stop_val <= 0:
                    raise ValueError()
            except Exception:
                await interaction.response.send_message(
                    embed=create_error_embed("❌ 錯誤：請輸入有效的停損價。"),
                    ephemeral=True,
                )
                return

        if self.order_type in ("TRAILING_STOP_USD", "TRAILING_STOP_PCT"):
            try:
                trailing_val = float(self.trailing_value.value.strip())
                if trailing_val <= 0:
                    raise ValueError()
            except Exception:
                await interaction.response.send_message(
                    embed=create_error_embed("❌ 錯誤：請輸入有效的追蹤停損值。"),
                    ephemeral=True,
                )
                return

        # 3. 映射有效期限到 DB Enum strings: ['DAY', 'EXT_DAY', 'NIGHT', 'GTC_90']
        validity_input = self.validity.value.strip().lower()
        validity_map = {
            "當日有效": "DAY",
            "day": "DAY",
            "盤前當日盤後": "EXT_DAY",
            "ext_day": "EXT_DAY",
            "夜盤": "NIGHT",
            "night": "NIGHT",
            "90天有效": "GTC_90",
            "gtc_90": "GTC_90",
        }

        validity_db = "DAY"  # 預設
        for k, v in validity_map.items():
            if k in validity_input:
                validity_db = v
                break

        # 4. 寫入資料庫
        try:
            order_id = add_active_order(
                user_id=interaction.user.id,
                symbol=self.ticker.value.strip().upper(),
                quantity=qty,
                order_type=self.order_type,
                validity=validity_db,
                limit_price=limit_val,
                stop_price=stop_val,
                trailing_value=trailing_val,
            )

            # 5. 回傳 Traditional Chinese Embed 成功訊息
            validity_zh = {
                "DAY": "當日有效 (DAY)",
                "EXT_DAY": "盤前 + 當日 + 盤後 (EXT_DAY)",
                "NIGHT": "夜盤 (NIGHT)",
                "GTC_90": "90 天有效 (GTC_90)",
            }.get(validity_db, validity_db)

            order_type_zh = {
                "MARKET": "市價單",
                "LIMIT": "限價單",
                "STOP": "停損價單",
                "STOP_LIMIT": "停損限價單",
                "TRAILING_STOP_USD": "追蹤停損單 ($)",
                "TRAILING_STOP_PCT": "追蹤停損單 (%)",
            }.get(self.order_type, self.order_type)

            msg = (
                f"✅ **訂單已成功建立並進入排程**\n\n"
                f"• **委託單 ID**: `{order_id}`\n"
                f"• **標的**: `{self.ticker.value.strip().upper()}`\n"
                f"• **類型**: `{order_type_zh}`\n"
                f"• **數量**: `{qty}`\n"
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

            embed = create_info_embed(
                title="訂單登錄成功",
                message=msg,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error persisting active order: {e}")
            await interaction.response.send_message(
                embed=create_error_embed(f"❌ 寫入資料庫時出錯：{str(e)}"),
                ephemeral=True,
            )


# ==========================================
# 2. 委託單管理交互 Modal
# ==========================================
class CancelOrderModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="取消待成交委託單")
        self.order_id = discord.ui.TextInput(
            label="委託單 ID (Order ID)",
            placeholder="例如: 1",
            required=True,
        )
        self.add_item(self.order_id)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            oid = int(self.order_id.value.strip())
        except Exception:
            await interaction.response.send_message(
                embed=create_error_embed("❌ 錯誤：請輸入有效的整數作為委託單 ID。"),
                ephemeral=True,
            )
            return

        try:
            success = delete_active_order(oid)
            if success:
                embed = create_info_embed(
                    title="取消委託成功",
                    message=f"✅ **成功取消委託單**：已自資料庫中撤銷委託單 ID `{oid}`。",
                )
            else:
                embed = create_error_embed(
                    f"❌ 錯誤：找不到委託單 ID `{oid}`，請確認 ID 是否正確。"
                )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to delete order {oid}: {e}")
            await interaction.response.send_message(
                embed=create_error_embed(f"❌ 錯誤：取消失敗：{e}"),
                ephemeral=True,
            )


class AdjustOrderModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="微調委託單價格")
        self.order_id = discord.ui.TextInput(
            label="委託單 ID (Order ID)",
            placeholder="例如: 1",
            required=True,
        )
        self.new_price = discord.ui.TextInput(
            label="新限價 / 新價格 / 新追蹤值",
            placeholder="例如: 82.5",
            required=True,
        )
        self.add_item(self.order_id)
        self.add_item(self.new_price)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            oid = int(self.order_id.value.strip())
        except Exception:
            await interaction.response.send_message(
                embed=create_error_embed("❌ 錯誤：請輸入有效的整數作為委託單 ID。"),
                ephemeral=True,
            )
            return

        try:
            price = float(self.new_price.value.strip())
            if price <= 0:
                raise ValueError()
        except Exception:
            await interaction.response.send_message(
                embed=create_error_embed("❌ 錯誤：請輸入有效的正數作為新價格。"),
                ephemeral=True,
            )
            return

        try:
            success = update_active_order_price(oid, price)
            if success:
                embed = create_info_embed(
                    title="價格微調成功",
                    message=f"✅ **成功更新委託單價格**：已將委託單 ID `{oid}` 的價格微調至 `${price:.2f}` (或 {price:.2f}%)。",
                )
            else:
                embed = create_error_embed(
                    f"❌ 錯誤：找不到委託單 ID `{oid}`，請確認 ID 是否正確。"
                )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to adjust order {oid}: {e}")
            await interaction.response.send_message(
                embed=create_error_embed(f"❌ 錯誤：更新失敗：{e}"),
                ephemeral=True,
            )


# ==========================================
# 3. 委託單管理與對齊 View
# ==========================================
class OrderManagementView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="❌ 取消委託", style=discord.ButtonStyle.danger)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(CancelOrderModal())

    @discord.ui.button(label="⚙️ 快速微調價格", style=discord.ButtonStyle.primary)
    async def adjust_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(AdjustOrderModal())


class ApplyTelemetryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="⚡ 一鍵套用遙測建議價", style=discord.ButtonStyle.success)
    async def apply_telemetry_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        from services.telemetry_pricing_engine import calculate_telemetry_price

        # 延遲回覆以防計算超時
        await interaction.response.defer(ephemeral=True)

        user_orders = get_user_active_orders(interaction.user.id)
        if not user_orders:
            await interaction.followup.send(
                embed=create_error_embed("❌ 您目前沒有任何活躍的待成交委託單。"),
                ephemeral=True,
            )
            return

        updated_count = 0
        details = []

        for order in user_orders:
            symbol = order["symbol"]
            current_price = (
                order["limit_price"]
                if order["limit_price"] > 0
                else (
                    order["stop_price"]
                    if order["stop_price"] > 0
                    else order["trailing_value"]
                )
            )

            # 模擬盤中行情遙測參數：IV 突發暴噴 55% vs 歷史 35%
            optimal_price, logs = await calculate_telemetry_price(
                symbol=symbol,
                base_price=current_price,
                spot_price=current_price * 1.02,
                iv=0.55,
                hist_iv=0.35,
                max_pain=100.0,
                prev_max_pain=100.0,
                skew_percentile=0.5,
                prev_close=current_price,
            )

            if optimal_price != current_price:
                update_active_order_price(order["id"], optimal_price)
                updated_count += 1
                details.append(
                    f"• **委託單 ID `{order['id']}` ({symbol})**:\n"
                    f"  - 原有價格: `${current_price:.2f}`\n"
                    f"  - 調整後安全建議價: `${optimal_price:.2f}` (IV 暴噴，向下修補 3%)\n"
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

        modal = DynamicOrderModal(order_type=order_type, title=modal_title)
        await interaction.response.send_modal(modal)


class OrderSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(OrderSetupSelect())


# ==========================================
# 5. Discord Cog 模組
# ==========================================
class OrderUICog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="order_panel", description="喚起交易委託單設定面板")
    async def order_panel(self, interaction: discord.Interaction):
        embed = create_info_embed(
            title="📥 交易委託單設定面版",
            message="請由下方下拉選單中選擇您要建立的**訂單類型**，系統將自動彈出專屬設定表單。",
        )
        view = OrderSetupView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="orders", description="列出目前所有活躍的待成交委託單")
    async def list_orders(self, interaction: discord.Interaction):
        orders = get_user_active_orders(interaction.user.id)

        if not orders:
            embed = create_info_embed(
                title="📋 待成交委託單列表",
                message="您目前沒有任何活躍的待成交委託單。可以使用 `/order_panel` 喚起面板新增。",
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        order_type_zh = {
            "MARKET": "市價單",
            "LIMIT": "限價單",
            "STOP": "停損價單",
            "STOP_LIMIT": "停損限價單",
            "TRAILING_STOP_USD": "追蹤停損單 ($)",
            "TRAILING_STOP_PCT": "追蹤停損單 (%)",
        }

        validity_zh = {
            "DAY": "當日有效 (DAY)",
            "EXT_DAY": "盤前 + 當日 + 盤後 (EXT_DAY)",
            "NIGHT": "夜盤 (NIGHT)",
            "GTC_90": "90 天有效 (GTC_90)",
        }

        lines = []
        for o in orders:
            price_details = ""
            if o["order_type"] in ("LIMIT", "STOP_LIMIT"):
                price_details += f" | 限價: `${o['limit_price']:.2f}`"
            if o["order_type"] in ("STOP", "STOP_LIMIT"):
                price_details += f" | 停損價: `${o['stop_price']:.2f}`"
            if o["order_type"] == "TRAILING_STOP_USD":
                price_details += f" | 追蹤值: `${o['trailing_value']:.2f}`"
            if o["order_type"] == "TRAILING_STOP_PCT":
                price_details += f" | 追蹤值: `{o['trailing_value']:.2f}%`"

            lines.append(
                f"• **ID `{o['id']}` - `{o['symbol']}`** ({order_type_zh.get(o['order_type'], o['order_type'])})\n"
                f"  - 數量: `{o['quantity']}` | 有效期: `{validity_zh.get(o['validity'], o['validity'])}`{price_details}"
            )

        embed = create_info_embed(
            title="📋 待成交委託單列表",
            message="\n\n".join(lines),
        )
        view = OrderManagementView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="telemetry_alert", description="[模擬] 喚起半小時心跳遙測價格偏離警報"
    )
    async def telemetry_alert(self, interaction: discord.Interaction):
        orders = get_user_active_orders(interaction.user.id)

        if not orders:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "❌ 您目前沒有任何活躍的待成交委託單，無法進行偏離度對齊分析。請先使用 `/order_panel` 建立委託。"
                ),
                ephemeral=True,
            )
            return

        # 模擬情境：市場 IV 突發暴噴，原有的掛單與預期波動率 (Expected Move) 下限偏離
        # 顯示警報 Embed
        msg = (
            "⚠️ **【動態掛單偏離度警報】**\n"
            "偵測到美股市場短線隱含波動率 (IV) 劇烈放大，導致您的待成交限價單面臨砸盤被穿風險：\n\n"
        )

        for o in orders:
            current_price = (
                o["limit_price"]
                if o["limit_price"] > 0
                else (o["stop_price"] if o["stop_price"] > 0 else o["trailing_value"])
            )
            suggested_price = current_price * 0.97
            msg += (
                f"• **標的 `{o['symbol']}` (ID `{o['id']}`)**:\n"
                f"  - 當前掛單價格: `${current_price:.2f}`\n"
                f"  - 遙測最佳防線價: `${suggested_price:.2f}` (IV 暴噴，預期震盪 EM 下移)\n"
                f"  - **狀態**: ⚠️ 偏離度過高，面臨被擊穿風險\n\n"
            )

        msg += "💡 **建議操作**：請點擊下方綠色按鈕「一鍵套用遙測建議價」，系統將自動安全調降您的委託限價以防守大後方。"

        embed = create_info_embed(
            title="📡 待成交委託單 - 盤中每半小時 telemetry 對齊警報",
            message=msg,
        )
        view = ApplyTelemetryView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(OrderUICog(bot))
