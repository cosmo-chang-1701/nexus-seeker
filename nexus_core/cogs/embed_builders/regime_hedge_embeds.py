"""兩級體制避險（反向 ETF 對沖）的 Discord 卡片建構。

無互動按鈕的獨立情境卡；資料來自 HedgePlan／InverseLeg
（market_analysis/dynamic_rollover/regime_hedge_sizing.py）。
"""

import discord
from typing import List

from cogs.embed_builders._core import NexusEmbed
from market_analysis.dynamic_rollover.regime_hedge_sizing import (
    HEDGE_MODE_NET_DELTA_RANGE_PCT,
    NET_DELTA_CEILING_PCT,
    NET_DELTA_FLOOR_PCT,
    STOP_SHORT_MAX_INVERSE_PCT,
    HedgePlan,
    InverseLeg,
)
from ui.panel_renderer import truncate_with_boundary

_FIELD_LIMIT = 1024
_FIELD_SAFE_LIMIT = 1000
_DESCRIPTION_REASON_LIMIT = 700
_MAX_NOTES = 10
_NOTE_LIMIT = 100

_MODE_TITLES = {
    "TIER1_PSQ": "🛡️ 第一級避險｜震盪市 PSQ 局部對沖",
    "HEDGE": "🚨 第二級避險｜負 Gamma 踩踏・反向對沖",
    "STOP_SHORT": "🚨 第二級避險｜負 Gamma 踩踏・止損轉空",
}

_STOP_MARKET_RULE = (
    "停損一律用 STOP MARKET（市價停損），禁用窄價差 STOP_LIMIT（跳空時可能不成交）"
)


def _safe(text: str) -> str:
    return truncate_with_boundary(text, _FIELD_SAFE_LIMIT)


def _pct(value: float) -> str:
    """帶號百分比，負號用全形減號「−」。"""
    return f"{value:+.0f}%".replace("-", "−")


def _title(plan: HedgePlan, is_preliminary: bool) -> str:
    if plan.mode in _MODE_TITLES:
        base = _MODE_TITLES[plan.mode]
    elif plan.tier >= 2:
        base = _MODE_TITLES["HEDGE"]
    else:
        base = _MODE_TITLES["TIER1_PSQ"]
    return f"⏳ 預警觀察｜{base}" if is_preliminary else base


def _delta_field(plan: HedgePlan) -> str:
    lines = [
        f"{plan.net_delta_before_pct:+.1f}% → {plan.net_delta_after_pct:+.1f}%",
        f"守門區間 {_pct(NET_DELTA_FLOOR_PCT)}～{_pct(NET_DELTA_CEILING_PCT)}"
        "｜融資使用率 0.0%（現金帳戶）",
    ]
    if not plan.within_guard:
        lines.append("⚠️ 超出守門區間")
    if plan.mode == "HEDGE":
        lo, hi = HEDGE_MODE_NET_DELTA_RANGE_PCT
        lines.append(f"目標 {hi:.0f}%～{_pct(lo)}")
    elif plan.mode == "STOP_SHORT":
        lines.append(
            f"SQQQ＋SOXS 合計上限 {STOP_SHORT_MAX_INVERSE_PCT:.0f}% NAV，其餘轉 BOXX"
        )
    elif plan.tier == 1:
        lines.append("僅允許 PSQ（−1x），SQQQ／SOXS 封鎖")
    return _safe("\n".join(lines))


def _leg_line(leg: InverseLeg, nav: float, is_preliminary: bool) -> str:
    if is_preliminary:
        pct = leg.target_value / nav * 100.0 if nav > 0 else 0.0
        line = f"{leg.symbol} 約 ${leg.target_value:,.0f}（{pct:.0f}% NAV）"
    elif leg.reference_price <= 0 or leg.shares <= 0:
        line = (
            f"買進 {leg.symbol} 股數待確認（約 ${leg.target_value:,.0f}，參考價缺失）"
            f"｜STOP MARKET 觸發價待確認（−{leg.stop_pct:.1f}%）"
        )
    else:
        line = (
            f"買進 {leg.symbol} {leg.shares} 股（約 ${leg.target_value:,.0f}，"
            f"參考價 ${leg.reference_price:.2f}）｜STOP MARKET 觸發 "
            f"${leg.stop_trigger_price:.2f}（−{leg.stop_pct:.1f}%）"
        )
    if leg.earnings_reduced:
        syms = "、".join(leg.earnings_symbols)
        line += f"（財報隔離 ×0.5：{syms}）"
    return line


def create_regime_hedge_embed(
    plan: HedgePlan,
    *,
    market_regime: str,
    is_preliminary: bool,
    reason: str,
) -> discord.Embed:
    """產生兩級體制避險卡；is_preliminary 為預警觀察版（不列股數與觸發價）。"""
    color = discord.Color.red() if plan.tier >= 2 else discord.Color.orange()
    embed = NexusEmbed(title=_title(plan, is_preliminary), color=color)
    safe_reason = truncate_with_boundary(reason, _DESCRIPTION_REASON_LIMIT)
    embed.description = f"{safe_reason}\n\n大盤 Regime：**{market_regime[:100]}**"

    embed.add_field(name="等效淨 Delta", value=_delta_field(plan), inline=False)

    if plan.liquidate_symbols:
        shown = plan.liquidate_symbols[:20]
        text = "、".join(shown)
        if len(plan.liquidate_symbols) > len(shown):
            text += f" 等共 {len(plan.liquidate_symbols)} 檔"
        embed.add_field(
            name="清倉",
            value=_safe(f"{text}\n合計約 ${plan.liquidate_value:,.0f}"),
            inline=False,
        )
    if plan.sell_for_funding_value > 0:
        embed.add_field(
            name="需減碼支應",
            value=_safe(
                f"約 ${plan.sell_for_funding_value:,.0f}\n"
                "現金帳戶零融資，買反向 ETF 的資金需由多頭減碼支應"
            ),
            inline=False,
        )

    if plan.legs:
        rows: List[str] = [_leg_line(g, plan.nav, is_preliminary) for g in plan.legs]
    else:
        rows = ["本輪不新增反向 ETF"]
    if not is_preliminary:
        rows.append(_STOP_MARKET_RULE)
    embed.add_field(name="反向 ETF 委託", value=_safe("\n".join(rows)), inline=False)

    embed.add_field(
        name="BOXX",
        value=f"${plan.boxx_target_value:,.0f}（0 Delta 現金等價物）",
        inline=False,
    )

    if plan.minus3x_frozen_reason:
        embed.add_field(
            name="🧊 −3x 凍結",
            value=_safe(plan.minus3x_frozen_reason),
            inline=False,
        )

    frozen = plan.minus3x_frozen_reason
    deduped = list(dict.fromkeys(n for n in plan.notes if n))
    if frozen:
        # 🧊 欄位已顯示凍結原因，略過 sizing 放進 notes 的重複說明
        deduped = [n for n in deduped if frozen not in n]
    notes = deduped[:_MAX_NOTES]
    if notes:
        lines = [f"• {truncate_with_boundary(n, _NOTE_LIMIT)}" for n in notes]
        if len(deduped) > len(notes):
            lines.append(f"…（另 {len(deduped) - len(notes)} 則略）")
        embed.add_field(name="備註", value=_safe("\n".join(lines)), inline=False)

    if is_preliminary:
        embed.set_footer(
            text="盤前／開盤 30 分內 GEX 與 OI 為前一交易日快取；"
            "10:00 ET（台北 22:00／23:00 依夏令）期權鏈重算後推送確定版股數"
        )
    else:
        embed.set_footer(text="顧問建議，需手動下單")
    return embed
