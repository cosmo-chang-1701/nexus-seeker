"""Covered Call 解鎖建議與防禦性收租篩選 Embed 建構函式。"""

import discord

from cogs.embed_builders._ansi_utils import (
    _render_aligned_table,
    _to_float_or_none,
    fmt_or_dash,
)
from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._embed_helpers import _add_chunked_fields, _chunk_ansi_table

_CC_REC_HEADER = ["到期日", "履約價", "預估 Delta", "參考權利金", "年化收益率"]
_CC_REC_ALIGNS = ["left", "right", "right", "right", "right"]


def _cc_recommendation_tables(recs: list, *, allow_missing_yield: bool) -> list[str]:
    """備兌推薦合約表（動態欄寬，保留 ANSI 配色），回傳已切分、fence 完整的區塊。"""
    rows: list[list[str]] = []
    for r in recs:
        # 權利金一律為 bid/ask 中間價；零 bid 時為 ask/2 估算，加註 `*`。
        est = "*" if r.get("premium_source") == "ASK_HALF" else ""
        if allow_missing_yield and "annualized_yield" not in r:
            yield_str = "\u001b[1;30mN/A\u001b[0m"
        else:
            ann_yield = r.get("annualized_yield", 0.0)
            color_yield = "\u001b[1;32m" if ann_yield >= 10.0 else "\u001b[1;35m"
            yield_str = f"{color_yield}{ann_yield:.2f}%\u001b[0m"
        rows.append(
            [
                str(r.get("expiration", "")),
                f"\u001b[1;33m${r.get('strike', 0.0):.2f}\u001b[0m",
                f"\u001b[1;36m{r.get('delta', 0.0):.3f}\u001b[0m",
                f"\u001b[1;32m${r.get('premium', 0.0):.2f}{est}\u001b[0m",
                yield_str,
            ]
        )
    header_line, divider, *data_lines = _render_aligned_table(
        _CC_REC_HEADER, rows, _CC_REC_ALIGNS, sep=" | "
    )
    return _chunk_ansi_table(
        " " + header_line, " " + divider, [" " + ln for ln in data_lines]
    )


def create_covered_call_unlock_embed(data: dict) -> discord.Embed:
    """建立物理死鎖解除與備兌建單指引 Embed (繁體中文)"""
    symbol = data.get("symbol", "")
    current_shares = data.get("current_shares", 0.0)
    current_cost = data.get("current_cost", 0.0)
    new_cost_basis = data.get("new_cost_basis", 0.0)
    current_price = data.get("current_price", 0.0)
    recs = data.get("recommendations", [])
    covered_shares = data.get("covered_shares", 0.0)
    uncovered_shares = data.get("uncovered_shares", current_shares)
    max_new_contracts = data.get("max_new_contracts", 0)
    existing_calls = data.get("existing_calls", [])

    embed = NexusEmbed(
        title=f"🔓 警報：物理死鎖解除與備兌建單 | {symbol}",
        color=discord.Color.green() if recs else discord.Color.orange(),
    )

    # 計算現價與成本價差比率
    diff_pct = (
        ((current_price - current_cost) / current_cost * 100.0)
        if current_cost > 0
        else 0.0
    )
    diff_color = "\u001b[1;32m" if diff_pct >= 0 else "\u001b[1;31m"

    spot_lines = [
        "```ansi",
        f" ├─ 持股數量: \u001b[1;36m{current_shares:.0f} 股\u001b[0m",
        f" ├─ 原始均價: \u001b[1;33m${current_cost:,.2f}\u001b[0m",
        f" ├─ 當前現價: \u001b[1;32m${current_price:,.2f}\u001b[0m (與均價價差: {diff_color}{diff_pct:+.2f}%\u001b[0m)",
        f" └─ 模擬吸籌後加權成本: \u001b[1;35m${new_cost_basis:,.2f}\u001b[0m",
        "```",
        "*(已計入所有活躍 GTC 買入網格單模擬成交後的成本調整)*",
    ]

    embed.add_field(
        name="💼 現貨與吸籌模擬 (Spot & Accumulation)",
        value="\n".join(spot_lines),
        inline=False,
    )

    # 🔒 既有備兌覆蓋狀態：避免對已被既有 Short Call 鎖定的股數重複建議備兌
    if covered_shares > 0 or existing_calls:
        coverage_lines = [
            "```ansi",
            f" ├─ 已被既有 Short Call 鎖定: \u001b[1;33m{covered_shares:.0f} 股\u001b[0m",
            f" └─ 尚未覆蓋可用股數: \u001b[1;36m{uncovered_shares:.0f} 股\u001b[0m (最多可再開 \u001b[1;32m{max_new_contracts}\u001b[0m 口)",
        ]
        if existing_calls:
            coverage_lines.append("")
            coverage_lines.append(" 既有合約明細:")
            for i, c in enumerate(existing_calls):
                prefix = " └─ " if i == len(existing_calls) - 1 else " ├─ "
                strike = c.get("strike", 0.0)
                expiry = c.get("expiry", "")
                coverage_lines.append(f"{prefix}${strike:,.2f} Call @ {expiry}")
        coverage_lines.append("```")

        embed.add_field(
            name="🔒 既有備兌覆蓋狀態 (Existing Covered Call Coverage)",
            value="\n".join(coverage_lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="🔒 既有備兌覆蓋狀態 (Existing Covered Call Coverage)",
            value="```ansi\n 目前無既有備兌部位 (No Existing Covered Call Position)\n```",
            inline=False,
        )

    if recs:
        _add_chunked_fields(
            embed,
            "🎯 推薦 Covered Call 備兌合約 (Recommended Contracts)",
            _cc_recommendation_tables(recs, allow_missing_yield=True),
        )
        if any(r.get("premium_source") == "ASK_HALF" for r in recs):
            embed.add_field(
                name="ℹ️ 權利金備註",
                value="`*` 零 bid，權利金以 ask/2 估算",
                inline=False,
            )

        unlock_guide_lines = [
            "```ansi",
            " └─ 現貨大跌至低位網格吸籌完成後，透過建立高於新成本線且 Delta < 0.15",
            "    且年化收益率 >= 10% 的極虛值備兌 Call，可以在安全保護現貨",
            "    （防止被平價收回）的同時收取權利金，加速降低整體持有成本。",
            "```",
        ]
        embed.add_field(
            name="💡 物理死鎖解鎖說明 (Recovery Guidance)",
            value="\n".join(unlock_guide_lines),
            inline=False,
        )
    else:
        status_lines = [
            "```ansi",
            " ⚠️ 解鎖警告 (Unlock Alert)",
            " └─ 狀態: \u001b[1;31m未尋獲符合條件之極虛值 Covered Call 合約\u001b[0m",
            "",
            " 篩選門檻 (Criteria)",
            " ├─ 履約價 > 模擬加權成本",
            " ├─ 預估 Delta < 0.15",
            " └─ 年化收益率 >= 10.0% 或單次權利金 >= 現貨 1.0%",
            "",
            " 💡 策略建議 (Strategy)",
            " └─ 目前市場隱含波動率低迷或現貨價格過低，不宜盲目開倉。建議等待現貨反彈或波動率回升，拉開與成本線之空間後再行評估。",
            "```",
        ]
        embed.add_field(
            name="⚠️ 解鎖狀態與策略建議 (Unlock Status & Strategy)",
            value="\n".join(status_lines),
            inline=False,
        )

    embed.set_footer(text="Risk Engine | 物理死鎖解除策略模組")
    return embed


def create_cc_recovery_embed(data: dict) -> discord.Embed:
    """建立 Covered Call 備兌合約防禦性收租指引 Embed (繁體中文)"""
    symbol = data.get("symbol", "")
    current_price = data.get("current_price")
    recs = data.get("recommendations", [])
    fallback_iv = _to_float_or_none(data.get("fallback_iv"))

    # Note: color parameter triggers appropriate NexusEmbed palette mapping automatically
    embed = NexusEmbed(
        title=f"🛡️ {symbol} Covered Call 防禦性收租篩選結果",
        color=discord.Color.blue() if recs else discord.Color.orange(),
    )

    iv_ref_str = (
        f"{fallback_iv * 100.0:.2f}% (HV 代理/備援)"
        if fallback_iv is not None
        else "--%"
    )
    spot_lines = [
        "```ansi",
        " 標的現貨狀態 (Spot Asset Status)",
        f" ├─ 當前現價: \u001b[1;32m{fmt_or_dash(current_price, prefix='$')}\u001b[0m",
        f" └─ 波動率參考值: \u001b[1;35m{iv_ref_str}\u001b[0m",
        "```",
    ]

    embed.add_field(
        name="💼 標的行情 (Spot Market)",
        value="\n".join(spot_lines),
        inline=False,
    )

    if recs:
        rec_notes: list[str] = []
        if any(r.get("premium_source") == "ASK_HALF" for r in recs):
            rec_notes.append("`*` 零 bid，權利金以 ask/2 估算")
        if any(r.get("has_earnings_risk") for r in recs):
            rec_notes.append(
                "🔴 **警示標籤**：此合約橫跨財報日，隱含波動率（IV）可能於選後崩跌（IV Crush），請謹慎開倉。"
            )
        _add_chunked_fields(
            embed,
            "🎯 推薦 Covered Call 備兌合約 (Recommended Contracts)",
            _cc_recommendation_tables(recs, allow_missing_yield=False),
        )
        if rec_notes:
            embed.add_field(name="ℹ️ 備註", value="\n".join(rec_notes), inline=False)

        embed.add_field(
            name="💡 防禦性收租指引 (Defensive Yield Guidance)",
            value="篩選出滿足 **DTE 30-50 天、預估 Delta < 0.15 且年化收益率 >= 10%** 的極虛值 Covered Call 合約。此策略適合持股套牢或欲進行防禦性收租之交易，在安全保護現貨（降低被平價收回機率）的同時收取權利金，藉以降低持股成本。",
            inline=False,
        )
    else:
        status_lines = [
            "```ansi",
            " ⚠️ 篩選警告 (Filter Alert)",
            " └─ 狀態: \u001b[1;31m未尋獲符合條件之極虛值 Covered Call 合約\u001b[0m",
            "",
            " 篩選門檻 (Criteria)",
            " ├─ 到期天數 (DTE) 介於 30 至 50 天之間",
            " ├─ 預估 Delta < 0.15",
            " └─ 年化收益率 >= 10.0%",
            "```",
            "💡 **策略建議**：目前市場隱含波動率低迷或無符合條件的期權合約，不宜盲目開倉。建議等待現貨反彈或波動率回升，拉開空間後再行評估。",
        ]
        embed.add_field(
            name="⚠️ 篩選狀態與策略建議 (Status & Strategy)",
            value="\n".join(status_lines),
            inline=False,
        )

    embed.set_footer(text="Covered Call 收租策略模組")
    return embed
