"""GTC 掛單現金赤字壓力測試 Embed 建構函式。"""

import discord

from cogs.embed_builders._ansi_utils import _to_float_or_none, fmt_or_dash
from cogs.embed_builders._core import NexusEmbed


def create_stress_test_embed(results: dict) -> discord.Embed:
    """建立 GTC 掛單現金赤字壓力測試 Embed (繁體中文)"""
    is_critical = results.get("is_critical", False)
    color = discord.Color.red() if is_critical else discord.Color.green()

    total_deficit = _to_float_or_none(results.get("total_deficit"))
    cash_reserve = _to_float_or_none(results.get("cash_reserve"))
    equiv_value = _to_float_or_none(results.get("cash_equiv_value"))
    equiv_detail = str(results.get("cash_equiv_detail") or "")
    load_failed = results.get("load_failed") is True
    withdrawal = _to_float_or_none(results.get("next_withdrawal"))
    if withdrawal is not None and withdrawal <= 0:
        withdrawal = None
    withdrawal_date = results.get("next_withdrawal_date")
    withdrawal_stale = results.get("next_withdrawal_stale") is True
    net_deficit = _to_float_or_none(results.get("net_deficit"))
    order_count = results.get("gtc_buy_orders_count")

    # 關鍵數值缺失時無法判定安全與否：降級顯示，且不得宣稱「充裕」
    degraded = (
        load_failed
        or net_deficit is None
        or total_deficit is None
        or cash_reserve is None
        or equiv_value is None
    )
    if degraded and not is_critical:
        color = discord.Color.default()

    embed = NexusEmbed(
        title="🚨 GTC 掛單現金赤字壓力測試 (Worst-Case Stress Test)"
        + (" [降級模式]" if degraded else ""),
        color=color,
    )

    if net_deficit is None:
        deficit_color = "\u001b[1;30m"
        net_deficit_str = "--"
    else:
        deficit_color = "\u001b[1;31m" if net_deficit < 0 else "\u001b[1;32m"
        net_deficit_str = (
            f"-${abs(net_deficit):,.2f}" if net_deficit < 0 else f"${net_deficit:,.2f}"
        )
    equiv_str = fmt_or_dash(equiv_value, ",.2f", prefix="$")
    if equiv_detail:
        equiv_str += f" ({equiv_detail})"
    order_count_str = "--" if order_count is None else str(order_count)

    summary_lines = [
        "```ansi",
        f" ├─ 活躍 GTC 網格買單: \u001b[1;36m{order_count_str} 筆\u001b[0m",
        f" ├─ 100% 全數成交所需總美金 (Total Cash Deficit): \u001b[1;33m{fmt_or_dash(total_deficit, ',.2f', prefix='$')}\u001b[0m",
        f" ├─ 常規可用現金 (cash_reserve): \u001b[1;32m{fmt_or_dash(cash_reserve, ',.2f', prefix='$')}\u001b[0m",
        f" ├─ 現金等價物 (BOXX/BIL/SHV/SGOV) 市值: \u001b[1;32m{equiv_str}\u001b[0m",
        f" └─ 壓測後淨赤字/淨值: {deficit_color}{net_deficit_str}\u001b[0m",
        "```",
    ]
    embed.add_field(
        name="📊 壓測摘要",
        value="\n".join(summary_lines),
        inline=False,
    )

    withdrawal_clause = ""
    if withdrawal is not None:
        date_part = f"{withdrawal_date} " if withdrawal_date else ""
        withdrawal_clause = f"，且危及 {date_part}預定提領 ${withdrawal:,.0f}！"
        if withdrawal_stale:
            withdrawal_clause += "（跑道快照過期）"

    if is_critical and not load_failed:
        warning_lines = [
            "```ansi",
            " ├─ \u001b[1;31m警告：當前 GTC 網格單潛在赤字已大於可用流動性！\u001b[0m",
            " ├─ 在極端無差別踩踏情境下，若所有掛單 100% 全數成交，將會抽乾 BOXX 水壩",
            f" ├─ 破壞 +{fmt_or_dash(cash_reserve, ',.0f', prefix='$')} 的安全常規現金水位{withdrawal_clause or '！'}",
            " └─ 建議立即取消部分 GTC 掛單，或注入額外資金以維持安全邊際。",
            "```",
        ]
        embed.add_field(
            name="🔥 CRITICAL WARNING",
            value="\n".join(warning_lines),
            inline=False,
        )
    elif degraded:
        degraded_msg = (
            "帳戶資料讀取失敗，無法判定。"
            if load_failed
            else "關鍵壓測數值缺失，無法判定壓測結果。"
        )
        embed.add_field(
            name="⚠️ 資料不足",
            value=f"```ansi\n └─ \u001b[1;33m{degraded_msg}\u001b[0m\n```",
            inline=False,
        )
    else:
        safe_tail = "，未威脅到預定提領" if withdrawal is not None else ""
        safe_lines = [
            "```ansi",
            f" └─ \u001b[1;32m目前可用現金儲備與 BOXX 備用流動性充裕，足以覆蓋所有活躍 GTC 掛單全數成交之極端情境{safe_tail}。\u001b[0m",
            "```",
        ]
        embed.add_field(
            name="✅ 系統安全狀態",
            value="\n".join(safe_lines),
            inline=False,
        )

    embed.set_footer(text="Risk Engine | 流動性水壩壓力測試")
    return embed
