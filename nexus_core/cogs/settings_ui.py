from typing import Any, Dict
import discord
import logging

import database
from cogs.embed_builder import (
    create_error_embed,
    create_info_embed,
    create_notification_settings_embed,
    create_account_settings_embed,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 🔔 使用者自訂通知開關 UI (Notification Toggles UI)
# ============================================================================

TRADING_MODULES: Dict[str, Dict[str, Any]] = {
    "portfolio": {
        "title": "🛡️ 部位管理與執行風控",
        "description": "專注於買賣點的動態對齊、持倉防禦與物理狀態管理。",
        "items": {
            "order_telemetry_alignment_alert": "🌌 快照：待成交委託單實時對齊",
            "hb_execution_risk": "🛡️ 心跳：操盤指引與委託風控",
            "radar_risk_defenses": "🛡️ 雷達：量化風控與避險屏障",
            "deadlock_recovery_alert": "🔓 警報：物理死鎖解除與備兌建單",
        },
    },
    "macro": {
        "title": "🌍 總經與微觀結構警戒",
        "description": "專注於市場水位、Gamma 脆弱性與機構暗池流動性。",
        "items": {
            "radar_macro_edge": "🌍 雷達：總經與微觀結構警戒",
            "gamma_fragility_alert": "🆘 警報：Gamma 脆弱性與斷層",
            "hb_options_structure": "🧱 心跳：期權結構與波動率",
        },
    },
    "alpha": {
        "title": "🎯 Alpha 獵取與異常數據",
        "description": "專注於發掘高勝率結構與機構異常行為。",
        "items": {
            "hb_uoa": "🔎 心跳：異常大單穿透 (UOA)",
            "radar_alpha_signals": "🎯 雷達：期權 Alpha 與異常訊號",
            "ddp_cheap_vol_alert": "🌌 警報：Nexus 戴維斯雙擊",
        },
    },
    "defense": {
        "title": "🚨 極端風險防禦系統",
        "description": "專注於保護既有獲利與規避毀滅性黑天鵝。",
        "items": {
            "profit_lock_alert": "🚨 警報：DITM 凸性防護與獲利鎖定",
            "option_defense_alert": "🛡️ 警報：期權轉倉防禦與結算",
            "rollover_rebalance_alert": "🔄 警報：動態轉倉與再平衡防禦",
            "volatility_risk_alert": "🛡️ 警報：重大事件即時防護",
        },
    },
    "briefings": {
        "title": "📋 每日綜整戰報",
        "description": "專注於每日復盤與盤前/盤後的結構化梳理。",
        "items": {
            "pre_market_briefing": "🌅 報告：盤前綜合宏觀與自選股",
            "intraday_decision_scan": "📊 報告：盤中量化掃描與避險執行",
            "post_market_intelligence": "📋 報告：盤後綜合風險與 AI 策略",
            "weekly_vtr_report": "📈 報告：虛擬交易室 (VTR) 績效總結",
        },
    },
    "polymarket": {
        "title": "🐳 Polymarket 巨鯨與 AI 監控",
        "description": "專注於 Polymarket 巨鯨動向監控與 AI 預測分析。",
        "items": {
            "polymarket_whale_alert": "🐳 警報：巨鯨交易異動",
            "polymarket_threshold": "🐋 設定：巨鯨監控門檻",
            "polymarket_use_llm": "🧠 設定：Polymarket AI 分析",
            "polymarket_slippage": "🌊 設定：Polymarket 滑價門檻",
        },
    },
}


class NotificationSettingsModal(discord.ui.Modal):
    def __init__(
        self,
        user_id: int,
        key: str,
        label: str,
        current_value: float,
        placeholder: str,
        view: discord.ui.View,
    ):
        super().__init__(title=f"設定 - {label}")
        self.user_id = user_id
        self.key = key
        self.label = label
        self.view = view

        self.input_field: discord.ui.TextInput = discord.ui.TextInput(
            label=f"請輸入新的數值 (目前: {current_value})",
            placeholder=placeholder,
            default=str(current_value),
            required=True,
            max_length=50,
        )
        self.add_item(self.input_field)

    async def on_submit(self, interaction: discord.Interaction) -> Any:
        value_str = self.input_field.value.strip()
        try:
            val = float(value_str)
        except ValueError:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "輸入無效，必須是有效的數字或小數。", title="輸入錯誤"
                ),
                ephemeral=True,
            )
            return

        # 數值邊界驗證與防錯
        if self.key == "polymarket_threshold":
            if val < 0:
                await interaction.response.send_message(
                    embed=create_error_embed("金額不能為負數", title="驗證失敗"),
                    ephemeral=True,
                )
                return
        elif self.key == "polymarket_slippage":
            if not (0.1 <= val <= 10.0):
                await interaction.response.send_message(
                    embed=create_error_embed(
                        "滑價門檻需介於 0.1% 至 10.0% 之間", title="驗證失敗"
                    ),
                    ephemeral=True,
                )
                return

        # 更新資料庫
        success = database.upsert_user_config(self.user_id, **{self.key: val})
        if not success:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "設定更新失敗，請稍後再試。", title="系統錯誤"
                ),
                ephemeral=True,
            )
            return

        # 刷新檢視
        if (
            self.view is not None
            and hasattr(self.view, "refresh_items")
            and hasattr(self.view, "build_embed")
        ):
            getattr(self.view, "refresh_items")()
            embed = getattr(self.view, "build_embed")()
            await interaction.response.edit_message(embed=embed, view=self.view)
        else:
            await interaction.response.send_message(
                embed=create_info_embed(
                    title="系統資訊", message="✅ 設定已成功更新！"
                ),
                ephemeral=True,
            )


class NotificationSettingsView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self.current_module = "portfolio"
        self.refresh_items()

    def refresh_items(self) -> None:
        self.clear_items()
        settings = database.get_user_notification_settings(self.user_id)
        ctx = database.get_full_user_context(self.user_id)

        # 1. 模組分類導航選單 (Category Selector)
        category_options = []
        for mod_key, mod_data in TRADING_MODULES.items():
            is_selected = mod_key == self.current_module
            category_options.append(
                discord.SelectOption(
                    label=mod_data["title"],
                    value=mod_key,
                    description=mod_data["description"][:100],
                    default=is_selected,
                )
            )

        category_select = discord.ui.Select(  # type: ignore
            placeholder="請選擇戰術模組...",
            options=category_options,
            custom_id="select_category",
            row=0,
        )
        category_select.callback = self.on_category_select  # type: ignore
        self.add_item(category_select)

        # 2. 當前模組的設定開關 (Toggle Select)
        module_items = TRADING_MODULES[self.current_module]["items"]
        if self.current_module != "polymarket":
            toggle_options = []
            for key, label in module_items.items():
                state_emoji = "🟢" if settings.get(key, True) else "🔴"
                toggle_options.append(
                    discord.SelectOption(
                        label=f"{state_emoji} {label}",
                        value=key,
                        description="點擊切換開啟/關閉狀態",
                    )
                )
            if toggle_options:
                toggle_select = discord.ui.Select(  # type: ignore
                    placeholder=f"設定 {TRADING_MODULES[self.current_module]['title']}...",
                    options=toggle_options,
                    custom_id="select_toggles",
                    row=1,
                )
                toggle_select.callback = self.on_select_callback  # type: ignore
                self.add_item(toggle_select)

            # 3. 按鈕 (Enable All / Disable All for current module)
            btn_enable = discord.ui.Button(  # type: ignore
                label="⚡ 開啟本區所有設定",
                style=discord.ButtonStyle.green,
                custom_id="btn_enable_module",
                row=2,
            )
            btn_enable.callback = self.on_enable_module  # type: ignore
            self.add_item(btn_enable)

            btn_disable = discord.ui.Button(  # type: ignore
                label="💤 關閉本區所有設定",
                style=discord.ButtonStyle.red,
                custom_id="btn_disable_module",
                row=2,
            )
            btn_disable.callback = self.on_disable_module  # type: ignore
            self.add_item(btn_disable)
        else:
            polymarket_options = []
            whale_alert = settings.get("polymarket_whale_alert", True)
            polymarket_options.append(
                discord.SelectOption(
                    label=module_items["polymarket_whale_alert"],
                    value="polymarket_whale_alert",
                    description=f"目前: {'🟢 開啟' if whale_alert else '🔴 關閉'} | 切換開關"[
                        :100
                    ],
                )
            )
            polymarket_options.append(
                discord.SelectOption(
                    label=module_items["polymarket_threshold"],
                    value="polymarket_threshold",
                    description=f"目前: {'🟢 $' + f'{ctx.polymarket_threshold:,.0f}' if ctx.polymarket_threshold > 0 else '🔴 關閉'} | 設定門檻"[
                        :100
                    ],
                )
            )
            polymarket_options.append(
                discord.SelectOption(
                    label=module_items["polymarket_use_llm"],
                    value="polymarket_use_llm",
                    description=f"目前: {'🟢 開啟' if ctx.polymarket_use_llm else '🔴 關閉'} | 切換開關"[
                        :100
                    ],
                )
            )
            polymarket_options.append(
                discord.SelectOption(
                    label=module_items["polymarket_slippage"],
                    value="polymarket_slippage",
                    description=f"目前: {ctx.polymarket_slippage}% | 設定滑價"[:100],
                )
            )

            pm_select = discord.ui.Select(  # type: ignore
                placeholder="🐳 設定 Polymarket 巨鯨與 AI 監控...",
                options=polymarket_options,
                custom_id="select_polymarket",
                row=1,
            )
            pm_select.callback = self.on_select_callback  # type: ignore
            self.add_item(pm_select)

    async def on_category_select(self, interaction: discord.Interaction) -> Any:
        if not interaction.data or not isinstance(interaction.data, dict):
            return
        select_values = interaction.data.get("values")
        if not select_values or not isinstance(select_values, list):
            return

        self.current_module = str(select_values[0])
        self.refresh_items()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_enable_module(self, interaction: discord.Interaction) -> Any:
        module_items = TRADING_MODULES[self.current_module]["items"]
        for key in module_items.keys():
            database.set_user_notification_setting(self.user_id, key, True)
        self.refresh_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_disable_module(self, interaction: discord.Interaction) -> Any:
        module_items = TRADING_MODULES[self.current_module]["items"]
        for key in module_items.keys():
            database.set_user_notification_setting(self.user_id, key, False)
        self.refresh_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_select_callback(self, interaction: discord.Interaction) -> Any:
        if interaction.data is None or not isinstance(interaction.data, dict):
            return
        select_values = interaction.data.get("values")
        if not select_values or not isinstance(select_values, list):
            return

        key = str(select_values[0])
        ctx = database.get_full_user_context(self.user_id)

        if key in ["polymarket_threshold", "polymarket_slippage"]:
            current_val = getattr(ctx, key, 0.0)
            if key == "polymarket_threshold":
                label, _, placeholder = (
                    TRADING_MODULES["polymarket"]["items"]["polymarket_threshold"],
                    "Polymarket 巨鯨監控門檻 (USD, 0=關閉)",
                    "輸入大於等於 0 的金額",
                )
            else:
                label, _, placeholder = (
                    TRADING_MODULES["polymarket"]["items"]["polymarket_slippage"],
                    "Polymarket 巨鯨判定目標滑價百分比 (0.1% - 10.0%)",
                    "輸入 0.1 - 10.0 之間的百分比",
                )

            modal = NotificationSettingsModal(
                user_id=self.user_id,
                key=key,
                label=label,
                current_value=current_val,
                placeholder=placeholder,
                view=self,
            )
            await interaction.response.send_modal(modal)
            return

        if key == "polymarket_use_llm":
            new_val = not ctx.polymarket_use_llm
            database.upsert_user_config(self.user_id, polymarket_use_llm=new_val)
        else:
            settings = database.get_user_notification_settings(self.user_id)
            current_val = settings.get(key, True)
            database.set_user_notification_setting(self.user_id, key, not current_val)

        self.refresh_items()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    def build_embed(self) -> discord.Embed:
        settings = database.get_user_notification_settings(self.user_id)
        ctx = database.get_full_user_context(self.user_id)

        module_fields = []
        for mod_key, mod_data in TRADING_MODULES.items():
            lines = []
            if mod_key == "polymarket":
                pm_alerts = settings.get("polymarket_whale_alert", True)
                lines.append(
                    f"* {mod_data['items']['polymarket_whale_alert']}: **{'🟢 開啟' if pm_alerts else '🔴 關閉'}**"
                )
                lines.append(
                    f"* {mod_data['items']['polymarket_threshold']}: **{'🟢 $' + f'{ctx.polymarket_threshold:,.0f}' if ctx.polymarket_threshold > 0 else '🔴 關閉'}**"
                )
                lines.append(
                    f"* {mod_data['items']['polymarket_use_llm']}: **{'🟢 開啟' if ctx.polymarket_use_llm else '🔴 關閉'}**"
                )
                lines.append(
                    f"* {mod_data['items']['polymarket_slippage']}: **`{ctx.polymarket_slippage}%`**"
                )
            else:
                for item_key, item_label in mod_data["items"].items():
                    status = "🟢 開啟" if settings.get(item_key, True) else "🔴 關閉"
                    lines.append(f"* {item_label}: **{status}**")

            # Show a marker for current module
            marker = "🔹 " if mod_key == self.current_module else ""
            module_fields.append((f"{marker}{mod_data['title']}", "\n".join(lines)))

        return create_notification_settings_embed(module_fields)


# ============================================================================
# ⚙️ 使用者全域參數設定 UI (Interactive Account Settings UI)
# ============================================================================

SETTINGS_LABELS = {
    "risk_limit": (
        "🛡️ 基準風險上限 %",
        "更新基準風險上限 % (1.0 - 50.0)",
        "輸入 1.0 - 50.0 之間的數值",
    ),
    "enable_vtr": (
        "👻 虛擬交易室 (VTR)",
        "是否啟用虛擬交易室 GhostTrader 自動建倉",
        None,
    ),
    "enable_psq_watchlist": (
        "⚡ PowerSqueeze 追蹤",
        "是否對自選股開啟 PowerSqueeze 戰情追蹤",
        None,
    ),
    "enable_local_tunnel": (
        "🛜 本地 Tunnel 呼叫",
        "是否允許呼叫本地 Tunnel/Edge Scraper（關閉時將不做任何 Tunnel I/O）",
        None,
    ),
    "monthly_expense": (
        "💸 每月支出預算",
        "每月生存支出預算 (USD, 用於財務跑道分析)",
        "輸入大於等於 0 的預算",
    ),
    "tax_reserve_rate": (
        "🏦 稅務預留比例",
        "稅務預留比例 (0.0 - 1.0)",
        "輸入 0.0 - 1.0 之間的數值",
    ),
    "cash_reserve": (
        "💰 現金儲備金額",
        "現金儲備金額 (USD, 用於生存天數計算)",
        "輸入大於等於 0 的現金儲備",
    ),
    "can_trade_spreads": (
        "📈 期權 Spread 權限",
        "是否具備複式選擇權 (Spread) 交易權限",
        None,
    ),
    "cash_reserve_protection": (
        "🛡️ 備用金防護",
        "是否啟動備用金與新資金動用率風控防禦",
        None,
    ),
}


class AccountSettingsModal(discord.ui.Modal):
    def __init__(
        self,
        user_id: int,
        key: str,
        label: str,
        current_value: float,
        placeholder: str,
        view: discord.ui.View,
    ):
        super().__init__(title=f"設定 - {label}")
        self.user_id = user_id
        self.key = key
        self.label = label
        self.view = view

        self.input_field: discord.ui.TextInput = discord.ui.TextInput(
            label=f"請輸入新的數值 (目前: {current_value})",
            placeholder=placeholder,
            default=str(current_value),
            required=True,
            max_length=50,
        )
        self.add_item(self.input_field)

    async def on_submit(self, interaction: discord.Interaction) -> Any:
        value_str = self.input_field.value.strip()
        try:
            val = float(value_str)
        except ValueError:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "輸入無效，必須是有效的數字或小數。", title="輸入錯誤"
                ),
                ephemeral=True,
            )
            return

        # 數值邊界驗證與防錯
        if self.key == "risk_limit":
            if not (1.0 <= val <= 50.0):
                await interaction.response.send_message(
                    embed=create_error_embed(
                        "風險限制需介於 1.0% 至 50.0% 之間", title="驗證失敗"
                    ),
                    ephemeral=True,
                )
                return
        elif self.key in ["polymarket_threshold", "monthly_expense", "cash_reserve"]:
            if val < 0:
                await interaction.response.send_message(
                    embed=create_error_embed("金額不能為負數", title="驗證失敗"),
                    ephemeral=True,
                )
                return
        elif self.key == "polymarket_slippage":
            if not (0.1 <= val <= 10.0):
                await interaction.response.send_message(
                    embed=create_error_embed(
                        "滑價門檻需介於 0.1% 至 10.0% 之間", title="驗證失敗"
                    ),
                    ephemeral=True,
                )
                return
        elif self.key == "tax_reserve_rate":
            # 支援百分比輸入 (例如輸入 20 轉換成 0.20)
            if val > 1.0:
                val = val / 100.0
            if not (0.0 <= val <= 1.0):
                await interaction.response.send_message(
                    embed=create_error_embed(
                        "稅務比例需介於 0.0 與 1.0 之間", title="驗證失敗"
                    ),
                    ephemeral=True,
                )
                return

        # 更新資料庫
        success = database.upsert_user_config(self.user_id, **{self.key: val})
        if not success:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "設定更新失敗，請稍後再試。", title="系統錯誤"
                ),
                ephemeral=True,
            )
            return

        # 刷新檢視
        if (
            self.view is not None
            and hasattr(self.view, "refresh_items")
            and hasattr(self.view, "build_embed")
        ):
            getattr(self.view, "refresh_items")()
            embed = getattr(self.view, "build_embed")()
            await interaction.response.edit_message(embed=embed, view=self.view)
        else:
            await interaction.response.send_message(
                embed=create_info_embed(
                    title="系統資訊", message="✅ 設定已成功更新！"
                ),
                ephemeral=True,
            )


class AccountSettingsView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self.refresh_items()

    def refresh_items(self) -> None:
        self.clear_items()
        ctx = database.get_full_user_context(self.user_id)

        # 動態生成下拉選單選項
        options = []
        for key, (label, desc, placeholder) in SETTINGS_LABELS.items():
            # 獲取當前設定值
            raw_val = getattr(ctx, key, None)

            # 美化展示格式
            if isinstance(raw_val, bool):
                val_display = "開啟" if raw_val else "關閉"
            elif key == "capital":
                val_display = f"${raw_val:,.2f}"
            elif key == "risk_limit":
                val_display = f"{raw_val}%"
            elif key in ["polymarket_threshold", "monthly_expense", "cash_reserve"]:
                val_display = f"${raw_val:,.0f}" if raw_val > 0 else "關閉/未設定"  # type: ignore
            elif key == "polymarket_slippage":
                val_display = f"{raw_val}%"
            elif key == "tax_reserve_rate":
                val_display = f"{raw_val:.1%}"
            else:
                val_display = str(raw_val)

            options.append(
                discord.SelectOption(
                    label=label,
                    value=key,
                    description=f"目前: {val_display} | {desc}"[:100],
                )
            )

        select = discord.ui.Select(  # type: ignore
            placeholder="⚙️ 請選擇要配置的帳戶全域參數...",
            options=options,
            custom_id="select_account_settings",
            row=0,
        )
        select.callback = self.on_select_callback  # type: ignore
        self.add_item(select)

    async def on_select_callback(self, interaction: discord.Interaction) -> Any:
        if interaction.data is None or not isinstance(interaction.data, dict):
            return
        select_values = interaction.data.get("values")
        if not select_values or not isinstance(select_values, list):
            return

        key = str(select_values[0])
        ctx = database.get_full_user_context(self.user_id)

        # 針對布林值，直接切換狀態
        if key in [
            "enable_vtr",
            "enable_psq_watchlist",
            "enable_local_tunnel",
            "polymarket_use_llm",
            "can_trade_spreads",
            "cash_reserve_protection",
        ]:
            current_val = getattr(ctx, key, False)
            new_val = not current_val
            database.upsert_user_config(self.user_id, **{key: new_val})

            self.refresh_items()
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            # 針對數值類型，彈出 Modal 視窗
            current_val = getattr(ctx, key, 0.0)
            label, desc, placeholder = SETTINGS_LABELS[key]
            modal = AccountSettingsModal(
                user_id=self.user_id,
                key=key,
                label=label,
                current_value=current_val,
                placeholder=placeholder or "",
                view=self,
            )
            await interaction.response.send_modal(modal)

    def build_embed(self) -> discord.Embed:
        ctx = database.get_full_user_context(self.user_id)

        # 分類展示當前設定
        basic_settings = [
            f"💰 **總資金**: `${ctx.capital:,.2f}` *(自動計算)*",
            f"🛡️ **基準風險上限**: `{ctx.risk_limit}%`",
            f"👻 **虛擬交易室 (VTR) 跟單**: `{'🟢 開啟' if ctx.enable_vtr else '🔴 關閉'}`",
            f"⚡ **PowerSqueeze 追蹤**: `{'🟢 開啟' if ctx.enable_psq_watchlist else '🔴 關閉'}`",
            f"🛜 **本地 Tunnel 呼叫**: `{'🟢 開啟' if ctx.enable_local_tunnel else '🔴 關閉'}`",
            f"📈 **期權 Spread 權限**: `{'🟢 開啟' if ctx.can_trade_spreads else '🔴 關閉'}`",
            f"🛡️ **備用金防護**: `{'🟢 開啟' if ctx.cash_reserve_protection else '🔴 關閉'}`",
        ]

        runway_settings = [
            f"💸 **每月生存支出預算**: `${ctx.monthly_expense:,.0f}`",
            f"🏦 **稅務預留比例**: `{ctx.tax_reserve_rate:.1%}`",
            f"💰 **現金儲備金額**: `${ctx.cash_reserve:,.0f}`",
        ]

        return create_account_settings_embed(
            basic_settings=basic_settings, runway_settings=runway_settings
        )

    @discord.ui.button(
        label="🏷️ 編輯自選標籤",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_edit_watchlist_tags",
        row=1,
    )
    async def edit_watchlist_tags_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> Any:
        from ui.watchlist_tags import WatchlistTagSelectView
        from cogs.embed_builders.settings_embeds import create_info_embed

        view = WatchlistTagSelectView(self.user_id)
        embed = create_info_embed(
            title="編輯自選標籤", message="請從下方選單選擇一個自選標的來編輯它的標籤。"
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
