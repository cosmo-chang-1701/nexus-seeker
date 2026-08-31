"""Unified Radar Panel 狀態顯示 Embed 建構函式。"""

from cogs.embed_builders._core import NexusEmbed


def build_unified_radar_panel_embed(state: dict) -> NexusEmbed:
    """
    Constructs the state display embed for the Unified Radar Panel.
    """
    embed = NexusEmbed(
        title="🎛️ 批次量化雷達 (Unified Radar Panel)",
        description="請設定您的掃描範圍與量化過濾條件，完成後點擊「🚀 執行量化雷達」。\n\n**當前設定狀態：**",
    )

    scope_name_map = {
        "WATCHLIST": "🌟 自選標的 (Watchlist)",
        "ALL": "🌀 全部標的 (持倉+掛單+期權)",
        "HOLDINGS": "💼 現貨持倉 (Holdings)",
        "ORDERS": "⏳ 待成交掛單 (Orders)",
        "OPTIONS": "📜 期權持倉 (Options)",
    }

    # Format and display Scope
    scope_raw = state.get("scope", "WATCHLIST")
    scope_display = scope_name_map.get(scope_raw, f"`{scope_raw}`")
    embed.add_field(
        name="🎯 掃描範圍 (Scope)", value=f"**{scope_display}**", inline=False
    )

    # Format and display Quant Filters and Params
    filter_name_map = {
        "exclude_martial_law": "🛡️ 排除底牆破位 / 負 Gamma (Martial Law)",
        "avoid_silent_period": "🛡️ 規避財報與總經靜默期",
        "dp_skew_defense": "🛡️ 暗池派發防護 (Skew < -0.3)",
        "tdp_mode": "🔵 TDP 估值三擊 (TDP Mode)",
        "squeeze_mode": "🔥 動能擠壓爆發 (Gamma Squeeze)",
        "uoa_mode": "🐋 嚴格機構籌碼 (Strict UOA)",
        "magnetic_filters": "🧲 高階磁吸過濾 (Magnetic Filters)",
    }

    filters = state.get("quant_filters", [])
    filters_display = (
        "\n".join([f"✅ {filter_name_map.get(f, f)}" for f in filters])
        if filters
        else "無 (顯示所有符合範圍之標的)"
    )

    params = state.get("params", {})
    min_dev_pct = float(params.get("min_max_pain_dev", 0.10) or 0.10) * 100
    params_str = (
        f"• Max Pain 偏離: `{params.get('max_pain_threshold')}%`\n"
        f"• 絕對支撐容錯: `{params.get('abs_support_tolerance')}%`\n"
        f"• 靜默期避讓: `{params.get('silent_period_days')} 天`\n"
        f"• 磁吸門檻: `{min_dev_pct:.0f}%`"
    )

    embed.add_field(name="⚙️ 量化過濾條件 (Filters)", value=filters_display, inline=True)
    embed.add_field(name="📐 微調參數 (Params)", value=params_str, inline=True)

    # Render selected tag if any
    selected_tag = state.get("selected_tag")
    if selected_tag:
        embed.add_field(
            name="🏷️ 選擇標籤 (Tag)", value=f"`{selected_tag}`", inline=False
        )

    embed.set_footer(text="Nexus Seeker • 量化雷達面板")
    return embed
