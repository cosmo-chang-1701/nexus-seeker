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

from typing import Any
import discord
from cogs.embed_builders._core import NexusEmbed
from market_analysis.scenario_classifier import MarketScenario
from datetime import datetime, timezone
from typing import Dict, List, Optional

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


# ============================================================================
# PowerSqueeze Embed
# ============================================================================


def create_psq_embed(data: dict) -> discord.Embed:
    """建構獨立的 PowerSqueeze (PSQ) 戰情報告 Embed"""
    sym = data.get("symbol", "UNKNOWN")
    psq = data.get("psq_result")

    if not psq:  # fallback
        return NexusEmbed(
            title=f"⚡ PowerSqueeze 戰情報告 | {sym}",
            description="無可用數據",
            color=discord.Color.dark_grey(),
        )

    color = discord.Color.purple() if psq.is_squeezing else discord.Color.dark_teal()
    if psq.is_breakout_long:
        color = discord.Color.green()
    elif psq.is_breakout_short:
        color = discord.Color.red()

    embed = NexusEmbed(
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

    if vix_label != "NORMAL":
        label_map = {
            "OVEREXTENDED_RISK": "⚠️ **過度延伸風險** | 低 VIX 環境多頭訊號可能是牛陷阱",
            "HIGH_CONVICTION_RECOVERY": "🚀 **高確信反彈** | 高 VIX + 空頭減速 = 反轉機會",
        }
        label_text = label_map.get(vix_label, vix_label)
        embed.add_field(name="🏅 VIX 動能判定", value=label_text, inline=False)

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


def create_news_scan_embed(symbol: Any, news_text: Any):  # type: ignore
    """建構新聞掃描結果的 Embed"""
    embed = NexusEmbed(title=f"📰 {symbol} 官方新聞掃描", color=discord.Color.blue())
    add_news_field(embed, news_text)
    embed.set_footer(text="Nexus Seeker 研報系統 • 資料來源: Yahoo Finance")
    return embed


def create_reddit_scan_embed(symbol: Any, reddit_text: Any):  # type: ignore
    """建構 Reddit 情緒掃描結果的 Embed"""
    embed = NexusEmbed(
        title=f"🔥 {symbol} 散戶情緒優勢 (Reddit 同步)", color=discord.Color.orange()
    )
    add_reddit_text = reddit_text
    add_reddit_field(embed, add_reddit_text)
    embed.set_footer(
        text="Nexus Seeker 研報系統 • 資料來源: Reddit (WSB/Stocks/Options)"
    )
    return embed


def create_media_sentiment_embed(symbol: Any, news_text: Any, reddit_text: Any):  # type: ignore
    """建構輿情與社群 (Media & Social) 掃描結果的統一 Embed"""
    embed = NexusEmbed(
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


def create_polymarket_list_embed(
    markets: List[Dict[str, Any]],
    chunk_size: int = 8,
    query: Optional[str] = None,
) -> List[discord.Embed]:
    """建構 Polymarket 監控中的熱門市場或搜尋結果 Embed 清單 (支援多頁分頁與完整文字 Markdown 連結)。"""
    base_title = (
        f"🐋 Polymarket 搜尋結果: {query.upper()}"
        if query
        else "🐋 Polymarket 巨鯨意圖圖譜"
    )

    if not markets:
        embed = NexusEmbed(
            title=base_title,
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.description = (
            f"查無與 '{query}' 相關之活躍美股預測合約。"
            if query
            else "目前沒有監控中的市場。"
        )
        return [embed]

    chunks: List[List[Dict[str, Any]]] = [
        markets[i : i + chunk_size] for i in range(0, len(markets), chunk_size)
    ]
    total_pages = len(chunks)
    embeds: List[discord.Embed] = []

    global_index = 1
    for page_idx, chunk in enumerate(chunks, 1):
        page_title = base_title
        if total_pages > 1:
            page_title += f" (第 {page_idx}/{total_pages} 頁)"

        embed = NexusEmbed(
            title=page_title,
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )

        lines: List[str] = []
        for m in chunk:
            question = str(m.get("question", "未知市場")).strip()
            event_slug = m.get("event_slug") or m.get("slug")
            url = (
                f"https://polymarket.com/event/{event_slug}"
                if event_slug
                else "https://polymarket.com"
            )

            # 取得 token 價格資訊 (如果有的話)
            tokens = m.get("tokens", [])
            price_info_parts: List[str] = []
            if tokens:
                for t in tokens[:2]:
                    outcome = str(t.get("outcome", "")).strip()
                    price = t.get("price", 0)

                    # 排除單字元的雜訊 (例如 [ or " )
                    if len(outcome) <= 1 and outcome not in ["?", "是", "否"]:
                        continue

                    try:
                        price_val = float(price)
                        prob_pct = f"{price_val * 100:.0f}%"
                        price_info_parts.append(
                            f"**{outcome}**: `{prob_pct}` (${price_val:.2f})"
                        )
                    except Exception:
                        price_info_parts.append(f"**{outcome}**: `{price}`")

            # 加入成交量標籤
            vol_num = float(m.get("volumeNum") or m.get("volume") or 0.0)
            vol_str = ""
            if vol_num > 1000000:
                vol_str = f" | 💵 `${vol_num / 1000000:.1f}M`"
            elif vol_num > 1000:
                vol_str = f" | 💵 `${vol_num / 1000:.1f}k`"
            elif vol_num > 0:
                vol_str = f" | 💵 `${vol_num:.0f}`"

            odds_str = (
                " │ ".join(price_info_parts) if price_info_parts else "等待流動性"
            )

            lines.append(f"`{global_index:02d}.` **[{question}]({url})**")
            lines.append(f"    └─ 📊 {odds_str}{vol_str}\n")
            global_index += 1

        full_desc = "\n".join(lines).strip()
        # 檢查總長度，避免超過 Discord 限制
        if len(full_desc) > 3900:
            full_desc = full_desc[:3890] + "\n..."

        embed.description = full_desc
        embed.set_footer(
            text=f"Nexus Seeker | Polymarket Monitor • 共 {len(markets)} 個活躍市場"
        )
        embeds.append(embed)

    return embeds


def create_polymarket_status_embed(status: Dict[str, Any]) -> discord.Embed:
    """建構 Polymarket 服務狀態 Embed。"""
    embed = NexusEmbed(
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
    embed = NexusEmbed(
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
    embed = NexusEmbed(
        title=f"🚨 警報：DITM 凸性防護與獲利鎖定 | {event.get('symbol', 'UNKNOWN')}",
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
    embed = NexusEmbed(
        title="🆘 警報：Gamma 脆弱性與斷層",
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
    embed = NexusEmbed(
        title=f"🚨 警報：DITM 凸性防護與獲利鎖定 | {symbol}",
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
    embed = NexusEmbed(
        title=f"📈 報告：虛擬交易室 (VTR) 績效總結 | {symbol}",
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


def create_scenario_alert_embed(
    symbol: str,
    scenario: MarketScenario,
    price: float,
    put_wall: float,
    call_wall: float,
    gamma_flip: float,
    ivr: float,
    hvn: float,
    lvn: float,
    skew_percentile: float = 50.0,
) -> discord.Embed:
    """建立戰場情境轉折警報 Embed"""

    color_map = {
        MarketScenario.GOLDEN_LEFT: discord.Color.gold(),
        MarketScenario.STRONG_BREAKOUT: discord.Color.green(),
        MarketScenario.GOLDEN_TAKE_PROFIT: discord.Color.teal(),
        MarketScenario.FAKE_SUPPORT_TRAP: discord.Color.orange(),
        MarketScenario.STRUCTURAL_BREAKDOWN: discord.Color.red(),
        MarketScenario.WHALE_ESCORT_RESONANCE: discord.Color.purple(),
    }

    title = f"🚨 戰場情境轉折警報 | {symbol}"
    desc = f"**🧭 觸發情境：{scenario.value}**"
    action = "未知操作"
    tool = "未定義"

    if scenario == MarketScenario.WHALE_ESCORT_RESONANCE:
        title = f"💎 巨鯨護航共振觸發：{symbol}"
        desc = f"偵測到 **GEX 正 Gamma 牆 (${put_wall:.2f})** 確立，配合 **Skew 避險分位 ({skew_percentile:.1f}%) < 50%**，以及 **UOA 巨鯨大單方向一致 (STO Put / BTO Call)**。"
        tool = "現貨分批 或 Sell Put Spread (高勝率防禦建倉)"
        action = "【勝率極值共振】巨鯨實質硬地板成型，建議可於此防禦水位建倉做多或賣出 Put Spread。"
    elif scenario == MarketScenario.GOLDEN_LEFT:
        tool = "現貨分批 或 Sell Put Spread (吃高 IV + Theta)"
        action = "【試水溫加碼 20%~30%】鋼鐵牆成型，做市商對沖盤與現貨買盤雙重護航。"
    elif scenario == MarketScenario.STRONG_BREAKOUT:
        tool = "Buy Call Debit Spread 或 現貨追擊 (軋空行情)"
        action = "【順勢追擊加碼】做市商進入 Call Squeeze（軋空追買），LVN 提供無阻力加速區。"
    elif scenario == MarketScenario.GOLDEN_TAKE_PROFIT:
        if ivr > 50.0:
            tool = "分批賣出現貨 或 Sell Call Spread"
            action = "【分批減碼 30%~50%】鎖定利潤；因 IVR > 50%，可疊加 Sell Call Spread 賺取波動率退潮紅利。"
        else:
            tool = "分批賣出現貨"
            action = "【分批減碼 30%~50%】鎖定利潤。做市商拋售賣壓與 HVN 籌碼牆將形成巨大上檔天花板。"
    elif scenario == MarketScenario.FAKE_SUPPORT_TRAP:
        tool = "禁止開多 (可佈局 Bear Spread)"
        action = "【嚴禁抄底 / 觀望】紙糊牆或連環砍單區，基本面再好也不接刀。"
    elif scenario == MarketScenario.STRUCTURAL_BREAKDOWN:
        tool = "清空個股多頭，資金轉入 QQQ / SPY 大盤 ETF 避風港"
        action = "【100% 絕對執行轉倉】個股護盤結構失效，停止對個股抱有幻想。"
    else:
        tool = "未定義"
        action = "未知操作"

    embed = NexusEmbed(
        title=title,
        description=desc,
        color=color_map.get(scenario, discord.Color.default()),
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="📊 關鍵風控指標",
        value=f"• 現價: `${price:.2f}`\n• Gamma Flip: `${gamma_flip:.2f}`\n• PutWall/CallWall: `${put_wall:.2f}` / `${call_wall:.2f}`\n• HVN/LVN: `${hvn:.2f}` / `${lvn:.2f}`\n• IV Rank: `{ivr:.1f}%`",
        inline=False,
    )
    embed.add_field(name="🛠️ 最優交易工具", value=tool, inline=False)
    embed.add_field(name="⚔️ 資金處置與加減碼指令", value=f"└─ {action}", inline=False)
    embed.set_footer(text="Nexus Risk Optimizer | 戰場情境決策矩陣 (v2.0)")
    return embed


def create_margin_api_alert_embed(ratio: float) -> discord.Embed:
    """建立保證金警戒與 API 斷線警告 Embed。"""
    embed = NexusEmbed(
        title="🚨 警報：保證金水位警戒與 API 斷線",
        description="偵測到帳戶保證金水位異常或券商 API 連線中斷，請立即確認！",
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="目前保證金使用率", value=f"`{ratio*100:.2f}%`", inline=True)
    embed.add_field(
        name="優先指令",
        value="🛡️ **立即降低曝險或補足保證金，避免面臨平倉 (Margin Call)**",
        inline=False,
    )
    embed.set_footer(text="System & Margin Guard | Nexus Seeker")
    return embed


def create_vix_tail_risk_embed(
    vts_ratio: float, vix: float, trigger_reason: Optional[str] = None
) -> discord.Embed:
    """建立 VIX 期限結構倒掛與黑天鵝預警 Embed。"""
    embed = NexusEmbed(
        title="🦇 雷達：VIX 期限結構倒掛與黑天鵝預警",
        description="偵測到 VIX 期限結構嚴重倒掛或 VIX 數值飆升，市場陷入極端恐慌。",
        color=discord.Color.purple(),
        timestamp=datetime.now(timezone.utc),
    )

    # VTS 呈現
    if vts_ratio >= 1.10:
        vts_str = f"`{vts_ratio:.2f}` (嚴重倒掛 🚨)"
    elif vts_ratio >= 1.00:
        vts_str = f"`{vts_ratio:.2f}` (期限倒掛 ⚠️)"
    elif vts_ratio > 0.0:
        vts_str = f"`{vts_ratio:.2f}` (正價差健康 🟢)"
    else:
        vts_str = "`N/A` (數據未更新)"

    # VIX 呈現
    if vix >= 30.0:
        vix_str = f"`{vix:.1f}` (極端恐慌 🚨)"
    elif vix >= 20.0:
        vix_str = f"`{vix:.1f}` (警戒水位 ⚠️)"
    elif vix > 0.0:
        vix_str = f"`{vix:.1f}` (波動平穩 🟢)"
    else:
        vix_str = "`N/A` (數據異常)"

    embed.add_field(name="VIX 期限結構比 (VTS)", value=vts_str, inline=True)
    embed.add_field(name="目前 VIX", value=vix_str, inline=True)

    if trigger_reason:
        embed.add_field(name="觸發原因", value=f"⚠️ **{trigger_reason}**", inline=False)

    embed.add_field(
        name="優先指令",
        value="🛡️ **全面啟動尾部風險防禦 (Tail Risk Hedging) 並縮減部位規模**",
        inline=False,
    )
    embed.set_footer(text="Macro Risk Intelligence | Nexus Seeker")
    return embed


def create_polymarket_prob_shift_embed(
    market: str, old_prob: float, new_prob: float
) -> discord.Embed:
    """建立 Polymarket 預測機率閃崩/暴拉警報 Embed。"""
    delta = (new_prob - old_prob) * 100
    emoji = "📈" if delta > 0 else "📉"
    embed = NexusEmbed(
        title="⚡ 警報：Polymarket 預測機率閃崩 / 暴拉",
        description=f"偵測到 Polymarket 特定事件預測機率發生 {emoji} **劇烈波動** (> 15%)，Delta 突變！",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="市場名稱", value=f"**{market}**", inline=False)
    embed.add_field(
        name="機率變化",
        value=f"`{old_prob*100:.1f}%` ➔ `{new_prob*100:.1f}%`",
        inline=True,
    )
    embed.add_field(name="Delta", value=f"`{delta:+.1f}%`", inline=True)
    embed.add_field(
        name="可能原因",
        value="📰 **突發新聞、重大事件落地、或大戶倒貨重新定價**",
        inline=False,
    )
    embed.set_footer(text="Polymarket AI Monitor | Nexus Seeker")
    return embed


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
