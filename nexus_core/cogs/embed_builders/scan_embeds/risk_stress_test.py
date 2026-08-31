"""GTC 掛單現金赤字壓力測試 Embed 建構函式。"""

import discord

from cogs.embed_builders._core import NexusEmbed


def create_stress_test_embed(results: dict) -> discord.Embed:
    """建立 GTC 掛單現金赤字壓力測試 Embed (繁體中文)"""
    is_critical = results.get("is_critical", False)
    color = discord.Color.red() if is_critical else discord.Color.green()

    embed = NexusEmbed(
        title="🚨 GTC 掛單現金赤字壓力測試 (Worst-Case Stress Test)", color=color
    )

    total_deficit = results.get("total_deficit", 0.0)
    cash_reserve = results.get("cash_reserve", 0.0)
    boxx_shares = results.get("boxx_shares", 0.0)
    boxx_cash = results.get("boxx_cash", 0.0)
    net_deficit = results.get("net_deficit", 0.0)
    order_count = results.get("gtc_buy_orders_count", 0)

    deficit_color = "\u001b[1;31m" if net_deficit < 0 else "\u001b[1;32m"
    net_deficit_str = (
        f"-${abs(net_deficit):,.2f}" if net_deficit < 0 else f"${net_deficit:,.2f}"
    )

    summary_lines = [
        "```ansi",
        f" ├─ 活躍 GTC 網格買單: \u001b[1;36m{order_count} 筆\u001b[0m",
        f" ├─ 100% 全數成交所需總美金 (Total Cash Deficit): \u001b[1;33m${total_deficit:,.2f}\u001b[0m",
        f" ├─ 常規可用現金 (cash_reserve): \u001b[1;32m${cash_reserve:,.2f}\u001b[0m",
        f" ├─ BOXX 持倉股數: \u001b[1;36m{boxx_shares:.1f} 股\u001b[0m (常規清算上限 180 股)",
        f" ├─ BOXX 最大套現金額: \u001b[1;32m${boxx_cash:,.2f}\u001b[0m",
        f" └─ 壓測後淨赤字/淨值: {deficit_color}{net_deficit_str}\u001b[0m",
        "```",
    ]
    embed.add_field(
        name="📊 壓測摘要",
        value="\n".join(summary_lines),
        inline=False,
    )

    if is_critical:
        warning_lines = [
            "```ansi",
            " ├─ \u001b[1;31m警告：當前 GTC 網格單潛在赤字已大於可用流動性！\u001b[0m",
            " ├─ 在極端無差別踩踏情境下，若所有掛單 100% 全數成交，將會抽乾 BOXX 水壩",
            f" ├─ 破壞 +${cash_reserve:,.0f} 的安全常規現金水位，且危及 7 月底 $13,000 實體提領紅線！",
            " └─ 建議立即取消部分 GTC 掛單，或注入額外資金以維持安全邊際。",
            "```",
        ]
        embed.add_field(
            name="🔥 CRITICAL WARNING",
            value="\n".join(warning_lines),
            inline=False,
        )
    else:
        safe_lines = [
            "```ansi",
            " └─ \u001b[1;32m目前可用現金儲備與 BOXX 備用流動性充裕，足以覆蓋所有活躍 GTC 掛單全數成交之極端情境，未威脅到提領紅線。\u001b[0m",
            "```",
        ]
        embed.add_field(
            name="✅ 系統安全狀態",
            value="\n".join(safe_lines),
            inline=False,
        )

    embed.set_footer(text="Nexus Risk Engine | 流動性水壩壓力測試")
    return embed
