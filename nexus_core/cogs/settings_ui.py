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

# ============================================================================
# 🔔 使用者自訂通知開關 UI (4 大戰術維度中控台)
# ============================================================================

TRADING_MODULES: Dict[str, Dict[str, Any]] = {
    "briefings": {
        "title": "📋 定時戰報與覆盤",
        "description": "每日盤前宏觀自選、盤後 AI 深度覆盤與週五 VTR 績效週報。",
        "items": {
            "briefing_pre_market": "🌅 盤前綜合戰報 (09:00 ET)",
            "briefing_post_market": "📋 盤後 AI 深度覆盤 (16:15 ET)",
            "briefing_weekly_vtr": "📈 虛擬交易室 (VTR) 績效週報 (週五 17:05 ET)",
        },
    },
    "telemetry": {
        "title": "📡 盤中自選與掛單遙測",
        "description": "盤中每 30 分鐘主動推送自選股量化雷達與掛單對齊。",
        "items": {
            "heartbeat_watchlist": "🧱 自選股 30 分鐘戰場心跳 (含微觀結構、Skew 與 UOA 巨鯨)",
            "telemetry_orders": "🌌 待成交掛單實時對齊與撤退線",
        },
    },
    "defense": {
        "title": "🛡️ 持倉風控與極端防禦",
        "description": "即時監控持倉負 Gamma、DITM 獲利鎖定、動態轉倉與黑天鵝。",
        "items": {
            "defense_portfolio_risk": "🆘 持倉負 Gamma 斷層、DITM 獲利鎖定與保證金警戒",
            "defense_option_rollover": "🔄 動態轉倉、套牢股票備兌解套與衛星再平衡",
            "defense_margin_call": "🚨 槓桿與保證金強制平倉警報 (帳戶生存等級)",
            "defense_fundamental_thesis": "📜 SEC 財報自動掃描與護城河破滅警報",
            "defense_macro_tail_risk": "🦇 VIX 期限結構倒掛 (VTS >= 1.0) 與重大事件防護",
        },
    },
    "alpha": {
        "title": "🎯 Alpha 策略與情報",
        "description": "即時捕捉 Nexus 量化 Alpha 機會、Polymarket 巨鯨與原油異動。",
        "items": {
            "alpha_market_signals": "✨ Nexus 戴維斯雙擊 (DDP) 與波動率優勢 (廉價期權)",
            "alpha_polymarket": "🐳 Polymarket 巨鯨異動與預測機率突變 (Delta 閃崩/暴拉)",
            "alpha_wti_oil": "🛢️ WTI 原油價格警報 (閾值突破與劇烈波動)",
            "alpha_price_volume_watch": "📊 個股 15 分鐘價量突破警報 (自訂目標價與放量倍數)",
        },
    },
}


class NotificationSettingsView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self.current_module = "briefings"
        self.refresh_items()

    def refresh_items(self) -> None:
        self.clear_items()
        settings = database.get_user_notification_settings(self.user_id)

        # 1. 模組分類導航選單 (Category Selector - Row 0)
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

        # 2. 當前模組的設定開關 (Toggle Select - Row 1)
        module_items = TRADING_MODULES[self.current_module]["items"]
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

        # 3. 本區批次按鈕 (Row 2)
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

        # 4. 全域快捷情境模式按鈕 (Preset Quick Buttons - Row 3)
        btn_all_on = discord.ui.Button(  # type: ignore
            label="🛡️ 戰備全開",
            style=discord.ButtonStyle.secondary,
            custom_id="btn_preset_all_on",
            row=3,
        )
        btn_all_on.callback = self.on_preset_all_on  # type: ignore
        self.add_item(btn_all_on)

        btn_focus = discord.ui.Button(  # type: ignore
            label="🎯 精準交易",
            style=discord.ButtonStyle.primary,
            custom_id="btn_preset_focus",
            row=3,
        )
        btn_focus.callback = self.on_preset_focus  # type: ignore
        self.add_item(btn_focus)

        btn_mute = discord.ui.Button(  # type: ignore
            label="🔕 盤中靜音",
            style=discord.ButtonStyle.secondary,
            custom_id="btn_preset_mute",
            row=3,
        )
        btn_mute.callback = self.on_preset_mute_intraday  # type: ignore
        self.add_item(btn_mute)

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

    async def on_preset_all_on(self, interaction: discord.Interaction) -> Any:
        database.apply_preset_settings(self.user_id, "all_on")
        self.refresh_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_preset_focus(self, interaction: discord.Interaction) -> Any:
        database.apply_preset_settings(self.user_id, "focus")
        self.refresh_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_preset_mute_intraday(self, interaction: discord.Interaction) -> Any:
        database.apply_preset_settings(self.user_id, "mute_intraday")
        self.refresh_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_select_callback(self, interaction: discord.Interaction) -> Any:
        if interaction.data is None or not isinstance(interaction.data, dict):
            return
        select_values = interaction.data.get("values")
        if not select_values or not isinstance(select_values, list):
            return

        key = str(select_values[0])
        settings = database.get_user_notification_settings(self.user_id)
        current_val = settings.get(key, True)
        database.set_user_notification_setting(self.user_id, key, not current_val)

        self.refresh_items()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    def build_embed(self) -> discord.Embed:
        settings = database.get_user_notification_settings(self.user_id)

        module_fields = []
        for mod_key, mod_data in TRADING_MODULES.items():
            lines = []
            for item_key, item_label in mod_data["items"].items():
                status = "🟢 開啟" if settings.get(item_key, True) else "🔴 關閉"
                lines.append(f"* {item_label}: **{status}**")

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
    "polymarket_threshold": (
        "🐋 Polymarket 巨鯨門檻",
        "Polymarket 巨鯨監控門檻 (USD, 0=關閉)",
        "輸入大於等於 0 的金額",
    ),
    "polymarket_use_llm": (
        "🧠 Polymarket AI 分析",
        "是否啟用 Polymarket 巨鯨 AI 解讀分析",
        None,
    ),
    "polymarket_slippage": (
        "🌊 Polymarket 滑價門檻",
        "Polymarket 巨鯨判定目標滑價百分比 (0.1% - 10.0%)",
        "輸入 0.1 - 10.0 之間的百分比",
    ),
    "escape_window": (
        "📅 宏觀逃頂窗口 (起~訖)",
        "設定自訂逃頂窗口 (MM-DD ~ MM-DD，如 09-15 ~ 09-30)",
        "09-15 ~ 09-30",
    ),
}


class AccountSettingsModal(discord.ui.Modal):
    def __init__(
        self,
        user_id: int,
        key: str,
        label: str,
        current_value: Any,
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

        if self.key == "escape_window":
            import re

            m = re.search(
                r"(\d{1,2})-(\d{1,2})\s*[\~\,\-至到]\s*(\d{1,2})-(\d{1,2})",
                value_str,
            )
            if not m:
                await interaction.response.send_message(
                    embed=create_error_embed(
                        "逃頂窗口格式錯誤，請使用 MM-DD ~ MM-DD 格式 (例如 09-15 ~ 09-30)",
                        title="輸入錯誤",
                    ),
                    ephemeral=True,
                )
                return
            sm, sd, em, ed = map(int, m.groups())
            if not (
                1 <= sm <= 12 and 1 <= sd <= 31 and 1 <= em <= 12 and 1 <= ed <= 31
            ):
                await interaction.response.send_message(
                    embed=create_error_embed(
                        "月份 (1-12) 或日期 (1-31) 超出有效範圍",
                        title="驗證失敗",
                    ),
                    ephemeral=True,
                )
                return
            start_str = f"{sm:02d}-{sd:02d}"
            end_str = f"{em:02d}-{ed:02d}"
            database.upsert_user_config(
                self.user_id,
                escape_window_start=start_str,
                escape_window_end=end_str,
            )
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
                        title="系統資訊", message="✅ 逃頂窗口已成功更新！"
                    ),
                    ephemeral=True,
                )
            return

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
            elif key == "escape_window":
                val_display = f"{ctx.escape_window_start} ~ {ctx.escape_window_end}"
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
            "polymarket_use_llm",
            "can_trade_spreads",
            "cash_reserve_protection",
        ]:
            current_bool = getattr(ctx, key, False)
            new_val = not current_bool
            database.upsert_user_config(self.user_id, **{key: new_val})

            self.refresh_items()
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            # 針對數值/字串類型，彈出 Modal 視窗
            modal_val: Any
            if key == "escape_window":
                modal_val = f"{ctx.escape_window_start} ~ {ctx.escape_window_end}"
            else:
                modal_val = getattr(ctx, key, 0.0)
            label, desc, placeholder = SETTINGS_LABELS[key]
            modal = AccountSettingsModal(
                user_id=self.user_id,
                key=key,
                label=label,
                current_value=modal_val,
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
            f"📅 **宏觀逃頂窗口**: `{ctx.escape_window_start} ~ {ctx.escape_window_end}`",
            f"👻 **虛擬交易室 (VTR) 跟單**: `{'🟢 開啟' if ctx.enable_vtr else '🔴 關閉'}`",
            f"⚡ **PowerSqueeze 追蹤**: `{'🟢 開啟' if ctx.enable_psq_watchlist else '🔴 關閉'}`",
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


class WtiConfigModal(discord.ui.Modal, title="🛢️ WTI 原油價格警報閾值設定"):
    """WTI 油價警報用戶閾值設定 Modal。"""

    upper: discord.ui.TextInput = discord.ui.TextInput(
        label="價格上限 (美元，留空表示不限制)",
        placeholder="例如: 95.00",
        required=False,
        max_length=10,
    )
    lower: discord.ui.TextInput = discord.ui.TextInput(
        label="價格下限 (美元，留空表示不限制)",
        placeholder="例如: 65.00",
        required=False,
        max_length=10,
    )
    pct: discord.ui.TextInput = discord.ui.TextInput(
        label="30 分鐘波動閾值 (%)",
        placeholder="例如: 3.0",
        required=False,
        max_length=10,
    )

    def __init__(self, current: Any) -> None:
        super().__init__()
        if current.upper_price is not None:
            self.upper.default = str(current.upper_price)
        if current.lower_price is not None:
            self.lower.default = str(current.lower_price)
        self.pct.default = str(current.pct_change_threshold)

    async def on_submit(self, interaction: discord.Interaction) -> Any:
        from database.wti_config import WtiAlertConfig, save_wti_config
        from cogs.embed_builders.settings_embeds import (
            create_info_embed,
            create_error_embed,
        )
        from pydantic import ValidationError

        try:
            upper_val = (
                float(self.upper.value.strip()) if self.upper.value.strip() else None
            )
            lower_val = (
                float(self.lower.value.strip()) if self.lower.value.strip() else None
            )
            pct_val = float(self.pct.value.strip()) if self.pct.value.strip() else 3.0

            config = WtiAlertConfig(
                upper_price=upper_val,
                lower_price=lower_val,
                pct_change_threshold=pct_val,
            )
            await save_wti_config(interaction.user.id, config)

            desc_parts: list[str] = [
                f"• 上限價格: `{f'${config.upper_price:.2f}' if config.upper_price is not None else '未設定'}`",
                f"• 下限價格: `{f'${config.lower_price:.2f}' if config.lower_price is not None else '未設定'}`",
                f"• 30分波動: `±{config.pct_change_threshold:.1f}%`",
                "\n💡 當 WTI 期貨 (`CL=F`) 觸發以上條件時，系統將主動發送富含技術指標與關聯股分析的戰術情報卡片。",
            ]

            embed = create_info_embed(
                "WTI 原油價格警報閾值已更新", "\n".join(desc_parts)
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except (ValueError, ValidationError) as e:
            embed = create_error_embed(
                f"請輸入有效的數值格式 (例如 95.00 或 3.0)。\n詳細錯誤: `{e}`",
                title="輸入格式錯誤",
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
