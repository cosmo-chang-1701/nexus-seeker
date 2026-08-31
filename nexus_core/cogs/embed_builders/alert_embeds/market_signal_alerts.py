"""WTI 原油與個股價量突破警報 Embed 建構函式。"""

import discord

from datetime import datetime, timezone
from typing import Any

from cogs.embed_builders._core import NexusEmbed


def create_wti_alert_embed(analysis: Any) -> discord.Embed:
    """建立 WTI 原油價格警報 Embed。

    嚴格遵循 Nexus Seeker field-based + ANSI 容器規範：
    - 區塊標題一律置入 field.name
    - 所有內文與指標一律封裝於 ```ansi 程式碼區塊內
    - 統一採用樹狀結構 ( ┌─,  ├─,  └─) 與 ANSI 調色盤渲染
    """
    from market_analysis.wti_analysis import WtiAlertType, OilTrend

    # 動態標題與顏色
    alert_config_map: dict[WtiAlertType, tuple[str, discord.Color]] = {
        WtiAlertType.UPPER_BREACH: ("🚀 WTI 原油突破上限警戒", discord.Color.orange()),
        WtiAlertType.LOWER_BREACH: ("📉 WTI 原油跌破下限警戒", discord.Color.red()),
        WtiAlertType.PCT_SURGE: ("⚡ WTI 原油劇烈飆漲", discord.Color.green()),
        WtiAlertType.PCT_PLUNGE: ("⚡ WTI 原油劇烈暴跌", discord.Color.red()),
    }

    title, color = alert_config_map.get(
        analysis.alert_type,
        ("🛢️ WTI 原油價格警報", discord.Color.orange()),
    )

    embed = NexusEmbed(
        title=title,
        description=None,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    tech = analysis.technicals

    # =========================================================================
    # Field 1: 🚨 觸發事件與即時遙測 (Trigger Telemetry)
    # =========================================================================
    if analysis.alert_type in (WtiAlertType.UPPER_BREACH, WtiAlertType.LOWER_BREACH):
        direction_verb = (
            "突破上限"
            if analysis.alert_type == WtiAlertType.UPPER_BREACH
            else "跌破下限"
        )
        status_color = (
            "\u001b[1;33m"
            if analysis.alert_type == WtiAlertType.UPPER_BREACH
            else "\u001b[1;31m"
        )
        trigger_ansi = (
            f"```ansi\n"
            f" ┌─ 觸發情境 ─ [{status_color}{direction_verb}\u001b[0m]\n"
            f" ├─ 即時現價: \u001b[1;37m${tech.price:.2f}\u001b[0m\n"
            f" ├─ 設定閾值: \u001b[1;37m${analysis.threshold_value:.2f}\u001b[0m\n"
            f" └─ 30分波動: \u001b[1;{'32' if analysis.pct_change_30min >= 0 else '31'}m{analysis.pct_change_30min:+.2f}%\u001b[0m\n"
            f"```"
        )
    else:
        direction_verb = "劇烈飆漲" if analysis.pct_change_30min > 0 else "劇烈暴跌"
        status_color = (
            "\u001b[1;32m" if analysis.pct_change_30min > 0 else "\u001b[1;31m"
        )
        trigger_ansi = (
            f"```ansi\n"
            f" ┌─ 觸發情境 ─ [{status_color}{direction_verb}\u001b[0m]\n"
            f" ├─ 即時現價: \u001b[1;37m${tech.price:.2f}\u001b[0m\n"
            f" ├─ 30分波動: {status_color}{analysis.pct_change_30min:+.2f}%\u001b[0m\n"
            f" └─ 波動閾值: \u001b[1;37m±{analysis.threshold_value:.1f}%\u001b[0m\n"
            f"```"
        )
    embed.add_field(name="🚨 觸發事件與即時遙測", value=trigger_ansi, inline=False)

    # =========================================================================
    # Field 2: 📊 技術結構與量化指標 (Technical Structure)
    # =========================================================================
    trend_labels: dict[OilTrend, tuple[str, str]] = {
        OilTrend.STRONG_BULLISH: ("強勢多頭", "\u001b[1;32m"),
        OilTrend.BULLISH: ("偏多排列", "\u001b[1;32m"),
        OilTrend.NEUTRAL: ("中性盤整", "\u001b[1;37m"),
        OilTrend.BEARISH: ("偏空排列", "\u001b[1;31m"),
        OilTrend.STRONG_BEARISH: ("強勢空頭", "\u001b[1;31m"),
    }
    trend_text, trend_color = trend_labels.get(tech.trend, ("中性", "\u001b[1;37m"))

    rsi_color = (
        "\u001b[1;31m"
        if tech.rsi_14 >= 70
        else ("\u001b[1;32m" if tech.rsi_14 <= 30 else "\u001b[1;37m")
    )
    daily_color = "\u001b[1;32m" if tech.daily_change_pct >= 0 else "\u001b[1;31m"
    weekly_color = "\u001b[1;32m" if tech.weekly_change_pct >= 0 else "\u001b[1;31m"

    tech_panel = (
        f"```ansi\n"
        f" ┌─ WTI 期貨指標 ─ [CL=F]\n"
        f" ├─ RSI(14) : {rsi_color}{tech.rsi_14:.1f}\u001b[0m\n"
        f" ├─ MA 均線 : \u001b[1;37m20D ${tech.ma_20:.2f} │ 50D ${tech.ma_50:.2f} │ 200D ${tech.ma_200:.2f}\u001b[0m\n"
        f" ├─ ATR(14) : \u001b[1;37m${tech.atr_14:.2f}\u001b[0m\n"
        f" ├─ 漲跌幅  : 日 {daily_color}{tech.daily_change_pct:+.2f}%\u001b[0m │ 週 {weekly_color}{tech.weekly_change_pct:+.2f}%\u001b[0m\n"
        f" └─ 趨勢判定: {trend_color}{trend_text}\u001b[0m\n"
        f"```"
    )
    embed.add_field(name="📊 技術結構與量化指標", value=tech_panel, inline=False)

    # =========================================================================
    # Field 3: ⛽ 能源板塊關聯股衝擊 (Correlated Assets)
    # =========================================================================
    if analysis.correlated_impacts:
        lines: list[str] = [" ┌─ 能源標的 ─ 現價 ─ 日漲跌 ─ [關聯標記]"]
        total_items = len(analysis.correlated_impacts)
        for i, imp in enumerate(analysis.correlated_impacts):
            prefix = " └─" if i == total_items - 1 else " ├─"
            chg_color = "\u001b[1;32m" if imp.daily_change_pct >= 0 else "\u001b[1;31m"
            badge = ""
            if imp.is_in_holdings:
                badge = " \u001b[1;33m[HOLDING]\u001b[0m"
            elif imp.is_in_watchlist:
                badge = " \u001b[1;36m[WATCH]\u001b[0m"
            lines.append(
                f"{prefix} \u001b[1;37m{imp.symbol:<5}\u001b[0m │ ${imp.price:>7.2f} │ {chg_color}{imp.daily_change_pct:>+6.2f}%\u001b[0m{badge}"
            )
        embed.add_field(
            name="⛽ 能源板塊關聯股衝擊",
            value="```ansi\n" + "\n".join(lines) + "\n```",
            inline=False,
        )

    # =========================================================================
    # Field 4: 🛡️ 投資組合風險與總經事件 (Portfolio Risk & Events)
    # =========================================================================
    weight = analysis.oil_risk_weight
    if weight >= 1.0:
        weight_status = "\u001b[1;32m無壓縮 (1.00x)\u001b[0m"
        directive = "油價處於安全區間 (<$75)，維持正常賣方限額與風險預算。"
    elif weight >= 0.9:
        weight_status = "\u001b[1;33m輕度壓縮 (0.90x)\u001b[0m"
        directive = "油價進入 $75-$85 警戒區，賣方曝險限額微幅收緊 10%。"
    elif weight >= 0.7:
        weight_status = "\u001b[1;33m中度壓縮 (0.70x)\u001b[0m"
        directive = "油價突破 $85 通膨警戒線，賣方曝險限額壓縮 30%，謹防成本端傳導。"
    else:
        weight_status = "\u001b[1;31m嚴重壓縮 (0.50x 🚨)\u001b[0m"
        directive = "油價突破 $95 極端衝擊線，全面強制減半賣方限額，啟動防禦模式。"

    risk_lines: list[str] = [
        f" ┌─ 風險權重: {weight_status}",
        f" ├─ 指令: \u001b[1;37m{directive}\u001b[0m",
    ]

    if analysis.geopolitical_events:
        risk_lines.append(" ├─ 近期地緣/總經事件:")
        for idx, ev in enumerate(analysis.geopolitical_events[:3]):
            sub_prefix = (
                " └─" if idx == len(analysis.geopolitical_events[:3]) - 1 else " ├─"
            )
            risk_lines.append(f"{sub_prefix}  • \u001b[1;37m{ev}\u001b[0m")
    else:
        risk_lines.append(" └─ 近期無高影響力 OPEC/原油地緣事件排程。")

    embed.add_field(
        name="🛡️ 投資組合風險與總經事件",
        value="```ansi\n" + "\n".join(risk_lines) + "\n```",
        inline=False,
    )

    embed.set_footer(text="Commodity Intelligence | WTI Crude Oil Monitor")
    return embed


def create_price_volume_alert_embed(watch: Any, bar: Any) -> discord.Embed:
    """建立個股 15 分鐘價量突破警報 Embed。

    嚴格遵循 Nexus Seeker field-based + ANSI 容器規範：
    - 區塊標題一律置入 field.name
    - 所有內文與指標一律封裝於 ```ansi 程式碼區塊內
    - 統一採用樹狀結構 ( ┌─,  ├─,  └─) 與 ANSI 調色盤渲染

    Args:
        watch: `database.price_volume_watch.PriceVolumeWatch` 實例。
        bar: `market_analysis.price_volume_alert.Confirmed15mBar` 實例。
    """
    from database.price_volume_watch import WatchDirection

    is_above = watch.direction == WatchDirection.ABOVE
    title = (
        f"🚀 {watch.symbol} 15分K突破警報"
        if is_above
        else f"📉 {watch.symbol} 15分K跌破警報"
    )
    color = discord.Color.green() if is_above else discord.Color.red()

    embed = NexusEmbed(
        title=title,
        description=None,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    direction_badge = "🚀 向上突破" if is_above else "📉 向下跌破"
    direction_color = "[1;32m" if is_above else "[1;31m"
    compare_symbol = ">=" if is_above else "<="
    volume_ratio = bar.volume / bar.avg_volume if bar.avg_volume > 0 else 0.0
    vol_threshold_str = (
        "無放量門檻限制"
        if watch.volume_multiplier <= 0
        else f"門檻 {watch.volume_multiplier:.2f}x"
    )

    trigger_ansi = (
        f"```ansi\n"
        f" ┌─ 觸發情境 ─ [{direction_color}{direction_badge}[0m]\n"
        f" ├─ K棒收盤時間: [1;37m{bar.bar_time.strftime('%Y-%m-%d %H:%M')} ET[0m\n"
        f" ├─ 實際收盤價 : [1;37m${bar.close:.2f}[0m {compare_symbol} 目標價 [1;37m${watch.target_price:.2f}[0m\n"
        f" ├─ 本根成交量 : [1;37m{bar.volume:,.0f}[0m\n"
        f" └─ 20根均量  : [1;37m{bar.avg_volume:,.0f}[0m (放大 [1;33m{volume_ratio:.2f}x[0m，{vol_threshold_str})\n"
        f"```"
    )
    embed.add_field(name="🎯 觸發事件", value=trigger_ansi, inline=False)

    followup_ansi = (
        "```ansi\n"
        " ┌─ 後續操作建議\n"
        " └─ 可使用 [1;36m/x[0m 開啟終端機雷達面板，進一步確認 Skew、GEX 牆與 UOA 巨鯨動向後再行決策。\n"
        "```"
    )
    embed.add_field(name="📡 後續操作建議", value=followup_ansi, inline=False)

    embed.set_footer(text="Price-Volume Breakout Alert | Nexus Seeker")
    return embed
