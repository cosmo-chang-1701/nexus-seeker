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

SCHEDULED_LABELS = {
    "hb_live_price": "🏷️ 心跳：基礎現價與區間",
    "hb_options_structure": "🧱 心跳：期權結構與波動率",
    "hb_uoa": "🔎 心跳：異常大單穿透 (UOA)",
    "hb_execution_risk": "🛡️ 心跳：操盤指引與委託風控",
    "pre_market_briefing": "🌅 盤前綜合宏觀與自選股報告",
    "intraday_decision_scan": "⚡ 盤中量化掃描與執行指南",
    "post_market_intelligence": "📋 盤後綜合風險與 AI 策略報告",
    "weekly_vtr_report": "📅 每週 VTR 績效週報",
}

REALTIME_LABELS = {
    "profit_lock_alert": "💰 期權實單利潤鎖定警報",
    "gamma_fragility_alert": "⚠️ 組合 Gamma 脆弱性警報",
    "option_defense_alert": "🛡️ 期權轉倉防禦與結算警報",
    "ddp_cheap_vol_alert": "🌌 雙擊與便宜波動率預警",
    "volatility_risk_alert": "🌪️ 波動率與重大事件對沖警報",
    "deadlock_recovery_alert": "🔓 物理死鎖解除與備兌建單指引",
}

POLYMARKET_SETTINGS_LABELS = {
    "polymarket_whale_alert": (
        "🐳 巨鯨交易異動警報",
        "切換巨鯨交易異動警報開啟/關閉狀態",
        None,
    ),
    "polymarket_threshold": (
        "🐋 巨鯨監控門檻",
        "Polymarket 巨鯨監控門檻 (USD, 0=關閉)",
        "輸入大於等於 0 的金額",
    ),
    "polymarket_use_llm": (
        "🧠 Polymarket AI 分析",
        "Polymarket 交易是否使用 AI 分析總結",
        None,
    ),
    "polymarket_slippage": (
        "🌊 Polymarket 滑價門檻",
        "Polymarket 巨鯨判定目標滑價百分比 (0.1% - 10.0%)",
        "輸入 0.1 - 10.0 之間的百分比",
    ),
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

    async def on_submit(self, interaction: discord.Interaction):
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
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.refresh_items()

    def refresh_items(self):
        self.clear_items()
        settings = database.get_user_notification_settings(self.user_id)
        ctx = database.get_full_user_context(self.user_id)

        # 1. 定時與掃描背景通知下拉選單
        scheduled_options = []
        for key, label in SCHEDULED_LABELS.items():
            state_emoji = "🟢" if settings.get(key, True) else "🔴"
            scheduled_options.append(
                discord.SelectOption(
                    label=f"{state_emoji} {label}",
                    value=key,
                    description="點擊切換開啟/關閉狀態",
                )
            )
        scheduled_select = discord.ui.Select(
            placeholder="⚙️ 設定 定時與掃描背景通知...",
            options=scheduled_options,
            custom_id="select_scheduled",
            row=0,
        )
        scheduled_select.callback = self.on_select_callback
        self.add_item(scheduled_select)

        # 2. 即時風險與事件警報下拉選單
        realtime_options = []
        for key, label in REALTIME_LABELS.items():
            state_emoji = "🟢" if settings.get(key, True) else "🔴"
            realtime_options.append(
                discord.SelectOption(
                    label=f"{state_emoji} {label}",
                    value=key,
                    description="點擊切換開啟/關閉狀態",
                )
            )
        realtime_select = discord.ui.Select(
            placeholder="🚨 設定 即時風險與事件警報...",
            options=realtime_options,
            custom_id="select_realtime",
            row=1,
        )
        realtime_select.callback = self.on_select_callback
        self.add_item(realtime_select)

        # 3. Polymarket 巨鯨與 AI 監控設定下拉選單
        polymarket_options = []

        # (a) 巨鯨交易異動警報
        whale_alert_enabled = settings.get("polymarket_whale_alert", True)
        whale_alert_emoji = "🟢" if whale_alert_enabled else "🔴"
        polymarket_options.append(
            discord.SelectOption(
                label="🐳 巨鯨交易異動警報",
                value="polymarket_whale_alert",
                description=f"目前: {whale_alert_emoji} {'開啟' if whale_alert_enabled else '關閉'} | 切換開關狀態"[
                    :100
                ],
            )
        )

        # (b) 巨鯨監控門檻
        threshold_val = ctx.polymarket_threshold
        threshold_emoji = "🟢" if threshold_val > 0 else "🔴"
        threshold_display = f"${threshold_val:,.0f}" if threshold_val > 0 else "關閉"
        polymarket_options.append(
            discord.SelectOption(
                label="🐋 巨鯨監控門檻",
                value="polymarket_threshold",
                description=f"目前: {threshold_emoji} {threshold_display} | 設定門檻金額"[
                    :100
                ],
            )
        )

        # (c) AI 分析
        use_llm_val = ctx.polymarket_use_llm
        use_llm_emoji = "🟢" if use_llm_val else "🔴"
        polymarket_options.append(
            discord.SelectOption(
                label="🧠 Polymarket AI 分析",
                value="polymarket_use_llm",
                description=f"目前: {use_llm_emoji} {'開啟' if use_llm_val else '關閉'} | 切換開關狀態"[
                    :100
                ],
            )
        )

        # (d) 滑價門檻
        slippage_val = ctx.polymarket_slippage
        polymarket_options.append(
            discord.SelectOption(
                label="🌊 Polymarket 滑價門檻",
                value="polymarket_slippage",
                description=f"目前: {slippage_val}% | 設定判定滑價門檻"[:100],
            )
        )

        polymarket_select = discord.ui.Select(
            placeholder="🐳 設定 Polymarket 巨鯨與 AI 監控...",
            options=polymarket_options,
            custom_id="select_polymarket",
            row=2,
        )
        polymarket_select.callback = self.on_select_callback
        self.add_item(polymarket_select)

        # 4. 按鈕
        btn_enable_all = discord.ui.Button(
            label="⚡ 全部開啟",
            style=discord.ButtonStyle.green,
            custom_id="btn_enable_all",
            row=3,
        )
        btn_enable_all.callback = self.on_enable_all
        self.add_item(btn_enable_all)

        btn_disable_all = discord.ui.Button(
            label="💤 全部關閉",
            style=discord.ButtonStyle.red,
            custom_id="btn_disable_all",
            row=3,
        )
        btn_disable_all.callback = self.on_disable_all
        self.add_item(btn_disable_all)

    async def on_select_callback(self, interaction: discord.Interaction):
        if interaction.data is None or not isinstance(interaction.data, dict):
            return
        select_values = interaction.data.get("values")
        if not select_values or not isinstance(select_values, list):
            return

        key = str(select_values[0])
        ctx = database.get_full_user_context(self.user_id)

        # 1. 處理 Polymarket 的非開關設定 (Modal)
        if key in ["polymarket_threshold", "polymarket_slippage"]:
            current_val = getattr(ctx, key, 0.0)
            label, desc, placeholder = POLYMARKET_SETTINGS_LABELS[key]
            modal = NotificationSettingsModal(
                user_id=self.user_id,
                key=key,
                label=label,
                current_value=current_val,
                placeholder=placeholder or "",
                view=self,
            )
            await interaction.response.send_modal(modal)
            return

        # 2. 處理 Polymarket AI 分析 (User settings boolean toggle)
        elif key == "polymarket_use_llm":
            current_val = getattr(ctx, key, False)
            new_val = not current_val
            database.upsert_user_config(self.user_id, **{key: new_val})

            self.refresh_items()
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)
            return

        # 3. 處理一般的通知 ON/OFF 開關
        else:
            settings = database.get_user_notification_settings(self.user_id)
            new_state = not settings.get(key, True)
            database.set_user_notification_setting(self.user_id, key, new_state)

            self.refresh_items()
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_enable_all(self, interaction: discord.Interaction):
        database.set_all_user_notification_settings(self.user_id, True)
        self.refresh_items()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_disable_all(self, interaction: discord.Interaction):
        database.set_all_user_notification_settings(self.user_id, False)
        self.refresh_items()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    def build_embed(self) -> discord.Embed:
        settings = database.get_user_notification_settings(self.user_id)
        ctx = database.get_full_user_context(self.user_id)

        scheduled_list = []
        for key, label in SCHEDULED_LABELS.items():
            status = "🟢 開啟" if settings.get(key, True) else "🔴 關閉"
            scheduled_list.append(f"* {label}: **{status}**")

        realtime_list = []
        for key, label in REALTIME_LABELS.items():
            status = "🟢 開啟" if settings.get(key, True) else "🔴 關閉"
            realtime_list.append(f"* {label}: **{status}**")

        polymarket_list = [
            f"* 🐳 巨鯨交易異動警報: **{'🟢 開啟' if settings.get('polymarket_whale_alert', True) else '🔴 關閉'}**",
            f"* 🐋 巨鯨監控門檻金額: **{'🟢 $' + f'{ctx.polymarket_threshold:,.0f}' if ctx.polymarket_threshold > 0 else '🔴 關閉'}**",
            f"* 🧠 Polymarket AI 深度分析: **{'🟢 開啟' if ctx.polymarket_use_llm else '🔴 關閉'}**",
            f"* 🌊 巨鯨判定滑價門檻: **`{ctx.polymarket_slippage}%`**",
        ]

        return create_notification_settings_embed(
            scheduled_list=scheduled_list,
            realtime_list=realtime_list,
            polymarket_list=polymarket_list,
        )


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

    async def on_submit(self, interaction: discord.Interaction):
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
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.refresh_items()

    def refresh_items(self):
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
                val_display = f"${raw_val:,.0f}" if raw_val > 0 else "關閉/未設定"
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

        select = discord.ui.Select(
            placeholder="⚙️ 請選擇要配置的帳戶全域參數...",
            options=options,
            custom_id="select_account_settings",
            row=0,
        )
        select.callback = self.on_select_callback
        self.add_item(select)

    async def on_select_callback(self, interaction: discord.Interaction):
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
        ]

        runway_settings = [
            f"💸 **每月生存支出預算**: `${ctx.monthly_expense:,.0f}`",
            f"🏦 **稅務預留比例**: `{ctx.tax_reserve_rate:.1%}`",
            f"💰 **現金儲備金額**: `${ctx.cash_reserve:,.0f}`",
        ]

        return create_account_settings_embed(
            basic_settings=basic_settings, runway_settings=runway_settings
        )
