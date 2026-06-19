"""對沖警報與系統監控 Embed 建構函式。

包含：
- create_event_impact_embed：事件風險 What-if 模擬
- create_hedge_settlement_embed：對沖結算完成
- create_hedge_list_embed：對沖警報列表
- create_hedge_alert_embed：自動化對沖警報
- create_proactive_event_alert_embed：重大事件主動預警
- create_memory_alert_embed：記憶體不足警報
- create_polymarket_whale_alert_embed：Polymarket 巨鯨戰報
- create_option_defense_alert_embed：期權防禦與結算
- create_volatility_risk_alert_embed：波動率風險警報
"""

import discord

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from cogs.embed_builders._embed_helpers import split_embed_by_fields


def create_event_impact_embed(
    symbol: str,
    vol_move: float,
    total_delta: float,
    total_vanna: float,
    adjusted_delta: float,
    delta_shift: float,
    exposure_shift_dollars: float,
) -> discord.Embed:
    """建立事件風險 What-if 模擬 Embed。"""
    embed = discord.Embed(
        title=f"🎲 【 {symbol} 事件風險模擬 (What-if) 】",
        description=f"假設波動率變動 `{vol_move}%` 時，部位 Greeks 的動態偏移：",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="目前 Beta-Weighted Delta",
        value=f"`{total_delta:.2f}`",
        inline=True,
    )
    embed.add_field(
        name="目前 Vanna (曝險變化率)",
        value=f"`{total_vanna:.2f}`",
        inline=True,
    )
    embed.add_field(
        name="預期 Hidden Delta",
        value=f"`{adjusted_delta:.2f}`",
        inline=False,
    )
    embed.add_field(name="Delta 偏移量", value=f"`{delta_shift:+.2f}`", inline=True)
    embed.add_field(
        name="等值曝險變動 (USD)",
        value=f"`${exposure_shift_dollars:,.2f}`",
        inline=True,
    )

    risk_status = "🔴 危險" if abs(adjusted_delta) > 100 else "🟢 安全"
    embed.add_field(name="風險狀態判定", value=f"**{risk_status}**", inline=False)
    embed.set_footer(text="NRO Vanna Simulation | Nexus Seeker")
    return embed


def create_hedge_settlement_embed(
    alert_id: int, hedge_instrument: str, executed_quantity: int
) -> discord.Embed:
    """建立對沖結算完成 Embed。"""
    embed = discord.Embed(
        title="✅ 對沖結算完成",
        description=f"已成功記錄警報 `#{alert_id}` 的對沖執行紀錄。",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="執行標的", value=f"`{hedge_instrument}`", inline=True)
    embed.add_field(name="執行數量", value=f"`{executed_quantity}`", inline=True)
    embed.set_footer(text="數據已同步至 SQLite 持久化層，可用於歸因分析。")
    return embed


def create_hedge_list_embed(rows: List[Any]) -> discord.Embed:
    """建立最近對沖警報列表 Embed。"""
    embed = discord.Embed(
        title="📜 最近對沖警報列表",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    content: List[str] = []
    for row in rows:
        status_emoji = (
            "⏳" if row[3] == "PENDING" else "✅" if row[3] == "EXECUTED" else "❌"
        )
        content.append(
            f"`#{row[0]}` | {status_emoji} | VIX: `{row[1]:.2f}` | 建議: `{row[2]}`股 | {row[4][:16]}"
        )

    embed.description = "\n".join(content) if content else "📭 目前無對沖警報紀錄。"
    embed.set_footer(text="Nexus Seeker | Hedge Ledger")
    return embed


def create_hedge_alert_embed(
    *,
    vix: float,
    stage_move: int,
    tier_name: str,
    tier_emoji: str,
    color_hex: int,
    total_beta_delta: float,
    adjusted_delta: float,
    total_vega: float,
    hedge_quantity: int,
    instruction_text: str,
    narration: str,
    alert_id: int,
    poly_snapshot: Optional[List[Dict[str, Any]]] = None,
) -> discord.Embed:
    """建立自動化對沖警報 Embed。"""
    embed = discord.Embed(
        title="🚨 【戰位報告：自動化對沖警報】",
        description=f"**警報等級：** {tier_emoji} {tier_name} (移動 `{stage_move:+} 階`)",
        color=discord.Color(color_hex),
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="📊 風險指標",
        value=(
            f"• **即時 VIX:** `{vix:.2f}`\n"
            f"• **淨 Delta:** `{total_beta_delta:+.1f}`\n"
            f"• **調整後 Delta:** `{adjusted_delta:+.1f}` (Hidden Delta)\n"
            f"• **Vega 脆弱性:** `{total_vega:+.2f}`"
        ),
        inline=False,
    )

    if poly_snapshot:
        snapshot_lines: List[str] = []
        for event in poly_snapshot:
            question = str(event.get("question", "N/A"))
            truncated = question[:40] + "..." if len(question) > 40 else question
            odds = event.get("odds_distribution", [])
            odds_str = " | ".join(
                f"{item.get('outcome')}: `{item.get('odds', 0.0) * 100:.0f}%`"
                for item in odds[:2]
            )
            snapshot_lines.append(f"• **{truncated}**\n  └ {odds_str}")

        if snapshot_lines:
            embed.add_field(
                name="🌐 [快取快照] Polymarket 即時機率",
                value="\n".join(snapshot_lines),
                inline=False,
            )

    embed.add_field(name="🤖 AI 風險敘述", value=f"*{narration}*", inline=False)
    embed.add_field(
        name="🛡️ 對沖建議指令",
        value=f"```fix\n{instruction_text}\n```",
        inline=False,
    )
    embed.add_field(
        name="📈 預期效果",
        value=(
            f"執行後淨 Delta 將回歸至 `{adjusted_delta + (hedge_quantity * -1.0):+.1f}` 附近，"
            "顯著降低系統性回撤風險。"
        ),
        inline=False,
    )
    embed.set_footer(text=f"🌌 Nexus Seeker • Battle Station | Alert ID: {alert_id}")
    return embed


def create_proactive_event_alert_embed(events: List[Any]) -> List[discord.Embed]:
    """建立重大事件主動預警 Embed。"""
    embed = discord.Embed(
        title="🛡️ 【 預警：重大事件即時防護 】",
        description=(
            "偵測到您的持倉標的即將迎來重大波動事件；以下 NRO 指令已依事件類型、"
            "剩餘時間與目前持倉風險狀態調整。"
        ),
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc),
    )

    if not events:
        embed.description = "📭 目前沒有偵測到即將來臨的重大波動事件。"
        embed.set_footer(text="Proactive Event Monitor | Nexus Seeker")
        return [embed]

    for event in events:
        if isinstance(event, dict):
            name = str(event.get("name", "⚠️ 重大事件"))
            tte_hours = float(event.get("tte_hours", 0.0) or 0.0)
            risk_status = str(event.get("risk_status", "持倉風險狀態暫不可用"))
            instruction = str(event.get("instruction", "請先降低曝險並觀察事件落地。"))
        else:
            if event.type == "ECONOMIC":
                name = f"🔴 經濟數據: {event.event}"
                instruction = "增加 Vanna 權重，縮減賣方曝險。"
            else:
                name = f"📊 財報預警: {event.symbol}"
                instruction = "已啟動 IV Crush 防護機制。"
            tte_hours = float(getattr(event, "tte_hours", 0.0) or 0.0)
            risk_status = "持倉風險狀態未提供，請搭配組合 Greeks 自行覆核。"

        # Determine colors for TTE
        if tte_hours <= 12.0:
            tte_color = "\u001b[1;31m"  # Urgent Red
        elif tte_hours <= 24.0:
            tte_color = "\u001b[1;33m"  # Warning Yellow
        else:
            tte_color = "\u001b[1;36m"  # Safe Cyan

        # Determine colors for Risk Status
        risk_str = str(risk_status)
        if any(
            w in risk_str
            for w in ["危險", "警告", "高", "🚨", "⚠️", "賣方偏重", "短 Gamma"]
        ):
            risk_color = "\u001b[1;31m"  # High Risk Red
        elif any(w in risk_str for w in ["安全", "低", "良好", "正常"]):
            risk_color = "\u001b[1;32m"  # Low Risk Green
        else:
            risk_color = "\u001b[1;33m"  # Neutral Yellow

        event_lines = [
            "```ansi",
            " 🛡️ 即時防護狀態 (Event Risk Protection)",
            " ----------------------------------",
            " 距離發布 (Time to Event)",
            f" └─ 剩餘時間: {tte_color}{tte_hours:.1f} 小時\u001b[0m",
            " 持倉風險狀態 (Position Risk Status)",
            f" └─ 狀態: {risk_color}{risk_status}\u001b[0m",
            " NRO 指令 (NRO Instruction)",
            f" └─ 指令: \u001b[1;36m{instruction}\u001b[0m",
            "```",
        ]
        value = "\n".join(event_lines)

        embed.add_field(name=name, value=value, inline=False)

    embed.set_footer(text="Proactive Event Monitor | Nexus Seeker")
    return split_embed_by_fields(embed)


def create_memory_alert_embed(
    total_usage: float,
    process_memory_mb: float,
    sma_cache_size: int,
    ema_cache_size: int,
) -> discord.Embed:
    """建立記憶體不足緊急警報 Embed。"""
    embed = discord.Embed(
        title="🆘 【系統緊急警報：記憶體不足】",
        description=(
            f"VPS 記憶體使用量已達臨界值 (`{total_usage}%`)，"
            "可能導致程序被 OOM Killer 終止。"
        ),
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="當前總占用", value=f"`{total_usage}%`", inline=True)
    embed.add_field(
        name="程序占用 (RSS)", value=f"`{process_memory_mb:.1f} MB`", inline=True
    )
    embed.add_field(
        name="📦 快取消費者",
        value=f"SMA/EMA: `{sma_cache_size}/{ema_cache_size}` 筆",
        inline=False,
    )
    embed.set_footer(text="建議重啟服務或增加 Swap 分區。")
    return embed


def create_polymarket_whale_alert_embed(
    *,
    intent_emoji: str,
    intent_label: str,
    market_question: str,
    usd_value: float,
    dynamic_threshold: float,
    win_rate: float,
    is_high_conviction: bool,
    is_bullish: bool,
    summary: str,
    event_slug: Optional[str] = None,
    uoa_correlation: Optional[Dict[str, Any]] = None,
) -> discord.Embed:
    """建立 Polymarket 巨鯨戰報 Embed。"""
    embed = discord.Embed(
        title="【 🐋 Polymarket 巨鯨戰報 】"
        + (" 🔥 高信心訊號" if is_high_conviction else ""),
        color=(
            discord.Color.gold()
            if is_high_conviction
            else (discord.Color.blue() if is_bullish else discord.Color.red())
        ),
        timestamp=datetime.now(timezone.utc),
    )

    content = [
        f"## {intent_emoji} {intent_label}",
        "---",
        f"**市場問題：** **{market_question}**",
        f"**交易金額：** `${usd_value:,.2f}`",
        f"**流動性倍數：** `{usd_value / dynamic_threshold:.2f}x`",
        f"**當前勝率：** {win_rate:.1f}%",
        "---",
    ]

    if uoa_correlation:
        uoa = uoa_correlation["uoa"]
        classification = uoa_correlation["classification"]
        content.append(f"🔍 **UOA 關聯偵測 ({uoa['symbol']})**")
        content.append(f"- 合約: `{uoa['expiry']}` `${uoa['strike']}` {uoa['type']}")
        content.append(
            f"- 性質: **{classification['classification']}** (信心: `{classification['confidence']:.2f}`)"
        )
        content.append(f"- 理由: {classification['explanation']}")
        content.append("---")

    if win_rate > 70 or win_rate < 30:
        content.append("🛡️ **【預測性對沖建議 (Predictive Hedge)】**")
        content.append(
            f"偵測到預測市場對 `{market_question}` 的機率激增至 `{win_rate:.1f}%`，建議提前在 VTR 執行 Delta 對沖，以應對潛在的波動率跳空。"
        )
        content.append("---")

    if summary and summary != "（未啟用 AI 分析）":
        content.append(f"**🤖 AI 總結分析**\n{summary}")
        content.append("---")

    market_url = (
        f"https://polymarket.com/event/{event_slug}"
        if event_slug
        else "https://polymarket.com"
    )
    content.append(f"[🔗 前往市場]({market_url})")

    embed.description = "\n".join(content)
    embed.set_footer(
        text=f"Nexus Seeker 監測系統 | 動態門檻: ${dynamic_threshold:,.0f}"
    )
    return embed


def create_option_defense_alert_embed(
    *,
    is_live: bool,
    symbol: str,
    status_icon: str,
    action_taken: str,
    pnl: float,
    exposure_pct: float,
    exit_reason: Optional[str] = None,
    regime: Optional[str] = None,
    target_delta: Optional[float] = None,
    hedge: Optional[Dict[str, Any]] = None,
) -> discord.Embed:
    """建立期權轉倉防禦與結算警報 Embed (合併實盤與 VTR)"""
    title_prefix = "🛡️ 【實盤防禦】" if is_live else "🤖 【VTR 自動處置結果】"
    color = (
        discord.Color.gold()
        if is_live or "轉倉" in action_taken
        else discord.Color.red()
    )
    if pnl < 0:
        color = discord.Color.red()
    elif pnl > 0:
        color = discord.Color.green()

    embed = discord.Embed(
        title=f"{title_prefix} {symbol} 轉倉防禦與結算",
        description=f"標的 **{symbol}** 已執行平倉或防禦性轉倉處置。\n\u200b",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="處置狀態", value=f"{status_icon} **{action_taken}**", inline=True
    )
    embed.add_field(name="實現損益", value=f"💰 `${pnl:.2f}`", inline=True)
    embed.add_field(
        name="目前帳戶總曝險",
        value=f"`{exposure_pct:.2f}%` (Beta-Weighted Delta)",
        inline=True,
    )

    if exit_reason:
        embed.add_field(
            name="觸發原因 / 指標", value=f"```\n{exit_reason}\n```", inline=False
        )

    if regime is not None and target_delta is not None:
        embed.add_field(name="🧠 系統自主位階判定", value=f"`{regime}`", inline=False)
        embed.add_field(
            name="理想總曝險目標", value=f"`{target_delta:.1f} Delta`", inline=True
        )

    if hedge:
        embed.add_field(
            name="🛡️ 自動對沖決策",
            value=f"{hedge['action']} (缺口: `{hedge['gap']}`)",
            inline=False,
        )

    embed.set_footer(text="🌌 Nexus Seeker • 期權轉倉防禦與結算警報")
    return embed


def create_volatility_risk_alert_embed(
    *,
    alert_type: str,
    title_text: str,
    description_text: str,
    color_hex: int,
    fields_data: List[Dict[str, Any]],
    footer_text: str = "Nexus Seeker Volatility Risk Alert",
) -> discord.Embed:
    """建立波動率與重大事件對沖警報 Embed (合併主動與被動)"""
    embed = discord.Embed(
        title=title_text,
        description=description_text,
        color=discord.Color(color_hex),
        timestamp=datetime.now(timezone.utc),
    )
    for field in fields_data:
        embed.add_field(
            name=field["name"],
            value=field["value"],
            inline=field.get("inline", False),
        )
    embed.set_footer(text=footer_text)
    return embed
