"""Option Scan（選擇權掃描報告）Embed 建構函式。"""

from typing import Any

import discord

from cogs.embed_builders._embed_helpers import (
    _build_embed_base,
    _add_vix_battle_status_field,
    _add_market_overview_fields,
    _add_volatility_fields,
    _add_sentiment_fields,
    _add_trend_and_support_fields,
    _add_performance_and_kelly_fields,
    _add_earnings_fields,
    _add_covered_call_fields,
    _add_expected_move_fields,
    _add_liquidity_fields,
    _add_strategy_upgrade_fields,
    _add_risk_optimization_fields,
    _add_hedge_unlock_fields,
    _add_ai_verification_fields,
    add_news_field,
    add_reddit_field,
)


def create_scan_embed(data: Any, user_capital: Any = 100000.0):  # type: ignore
    strategy = data.get("strategy", "UNKNOWN")
    stock_cost = data.get("stock_cost", 0.0)

    embed, is_covered = _build_embed_base(data, strategy, stock_cost)

    # VIX tier color override
    vix_color = data.get("vix_tier_color") or (
        data.get("vix_battle_status", {}).get("color_hex")
    )
    if vix_color:
        embed.color = discord.Color(vix_color)

    # Render UI fields (VIX Battle Status first)
    _add_vix_battle_status_field(embed, data)

    # 🚀 整合 Gap & Fill 狀態 (New Engine)
    gap = data.get("gap_status")
    if gap:
        from market_analysis.gap_analysis import GapStatus

        status_emoji = {
            GapStatus.GAP_HOLDING: "🟢 持續跳空 (Holding)",
            GapStatus.PARTIAL_FILL: "🟡 部分回補 (Filling)",
            GapStatus.FULL_FILL: "🔴 完全回補 (Filled)",
            GapStatus.NO_GAP: "⚪ 無跳空",
        }.get(gap.current_fill_status, "⚪ N/A")

        support_tag = " | 🛡️ 支撐已確認" if gap.is_support_confirmed else ""
        gap_color = "🟢" if gap.gap_size > 0 else "🔴"

        gap_info = (
            f"{gap_color} **{'向上跳空 (UP-GAP)' if gap.gap_size > 0 else '向下跳空 (DOWN-GAP)'}**: `{gap.gap_pct:+.2f}%` (${gap.gap_size:+.2f})\n"
            f"**狀態:** {status_emoji}{support_tag}\n"
            f"**區間:** `${gap.gap_zone[0]:.2f}` - `${gap.gap_zone[1]:.2f}`\n\u200b"
        )
        embed.add_field(name="📈 Gap & Fill 跳空監控", value=gap_info, inline=False)

    _add_market_overview_fields(embed, data)
    _add_volatility_fields(embed, data, strategy)
    _add_sentiment_fields(embed, data)
    _add_trend_and_support_fields(embed, data)
    _add_performance_and_kelly_fields(embed, data, user_capital)
    _add_earnings_fields(embed, data, strategy)

    if is_covered:
        _add_covered_call_fields(embed, data, stock_cost)

    _add_expected_move_fields(embed, data, strategy, is_covered)
    _add_liquidity_fields(embed, data)
    _add_strategy_upgrade_fields(embed, data, strategy)

    # 🚀 執行優化回饋顯示
    _add_risk_optimization_fields(embed, data, user_capital)
    _add_hedge_unlock_fields(embed, data)

    add_news_field(embed, data.get("news_text"))
    add_reddit_field(embed, data.get("reddit_text"))
    _add_ai_verification_fields(embed, data)

    # 🚀 AlertFilter 推播理由 (僅在條件式過濾觸發時顯示)
    alert_reason = data.get("alert_reason")
    if alert_reason:
        embed.add_field(
            name="📢 推播觸發條件",
            value=f"```\n{alert_reason}\n```",
            inline=False,
        )

    vix_spot = data.get("vix_spot") or (
        data.get("vix_battle_status", {}).get("vix_spot")
    )
    vix_emoji = data.get("vix_tier_emoji") or (
        data.get("vix_battle_status", {}).get("emoji", "")
    )
    vix_name = data.get("vix_tier_name") or (
        data.get("vix_battle_status", {}).get("name", "")
    )
    vix_footer = f" | VIX: {vix_spot:.1f} {vix_emoji} {vix_name}" if vix_spot else ""
    embed.set_footer(
        text=f"Nexus Seeker 風控引擎 • 基準 SPY: ${data.get('spy_price', 500):.1f}{vix_footer}"
    )
    return embed
