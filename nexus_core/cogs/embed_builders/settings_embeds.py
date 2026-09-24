"""設定與系統通知 Embed 建構函式。

包含：
- create_notification_settings_embed：通知偏好設定中心
- create_account_settings_embed：帳戶全域參數配置中心
- create_info_embed：標準資訊通知
- create_error_embed：標準錯誤通知
"""

import discord
from cogs.embed_builders._core import NexusEmbed
from database.notification_channels import ALL_NOTIFICATION_KEYS

from datetime import datetime, timezone


def create_notification_settings_embed(
    module_fields: list, recommend_bh_defense: bool = False
) -> discord.Embed:
    """建立自訂通知設定偏好中心 Embed。

    模組依「對投組下行風險的影響」分組；每個頻道附「頻率 · 作用」標籤。
    `recommend_bh_defense` 為 True（帳戶 portfolio_mode='ADVISORY'）時標示 B&H 防守為建議情境。
    """
    bh_hint = "（你的帳戶為 B&H 顧問模式，建議使用）" if recommend_bh_defense else ""
    embed = NexusEmbed(
        title="🌌 Nexus Seeker ｜ 通知頻道中心 (依下行風險影響分組)",
        description=(
            "選擇模組後，在第二個選單勾選要開啟的頻道（未勾選者關閉）；"
            "或點擊下方預設情境：\n"
            f"• **🧭 B&H 防守**{bh_hint}：開啟左尾防護與上行捕捉，關閉所有上行削減"
            "（停利分批、獲利鎖定、換股、賣 Covered Call），情報只保留自訂門檻型\n"
            "• **🎯 精準交易**：只關閉雜訊類頻道（自選雷達、Alpha 掃描、做空訊號等）\n"
            "• **🔕 盤中靜音**：再關閉所有盤中節奏的推播，保留每日 / 每週 / 全天候頻道\n"
            f"• **🛡️ 戰備全開**：開啟全部 {len(ALL_NOTIFICATION_KEYS)} 個頻道\n"
            "🛡️ 左尾防護的頻道任何預設情境都不會關閉，只能逐項手動關。\n"
            "情報與戰報類頻道收在「⚙️ 進階」，預設情境仍會一併套用。"
        ),
        color=discord.Color.dark_magenta(),
        timestamp=datetime.now(timezone.utc),
    )
    for name, value in module_fields:
        if value.strip():
            embed.add_field(name=name, value=value[:1024], inline=False)

    embed.set_footer(text="頻率 · 作用：截左尾 / 捕捉上行 / 削減上行 / 情報 / 戰報")
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
