"""Discord Embed 通用工具函式。

收錄跨多種 Embed 類型共用的 helper，包含：
- 欄位值安全截斷
- ANSI 表格分頁
- AI 報告欄位附加
- Embed 按欄位拆分
- 持倉表格渲染
- 共用欄位 helper (_add_vix_battle_status_field, _add_market_overview_fields…)
"""

import re
import discord

from typing import List, Dict, Any

from ui import panel_renderer
from cogs.embed_builders._ansi_utils import (
    _clean_ansi,
    _truncate_with_boundary,
)


# ============================================================================
# Embed field value safety utilities
# ============================================================================


def _safe_embed_field_value(text: str, fallback: str, max_len: int = 1024) -> str:
    """確保欄位值非空且符合 Discord 長度上限。"""
    value = (text or "").strip()
    if not value:
        value = fallback

    # 保留尾端間距，讓下一個欄位視覺更乾淨。
    suffix = "\n\u200b"
    room = max(1, max_len - len(suffix))
    value = _truncate_with_boundary(value, room)
    value = value + suffix

    if len(value) > max_len:
        value = _truncate_with_boundary(value, max_len)
    if not value.strip():
        value = fallback + suffix
    return value


def _chunk_ansi_table(
    header: str, divider: str, data_lines: List[str], max_len: int = 1024
) -> List[str]:
    """將 ANSI 表格切成多個符合 Discord Embed field value 長度限制的區塊。"""
    return panel_renderer.chunk_ansi_table(
        header,
        divider,
        data_lines,
        max_len=max_len,
    )


def _safe_embed_codeblock_value(
    content: str,
    fallback: str,
    *,
    lang: str = "text",
    max_len: int = 1024,
) -> str:
    """產生安全的 code block 欄位值，避免截斷導致 fence 不閉合。"""
    return panel_renderer.safe_codeblock_value(
        content,
        fallback,
        lang=lang,
        max_len=max_len,
        add_zws_suffix=True,
    )


def _build_watchlist_style_panel(
    title_line: str,
    content: str,
    *,
    width: int = 45,
    empty_msg: str = "暫無資料",
) -> str:
    """建立 Watchlist 半小時戰報風格的 ANSI 面板文字內容 (不含尾端 \u200b)。"""
    return panel_renderer.build_watchlist_style_panel(
        title_line,
        content,
        width=width,
        empty_msg=empty_msg,
    )


def _report_embed_color(text: str) -> discord.Color:
    """根據報告內容關鍵字推導一致的報告顏色。"""
    if "🚨" in text or "🆘" in text:
        return discord.Color.red()
    if "⚠️" in text:
        return discord.Color.orange()
    return discord.Color.blue()


def _extract_report_batch(report_type: str) -> str:
    match = re.search(r"(\[[^\]]+\])", report_type)
    if match:
        return match.group(1)
    return report_type


def _parse_ai_report_sections(ai_report_text: str) -> list[tuple[str, str]]:
    """將 LLM 報告切成可直接映射到 Embed 欄位的段落。"""
    sections = re.split(r"\n(?=\d+\.\s+\*\*|(?:\*\*[\u2600-\u2BFF]))", ai_report_text)
    parsed_sections: list[tuple[str, str]] = []

    if len(sections) <= 1:
        return parsed_sections

    for section in sections:
        section = section.strip()
        if not section:
            continue

        lines = section.split("\n", 1)
        header = lines[0].replace("**", "").strip()
        header = re.sub(r"^\d+\.\s*", "", header)
        content = lines[1].strip() if len(lines) > 1 else "無內容"
        content = re.sub(r"^-{3,}$", "", content, flags=re.MULTILINE).strip()
        if header:
            parsed_sections.append((header, content or "無詳細資訊"))

    return parsed_sections


def _append_ai_report_fields(
    embed: discord.Embed,
    ai_report_text: str,
    *,
    fallback_field_name: str = "🤖 AI 分析摘要",
) -> None:
    """將 AI 報告以一致的欄位格式附加到 Embed。"""
    sections = _parse_ai_report_sections(ai_report_text)
    if sections:
        for header, content in sections:
            embed.add_field(
                name=header,
                value=_safe_embed_field_value(content, "無詳細資訊"),
                inline=False,
            )
        return

    embed.add_field(
        name=fallback_field_name,
        value=_safe_embed_field_value(ai_report_text, "無詳細資訊"),
        inline=False,
    )


def split_embed_by_fields(
    embed: discord.Embed, max_size: int = 5000
) -> list[discord.Embed]:
    """將多欄位報告拆成多則 Embed，儘量合併欄位以防訊息過於零碎，且確保單則長度不超過 max_size。"""
    if len(embed.fields) <= 1:
        return [embed]

    base_payload = embed.to_dict()
    base_payload.pop("fields", None)

    def _get_base_len(include_desc: bool) -> int:
        total = 0
        title = base_payload.get("title")
        if title:
            total += len(title)
        if include_desc:
            desc = base_payload.get("description")
            if desc:
                total += len(desc)
        footer = base_payload.get("footer")
        if footer and footer.get("text"):
            total += len(footer["text"])
        author = base_payload.get("author")
        if author and author.get("name"):
            total += len(author["name"])
        return total

    base_len_first = _get_base_len(include_desc=True)
    base_len_subsequent = _get_base_len(include_desc=False)

    groups: List[List[Any]] = []
    current_group: List[Any] = []
    current_len = 0

    for field in embed.fields:
        field_len = len(field.name or "") + len(field.value or "")
        is_first = len(groups) == 0
        base_len = base_len_first if is_first else base_len_subsequent

        if current_group and (
            base_len + current_len + field_len > max_size or len(current_group) >= 24
        ):
            groups.append(current_group)
            current_group = [field]
            current_len = field_len
        else:
            current_group.append(field)
            current_len += field_len

    if current_group:
        groups.append(current_group)

    if len(groups) <= 1:
        return [embed]

    split_embeds: list[discord.Embed] = []
    total = len(groups)

    for index, group in enumerate(groups, start=1):
        payload = dict(base_payload)
        title = payload.get("title")
        if title:
            payload["title"] = f"{title} ({index}/{total})"
        if index > 1:
            payload.pop("description", None)

        split_embed = discord.Embed.from_dict(payload)
        for field in group:
            split_embed.add_field(
                name=field.name, value=field.value, inline=field.inline
            )
        split_embeds.append(split_embed)

    return split_embeds


# ============================================================================
# Shared news/reddit field helpers
# ============================================================================


def add_news_field(embed, news_text):
    if news_text:
        if len(news_text) > 1000:
            news_text = news_text[:997] + "..."
        news_context = f"```{news_text}\n\u200b```"
        embed.add_field(name="📰 最新新聞", value=news_context, inline=False)


def add_reddit_field(embed, reddit_text):
    if reddit_text:
        if len(reddit_text) > 1000:
            reddit_text = reddit_text[:997] + "..."
        reddit_context = f"```{reddit_text}\n\u200b```"
        embed.add_field(name="📰 Reddit 討論", value=reddit_context, inline=False)


# ============================================================================
# Positions table formatter (used by portfolio embeds)
# ============================================================================


def _parse_and_format_positions_table(
    positions_list: List[str], survival_runway=None
) -> str:
    if not positions_list:
        return "目前無持倉部位。"

    parsed_blocks = []
    total_debit_cost = 0.0
    total_credit_cash = 0.0
    total_unrealized_pnl = 0.0

    for pos_item in positions_list:
        # Regex parsing
        sym_match = re.search(r"🔹\s*\*\*(.*?)\*\*", pos_item)
        exp_match = re.search(r"`(\d{4}-\d{2}-\d{2})`", pos_item)
        strike_type_match = re.search(r"`?\$([\d\.]+)`?\s*\*\*(.*?)\*\*", pos_item)

        cost_match = re.search(r"成本:\s*`\$?([\d\.,\-]+)`", pos_item)
        price_match = re.search(r"現價:\s*`\$?([\d\.,\-]+)`", pos_item)
        dte_match = re.search(r"DTE:\s*`(.*?)`", pos_item)
        status_match = re.search(r"動作:\s*(.*)", pos_item)

        dir_match = re.search(r"方向:\s*`(.*?)`", pos_item)
        qty_match = re.search(r"數量:\s*`(.*?)`", pos_item)
        iv_match = re.search(r"IV/IVR:\s*`(.*?)`", pos_item)

        symbol = sym_match.group(1).strip() if sym_match else "UNKNOWN"
        expiry = exp_match.group(1).strip() if exp_match else "N/A"
        strike_val = strike_type_match.group(1).strip() if strike_type_match else ""
        opt_type = strike_type_match.group(2).strip() if strike_type_match else ""
        type_letter = (
            "CALL"
            if "CALL" in opt_type.upper()
            else "PUT"
            if "PUT" in opt_type.upper()
            else opt_type
        )

        direction = dir_match.group(1).strip() if dir_match else "BTO"
        qty = abs(float(qty_match.group(1).strip())) if qty_match else 1.0

        cost_val = (
            float(cost_match.group(1).strip().replace(",", "")) if cost_match else 0.0
        )
        price_val = (
            float(price_match.group(1).strip().replace(",", "")) if price_match else 0.0
        )

        if direction == "BTO":
            debit_cost = cost_val * 100 * qty
            total_debit_cost += debit_cost
            pnl_val = (price_val - cost_val) * 100 * qty
        else:
            credit_cash = cost_val * 100 * qty
            total_credit_cash += credit_cash
            pnl_val = (cost_val - price_val) * 100 * qty

        total_unrealized_pnl += pnl_val

        dte_val = dte_match.group(1).strip() if dte_match else "0"
        if "天" in dte_val:
            dte_val = dte_val.replace("天", "").strip()

        qty_str = f"{int(qty)}" if qty.is_integer() else f"{qty}"

        pnl_pct = 0.0
        if cost_val > 0.0:
            if direction == "BTO":
                pnl_pct = (price_val - cost_val) / cost_val
            else:
                pnl_pct = (cost_val - price_val) / cost_val

        iv_ivr_str = iv_match.group(1).strip() if iv_match else "--% / --%"
        status = status_match.group(1).strip() if status_match else "HOLD"
        status = _clean_ansi(status)

        pnl_sign = "+" if pnl_val > 0 else "-" if pnl_val < 0 else ""
        pnl_abs_usd = abs(pnl_val)
        pnl_emoji = "🟢" if pnl_val > 0 else "🚨" if pnl_val < 0 else "⚖️"
        pnl_formatted = (
            f"**{pnl_sign}${pnl_abs_usd:,.2f} ({pnl_pct*100:+.2f}%)** {pnl_emoji}"
        )

        pos_block = (
            f"**當前持倉明細 (標的: {symbol})**\n"
            f"* 部位：`{direction} {type_letter}` | 數量：`{qty_str}` | {expiry} (**{dte_val}d DTE**)\n"
            f"* 價格：履約價 `${strike_val}` | 現價 `${price_val:.2f}` *(成本 ${cost_val:.2f})* | IV/IVR: `{iv_ivr_str}`\n"
            f"* 損益：{pnl_formatted} *{status}*"
        )
        parsed_blocks.append(pos_block)

    positions_part = "\n\n".join(parsed_blocks)

    summary_lines = [
        "財務摘要 (Financial Summary)",
        f"* 衍生品實質現金暴露 (Debit Cost): `${total_debit_cost:,.2f}` USD",
        f"* 造市商已沒收權利金 (Credit Cash): `${total_credit_cash:,.2f}` USD",
    ]
    pnl_sign = (
        "+" if total_unrealized_pnl > 0 else "-" if total_unrealized_pnl < 0 else ""
    )
    pnl_emoji = (
        "🟢" if total_unrealized_pnl > 0 else "🚨" if total_unrealized_pnl < 0 else "⚖️"
    )
    summary_lines.append(
        f"* 盤中實時未實現損益 (Unrealized PnL): **{pnl_sign}${abs(total_unrealized_pnl):,.2f}** USD {pnl_emoji}"
    )

    if survival_runway is not None:
        if survival_runway >= 9999:
            runway_years_str = "無限 年 (鐵血不破)"
        else:
            runway_years_str = f"{float(survival_runway)/365.0:.1f}+ 年 (鐵血不破)"
    else:
        runway_years_str = "4.6+ 年 (鐵血不破)"
    summary_lines.append(
        f"* 全域生存跑道安全係數 (Runway Buffer): `{runway_years_str}`"
    )

    summary_part = "\n".join(summary_lines)

    return f"{positions_part}\n\n{summary_part}"


# ============================================================================
# Shared scan embed field helpers (used by create_scan_embed)
# ============================================================================


def _build_embed_base(data, strategy, stock_cost):
    colors = {
        "STO_PUT": discord.Color.green(),
        "STO_CALL": discord.Color.red(),
        "BTO_CALL": discord.Color.blue(),
        "BTO_PUT": discord.Color.orange(),
    }
    titles = {
        "STO_PUT": "🟢 賣出賣權 (STO Put)",
        "STO_CALL": "🔴 賣出買權 (STO Call)",
        "BTO_CALL": "🚀 買入買權 (BTO Call)",
        "BTO_PUT": "⚠️ 買入賣權 (BTO Put)",
    }

    is_covered = strategy == "STO_CALL" and stock_cost > 0.0
    if is_covered:
        titles["STO_CALL"] = "🛡️ 掩護性買權 (Covered Call)"
        colors["STO_CALL"] = discord.Color.teal()

    embed = discord.Embed(
        title=f"{titles.get(strategy, strategy)} | {data.get('symbol', 'UNKNOWN')}",
        description=f"📅 **到期日:** `{data.get('target_date', 'UNKNOWN')}` ｜ 🎯 **履約價:** `${data.get('strike', 'UNKNOWN')}`\n\u200b",
        color=colors.get(strategy, discord.Color.default()),
    )
    return embed, is_covered


def _add_vix_battle_status_field(embed, data):
    """Add VIX Battle Ladder status indicator (highest priority field)."""
    vix_status = data.get("vix_battle_status") or {}
    vix_spot = vix_status.get("vix_spot") or data.get("vix_spot")
    tier_name = vix_status.get("name") or data.get("vix_tier_name", "N/A")
    tier_emoji = vix_status.get("emoji") or data.get("vix_tier_emoji", "")
    delta_cap = vix_status.get("sto_delta_cap") or data.get("vix_sto_delta_cap", 0.0)
    sizing_mult = vix_status.get("sizing_multiplier") or data.get(
        "vix_sizing_multiplier", 1.0
    )

    if vix_spot is None:
        return

    status_line = f"{tier_emoji} **{tier_name}** | VIX: `{vix_spot:.1f}`"
    details = []
    if delta_cap != 0.0:
        details.append(f"Delta Cap: `{delta_cap:.2f}`")
    if sizing_mult != 1.0:
        details.append(f"\u5009\u4f4d\u4e58\u6578: `{sizing_mult:.1f}x`")

    value = status_line
    if details:
        value += "\n" + " | ".join(details)
    value += "\n\u200b"

    embed.add_field(name="🛡️ VIX 戰情階梯狀態", value=value, inline=False)


def _add_market_overview_fields(embed, data):
    beta = data.get("beta", 1.0)
    beta_status = "🚀" if beta > 1.3 else ("⚖️" if beta >= 0.8 else "🧊")
    embed.add_field(
        name="🏷️ 標價 / Beta\u2800\u2800",
        value=f"${data['price']:.2f} / `{beta:.2f}` {beta_status}\n\u200b",
        inline=True,
    )
    embed.add_field(
        name="📈 RSI / 20MA\u2800\u2800\u2800",
        value=f"{data['rsi']:.2f} / ${data['sma20']:.2f}\n\u200b",
        inline=True,
    )
    hvr_status = (
        "🔥 高"
        if data["hv_rank"] >= 50
        else ("⚡ 中" if data["hv_rank"] >= 30 else "🧊 低")
    )
    embed.add_field(
        name="🔥 HV Rank\u2800\u2800\u2800\u2800",
        value=f"`{data['hv_rank']:.1f}%` {hvr_status}\n\u200b",
        inline=True,
    )


def _add_volatility_fields(embed, data, strategy):
    vrp_pct = data.get("vrp", 0.0) * 100
    vrp_icon = (
        "✅"
        if (("STO" in strategy and vrp_pct > 0) or ("BTO" in strategy and vrp_pct < 0))
        else "⚠️"
    )
    embed.add_field(
        name="⚖️ VRP 溢酬\u2800\u2800\u2800\u2800",
        value=f"`{vrp_pct:+.2f}%` {vrp_icon}\n\u200b",
        inline=True,
    )
    ts_ratio_str = f"`{data['ts_ratio']:.2f}` {data['ts_state']}"
    embed.add_field(
        name="⏳ 個股 IV 期限結構\u2800\u2800",
        value=f"{ts_ratio_str}\n\u200b",
        inline=True,
    )
    v_skew_str = f"`{data['v_skew']:.2f}` {data.get('v_skew_state', '')}"
    embed.add_field(
        name="📉 垂直偏態\u2800\u2800\u2800\u2800",
        value=f"{v_skew_str}\n\u200b",
        inline=True,
    )

    # ---------------- VIX 306 Volatility Metrics ----------------
    vts_str = f"`{data.get('vix_vts_ratio', 1.0):.2f}` {data.get('vix_regime', '')}"
    embed.add_field(
        name="🌐 大盤 VIX 期限結構\u2800", value=f"{vts_str}\n\u200b", inline=True
    )

    z30 = data.get("vix_z30", 0.0)
    z60 = data.get("vix_z60", 0.0)
    z_icon = "📈 擴張" if z30 > 0.5 and z60 > 0 else "📉 收斂"
    embed.add_field(
        name="🔥 VIX 30/60 Z-Score",
        value=f"Z30:`{z30:.1f}` Z60:`{z60:.1f}` {z_icon}\n\u200b",
        inline=True,
    )

    tail_risk = (
        "🚨 觸發降規 (1/4 Kelly)"
        if data.get("is_high_tail_risk", False)
        else "✅ 正常 (1/2 Kelly)"
    )
    embed.add_field(
        name="🛡️ 尾部風險管理\u2800\u2800\u2800",
        value=f"{tail_risk}\n\u200b",
        inline=True,
    )


def _add_performance_and_kelly_fields(embed, data, user_capital):
    """添加績效與風控（含凱利倉位計算）欄位，並校正部位方向"""
    strategy = data.get("strategy", "")
    raw_delta = data.get("delta", 0.0)
    weighted_delta = data.get("weighted_delta", 0.0)

    # 🚀 方向校正邏輯：
    # 若是賣方 (STO)，部位方向 = 合約方向 * -1
    # 若是買方 (BTO)，部位方向 = 合約方向
    pos_multiplier = -1 if "STO" in strategy else 1
    pos_weighted_shares = weighted_delta * pos_multiplier

    embed.add_field(
        name="🧩 Delta (部位加權)\u2800\u2800",
        value=f"{raw_delta:.3f} (`{pos_weighted_shares:+.1f}`股)\n\u200b",
        inline=True,
    )

    embed.add_field(
        name="💰 AROC / IV\u2800\u2800\u2800\u2800",
        value=f"`{data['aroc']:.1f}%` / {data['iv']:.1%}\n\u200b",
        inline=True,
    )

    alloc_pct = data.get("alloc_pct", 0.0)
    suggested = data.get("suggested_contracts", 0)

    if alloc_pct <= 0:
        kelly_value = "`不建議建倉`"
    elif not user_capital or user_capital <= 0:
        kelly_value = f"`未設資金` ({alloc_pct*100:.1f}%)"
    else:
        kelly_value = (
            f"`{suggested} 口` ({min(alloc_pct, 0.25)*100:.1f}%)"
            if suggested > 0
            else "`本金不足`"
        )

    embed.add_field(
        name="🧮 凱利原始建議\u2800\u2800", value=f"{kelly_value}\n\u200b", inline=True
    )


def _add_earnings_fields(embed, data, strategy):
    """添加財報預期波動欄位"""
    if 0 <= data.get("earnings_days", -1) <= 14:
        mmm_str = f"±{data['mmm_pct']:.1f}% (倒數 {data['earnings_days']} 天)"
        bounds_str = f"🛡️ 安全區間: **`${data['safe_lower']:.2f}`** ~ **`${data['safe_upper']:.2f}`**"
        strike = data["strike"]

        if "STO" in strategy:
            is_safe = (strategy == "STO_PUT" and strike <= data["safe_lower"]) or (
                strategy == "STO_CALL" and strike >= data["safe_upper"]
            )
            safety_icon = (
                "✅ 避開雷區 (適宜收租)" if is_safe else "💣 位於雷區 (極高風險)"
            )
        else:
            safety_icon = "🎲 財報盲盒 (注意 IV Crush 波動率壓縮風險)"

        embed.add_field(
            name="📊 財報預期波動 (MMM)",
            value=f"`{mmm_str}`\n{bounds_str}\n{safety_icon}\n\u200b",
            inline=False,
        )


def _add_covered_call_fields(embed, data, stock_cost):
    """添加 Covered Call 專屬防護欄位"""
    bid = data.get("bid", 0)
    true_breakeven = stock_cost - bid
    yoc = (bid / stock_cost) * 100 if stock_cost > 0 else 0

    cc_info = (
        f"📦 **真實現股成本:** `${stock_cost:.2f}`\n"
        f"🛡️ **真實下檔防線:** `${true_breakeven:.2f}`\n"
        f"💸 **單次收租殖利率 (Yield on Cost):** `{yoc:.2f}%`\n"
        f"👉 *您的持倉成本已透過收租進一步降低！*\n\u200b"
    )
    embed.add_field(name="🛡️ Covered Call 專屬防護", value=cc_info, inline=False)


def _add_expected_move_fields(embed, data, strategy, is_covered):
    """添加預期波動區間與損益兩平防線欄位"""
    em = data.get("expected_move", 0.0)
    em_lower = data.get("em_lower", 0.0)
    em_upper = data.get("em_upper", 0.0)

    if "STO_PUT" in strategy:
        breakeven = data["strike"] - data.get("bid", 0)
        safe = breakeven < em_lower
        safety_text = (
            "✅ 防線已建構於預期暴跌區間外"
            if safe
            else "⚠️ 損益兩平點位於預期波動區間內，風險較高"
        )
        em_info = f"1σ 預期下緣: `${em_lower:.2f}` (預期最大跌幅 -${em:.2f})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n{safety_text}\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "STO_CALL" in strategy:
        breakeven = data["strike"] + data.get("bid", 0)
        safe = breakeven > em_upper
        if is_covered:
            safety_text = "✅ 若漲破此價位，將以最高獲利出場 (股票被 Call 走)"
        else:
            safety_text = (
                "✅ 防線已建構於預期暴漲區聯外"
                if safe
                else "⚠️ 損益兩平點位於預期波動區間內，風險較高"
            )

        em_info = f"1σ 預期上緣: `${em_upper:.2f}` (預期最大漲幅 +${em:.2f})\n🛡️ 合約兩平點: **`${breakeven:.2f}`**\n{safety_text}\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "BTO_PUT" in strategy:
        breakeven = data["strike"] - data.get("ask", 0)
        em_info = f"1σ 預期下緣: `${em_lower:.2f}` (預期最大跌幅 -${em:.2f})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n✅ 目標跌破此防線即開始獲利\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "BTO_CALL" in strategy:
        breakeven = data["strike"] + data.get("ask", 0)
        em_info = f"1σ 預期上緣: `${em_upper:.2f}` (預期最大漲幅 +${em:.2f})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n✅ 目標突破此防線即開始獲利\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)


def _add_liquidity_fields(embed, data):
    """添加報價與流動性分析欄位"""
    mid_price = data.get("mid_price", (data.get("bid", 0) + data.get("ask", 0)) / 2)
    liq_status = data.get("liq_status", "N/A")
    liq_msg = data.get("liq_msg", "")

    spread_info = (
        f"**Bid:** `{data.get('bid', 0):.2f}` ｜ **Ask:** `{data.get('ask', 0):.2f}` (價差 `{data.get('spread_ratio', 0):.1f}%`)\n"
        f"**狀態:** {liq_status} {liq_msg}\n"
        f"🎯 **Limit (中價掛單建議):** `{mid_price:.2f}`\n\u200b"
    )
    embed.add_field(name="💱 報價與流動性分析", value=spread_info, inline=False)


def _add_strategy_upgrade_fields(embed, data, strategy):
    """添加策略升級提示欄位"""
    if strategy in ["BTO_CALL", "BTO_PUT"]:
        hedge_strike = data.get("suggested_hedge_strike")
        if hedge_strike:
            spread_type = (
                "多頭價差 (Bull Call Spread)"
                if strategy == "BTO_CALL"
                else "空頭價差 (Bear Put Spread)"
            )
            hedge_type = "Call" if strategy == "BTO_CALL" else "Put"

            upgrade_text = (
                f"為抵銷 Theta (時間價值) 衰減並降低建倉成本，\n"
                f"建議在買入本合約的同時，賣出更價外的 **${hedge_strike:.0f} {hedge_type}**\n"
                f"👉 組合為: **{spread_type}**\n\u200b"
            )
            embed.add_field(
                name="💡 經理人策略升級建議", value=upgrade_text, inline=False
            )


def _add_risk_optimization_fields(embed, data, user_capital=None):
    """
    添加事前曝險模擬與自動風控優化建議
    🚀 強化版：增加閾值動態化與基準價校驗
    """
    projected_pct = data.get("projected_exposure_pct")
    # 若無數據則不顯示 (注意：不要用 if projected_pct == 0)
    if projected_pct is None:
        return

    safe_qty = data.get("safe_qty", 0)
    hedge_spy = data.get("hedge_spy", 0.0)
    suggested = data.get("suggested_contracts", 0)

    RISK_THRESHOLD = data.get("risk_limit", 15.0)

    # 1. 曝險現況區塊
    is_overloaded = abs(projected_pct) > RISK_THRESHOLD

    if is_overloaded:
        sim_status = "🚨 警告：曝險過載"
        sim_block = (
            f"```diff\n"
            f"- 成交後預期總曝險: {projected_pct:+.1f}%\n"
            f"- 超過 {RISK_THRESHOLD}% 宏觀紅線\n"
            f"```"
        )
    else:
        sim_status = "✅ 狀態：風險受控"
        sim_block = (
            f"```yaml\n"
            f"成交後的預期總曝險: {projected_pct:+.1f}%\n"
            f"符合資產組合平衡標準\n"
            f"```"
        )

    embed.add_field(
        name=f"🛡️ What-if 曝險模擬 | {sim_status}",
        value=f"{sim_block}\n\u200b",
        inline=False,
    )

    # 2. Nexus Risk Optimizer 自動優化建議
    if suggested > safe_qty:
        opt_title = "⚖️ Nexus Risk Optimizer (自動優化建議)"

        spy_p = data.get("spy_price", 690.0)

        actions = ["--- 偵測到風險超標，執行自動降規 ---"]
        actions.append(f"❌ 原始建議: {suggested} 口")
        actions.append(f"✅ 安全成交: {safe_qty} 口 (符合風控)")

        if safe_qty == 0 and hedge_spy != 0:
            actions.append("\n⚠️ 警告: 即使下 1 口也過載")
            direction = "賣出" if hedge_spy > 0 else "買入"
            actions.append(
                f"🛡️ 建議對沖: {direction} {abs(hedge_spy):.1f} 股 SPY (@${spy_p:.1f})"
            )

        opt_block = "```diff\n" + "\n".join(actions) + "\n```"
        embed.add_field(name=opt_title, value=f"{opt_block}\n\u200b", inline=False)


def _add_hedge_unlock_fields(embed, data):
    """添加對沖解除建議欄位 (Hedge Unlocking)"""
    unlock = data.get("hedge_unlock")
    if not unlock:
        return

    symbol = data.get("symbol", "N/A")
    suggested_qty = unlock.get("reduce_spy_qty", 0)
    new_delta = unlock.get("new_delta", 0.0)
    reason = unlock.get("reason", "")
    risk_note = unlock.get("risk_note", "")

    unlock_text = (
        f"偵測到 **{symbol}** 強勢突破。目前您的 SPY 對沖正在產生 Hedge Drag。\n\n"
        f"✅ **建議動作：** 買回/平倉 `{suggested_qty}` 股 SPY。\n"
        f"🚀 **預計效應：** 釋放 Beta 動能，預計提升總組合 Delta 至 `{new_delta:+.1f}`。\n"
        f"🛡️ **防禦補償：** {risk_note}\n\u200b"
    )

    embed.add_field(name=f"🔓 對沖優化建議 ({reason})", value=unlock_text, inline=False)


def _add_ai_verification_fields(embed, data):
    """添加 AI 驗證決策欄位"""
    ai_decision = data.get("ai_decision")
    ai_reasoning = data.get("ai_reasoning")
    if ai_decision:
        if ai_decision == "APPROVE":
            ai_title = "🤖 Argo Cortex: ✅ 交易批准 (APPROVE)"
            ai_value = f"```\n{ai_reasoning}\n```"
        elif ai_decision == "VETO":
            ai_title = "🤖 Argo Cortex: ⛔ 否決交易 (VETO 黑天鵝警告)"
            ai_value = f"```diff\n- 警告: {ai_reasoning}\n```"
            embed.color = discord.Color.dark_red()
        elif ai_decision == "SKIP":
            ai_title = "🤖 Argo Cortex: ⚠️ 未啟用 (SKIP)"
            ai_value = f"```\n{ai_reasoning}\n```"
            embed.color = discord.Color.blue()

        embed.add_field(name=ai_title, value=ai_value, inline=False)


def get_ema_signal_ui(ema_signals: List[Dict[str, Any]]) -> str:
    """將 EMA 訊號清單轉化為 Discord 友善的文字流。"""
    if not ema_signals:
        return "⚪ *暫無關鍵 EMA 觸碰訊號*"

    ui_lines = []
    for sig in ema_signals:
        window = sig["window"]
        sig_type = sig["type"]
        direction = sig["direction"]
        dist = sig["distance_pct"]

        if sig_type == "CROSSOVER":
            icon = "🚀" if direction == "BULLISH" else "💀"
            action = "強勢突破" if direction == "BULLISH" else "失守跌破"
        else:  # TEST
            icon = "🛡️" if direction == "SUPPORT" else "🛑"
            action = "回測支撐" if direction == "SUPPORT" else "觸碰壓力"

        line = f"{icon} **EMA {window} {action}** (偏離: `{dist}%`)"
        ui_lines.append(line)

    return "\n".join(ui_lines)


def _add_trend_and_support_fields(embed, data):
    """添加 EMA 狀態圖形化燈號欄位"""
    trend = data.get("trend", "UNKNOWN")
    ema21 = data.get("ema_21", 0.0)
    distance = data.get("distance_from_21", 0.0)

    if trend == "BULLISH_STRONG":
        trend_str = "📈 強勢多頭 (現價 > 8 > 21)"
    elif trend == "BULLISH_CORRECTION":
        trend_str = "📉 多頭回檔 (EMA 8 > 21 ≥ 現價)"
    elif trend == "BEARISH_STRONG":
        trend_str = "🐻 強勢空頭 (現價 < 8 < 21)"
    else:
        trend_str = "⚖️ 趨勢中性"

    if distance > 10.0:
        risk_str = "⚠️ 過度擴張 (乖離率 > 10%)"
    elif distance < -10.0:
        risk_str = "⚠️ 超跌區間 (乖離率 < -10%)"
    else:
        risk_str = "✅ 穩定區間"

    support_str = f"EMA 21 位於 `${ema21:.2f}` (乖離: {distance:+.1f}%)"

    trend_info = f"**目前趨勢:** {trend_str}\n**支撐參考:** {support_str}\n**風險評估:** {risk_str}\n"
    trend_info += get_ema_signal_ui(data.get("ema_signals", []))

    embed.add_field(
        name="🧭 趨勢與支撐 (EMA 8/21)", value=trend_info + "\n\u200b", inline=False
    )


def _add_sentiment_fields(embed, data):
    """添加情緒指標欄位 (used by create_scan_embed)"""
    pcr = data.get("pcr", 0.0)
    pcr_label = "🐂 偏多" if pcr < 0.7 else ("🐻 偏空" if pcr > 1.0 else "⚖️ 中性")
    embed.add_field(
        name="📊 P/C Ratio\u2800\u2800\u2800",
        value=f"`{pcr:.2f}` {pcr_label}\n\u200b",
        inline=True,
    )

    skew = data.get("skew", 0.0)
    skew_label = (
        "🌪️ 下行恐懼" if skew > 5 else ("💫 上行熱情" if skew < -5 else "⚖️ 平衡")
    )
    embed.add_field(
        name="📐 Skew\u2800\u2800\u2800\u2800\u2800\u2800",
        value=f"`{skew:.2f}` {skew_label}\n\u200b",
        inline=True,
    )

    iv_rank = data.get("iv_rank", 0.0)
    ivr_label = (
        "🔥 高 IVR"
        if iv_rank >= 50
        else ("⚡ 中 IVR" if iv_rank >= 30 else "🧊 低 IVR")
    )
    embed.add_field(
        name="📉 IV Rank\u2800\u2800\u2800\u2800",
        value=f"`{iv_rank:.1f}%` {ivr_label}\n\u200b",
        inline=True,
    )
