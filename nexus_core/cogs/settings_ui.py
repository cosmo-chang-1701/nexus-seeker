import asyncio
from typing import Any
import discord
import logging

import database
from database.notification_channels import TRADING_MODULES as TRADING_MODULES
from cogs.embed_builder import (
    create_error_embed,
    create_info_embed,
    create_notification_settings_embed,
    create_account_settings_embed,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 🔔 使用者自訂通知開關 UI (4 大戰術維度中控台)
# ============================================================================
# 模組分組與標籤由 database/notification_channels.py 的註冊表衍生（單一真實來源），
# TRADING_MODULES 由此重新匯出以相容既有呼叫者。


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
            await asyncio.to_thread(
                database.set_user_notification_setting, self.user_id, key, True
            )
        self.refresh_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_disable_module(self, interaction: discord.Interaction) -> Any:
        module_items = TRADING_MODULES[self.current_module]["items"]
        for key in module_items.keys():
            await asyncio.to_thread(
                database.set_user_notification_setting, self.user_id, key, False
            )
        self.refresh_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_preset_all_on(self, interaction: discord.Interaction) -> Any:
        await asyncio.to_thread(database.apply_preset_settings, self.user_id, "all_on")
        self.refresh_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_preset_focus(self, interaction: discord.Interaction) -> Any:
        await asyncio.to_thread(database.apply_preset_settings, self.user_id, "focus")
        self.refresh_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_preset_mute_intraday(self, interaction: discord.Interaction) -> Any:
        await asyncio.to_thread(
            database.apply_preset_settings, self.user_id, "mute_intraday"
        )
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
        await asyncio.to_thread(
            database.set_user_notification_setting, self.user_id, key, not current_val
        )

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
    "enable_macro_top_escape_defense": (
        "🧭 宏觀逃頂前瞻防禦",
        "是否啟用逃頂綜合評分達最高警戒時的主動衛星減碼防禦 (Dynamic Rollover Scenario 6)",
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
    "trading_strategy": (
        "📐 交易策略",
        "選擇動態轉倉引擎的進場邏輯模式 (動態調整/左側交易/右側交易/做空交易)",
        None,
    ),
    "risk_appetite": (
        "⚖️ 風險偏好",
        "選擇動態轉倉引擎的 TP 階梯比例、EV 轉倉門檻與核心資金部署比例 (防禦/進攻)",
        None,
    ),
    "portfolio_mode": (
        "🧭 持倉管理模式",
        "選擇持倉停利停損的輸出語意：指令 (建議減碼/換股) 或顧問 (僅告知位階，適合 Buy & Hold)",
        None,
    ),
}

# 交易策略模式 (trading_strategy) 中文顯示對照與說明 —— DB 內部一律儲存英文
# enum code (比照 market_analysis/dynamic_rollover/models.py::RolloverScenario 的
# 既有慣例)，這裡是純呈現層 mapping，不落地到 DB。
TRADING_STRATEGY_DISPLAY = {
    "DYNAMIC": "動態調整",
    "LEFT_SIDE": "左側交易",
    "RIGHT_SIDE": "右側交易",
    "SHORT_SIDE": "做空交易",
}

# 風險偏好 (risk_appetite) 中文顯示對照與說明 —— 同上，DB 內部一律儲存英文
# enum code，此處為純呈現層 mapping。
RISK_APPETITE_DISPLAY = {
    "DEFENSIVE": "穩健防禦",
    "AGGRESSIVE": "動能進攻",
}

# ⚠️ 左側交易**仍是做多**（逆勢均值回歸、Put Wall 底牆接刀，算的是向上回歸
# 空間）。做空交易 (SHORT_SIDE) 才是本系統唯一的空頭方向進場路徑，描述文字
# 刻意寫明方向，避免使用者把「左側」誤讀為「做空」。
TRADING_STRATEGY_DESCRIPTIONS = {
    "DYNAMIC": "5 態 Regime 路由：依結構自動切換左/右側/做空六重鐵律或強制鎖倉",
    "LEFT_SIDE": "做多・逆勢均值回歸：Put Wall 底牆接刀六重鐵律",
    "RIGHT_SIDE": "做多・順勢動能突破：現行六重鐵律 (預設)",
    "SHORT_SIDE": "做空・結構破位追空：負 Gamma 順勢助跌六重鐵律",
}

# 持倉管理模式 (portfolio_mode) —— 同上為純呈現層 mapping。
PORTFOLIO_MODE_DISPLAY = {
    "COMMAND": "指令模式",
    "ADVISORY": "顧問模式",
}

PORTFOLIO_MODE_DESCRIPTIONS = {
    "COMMAND": "停利/停損/比例控管輸出減碼與換股指令 (預設，現行行為)",
    "ADVISORY": "僅告知目標區與結構失效位階，不建議減碼換股 (適合 Buy & Hold)",
}

RISK_APPETITE_DESCRIPTIONS = {
    "DEFENSIVE": "TP1 執行 50%、EV 門檻較高、核心資金機會部署 50% (預設，現行行為)",
    "AGGRESSIVE": "TP1 執行 30%、EV 門檻較低、核心資金機會部署 80% (2025 回測驗證)",
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
            await asyncio.to_thread(
                database.upsert_user_config,
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
        elif self.key in [
            "polymarket_threshold",
            "monthly_expense",
            "cash_reserve",
        ]:
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
        success = await asyncio.to_thread(
            database.upsert_user_config, self.user_id, **{self.key: val}
        )
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
            elif key in [
                "polymarket_threshold",
                "monthly_expense",
                "cash_reserve",
            ]:
                val_display = f"${raw_val:,.0f}" if raw_val > 0 else "關閉/未設定"  # type: ignore
            elif key == "polymarket_slippage":
                val_display = f"{raw_val}%"
            elif key == "tax_reserve_rate":
                val_display = f"{raw_val:.1%}"
            elif key == "escape_window":
                val_display = f"{ctx.escape_window_start} ~ {ctx.escape_window_end}"
            elif key == "trading_strategy":
                val_display = TRADING_STRATEGY_DISPLAY.get(str(raw_val), str(raw_val))
            elif key == "risk_appetite":
                val_display = RISK_APPETITE_DISPLAY.get(str(raw_val), str(raw_val))
            elif key == "portfolio_mode":
                val_display = PORTFOLIO_MODE_DISPLAY.get(str(raw_val), str(raw_val))
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
            "enable_macro_top_escape_defense",
        ]:
            current_bool = getattr(ctx, key, False)
            new_val = not current_bool
            await asyncio.to_thread(
                database.upsert_user_config, self.user_id, **{key: new_val}
            )

            self.refresh_items()
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        elif key == "trading_strategy":
            # 固定 3 選項的策略模式選單，直接寫入 DB，不需 Modal
            view: discord.ui.View = TradingStrategySelectView(
                self.user_id, parent_view=self
            )
            embed = create_info_embed(
                title="📐 選擇交易策略",
                message="請選擇動態轉倉引擎的進場邏輯模式：",
            )
            await interaction.response.edit_message(embed=embed, view=view)
        elif key == "risk_appetite":
            # 固定 2 選項的風險偏好選單，直接寫入 DB，不需 Modal
            view = RiskAppetiteSelectView(self.user_id, parent_view=self)
            embed = create_info_embed(
                title="⚖️ 選擇風險偏好",
                message="請選擇動態轉倉引擎的風險偏好參數組：",
            )
            await interaction.response.edit_message(embed=embed, view=view)
        elif key == "portfolio_mode":
            # 固定 2 選項的持倉管理模式選單，直接寫入 DB，不需 Modal
            view = PortfolioModeSelectView(self.user_id, parent_view=self)
            embed = create_info_embed(
                title="🧭 選擇持倉管理模式",
                message=(
                    "請選擇持倉停利停損的輸出語意 (單檔可用 `/edit_holding "
                    "advisory_mode` 覆寫)："
                ),
            )
            await interaction.response.edit_message(embed=embed, view=view)
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
            f"🧭 **宏觀逃頂前瞻防禦**: `{'🟢 開啟' if ctx.enable_macro_top_escape_defense else '🔴 關閉'}`",
            f"📐 **交易策略**: `{TRADING_STRATEGY_DISPLAY.get(ctx.trading_strategy, ctx.trading_strategy)}`",
            f"⚖️ **風險偏好**: `{RISK_APPETITE_DISPLAY.get(ctx.risk_appetite, ctx.risk_appetite)}`",
            f"🧭 **持倉管理模式**: `{PORTFOLIO_MODE_DISPLAY.get(ctx.portfolio_mode, ctx.portfolio_mode)}`",
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


class TradingStrategySelect(discord.ui.Select):
    """交易策略模式 (動態調整/左側/右側/做空) 固定 4 選項選單，選中即直接寫入
    DB 並導回父層 AccountSettingsView，不需要 Modal（比照
    ui/watchlist_tags.py::WatchlistTagSelect 的 Select 子類別模式）。"""

    def __init__(self, user_id: int, parent_view: "AccountSettingsView") -> None:
        current = database.get_full_user_context(user_id).trading_strategy
        options = [
            discord.SelectOption(
                label=TRADING_STRATEGY_DISPLAY[code],
                value=code,
                description=TRADING_STRATEGY_DESCRIPTIONS[code],
                default=(code == current),
            )
            for code in ("DYNAMIC", "LEFT_SIDE", "RIGHT_SIDE", "SHORT_SIDE")
        ]
        super().__init__(
            placeholder="請選擇交易策略模式...",
            options=options,
            custom_id="select_trading_strategy",
        )
        self.user_id = user_id
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> Any:
        if interaction.data is None or not isinstance(interaction.data, dict):
            return
        select_values = interaction.data.get("values")
        if not select_values or not isinstance(select_values, list):
            return

        selected = str(select_values[0])
        await asyncio.to_thread(
            database.upsert_user_config, self.user_id, trading_strategy=selected
        )

        self.parent_view.refresh_items()
        embed = self.parent_view.build_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class TradingStrategySelectView(discord.ui.View):
    def __init__(self, user_id: int, parent_view: "AccountSettingsView") -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self.parent_view = parent_view
        self.add_item(TradingStrategySelect(user_id, parent_view))


class RiskAppetiteSelect(discord.ui.Select):
    """風險偏好 (穩健防禦/動能進攻) 固定 2 選項選單，選中即直接寫入 DB 並導回
    父層 AccountSettingsView，不需要 Modal（比照 TradingStrategySelect 的
    既有 Select 子類別模式）。"""

    def __init__(self, user_id: int, parent_view: "AccountSettingsView") -> None:
        current = database.get_full_user_context(user_id).risk_appetite
        options = [
            discord.SelectOption(
                label=RISK_APPETITE_DISPLAY[code],
                value=code,
                description=RISK_APPETITE_DESCRIPTIONS[code],
                default=(code == current),
            )
            for code in ("DEFENSIVE", "AGGRESSIVE")
        ]
        super().__init__(
            placeholder="請選擇風險偏好...",
            options=options,
            custom_id="select_risk_appetite",
        )
        self.user_id = user_id
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> Any:
        if interaction.data is None or not isinstance(interaction.data, dict):
            return
        select_values = interaction.data.get("values")
        if not select_values or not isinstance(select_values, list):
            return

        selected = str(select_values[0])
        await asyncio.to_thread(
            database.upsert_user_config, self.user_id, risk_appetite=selected
        )

        self.parent_view.refresh_items()
        embed = self.parent_view.build_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class RiskAppetiteSelectView(discord.ui.View):
    def __init__(self, user_id: int, parent_view: "AccountSettingsView") -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self.parent_view = parent_view
        self.add_item(RiskAppetiteSelect(user_id, parent_view))


class PortfolioModeSelect(discord.ui.Select):
    """持倉管理模式 (指令/顧問) 固定 2 選項選單，選中即直接寫入 DB 並導回父層
    AccountSettingsView（比照 RiskAppetiteSelect）。"""

    def __init__(self, user_id: int, parent_view: "AccountSettingsView") -> None:
        current = database.get_full_user_context(user_id).portfolio_mode
        options = [
            discord.SelectOption(
                label=PORTFOLIO_MODE_DISPLAY[code],
                value=code,
                description=PORTFOLIO_MODE_DESCRIPTIONS[code],
                default=(code == current),
            )
            for code in ("COMMAND", "ADVISORY")
        ]
        super().__init__(
            placeholder="請選擇持倉管理模式...",
            options=options,
            custom_id="select_portfolio_mode",
        )
        self.user_id = user_id
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> Any:
        if interaction.data is None or not isinstance(interaction.data, dict):
            return
        select_values = interaction.data.get("values")
        if not select_values or not isinstance(select_values, list):
            return

        selected = str(select_values[0])
        await asyncio.to_thread(
            database.upsert_user_config, self.user_id, portfolio_mode=selected
        )

        self.parent_view.refresh_items()
        embed = self.parent_view.build_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class PortfolioModeSelectView(discord.ui.View):
    def __init__(self, user_id: int, parent_view: "AccountSettingsView") -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self.parent_view = parent_view
        self.add_item(PortfolioModeSelect(user_id, parent_view))


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
