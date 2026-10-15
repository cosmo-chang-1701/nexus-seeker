"""即時報價與持倉風險/警報類 Embed 建構函式。"""

import discord

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from cogs.embed_builders._ansi_utils import _to_float_or_none
from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._embed_helpers import (
    _ANSI_0,
    _ANSI_G,
    _ANSI_R,
    _ANSI_W,
    _add_ansi_field_safely,
    _ansi_tree_lines,
)


def create_quote_embed(symbol: str, data: Dict[str, Any]) -> discord.Embed:
    """建構即時報價 Embed。"""
    dp = data.get("dp")
    pc = data.get("pc")
    # 封盤或上游無資料時 Finnhub 以 0 填值：現價缺失／為 0 一律視為無報價
    c = _to_float_or_none(data.get("c"))
    no_quote = c is None or c == 0
    h = _to_float_or_none(data.get("h"))
    low = _to_float_or_none(data.get("l"))
    if no_quote:
        dp = None
    embed = NexusEmbed(
        title=f"💹 {symbol} 即時報價 (Real-time Quote)"
        + (" [封盤中/數據未更新]" if no_quote else ""),
        color=discord.Color.red()
        if dp is not None and dp < 0
        else discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    price_txt = f"${data['c']}" if not no_quote else "--"
    if dp is None:
        dp_txt = f"{_ANSI_W}--{_ANSI_0}"
    else:
        dp_txt = f"{_ANSI_R if dp < 0 else _ANSI_G}{dp}%{_ANSI_0}"
    if not no_quote and h is not None and low is not None and h != 0:
        hl_txt = f"H: ${data['h']} / L: ${data['l']}"
    else:
        hl_txt = "H: -- / L: --"
    pc_txt = f"${pc}" if pc is not None and not no_quote else "--"
    _add_ansi_field_safely(
        embed,
        "💹 報價明細",
        _ansi_tree_lines(
            [
                f"現價 (Current) : {_ANSI_W}{price_txt}{_ANSI_0}",
                f"漲跌幅 (%)     : {dp_txt}",
                f"今日高/低      : {_ANSI_W}{hl_txt}{_ANSI_0}",
                f"前收盤 (PC)    : {_ANSI_W}{pc_txt}{_ANSI_0}",
            ]
        ),
    )
    embed.set_footer(text="Market Intelligence Feed")
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
    pnl_txt = (
        f"{event['pnl_pct']}%" if event.get("pnl_pct") is not None else "-- (報價缺失)"
    )
    _add_ansi_field_safely(
        embed,
        "🎯 觸發指標",
        _ansi_tree_lines(
            [
                f"未實現損益: {_ANSI_W}{pnl_txt}{_ANSI_0}",
                f"DTE: {_ANSI_W}{event['dte']}{_ANSI_0}",
                f"執行指令: {_ANSI_G}獲利鎖定 (Profit Lock){_ANSI_0}",
            ]
        ),
    )
    embed.add_field(name="🧠 核心邏輯", value=event["reason"], inline=False)
    embed.set_footer(text="Mission-Critical Risk Environment")
    return embed


def create_gamma_fragility_embed(event: Dict[str, Any]) -> discord.Embed:
    """建立 Gamma 脆弱性警告 Embed。"""
    embed = NexusEmbed(
        title="🆘 警報：Gamma 脆弱性與斷層",
        description="偵測到投資組合淨 Gamma 已跌破臨界點，曝險加速度呈非線性擴張。",
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc),
    )
    _add_ansi_field_safely(
        embed,
        "🧮 Gamma 水位",
        _ansi_tree_lines(
            [
                f"目前淨 Gamma : {_ANSI_R}{event['net_gamma']}{_ANSI_0}",
                f"安全臨界點   : {_ANSI_W}{event['threshold']}{_ANSI_0}",
            ]
        ),
    )
    embed.add_field(
        name="⚡ 優先指令",
        value="🛡️ **注入正 Gamma 緩衝 (買入近月 ATM 期權) 或 立即減倉**",
        inline=False,
    )
    embed.set_footer(text="Fragility Guard Engine v2.0")
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
    _add_ansi_field_safely(
        embed, "🎯 觸發指標", [f" {ln}" for ln in str(exit_reason).split("\n")]
    )
    pnl_color = _ANSI_G if pnl >= 0 else _ANSI_R
    _add_ansi_field_safely(
        embed,
        "💰 鎖定結果",
        _ansi_tree_lines(
            [
                f"執行動作 : {_ANSI_G}{action_taken}{_ANSI_0}",
                f"鎖定利潤 : {pnl_color}${pnl:.2f}{_ANSI_0}",
                f"帳戶目前總曝險 : {_ANSI_W}{exposure_pct:.2f}%{_ANSI_0} (Beta-Weighted Delta)",
            ]
        ),
    )
    if hedge:
        _add_ansi_field_safely(
            embed,
            "🛡️ NRO 對沖建議",
            _ansi_tree_lines(
                [f"{hedge['action']} (缺口: {_ANSI_W}{hedge['gap']}{_ANSI_0})"]
            ),
        )
    embed.set_footer(text="Quantitative Defense Pipeline | Risk Optimizer")
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
    vtr_rows = [
        f"損益 : {_ANSI_G if pnl >= 0 else _ANSI_R}${pnl:.2f}{_ANSI_0}",
        f"目前總曝險 : {_ANSI_W}{exposure_pct:.2f}%{_ANSI_0} (Beta-Weighted Delta)",
    ]
    if regime is not None and target_delta is not None:
        vtr_rows.append(f"系統自主位階判定 : {_ANSI_W}{regime}{_ANSI_0}")
        vtr_rows.append(f"理想總曝險目標 : {_ANSI_W}{target_delta:.1f} Delta{_ANSI_0}")
    _add_ansi_field_safely(embed, "💰 結算摘要", _ansi_tree_lines(vtr_rows))
    if hedge:
        _add_ansi_field_safely(
            embed,
            "🛡️ 自動對沖決策",
            _ansi_tree_lines(
                [f"{hedge['action']} (缺口: {_ANSI_W}{hedge['gap']}{_ANSI_0})"]
            ),
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
    _add_ansi_field_safely(
        embed,
        "📊 目前保證金使用率",
        _ansi_tree_lines([f"使用率 : {_ANSI_R}{ratio*100:.2f}%{_ANSI_0}"]),
    )
    embed.add_field(
        name="⚡ 優先指令",
        value="🛡️ **立即降低曝險或補足保證金，避免面臨平倉 (Margin Call)**",
        inline=False,
    )
    embed.set_footer(text="System & Margin Guard")
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
        vts_str = f"{_ANSI_W}{vts_ratio:.2f}{_ANSI_0} (嚴重倒掛 🚨)"
    elif vts_ratio >= 1.00:
        vts_str = f"{_ANSI_W}{vts_ratio:.2f}{_ANSI_0} (期限倒掛 ⚠️)"
    elif vts_ratio > 0.0:
        vts_str = f"{_ANSI_W}{vts_ratio:.2f}{_ANSI_0} (正價差健康 🟢)"
    else:
        vts_str = f"{_ANSI_W}N/A{_ANSI_0} (數據未更新)"

    # VIX 呈現
    if vix >= 30.0:
        vix_str = f"{_ANSI_W}{vix:.1f}{_ANSI_0} (極端恐慌 🚨)"
    elif vix >= 20.0:
        vix_str = f"{_ANSI_W}{vix:.1f}{_ANSI_0} (警戒水位 ⚠️)"
    elif vix > 0.0:
        vix_str = f"{_ANSI_W}{vix:.1f}{_ANSI_0} (波動平穩 🟢)"
    else:
        vix_str = f"{_ANSI_W}N/A{_ANSI_0} (數據異常)"

    _add_ansi_field_safely(
        embed,
        "🌐 VIX 水位",
        _ansi_tree_lines(
            [f"VIX 期限結構比 (VTS) : {vts_str}", f"目前 VIX : {vix_str}"]
        ),
    )

    if trigger_reason:
        embed.add_field(
            name="🎯 觸發原因", value=f"⚠️ **{trigger_reason}**", inline=False
        )

    embed.add_field(
        name="⚡ 優先指令",
        value="🛡️ **全面啟動尾部風險防禦 (Tail Risk Hedging) 並縮減部位規模**",
        inline=False,
    )
    embed.set_footer(text="Macro Risk Intelligence")
    return embed
