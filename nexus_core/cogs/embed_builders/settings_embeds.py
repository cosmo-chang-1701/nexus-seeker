"""設定與系統通知 Embed 建構函式。

包含：
- create_notification_settings_embed：通知偏好設定中心
- create_account_settings_embed：帳戶全域參數配置中心
- create_info_embed：標準資訊通知
- create_error_embed：標準錯誤通知
"""

import discord

from datetime import datetime, timezone


def create_notification_settings_embed(
    scheduled_list: list, realtime_list: list, polymarket_list: list
) -> discord.Embed:
    """建立自訂通知設定偏好中心 Embed"""
    embed = discord.Embed(
        title="🌌 Nexus Seeker ｜ 通知偏好設定中心",
        description="請使用下方下拉選單點擊要切換的項目，或使用一鍵按鈕管理所有通知。\n🟢 代表開啟，🔴 代表關閉。",
        color=discord.Color.dark_magenta(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="📅 定時與掃描背景通知 (Scheduled & Scan)",
        value="\n".join(scheduled_list),
        inline=False,
    )
    embed.add_field(
        name="⚡ 即時風險與事件警報 (Real-time & Events)",
        value="\n".join(realtime_list),
        inline=False,
    )
    embed.add_field(
        name="🐳 Polymarket 巨鯨與 AI 監控 (Polymarket Settings)",
        value="\n".join(polymarket_list),
        inline=False,
    )
    embed.set_footer(text="Quantitative Preferences | Ephemeral Configuration")
    return embed


def create_account_settings_embed(
    basic_settings: list, runway_settings: list
) -> discord.Embed:
    """建立帳戶全域參數配置中心 Embed"""
    embed = discord.Embed(
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
    embed = discord.Embed(
        title=f"ℹ️ {title}",
        description=message,
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Nexus Seeker | System Notification")
    return embed


def create_error_embed(message: str, title: str = "系統錯誤") -> discord.Embed:
    """建立標準錯誤通知 Embed"""
    embed = discord.Embed(
        title=f"❌ {title}",
        description=message,
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Nexus Seeker | Error Report")
    return embed
