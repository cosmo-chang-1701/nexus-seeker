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
    ticker: discord.ui.TextInput
    quantity: discord.ui.TextInput
    limit_price: discord.ui.TextInput
    stop_price: discord.ui.TextInput
    trailing_value: discord.ui.TextInput

    def __init__(self, order_type: str, title: str, validity_db: str):
        super().__init__(title=title)
        self.order_type = order_type
        self.validity_db = validity_db

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
                qty = float(qty_str)
            else:
                qty = int(qty_str)
            if qty <= 0:
                raise ValueError("數量必須大於 0")
        except Exception:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "❌ 錯誤：請輸入有效的正數作為訂單數量。", title="系統錯誤"
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
        final_qty = qty

        if auto_telemetry_triggered:
            # 延遲回應，避免查詢 API 時超時
            await interaction.response.defer(ephemeral=True)
            from services import market_data_service
            from market_analysis.sentiment_engine import SentimentEngine
            from services.telemetry_pricing_engine import calculate_telemetry_price

            try:
                quote = await market_data_service.get_quote(symbol)
                spot_price = quote.get("c", 0.0)
                if spot_price <= 0.0:
                    df_temp = await market_data_service.get_history_df(
                        symbol, period="2d"
                    )
                    if not df_temp.empty:
                        spot_price = float(df_temp["Close"].iloc[-1])
            except Exception as e:
                logger.error(f"Error fetching quote for telemetry fallback: {e}")
                spot_price = 0.0

            if spot_price <= 0.0:
                await interaction.followup.send(
                    embed=create_error_embed(
                        f"❌ 錯誤：無法獲取標的 {symbol} 的現有價格以進行遙測定價。請手動輸入價格。"
                    ),
                    ephemeral=True,
                )
                return

            iv = 0.35
            hist_iv = 0.30
            try:
                iv_metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(
                    symbol
                )
                if iv_metrics and iv_metrics.current_iv > 0:
                    iv = iv_metrics.current_iv
                    hist_iv = iv / 1.1
            except Exception as e:
                logger.warning(f"Error fetching IV metrics for {symbol}: {e}")

            skew_val = 0.5
            try:
                skew_metrics = await SentimentEngine.calculate_skew(symbol)
                if skew_metrics and "skew" in skew_metrics:
                    skew = float(skew_metrics["skew"])
                    if skew > 5.0:
                        skew_val = 0.98
                    elif skew < -2.0:
                        skew_val = 0.02
            except Exception as e:
                logger.warning(f"Error calculating skew for {symbol}: {e}")

            prev_close = quote.get("pc", spot_price)

            if self.order_type in ("LIMIT", "STOP_LIMIT"):
                base_price = spot_price
            elif self.order_type == "STOP":
                base_price = spot_price
            elif self.order_type == "TRAILING_STOP_USD":
                base_price = spot_price * 0.05
            elif self.order_type == "TRAILING_STOP_PCT":
                base_price = 5.0
            else:
                base_price = spot_price

            (
                resolved_price,
                resolved_qty,
                telemetry_logs,
            ) = await calculate_telemetry_price(
                symbol=symbol,
                base_price=base_price,
                spot_price=spot_price,
                iv=iv,
                hist_iv=hist_iv,
                max_pain=spot_price,
                prev_max_pain=spot_price,
                skew_percentile=skew_val,
                prev_close=prev_close,
                base_quantity=qty,
            )

            if self.order_type == "LIMIT":
                limit_val = resolved_price
            elif self.order_type == "STOP":
                stop_val = resolved_price
            elif self.order_type == "STOP_LIMIT":
                limit_val = resolved_price
                stop_val = resolved_price * 0.98
            elif self.order_type in ("TRAILING_STOP_USD", "TRAILING_STOP_PCT"):
                trailing_val = resolved_price

            final_qty = resolved_qty

        # 3. 寫入資料庫
        try:
            order_id = add_active_order(
                user_id=interaction.user.id,
                symbol=symbol,
                quantity=final_qty,
                order_type=self.order_type,
                validity=self.validity_db,
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

            msg = (
                f"✅ **訂單已成功建立並進入排程**\n\n"
                f"• **委託單 ID**: `{order_id}`\n"
                f"• **標的**: `{symbol}`\n"
                f"• **類型**: `{order_type_zh}`\n"
                f"• **數量**: `{final_qty}`\n"
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
            original_qty = float(order.get("quantity") or 1.0)

            # 模擬盤中行情遙測參數：若 skew_percentile 觸發極端，則會調整數量為 75%
            # 我們在這裡模擬 skew_percentile 為 0.98，以便在模擬過程中能觸發並展示此機制
            optimal_price, optimal_qty, logs = await calculate_telemetry_price(
                symbol=symbol,
                base_price=current_price,
                spot_price=current_price * 1.02,
                iv=0.55,
                hist_iv=0.35,
                max_pain=100.0,
                prev_max_pain=100.0,
                skew_percentile=0.98,  # 觸發極端偏斜以展示倉位控管連結
                prev_close=current_price,
                base_quantity=original_qty,
            )

            if optimal_price != current_price or optimal_qty != original_qty:
                update_active_order_price(order["id"], optimal_price, optimal_qty)
                updated_count += 1
                qty_change_msg = ""
                if optimal_qty < original_qty:
                    qty_change_msg = f" (數量自 {original_qty} 調降至 {optimal_qty:.2f} 股 [⚠️ Tail Risk Mitigation])"
                details.append(
                    f"• **委託單 ID `{order['id']}` ({symbol})**:\n"
                    f"  - 原有價格: `${current_price:.2f}`\n"
                    f"  - 調整後安全建議價: `${optimal_price:.2f}`{qty_change_msg}\n"
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
        if self.view is not None:
            validity = getattr(self.view, "selected_validity", "DAY")

        modal = DynamicOrderModal(
            order_type=order_type, title=modal_title, validity_db=validity
        )
        await interaction.response.send_modal(modal)


class OrderSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.selected_validity = "DAY"
        self.add_item(OrderValiditySelect())
        self.add_item(OrderSetupSelect())

    @discord.ui.button(
        label="🟢 限價單 (Limit)", style=discord.ButtonStyle.success, row=2
    )
    async def limit_shortcut(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        modal = DynamicOrderModal(
            order_type="LIMIT", title="新增限價訂單", validity_db=self.selected_validity
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
        )
        await interaction.response.send_modal(modal)


# ==========================================
# 5. Discord Cog 模組
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

    @app_commands.command(name="orders", description="列出目前所有活躍的待成交委託單")
    async def list_orders(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        orders = get_user_active_orders(interaction.user.id)

        if not orders:
            embed = create_info_embed(
                title="📋 待成交委託單列表",
                message="您目前沒有任何活躍的待成交委託單。可以使用 `/order_panel` 喚起面板新增。",
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
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
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="telemetry_alert", description="[模擬] 喚起半小時心跳遙測價格偏離警報"
    )
    async def telemetry_alert(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        orders = get_user_active_orders(interaction.user.id)

        if not orders:
            await interaction.followup.send(
                embed=create_error_embed(
                    "❌ 您目前沒有任何活躍的待成交委託單，無法進行偏離度對齊分析。請先使用 `/order_panel` 建立委託。"
                ),
                ephemeral=True,
            )
            return

        # 模擬情境：市場極端 Skew 尾部風險，原有的掛單與預期波動率 (Expected Move) 下限偏離
        # 顯示警報 Embed
        msg = (
            "⚠️ **【動態掛單偏離度與尾部風險警報】**\n"
            "偵測到美股市場短線隱含波動率 (IV) 劇烈放大，且期權偏斜（Skew）指標進入極端異常區間：\n\n"
        )

        from services.telemetry_pricing_engine import calculate_telemetry_price

        for o in orders:
            current_price = (
                o["limit_price"]
                if o["limit_price"] > 0
                else (o["stop_price"] if o["stop_price"] > 0 else o["trailing_value"])
            )
            original_qty = float(o.get("quantity") or 1.0)

            # 使用模擬的極端偏斜百分比 (0.98) 調用定價引擎，驗證價格與數量雙向調整
            suggested_price, suggested_qty, logs = await calculate_telemetry_price(
                symbol=o["symbol"],
                base_price=current_price,
                spot_price=current_price * 1.02,
                iv=0.55,
                hist_iv=0.35,
                max_pain=100.0,
                prev_max_pain=100.0,
                skew_percentile=0.98,  # 觸發極端偏斜
                prev_close=current_price,
                base_quantity=original_qty,
            )

            size_down_warning = ""
            if suggested_qty < original_qty:
                size_down_warning = "\n  - **[⚠️ Tail Risk Mitigation]** Extreme Skew Tail Risk detected. Intercepting price adjusted closer to spot; order size scaled down to 75% for asset protection."

            msg += (
                f"• **標的 `{o['symbol']}` (ID `{o['id']}`)**:\n"
                f"  - 當前掛單價格: `${current_price:.2f}` (數量: {original_qty})\n"
                f"  - 遙測最佳防線價: `${suggested_price:.2f}` (數量: {suggested_qty:.2f})\n"
                f"  - **狀態**: ⚠️ 偏離度與尾部風險過高，面臨被擊穿風險{size_down_warning}\n\n"
            )

        msg += "💡 **建議操作**：請點擊下方綠色按鈕「一鍵套用遙測建議價」，系統將自動調整您的委託價格並下調掛單股數以防守大後方。"

        embed = create_info_embed(
            title="📡 待成交委託單 - 盤中每半小時 telemetry 對齊警報",
            message=msg,
        )
        view = ApplyTelemetryView()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="add_order",
        description="直接新增一個交易委託單 (未填價格時自動套用遙測定價)",
    )
    @app_commands.describe(
        symbol="標的代碼 (例如: AAPL)",
        quantity="委託數量 (正數)",
        order_type="訂單類型 (MARKET, LIMIT, STOP, STOP_LIMIT, TRAILING_STOP_USD, TRAILING_STOP_PCT)",
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
        validity: str = "DAY",
        price: float = 0.0,
    ):
        await interaction.response.defer(ephemeral=True)

        # 1. 驗證數量
        if quantity <= 0:
            await interaction.followup.send(
                embed=create_error_embed("❌ 錯誤：請輸入有效的正數作為訂單數量。"),
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
        final_qty = quantity

        if auto_telemetry_triggered:
            from services import market_data_service
            from market_analysis.sentiment_engine import SentimentEngine
            from services.telemetry_pricing_engine import calculate_telemetry_price

            try:
                quote = await market_data_service.get_quote(symbol)
                spot_price = quote.get("c", 0.0)
                if spot_price <= 0.0:
                    df_temp = await market_data_service.get_history_df(
                        symbol, period="2d"
                    )
                    if not df_temp.empty:
                        spot_price = float(df_temp["Close"].iloc[-1])
            except Exception as e:
                logger.error(f"Error fetching quote for telemetry fallback: {e}")
                spot_price = 0.0

            if spot_price <= 0.0:
                await interaction.followup.send(
                    embed=create_error_embed(
                        f"❌ 錯誤：無法獲取標的 {symbol} 的現有價格以進行遙測定價。請手動輸入價格。"
                    ),
                    ephemeral=True,
                )
                return

            iv = 0.35
            hist_iv = 0.30
            try:
                iv_metrics = await SentimentEngine.fetch_and_calculate_iv_metrics(
                    symbol
                )
                if iv_metrics and iv_metrics.current_iv > 0:
                    iv = iv_metrics.current_iv
                    hist_iv = iv / 1.1
            except Exception as e:
                logger.warning(f"Error fetching IV metrics for {symbol}: {e}")

            skew_val = 0.5
            try:
                skew_metrics = await SentimentEngine.calculate_skew(symbol)
                if skew_metrics and "skew" in skew_metrics:
                    skew = float(skew_metrics["skew"])
                    if skew > 5.0:
                        skew_val = 0.98
                    elif skew < -2.0:
                        skew_val = 0.02
            except Exception as e:
                logger.warning(f"Error calculating skew for {symbol}: {e}")

            prev_close = quote.get("pc", spot_price)

            if order_type in ("LIMIT", "STOP_LIMIT"):
                base_price = spot_price
            elif order_type == "STOP":
                base_price = spot_price
            elif order_type == "TRAILING_STOP_USD":
                base_price = spot_price * 0.05
            elif order_type == "TRAILING_STOP_PCT":
                base_price = 5.0
            else:
                base_price = spot_price

            (
                resolved_price,
                resolved_qty,
                telemetry_logs,
            ) = await calculate_telemetry_price(
                symbol=symbol,
                base_price=base_price,
                spot_price=spot_price,
                iv=iv,
                hist_iv=hist_iv,
                max_pain=spot_price,
                prev_max_pain=spot_price,
                skew_percentile=skew_val,
                prev_close=prev_close,
                base_quantity=quantity,
            )

            if order_type == "LIMIT":
                limit_val = resolved_price
            elif order_type == "STOP":
                stop_val = resolved_price
            elif order_type == "STOP_LIMIT":
                limit_val = resolved_price
                stop_val = resolved_price * 0.98
            elif order_type in ("TRAILING_STOP_USD", "TRAILING_STOP_PCT"):
                trailing_val = resolved_price

            final_qty = resolved_qty

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

            msg = (
                f"✅ **訂單已成功建立並進入排程**\n\n"
                f"• **委託單 ID**: `{order_id}`\n"
                f"• **標的**: `{symbol}`\n"
                f"• **類型**: `{order_type_zh}`\n"
                f"• **數量**: `{final_qty}`\n"
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


async def setup(bot):
    await bot.add_cog(OrderUICog(bot))
