"""設定與系統通知 Embed 建構函式。

包含：
- create_notification_settings_embed：通知偏好設定中心
- create_account_settings_embed：帳戶全域參數配置中心
- create_info_embed：標準資訊通知
- create_error_embed：標準錯誤通知
"""

import discord
from cogs.embed_builders._core import NexusEmbed

from datetime import datetime, timezone


def create_notification_settings_embed(module_fields: list) -> discord.Embed:
    """建立自訂通知設定偏好中心 Embed"""
    embed = NexusEmbed(
        title="🌌 Nexus Seeker ｜ 戰術通知管理中樞 (4 大戰術維度)",
        description=(
            "點擊下方選單切換模組設定，或直接點擊快捷情境按鈕：\n"
            "• **🛡️ 戰備全開**：開啟所有 10 項風控與戰報頻道\n"
            "• **🎯 精準交易**：保留定時戰報與持倉防禦，關閉盤中 Alpha 雜訊\n"
            "• **🔕 盤中靜音**：僅保留盤前/盤後戰報與保證金警戒"
        ),
        color=discord.Color.dark_magenta(),
        timestamp=datetime.now(timezone.utc),
    )
    for name, value in module_fields:
        if value.strip():
            embed.add_field(name=name, value=value, inline=False)

    embed.set_footer(text="Quantitative Preferences | Tactical Dashboard")
    return embed


def create_account_settings_embed(
    basic_settings: list, runway_settings: list
) -> discord.Embed:
    """建立帳戶全域參數配置中心 Embed"""
    embed = NexusEmbed(
        title="🌌 Nexus Seeker ｜ 帳戶全域參數配置中心",
        description="請使用下方下拉選單選擇想要更改的參數。\n布林值項目將會立即切換，數值項目將會彈出輸入框供您修改。",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="📊 核心帳戶與交易參數 (Core Settings)",
        value="\n".join(basic_settings),
        inline=False,
    )
    embed.add_field(
        name="💸 財務生存跑道指標 (Runway Settings)",
        value="\n".join(runway_settings),
        inline=False,
    )
    embed.set_footer(text="Quantitative Preferences | Ephemeral Configuration")
    return embed


def create_info_embed(title: str, message: str) -> discord.Embed:
    """建立標準資訊通知 Embed"""
    embed = NexusEmbed(
        title=f"ℹ️ {title}",
        description=message,
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Nexus Seeker | System Notification")
    return embed


def create_error_embed(message: str, title: str = "系統錯誤") -> discord.Embed:
    """建立標準錯誤通知 Embed"""
    embed = NexusEmbed(
        title=f"❌ {title}",
        description=message,
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Nexus Seeker | Error Report")
    return embed
