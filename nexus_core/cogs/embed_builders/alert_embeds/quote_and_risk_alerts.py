"""即時報價與持倉風險/警報類 Embed 建構函式。"""

import discord

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from cogs.embed_builders._core import NexusEmbed


def create_quote_embed(symbol: str, data: Dict[str, Any]) -> discord.Embed:
    """建構即時報價 Embed。"""
    embed = NexusEmbed(
        title=f"💹 {symbol} 即時報價 (Real-time Quote)",
        color=discord.Color.blue() if data["dp"] >= 0 else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="💲 現價 (Current)", value=f"**${data['c']}**", inline=True)
    embed.add_field(name="📈 漲跌幅 (%)", value=f"`{data['dp']}%`", inline=True)
    embed.add_field(
        name="📊 今日高/低",
        value=f"H: `${data['h']}` / L: `${data['l']}`",
        inline=False,
    )
    embed.add_field(name="📉 前收盤 (PC)", value=f"`${data['pc']}`", inline=True)
    embed.set_footer(text="Nexus Seeker | Market Intelligence Feed")
    return embed


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
        name="🎯 觸發指標",
        value=f"```\n未實現損益: {event['pnl_pct']}% | DTE: {event['dte']}\n```",
        inline=False,
    )
    embed.add_field(
        name="✅ 執行指令", value="✅ **獲利鎖定 (Profit Lock)**", inline=True
    )
    embed.add_field(name="🧠 核心邏輯", value=event["reason"], inline=False)
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
    embed.add_field(
        name="🧮 目前淨 Gamma", value=f"`{event['net_gamma']}`", inline=True
    )
    embed.add_field(name="🛡️ 安全臨界點", value=f"`{event['threshold']}`", inline=True)
    embed.add_field(
        name="⚡ 優先指令",
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
    embed.add_field(name="🎯 觸發指標", value=f"```\n{exit_reason}\n```", inline=False)
    embed.add_field(name="✅ 執行動作", value=f"✅ **{action_taken}**", inline=True)
    embed.add_field(name="💰 鎖定利潤", value=f"💰 `${pnl:.2f}`", inline=True)
    embed.add_field(
        name="📊 帳戶目前總曝險",
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
    embed.add_field(name="💰 損益", value=f"`${pnl:.2f}`", inline=True)
    embed.add_field(
        name="📊 目前總曝險",
        value=f"`{exposure_pct:.2f}%` (Beta-Weighted Delta)",
        inline=True,
    )

    if regime is not None and target_delta is not None:
        embed.add_field(name="🧠 系統自主位階判定", value=f"`{regime}`", inline=False)
        embed.add_field(
            name="🎯 理想總曝險目標", value=f"`{target_delta:.1f} Delta`", inline=True
        )
    if hedge:
        embed.add_field(
            name="🛡️ 自動對沖決策",
            value=f"{hedge['action']} (缺口: `{hedge['gap']}`)",
            inline=False,
        )
    embed.set_footer(text="GhostTrader | Settlement Notice")
    return embed


def create_margin_api_alert_embed(ratio: float) -> discord.Embed:
    """建立保證金警戒與 API 斷線警告 Embed。"""
    embed = NexusEmbed(
        title="🚨 警報：保證金水位警戒與 API 斷線",
        description="偵測到帳戶保證金水位異常或券商 API 連線中斷，請立即確認！",
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="📊 目前保證金使用率", value=f"`{ratio*100:.2f}%`", inline=True
    )
    embed.add_field(
        name="⚡ 優先指令",
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

    embed.add_field(name="📐 VIX 期限結構比 (VTS)", value=vts_str, inline=True)
    embed.add_field(name="🌐 目前 VIX", value=vix_str, inline=True)

    if trigger_reason:
        embed.add_field(
            name="🎯 觸發原因", value=f"⚠️ **{trigger_reason}**", inline=False
        )

    embed.add_field(
        name="⚡ 優先指令",
        value="🛡️ **全面啟動尾部風險防禦 (Tail Risk Hedging) 並縮減部位規模**",
        inline=False,
    )
    embed.set_footer(text="Macro Risk Intelligence | Nexus Seeker")
    return embed
