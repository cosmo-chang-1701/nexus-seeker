"""Alert and notification Discord embed builders for Nexus Seeker.

This module contains embed-building functions for:
- Option scan reports (create_scan_embed)
- PowerSqueeze (PSQ) strategy reports (create_psq_embed)
- News and Reddit sentiment scans (create_news_scan_embed, create_reddit_scan_embed,
  create_media_sentiment_embed)
- Polymarket whale-tracking embeds (create_polymarket_list_embed,
  create_polymarket_status_embed)
- Real-time quote embeds (create_quote_embed)
- Trading risk and alert embeds:
    - DITM profit-lock alerts (create_profit_lock_alert_embed)
    - Gamma fragility warnings (create_gamma_fragility_embed)
    - Pre-market earnings radar (create_pre_market_earnings_embed)
    - DITM transition alerts (create_ditm_transition_alert_embed)
    - Intraday execution guide (create_intraday_execution_guide_embed)
    - VTR settlement notices (create_vtr_settlement_notice_embed)

All functions copy their embed bodies exactly from the canonical embed_builder.py
source and must not alter any business logic.
"""

import discord

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from cogs.embed_builders._embed_helpers import (
    add_news_field,
    add_reddit_field,
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
)


# ============================================================================
# Option Scan Embed
# ============================================================================


def create_scan_embed(data, user_capital=100000.0):
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


# ============================================================================
# PowerSqueeze Embed
# ============================================================================


def create_psq_embed(data: dict) -> discord.Embed:
    """建構獨立的 PowerSqueeze (PSQ) 戰情報告 Embed"""
    sym = data.get("symbol", "UNKNOWN")
    psq = data.get("psq_result")

    if not psq:  # fallback
        return discord.Embed(
            title=f"⚡ PowerSqueeze 戰情報告 | {sym}",
            description="無可用數據",
            color=discord.Color.dark_grey(),
        )

    color = discord.Color.purple() if psq.is_squeezing else discord.Color.dark_teal()
    if psq.is_breakout_long:
        color = discord.Color.green()
    elif psq.is_breakout_short:
        color = discord.Color.red()

    embed = discord.Embed(
        title=f"⚡ PowerSqueeze 戰情報告 | {sym}",
        description=f"💰 最新股價: `${data.get('price', 0.0):.2f}`\n\u200b",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    C_RESET = "\u001b[0m"
    C_GREEN = "\u001b[1;32m"
    C_YELLOW = "\u001b[1;33m"
    C_RED = "\u001b[1;31m"

    # Format PSQ quantitative metrics into a premium monospace tree
    psq_lines = ["```ansi"]
    psq_lines.append(f" ⚡ {sym} PowerSqueeze 量化指標")
    psq_lines.append(" ----------------------------------")

    # 1. 能量壓縮
    sq_status = "ON" if psq.is_squeezing else "OFF"
    sq_color = C_RED if psq.is_squeezing else C_GREEN
    psq_lines.append(
        f"  ├─ 能量壓縮狀態: {sq_color}{sq_status}{C_RESET} ({'壓縮蓄力中' if psq.is_squeezing else '無壓縮'})"
    )

    # 2. 動能爆發
    energy_val = (
        "LONG"
        if psq.is_breakout_long
        else ("SHORT" if psq.is_breakout_short else "STABLE")
    )
    energy_color = (
        C_GREEN
        if psq.is_breakout_long
        else (C_RED if psq.is_breakout_short else C_RESET)
    )
    energy_desc = (
        "多頭向上爆發"
        if psq.is_breakout_long
        else ("空頭向下崩潰" if psq.is_breakout_short else "波動蓄勢")
    )
    psq_lines.append(
        f"  ├─ 動能爆發方向: {energy_color}{energy_val}{C_RESET} ({energy_desc})"
    )

    # 3. 線性動能
    mom_trend = "多方主導" if psq.momentum_value > 0 else "空方主導"
    mom_color = C_GREEN if psq.momentum_value > 0 else C_RED
    psq_lines.append(
        f"  ├─ 線性動能 (Mom): {mom_color}{psq.momentum_value:+.2f}{C_RESET} ({mom_trend})"
    )

    # 4. 均線支撐
    dist_val = f"{psq.sma_distance_pct:+.2f}%"
    dist_desc = f"20SMA支撐 (${psq.sma_20:.2f})"
    dist_color = C_GREEN if psq.is_near_support else C_YELLOW
    psq_lines.append(f"  └─ 偏離 20SMA: {dist_color}{dist_val}{C_RESET} ({dist_desc})")

    psq_lines.append("```")
    embed.add_field(name="⚡ PSQ 量化指標", value="\n".join(psq_lines), inline=False)

    # VIX momentum label
    vix_label = (
        psq.vix_momentum_label if hasattr(psq, "vix_momentum_label") else "NORMAL"
    )
    vix_tf_note = psq.vix_timeframe_note if hasattr(psq, "vix_timeframe_note") else ""

    if vix_label != "NORMAL":
        label_map = {
            "OVEREXTENDED_RISK": "⚠️ **過度延伸風險** | 低 VIX 環境多頭訊號可能是牛陷阱",
            "HIGH_CONVICTION_RECOVERY": "🚀 **高確信反彈** | 高 VIX + 空頭減速 = 反轉機會",
        }
        label_text = label_map.get(vix_label, vix_label)
        embed.add_field(name="🏅 VIX 動能判定", value=label_text, inline=False)

    if vix_tf_note:
        embed.add_field(name="⏱️ 時間框架建議", value=f"`{vix_tf_note}`", inline=False)

    # 最新新聞
    add_news_field(embed, data.get("news_text"))

    # VIX tier info in footer
    vix_spot_val = data.get("vix_spot") or (
        data.get("vix_battle_status", {}).get("vix_spot")
    )
    vix_emoji_val = data.get("vix_battle_status", {}).get("emoji", "")
    vix_name_val = data.get("vix_battle_status", {}).get("name", "")
    vix_footer = (
        f" | VIX: {vix_spot_val:.1f} {vix_emoji_val} {vix_name_val}"
        if vix_spot_val
        else ""
    )
    embed.set_footer(text=f"Nexus Seeker • PowerSqueeze 日K量化引擎{vix_footer}")
    return embed


# ============================================================================
# News / Reddit / Media Sentiment Embeds
# ============================================================================


def create_news_scan_embed(symbol, news_text):
    """建構新聞掃描結果的 Embed"""
    embed = discord.Embed(title=f"📰 {symbol} 官方新聞掃描", color=discord.Color.blue())
    add_news_field(embed, news_text)
    embed.set_footer(text="Nexus Seeker 研報系統 • 資料來源: Yahoo Finance")
    return embed


def create_reddit_scan_embed(symbol, reddit_text):
    """建構 Reddit 情緒掃描結果的 Embed"""
    embed = discord.Embed(
        title=f"🔥 {symbol} 散戶情緒優勢 (Reddit 同步)", color=discord.Color.orange()
    )
    add_reddit_text = reddit_text
    add_reddit_field(embed, add_reddit_text)
    embed.set_footer(
        text="Nexus Seeker 研報系統 • 資料來源: Reddit (WSB/Stocks/Options)"
    )
    return embed


def create_media_sentiment_embed(symbol, news_text, reddit_text):
    """建構輿情與社群 (Media & Social) 掃描結果的統一 Embed"""
    embed = discord.Embed(
        title=f"🎭 {symbol} 輿情與社群大盤掃描 (Media & Social)",
        color=discord.Color.blue(),
    )
    add_news_field(embed, news_text)
    add_reddit_field(embed, reddit_text)
    embed.set_footer(
        text="Nexus Seeker 輿情中心 • 資料來源: Yahoo Finance & Reddit (WSB/Stocks/Options)"
    )
    return embed


# ============================================================================
# Polymarket Embeds
# ============================================================================


def create_polymarket_list_embed(markets: List[Dict[str, Any]]):
    """建構 Polymarket 監控中的熱門市場 Embed"""
    embed = discord.Embed(
        title="🐋 Polymarket 巨鯨意圖圖譜",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    if not markets:
        embed.description = "目前沒有監控中的市場。"
        return embed

    description_lines = ["```ansi"]
    for i, m in enumerate(markets, 1):
        question = m.get("question", "未知市場")
        # 截斷過長的標題
        if len(question) > 55:
            question = question[:52] + "..."

        # 取得 token 價格資訊 (如果有的話)
        tokens = m.get("tokens", [])
        price_info = ""
        if tokens:
            # 顯示前兩個 outcome 的價格
            p_list = []
            for t in tokens[:2]:
                outcome = str(t.get("outcome", "")).strip()
                price = t.get("price", 0)

                # 排除單字元的雜訊 (例如 [ or " )
                if len(outcome) <= 1 and outcome not in ["?", "是", "否"]:
                    continue

                # 簡單格式化價格 (0-1)
                try:
                    price_val = float(price)
                    price_str = f"{price_val:.2f}"
                except Exception:
                    price_str = str(price)

                if outcome:
                    p_list.append(f"{outcome}: {price_str}")

            if p_list:
                price_info = " | ".join(p_list)

        description_lines.append(f"{i:2d}. {question}")
        if price_info:
            description_lines.append(f"    └─ {price_info}")
        else:
            description_lines.append("")

    if len(description_lines) > 1 and description_lines[-1] == "":
        description_lines.pop()
    description_lines.append("```")

    full_desc = "\n".join(description_lines)
    # 檢查總長度，避免超過 Discord 限制
    if len(full_desc) > 3900:
        full_desc = full_desc[:3890] + "\n...\n```"

    embed.description = full_desc
    embed.set_footer(text="Nexus Seeker | Polymarket Monitor (Top 20 Active Markets)")
    return embed


def create_polymarket_status_embed(status: Dict[str, Any]) -> discord.Embed:
    """建構 Polymarket 服務狀態 Embed。"""
    embed = discord.Embed(
        title="【 🐋 Polymarket 服務狀態 】",
        color=discord.Color.green() if status["connected"] else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )

    status_emoji = "🟢 已連線" if status["connected"] else "🔴 斷線中"
    running_emoji = "✅ 運行中" if status["running"] else "🛑 已停止"
    content = [
        "## 🖥️ 監控系統運行資訊",
        "---",
        f"**服務狀態：** {running_emoji}",
        f"**連線狀態：** {status_emoji}",
        f"**訂閱資產：** `{status['asset_count']}` 個標的",
        f"**最後訊息：** {status['last_message']}",
        f"**異常計數：** `{status['errors']}` 次",
        "---",
    ]
    embed.description = "\n".join(content)
    embed.set_footer(text="Nexus Seeker | Polymarket Monitor")
    return embed


# ============================================================================
# Real-time Quote Embed
# ============================================================================


def create_quote_embed(symbol: str, data: Dict[str, Any]) -> discord.Embed:
    """建構即時報價 Embed。"""
    embed = discord.Embed(
        title=f"💹 {symbol} 即時報價 (Real-time Quote)",
        color=discord.Color.blue() if data["dp"] >= 0 else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="現價 (Current)", value=f"**${data['c']}**", inline=True)
    embed.add_field(name="漲跌幅 (%)", value=f"`{data['dp']}%`", inline=True)
    embed.add_field(
        name="今日高/低", value=f"H: `${data['h']}` / L: `${data['l']}`", inline=False
    )
    embed.add_field(name="前收盤 (PC)", value=f"`${data['pc']}`", inline=True)
    embed.set_footer(text="Nexus Seeker | Market Intelligence Feed")
    return embed


# ============================================================================
# Trading, risk, and alert embeds
# ============================================================================


def create_profit_lock_alert_embed(event: Dict[str, Any]) -> discord.Embed:
    """建立 DITM 獲利鎖定警報 Embed。"""
    embed = discord.Embed(
        title="🚨 DITM 凸性防護：獲利鎖定已觸發",
        description=(
            f"偵測到標的 **{event['symbol']}** 已進入深價內 (DITM)，"
            "凸性消失且風險報酬比惡化。"
        ),
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="觸發指標",
        value=f"```\n未實現損益: {event['pnl_pct']}% | DTE: {event['dte']}\n```",
        inline=False,
    )
    embed.add_field(name="執行指令", value="✅ **獲利鎖定 (Profit Lock)**", inline=True)
    embed.add_field(name="核心邏輯", value=event["reason"], inline=False)
    embed.set_footer(text="Mission-Critical Risk Environment | Nexus Seeker")
    return embed


def create_gamma_fragility_embed(event: Dict[str, Any]) -> discord.Embed:
    """建立 Gamma 脆弱性警告 Embed。"""
    embed = discord.Embed(
        title="🆘 Gamma 脆弱性警告 (Net Gamma < -20)",
        description="偵測到投資組合淨 Gamma 已跌破臨界點，曝險加速度呈非線性擴張。",
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="目前淨 Gamma", value=f"`{event['net_gamma']}`", inline=True)
    embed.add_field(name="安全臨界點", value=f"`{event['threshold']}`", inline=True)
    embed.add_field(
        name="優先指令",
        value="🛡️ **注入正 Gamma 緩衝 (買入近月 ATM 期權) 或 立即減倉**",
        inline=False,
    )
    embed.set_footer(text="Fragility Guard Engine v2.0 | Nexus Seeker")
    return embed


def create_pre_market_earnings_embed(
    alerts: List[Dict[str, Any]], scanned_symbols: List[str], warning_days: int
) -> discord.Embed:
    """建立盤前財報雷達通知 Embed。"""
    if alerts:
        description = "\n\n".join(
            (
                f"**{item['symbol']}** "
                f"({'⚠️ **持倉高風險**' if item['is_portfolio'] else '👀 觀察清單'})\n"
                f"└ 📅 財報日: `{item['earnings_date']}` (倒數 **{item['days_left']}** 天)"
            )
            for item in alerts
        )
        return discord.Embed(
            title="🚨 【盤前財報季雷達預警】",
            description=description,
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )

    scanned_list = "、".join(f"`{symbol}`" for symbol in scanned_symbols)
    return discord.Embed(
        title="✅ 【盤前財報季雷達掃描完畢】",
        description=f"已掃描：{scanned_list}\n\n近 {warning_days} 日內無財報風險，安全過關！",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )


def create_ditm_transition_alert_embed(
    *,
    symbol: str,
    exit_reason: str,
    action_taken: str,
    pnl: float,
    exposure_pct: float,
    hedge: Optional[Dict[str, Any]] = None,
) -> discord.Embed:
    """建立 VTR DITM 防禦通知 Embed。"""
    embed = discord.Embed(
        title="🚨 NRO 優先指令：Profit Lock (DITM 凸性防禦)",
        description=f"偵測到標的 **{symbol}** 已進入深價內 (DITM)，凸性消失且風險報酬比惡化。",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="觸發指標", value=f"```\n{exit_reason}\n```", inline=False)
    embed.add_field(name="執行動作", value=f"✅ **{action_taken}**", inline=True)
    embed.add_field(name="鎖定利潤", value=f"💰 `${pnl:.2f}`", inline=True)
    embed.add_field(
        name="帳戶目前總曝險",
        value=f"`{exposure_pct:.2f}%` (Beta-Weighted Delta)",
        inline=False,
    )
    if hedge:
        embed.add_field(
            name="🛡️ NRO 對沖建議",
            value=f"{hedge['action']} (缺口: `{hedge['gap']}`)",
            inline=False,
        )
    embed.set_footer(text="Quantitative Defense Pipeline | Nexus Risk Optimizer")
    return embed


def create_intraday_execution_guide_embed(
    *,
    phase_name: str,
    vix: float,
    memory_percent: float,
    is_memory_gated: bool,
    vix_level_name: str = "",
    greeks_status: str = "",
    runway_days: float = 0.0,
    theta_cov: float = 0.0,
    active_signal_content: str = "",
    sma_cache_size: int = 0,
    ema_cache_size: int = 0,
) -> discord.Embed:
    """建立盤中量化執行指引 Embed。"""
    embed = discord.Embed(
        title=f"🛡️ 盤中量化執行指引 - {phase_name}",
        color=discord.Color.red() if vix > 25 else discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    if is_memory_gated:
        embed.description = (
            "⚠️ **Memory Safety Gate Active**: VPS RAM > 85%。"
            "已暫停部分耗能分析以保證風控引擎穩定。"
        )
        embed.add_field(
            name="系統狀態", value=f"RAM: `{memory_percent}%`", inline=False
        )
        embed.set_footer(text="Nexus Seeker | NRO Vanna-Aware Intelligence")
        return embed

    embed.add_field(
        name="1️⃣ 風險狀態 (Risk Status)",
        value=f"**VIX 階級:** {vix_level_name} ({vix:.1f})\n**Greeks 完整性:** {greeks_status}",
        inline=False,
    )
    embed.add_field(
        name="2️⃣ 財務健康 (Financial Health)",
        value=f"**剩餘跑道:** `{runway_days:.1f}` 天\n**Theta 覆蓋率:** `{theta_cov:.1f}%`",
        inline=False,
    )
    embed.add_field(
        name="3️⃣ 活躍信號 (Active Signal)",
        value=active_signal_content,
        inline=False,
    )
    embed.add_field(
        name="4️⃣ 系統狀態 (System Health)",
        value=f"RAM: `{memory_percent}%` | BoundedCache (SMA/EMA): `{sma_cache_size}/{ema_cache_size}`",
        inline=False,
    )
    embed.set_footer(text="Nexus Seeker | NRO Vanna-Aware Intelligence")
    return embed


def create_vtr_settlement_notice_embed(
    *,
    status_icon: str,
    symbol: str,
    pnl: float,
    exposure_pct: float,
    regime: Optional[str] = None,
    target_delta: Optional[float] = None,
    hedge: Optional[Dict[str, Any]] = None,
) -> discord.Embed:
    """建立 VTR 轉倉/平倉結算通知 Embed。"""
    embed = discord.Embed(
        title=f"{status_icon} {symbol} 結算通知",
        color=discord.Color.blue() if "轉倉" in status_icon else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="損益", value=f"`${pnl:.2f}`", inline=True)
    embed.add_field(
        name="目前總曝險",
        value=f"`{exposure_pct:.2f}%` (Beta-Weighted Delta)",
        inline=True,
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
    embed.set_footer(text="GhostTrader | Settlement Notice")
    return embed
