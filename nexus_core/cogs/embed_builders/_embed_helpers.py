"""Discord Embed 通用工具函式。

收錄跨多種 Embed 類型共用的 helper，包含：
- 欄位值安全截斷
- ANSI 表格分頁
- AI 報告欄位附加
- Embed 按欄位拆分
- 持倉表格渲染
- 共用欄位 helper (_add_vix_battle_status_field, _add_market_overview_fields…)
"""

from typing import Any
import re
import discord
from cogs.embed_builders._core import NexusEmbed

from typing import List, Dict, Optional

from ui import panel_renderer
from cogs.embed_builders._ansi_utils import (
    _clean_ansi,
    _to_float_or_none,
    _truncate_with_boundary,
    fmt_or_dash,
)


# ============================================================================
# Shared Embed Formatting Helpers
# ============================================================================

# UOA 異常大單表格的代理判定揭露（watchlist／sentiment_scan 共用）
UOA_HEURISTIC_DISCLOSURE = (
    "⚠️ SWEEP/BLOCK 為成交量整數手數啟發式代理判定，非真實 order-type 逐筆 tape 數據。"
)
UOA_OI_PREV_DAY_NOTE = "⚠️ OI 為前一交易日收盤未平倉量，非盤中即時數據；比例欄位為當日累積量對此固定值的比值。"
UOA_DELTA_OI_NOTE = "⚠️ ΔOI* 於上游未提供實際未平倉變動時，以「當日成交量 − 前一日 OI」代理推估，非真實 ΔOI。"


def get_sqz_status_display(
    is_squeezing: bool | None,
    momentum: float | None,
    signal_dir: str,
    short: bool = False,
    momentum_color: str | None = None,
) -> tuple[str, str]:
    """Returns a unified (directional_status, ansi_color_code) for Squeeze momentum."""
    if is_squeezing is None:
        is_squeezing = False
    if momentum is None:
        momentum = 0.0

    dir_to_icon = {
        "Long": "🟢",
        "Short": "🔴",
        "Neutral": "⚪",
    }
    dir_icon = dir_to_icon.get(signal_dir, "⚪")
    mom_str = f"{momentum:+.2f}"

    tw_dir = {"Long": "多頭", "Short": "空頭", "Neutral": "中性"}.get(
        signal_dir, "中性"
    )

    if momentum_color == "DarkBlue" and momentum > 0:
        label = "擠壓中" if is_squeezing else "無擠壓"
        text = (
            f"🟢 多頭減速 {mom_str}"
            if short
            else f"🟢 多頭減速 / {label} (SQZ MOM: {mom_str})"
        )
        return text, "\u001b[1;32m"
    if momentum_color == "Golden" and momentum < 0:
        label = "擠壓中" if is_squeezing else "無擠壓"
        text = (
            f"🔴 空頭減速 {mom_str}"
            if short
            else f"🔴 空頭減速 / {label} (SQZ MOM: {mom_str})"
        )
        return text, "\u001b[1;31m"

    if is_squeezing:
        if momentum > 0:
            text = (
                f"🟢 多頭 {mom_str}"
                if short
                else f"🟢 多頭推進 / 蓄力突破 (SQZ MOM: {mom_str})"
            )
            color = "\u001b[1;32m"
        elif momentum < 0:
            text = (
                f"🔴 空頭 {mom_str}"
                if short
                else f"🔴 空頭推進 / 蓄力下殺 (SQZ MOM: {mom_str})"
            )
            color = "\u001b[1;31m"
        else:
            text = (
                f"⚪ 中性 {mom_str}"
                if short
                else f"⚪ 擠壓中 / 方向待確認 (SQZ MOM: {mom_str})"
            )
            color = "\u001b[1;30m"
    else:
        text = (
            f"{dir_icon} {tw_dir} {mom_str}"
            if short
            else f"{dir_icon} {tw_dir} / 無擠壓 (SQZ MOM: {mom_str})"
        )
        color = "\u001b[1;30m"

    return text, color


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

        split_embed = NexusEmbed.from_dict(payload)
        for field in group:
            split_embed.add_field(
                name=field.name, value=field.value, inline=field.inline
            )
        split_embeds.append(split_embed)

    return split_embeds


# ============================================================================
# Shared news/reddit field helpers
# ============================================================================


def add_news_field(embed: Any, news_text: Any):  # type: ignore
    if news_text:
        news_text = _truncate_with_boundary(str(news_text), 1000)
        news_context = f"```{news_text}\n\u200b```"
        embed.add_field(name="📰 最新新聞", value=news_context, inline=False)


def add_reddit_field(embed: Any, reddit_text: Any):  # type: ignore
    if reddit_text:
        reddit_text = _truncate_with_boundary(str(reddit_text), 1000)
        reddit_context = f"```{reddit_text}\n\u200b```"
        embed.add_field(name="📰 Reddit 討論", value=reddit_context, inline=False)


# 「```ansi\n」(8) 起算，加上收尾「```」(3) 與「\n\u200b」(2) 仍落在 Discord
# 單欄 1024 字元上限內。
_ANSI_FIELD_BODY_LIMIT = 1018

# 續欄的欄位名：零寬字元讓續段看起來是同一章節的延續，而不是另起一個「(續 N)」。
ANSI_CONTINUATION_FIELD_NAME = "\u200b"


def _add_ansi_field_safely(embed: Any, name: str, lines: list) -> None:
    """將包含 ANSI 碼的字串陣列安全地加入 embed，超過單欄上限時自動切分為多個 Field。

    以空行分隔的段落（樹狀區塊、表格與註腳）為裝箱單位：放不下就整段移到下一欄，
    不從段落中間截斷；只有單一段落本身就超過上限時才退回逐行切。續欄沿用
    `ANSI_CONTINUATION_FIELD_NAME`，不印「(續 N)」。
    """
    paragraphs: list[list[str]] = [[]]
    for line in lines:
        if line == "```ansi" or line == "```":
            continue
        if line == "":
            if paragraphs[-1]:
                paragraphs.append([])
            continue
        paragraphs[-1].append(line)
    paragraphs = [p for p in paragraphs if p]

    chunks: list[list[str]] = []
    current: list[str] = []
    current_length = 8  # len("```ansi\n")

    for para in paragraphs:
        # 欄內非首段時，段落前補回一個空行分隔
        sep_len = 1 if current else 0
        para_len = sum(len(line) + 1 for line in para)
        if current_length + sep_len + para_len <= _ANSI_FIELD_BODY_LIMIT:
            if current:
                current.append("")
            current.extend(para)
            current_length += sep_len + para_len
            continue
        if current:
            chunks.append(current)
        current = []
        current_length = 8
        # 單段超長才逐行切
        for line in para:
            line_len = len(line) + 1
            if current and current_length + line_len > _ANSI_FIELD_BODY_LIMIT:
                chunks.append(current)
                current = []
                current_length = 8
            current.append(line)
            current_length += line_len
    if current:
        chunks.append(current)

    for idx, chunk in enumerate(chunks):
        embed.add_field(
            name=name if idx == 0 else ANSI_CONTINUATION_FIELD_NAME,
            value="```ansi\n" + "\n".join(chunk) + "\n```\n\u200b",
            inline=False,
        )


_ANSI_W = "\u001b[1;37m"
_ANSI_R = "\u001b[1;31m"
_ANSI_G = "\u001b[1;32m"
_ANSI_0 = "\u001b[0m"


def _ansi_tree_lines(rows: list, *, indent: str = " ") -> list:
    """將已著色的資料列加上 ` ├─ ` / ` └─ ` 樹狀前綴（最後一列用 └─）。"""
    last = len(rows) - 1
    return [
        f"{indent}{'└─' if i == last else '├─'} {row}" for i, row in enumerate(rows)
    ]


def _fmt_gex_notional(raw: float) -> str:
    """GEX 原始值（OI×100×Γ×S²，每 100% 變動）→ 每 1% 變動避險名目，自動 $B/$M/$K。

    docs/microstructure/01：原始值乘 0.01 才是每 1% 價格變動的做市商避險名目。
    """
    v = raw * 0.01
    sign = "+" if v > 0 else ("-" if v < 0 else " ")
    a = abs(v)
    if a >= 1e9:
        body = f"${a / 1e9:.2f}B"
    elif a >= 1e6:
        body = f"${a / 1e6:.1f}M"
    elif a >= 1e3:
        body = f"${a / 1e3:.0f}K"
    else:
        body = f"${a:.0f}"
    return f"{sign}{body}"


def gamma_flip_noise_note(materiality: Any, filtered_flip: float) -> str:
    """閘門 Gamma Flip 屬雜訊交叉時的揭露文字；否則回傳空字串（docs/microstructure/03 §5.6）。

    `materiality` 為 `gamma_flip_materiality()` 的輸出（可為 None）；`filtered_flip`
    為 `estimate_material_gamma_flip(..., GAMMA_FLIP_MATERIALITY_DISPLAY)`。閘門仍以
    原 Flip 為準，這裡只揭露。
    """
    from market_analysis.index_microstructure import GAMMA_FLIP_MATERIALITY_DISPLAY

    if materiality is None or materiality.ratio >= GAMMA_FLIP_MATERIALITY_DISPLAY:
        return ""
    after = f"${filtered_flip:.2f}" if filtered_flip > 0 else "無"
    return (
        f"⚠️ 翻轉檔負值僅 {_fmt_gex_notional(-materiality.neg_peak)}"
        f"（視窗最大 |GEX| 的 {materiality.ratio * 100:.1f}%），屬雜訊交叉；"
        f"排除後 Flip: {after}（閘門暫用原值，待校準：microstructure/03 §5.6）"
    )


# ============================================================================
# 提領跑道摘要（docs/risk_portfolio/05；唯一格式來源，各報表共用）
# ============================================================================


def _years_text(years: float, capped: bool = False) -> str:
    return "≥ 10 年" if capped else f"{years:.1f} 年"


def format_runway_lines(runway: Any, *, stale: bool = False) -> List[str]:
    """提領跑道快照 → 純文字行（無 ANSI／Markdown）。`runway` 為 None 表示尚無快照。"""
    if runway is None:
        return ["提領跑道：尚無資料（未設定提領，或尚未經過收盤計算）"]
    worst_is_gfc = runway.gfc_years <= runway.dotcom_years
    lines = [
        f"壓力跑道 {_years_text(runway.stress_years, runway.capped)}"
        f"（2008：{_years_text(runway.gfc_years, runway.gfc_years >= 10.0)}"
        f"／2000：{_years_text(runway.dotcom_years, runway.dotcom_years >= 10.0)}"
        f"，較差者為{'2008' if worst_is_gfc else '2000'}）",
        f"零報酬對照 {runway.zero_years:.1f} 年｜BOXX 可支付 {runway.boxx_payments} 次提領"
        f"｜下次提領 ${runway.next_withdrawal:,.0f}"
        + (f"（{runway.next_date}）" if runway.next_date else ""),
    ]
    notes: List[str] = []
    if runway.beta_is_fallback:
        notes.append(f"Beta 無法計算，採保守預設 {runway.beta:.1f}")
    if runway.cpi_missing:
        notes.append("未含通膨調整")
    if stale:
        notes.append(f"資料過期（NAV 快照日 {runway.nav_date}）")
    if notes:
        lines.append("註：" + "；".join(notes))
    lines.append("※ 歷史崩跌從今天重演的條件式數字，非預測")
    return lines


# ============================================================================
# Positions table formatter (used by portfolio embeds)
# ============================================================================


def _parse_and_format_positions_table(
    positions_list: List[str], runway_lines: Optional[List[str]] = None
) -> str:
    if not positions_list:
        return "目前無持倉部位。"

    parsed_blocks = []
    total_debit_cost = 0.0
    total_credit_cash = 0.0
    total_unrealized_pnl = 0.0
    missing_price_count = 0

    for pos_item in positions_list:
        # Regex parsing
        sym_match = re.search(r"🔹\s*\*\*(.*?)\*\*", pos_item)
        exp_match = re.search(r"`(\d{4}-\d{2}-\d{2}|PERPETUAL|現貨|N/A)`", pos_item)
        strike_type_match = re.search(r"`?\$([\d\.,]+)`?\s*\*\*(.*?)\*\*", pos_item)

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
            else "STOCK"
            if "STOCK" in opt_type.upper()
            else opt_type
        )

        direction = dir_match.group(1).strip() if dir_match else "BTO"
        qty = abs(float(qty_match.group(1).strip())) if qty_match else 1.0

        cost_val = (
            float(cost_match.group(1).strip().replace(",", "")) if cost_match else 0.0
        )
        # 缺現價時不得以 0 計算損益（會把部位顯示成 -100%）：price_val 為 None
        price_val: Optional[float] = (
            _to_float_or_none(price_match.group(1).strip().replace(",", ""))
            if price_match
            else None
        )

        is_stock = "STOCK" in opt_type.upper() or type_letter.upper() == "STOCK"
        multiplier = 1.0 if is_stock else 100.0

        if direction == "BTO":
            debit_cost = cost_val * multiplier * qty
            total_debit_cost += debit_cost
            pnl_val = (
                (price_val - cost_val) * multiplier * qty
                if price_val is not None
                else None
            )
        else:
            credit_cash = cost_val * multiplier * qty
            total_credit_cash += credit_cash
            pnl_val = (
                (cost_val - price_val) * multiplier * qty
                if price_val is not None
                else None
            )

        if pnl_val is not None:
            total_unrealized_pnl += pnl_val
        else:
            missing_price_count += 1

        dte_val = dte_match.group(1).strip() if dte_match else "0"
        if "天" in dte_val:
            dte_val = dte_val.replace("天", "").strip()

        qty_str = f"{int(qty)}" if qty.is_integer() else f"{qty}"

        pnl_pct = 0.0
        if cost_val > 0.0 and price_val is not None:
            if direction == "BTO":
                pnl_pct = (price_val - cost_val) / cost_val
            else:
                pnl_pct = (cost_val - price_val) / cost_val

        iv_ivr_str = iv_match.group(1).strip() if iv_match else "--% / --%"
        status = status_match.group(1).strip() if status_match else "HOLD"
        status = _clean_ansi(status)

        if pnl_val is None:
            pnl_formatted = "**-- (--%)** ⚪"
        else:
            pnl_sign = "+" if pnl_val > 0 else "-" if pnl_val < 0 else ""
            pnl_abs_usd = abs(pnl_val)
            pnl_emoji = "🟢" if pnl_val > 0 else "🚨" if pnl_val < 0 else "⚖️"
            pnl_formatted = (
                f"**{pnl_sign}${pnl_abs_usd:,.2f} ({pnl_pct*100:+.2f}%)** {pnl_emoji}"
            )
        price_txt = fmt_or_dash(price_val, ".2f", prefix="$")

        if is_stock:
            pos_block = (
                f"**當前持倉明細 (標的: {symbol})**\n"
                f"* 部位：`現貨 (HOLDING)` | 數量：`{qty_str} 股` | 永久持有 (PERPETUAL)\n"
                f"* 價格：買入成本 `${cost_val:.2f}` | 現價 `{price_txt}` | IV/IVR: `{iv_ivr_str}`\n"
                f"* 損益：{pnl_formatted} *{status}*"
            )
        else:
            pos_block = (
                f"**當前持倉明細 (標的: {symbol})**\n"
                f"* 部位：`{direction} {type_letter}` | 數量：`{qty_str}` | {expiry} (**{dte_val}d DTE**)\n"
                f"* 價格：履約價 `${strike_val}` | 現價 `{price_txt}` *(成本 ${cost_val:.2f})* | IV/IVR: `{iv_ivr_str}`\n"
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
    if missing_price_count:
        summary_lines.append(f"* ⚠️ {missing_price_count} 筆缺現價，未計入上述損益")

    if runway_lines:
        summary_lines.append("* 🏁 提領跑道 (Withdrawal Runway):")
        summary_lines.extend(f"  - {ln}" for ln in runway_lines)

    summary_part = "\n".join(summary_lines)

    return f"{positions_part}\n\n{summary_part}"


# ============================================================================
# Shared scan embed field helpers (used by create_scan_embed)
# ============================================================================


def _build_embed_base(data: Any, strategy: Any, stock_cost: Any):  # type: ignore
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

    embed = NexusEmbed(
        title=f"{titles.get(strategy, strategy)} | {data.get('symbol', 'UNKNOWN')}",
        description=f"📅 **到期日:** `{data.get('target_date', 'UNKNOWN')}` ｜ 🎯 **履約價:** `${data.get('strike', 'UNKNOWN')}`\n\u200b",
        color=colors.get(strategy, discord.Color.default()),
    )
    return embed, is_covered


def _add_vix_battle_status_field(embed: Any, data: Any):  # type: ignore
    """Add VIX Battle Ladder status indicator (highest priority field)."""
    vix_status = data.get("vix_battle_status") or {}
    vix_spot = vix_status.get("vix_spot") or data.get("vix_spot")
    tier_name = vix_status.get("name") or data.get("vix_tier_name", "N/A")
    tier_emoji = vix_status.get("emoji") or data.get("vix_tier_emoji", "")
    delta_cap = vix_status.get("sto_delta_cap") or data.get("vix_sto_delta_cap", 0.0)
    # 不可用 `or`：0.0 乘數 (休兵賣方 / 極端區做空) 是有效值，`or` 會把它當成
    # 缺值而落到 1.0，使「禁止開倉」顯示成「正常倉位」。
    sizing_mult = vix_status.get("sizing_multiplier")
    if sizing_mult is None:
        sizing_mult = data.get("vix_sizing_multiplier", 1.0)
    trade_intent = vix_status.get("trade_intent") or data.get("trade_intent")
    sizing_label = "做空倉位乘數" if trade_intent == "DIRECTIONAL_SHORT" else "倉位乘數"

    if vix_spot is None:
        # VIX 未知：明確標示，而非靜默略過 (使用者看不到就會以為一切照常)。
        if "vix_battle_status" in data or "vix_spot" in data:
            embed.add_field(
                name="🛡️ VIX 戰情階梯狀態",
                value="⚪ **VIX 資料不足** | 倉位不放大、賣方新倉暫停\n\u200b",
                inline=False,
            )
        return

    status_line = f"{tier_emoji} **{tier_name}** | VIX: `{vix_spot:.1f}`"
    details = []
    if delta_cap != 0.0:
        details.append(f"Delta Cap: `{delta_cap:.2f}`")
    if sizing_mult != 1.0:
        details.append(f"{sizing_label}: `{sizing_mult:.2f}x`")

    value = status_line
    if details:
        value += "\n" + " | ".join(details)
    value += "\n\u200b"

    embed.add_field(name="🛡️ VIX 戰情階梯狀態", value=value, inline=False)


def _add_market_overview_fields(embed: Any, data: Any):  # type: ignore
    beta = data.get("beta", 1.0)
    beta_status = "🚀" if beta > 1.3 else ("⚖️" if beta >= 0.8 else "🧊")
    if data.get("beta_estimated"):
        beta_status = "⚠️ 資料不足，暫以 1.0 估算"
    hvr_status = (
        "🔥 高"
        if data["hv_rank"] >= 50
        else ("⚡ 中" if data["hv_rank"] >= 30 else "🧊 低")
    )
    rows = [
        f"標價 / Beta   : {_ANSI_W}${data['price']:.2f} / {beta:.2f}{_ANSI_0} {beta_status}",
        f"RSI / 20MA    : {_ANSI_W}{data['rsi']:.2f} / ${data['sma20']:.2f}{_ANSI_0}",
        f"HV Rank       : {_ANSI_W}{data['hv_rank']:.1f}%{_ANSI_0} {hvr_status}",
    ]
    _add_ansi_field_safely(embed, "📊 市場概覽", _ansi_tree_lines(rows))


def _add_volatility_fields(embed: Any, data: Any, strategy: Any):  # type: ignore
    vrp_raw = _to_float_or_none(data.get("vrp"))
    if vrp_raw is None:
        # 缺值不做優劣判定（不以 0 冒充中性）
        vrp_text = f"{_ANSI_W}--%{_ANSI_0}"
    else:
        vrp_pct = vrp_raw * 100
        vrp_icon = (
            "✅"
            if (
                ("STO" in strategy and vrp_pct > 0)
                or ("BTO" in strategy and vrp_pct < 0)
            )
            else "⚠️"
        )
        vrp_text = f"{_ANSI_W}{vrp_pct:+.2f}%{_ANSI_0} {vrp_icon}"
    ts_ratio_str = f"{_ANSI_W}{data['ts_ratio']:.2f}{_ANSI_0} {data['ts_state']}"
    v_skew_str = (
        f"{_ANSI_W}{data['v_skew']:.2f}{_ANSI_0} {data.get('v_skew_state', '')}"
    )

    # ---------------- VIX 306 Volatility Metrics ----------------
    vts_ratio = _to_float_or_none(data.get("vix_vts_ratio"))
    if vts_ratio is None:
        vts_str = f"{_ANSI_W}--{_ANSI_0}"
    else:
        vts_str = f"{_ANSI_W}{vts_ratio:.2f}{_ANSI_0} {data.get('vix_regime', '')}"

    z30 = _to_float_or_none(data.get("vix_z30"))
    z60 = _to_float_or_none(data.get("vix_z60"))
    if z30 is None or z60 is None:
        z_icon = ""
    else:
        z_icon = "📈 擴張" if z30 > 0.5 and z60 > 0 else "📉 收斂"
    z_str = (
        f"Z30:{_ANSI_W}{fmt_or_dash(z30, '.1f')}{_ANSI_0} "
        f"Z60:{_ANSI_W}{fmt_or_dash(z60, '.1f')}{_ANSI_0} {z_icon}"
    ).rstrip()

    tail_risk = (
        f"{_ANSI_R}🚨 觸發降規 (1/4 Kelly){_ANSI_0}"
        if data.get("is_high_tail_risk", False)
        else f"{_ANSI_G}✅ 正常 (1/2 Kelly){_ANSI_0}"
    )
    rows = [
        f"VRP 溢酬         : {vrp_text}",
        f"個股 IV 期限結構 : {ts_ratio_str}",
        f"垂直偏態         : {v_skew_str}",
        f"大盤 VIX 期限結構: {vts_str}",
        f"VIX 30/60 Z-Score: {z_str}",
        f"尾部風險管理     : {tail_risk}",
    ]
    _add_ansi_field_safely(embed, "🌪️ 波動率結構", _ansi_tree_lines(rows))


def _add_performance_and_kelly_fields(embed: Any, data: Any, user_capital: Any):  # type: ignore
    """添加績效與風控（含凱利倉位計算）欄位，並校正部位方向"""
    strategy = data.get("strategy", "")
    raw_delta = data.get("delta", 0.0)
    weighted_delta = data.get("weighted_delta", 0.0)

    # 🚀 方向校正邏輯：
    # 若是賣方 (STO)，部位方向 = 合約方向 * -1
    # 若是買方 (BTO)，部位方向 = 合約方向
    from market_analysis.risk_engine import position_delta_sign

    # 與 risk_engine 的 NRO 倉位模型共用同一個方向乘數，避免呈現層與計算層
    # 對同一筆交易給出相反的方向。買進 Put 的合約 Delta 已為負，乘數為 +1。
    pos_multiplier = position_delta_sign(strategy)
    pos_weighted_shares = weighted_delta * pos_multiplier

    alloc_pct = data.get("alloc_pct", 0.0)
    suggested = data.get("suggested_contracts", 0)

    if alloc_pct <= 0:
        kelly_value = "不建議建倉"
    elif not user_capital or user_capital <= 0:
        kelly_value = f"未設資金 ({alloc_pct*100:.1f}%)"
    else:
        kelly_value = (
            f"{suggested} 口 ({min(alloc_pct, 0.25)*100:.1f}%)"
            if suggested > 0
            else "本金不足"
        )

    rows = [
        f"Delta (部位加權): {_ANSI_W}{raw_delta:.3f}{_ANSI_0} ({_ANSI_W}{pos_weighted_shares:+.1f}{_ANSI_0}股)",
        f"AROC / IV       : {_ANSI_W}{data['aroc']:.1f}%{_ANSI_0} / {data['iv']:.1%}",
        f"凱利原始建議    : {_ANSI_W}{kelly_value}{_ANSI_0}",
    ]
    _add_ansi_field_safely(embed, "🧩 部位與凱利", _ansi_tree_lines(rows))


def _add_earnings_fields(embed: Any, data: Any, strategy: Any):  # type: ignore
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


def _add_covered_call_fields(embed: Any, data: Any, stock_cost: Any):  # type: ignore
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


def _add_expected_move_fields(embed: Any, data: Any, strategy: Any, is_covered: Any):  # type: ignore
    """添加預期波動區間與損益兩平防線欄位"""
    em = _to_float_or_none(data.get("expected_move"))
    em_lower = _to_float_or_none(data.get("em_lower"))
    em_upper = _to_float_or_none(data.get("em_upper"))
    # 預期波動缺失或為 0 時，區間無意義：顯示 `--` 並跳過防線判定
    # （過去以 0 區間得出「防線已建構於區間外」的錯誤結論）。
    em_ok = em is not None and em > 0
    em_dn = fmt_or_dash(em, ",.2f", prefix="-$") if em_ok else "--"
    em_up = fmt_or_dash(em, ",.2f", prefix="+$") if em_ok else "--"
    no_judge = "⚪ 預期波動資料不足，無法判定防線"

    if "STO_PUT" in strategy:
        breakeven = data["strike"] - data.get("bid", 0)
        if em_ok and em_lower is not None:
            safe = breakeven < em_lower
            safety_text = (
                "✅ 防線已建構於預期暴跌區間外"
                if safe
                else "⚠️ 損益兩平點位於預期波動區間內，風險較高"
            )
        else:
            safety_text = no_judge
        lower_txt = fmt_or_dash(em_lower if em_ok else None, ".2f", prefix="$")
        em_info = f"1σ 預期下緣: `{lower_txt}` (預期最大跌幅 {em_dn})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n{safety_text}\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "STO_CALL" in strategy:
        breakeven = data["strike"] + data.get("bid", 0)
        if is_covered:
            safety_text = "✅ 若漲破此價位，將以最高獲利出場 (股票被 Call 走)"
        elif em_ok and em_upper is not None:
            safe = breakeven > em_upper
            safety_text = (
                "✅ 防線已建構於預期暴漲區聯外"
                if safe
                else "⚠️ 損益兩平點位於預期波動區間內，風險較高"
            )
        else:
            safety_text = no_judge

        upper_txt = fmt_or_dash(em_upper if em_ok else None, ".2f", prefix="$")
        em_info = f"1σ 預期上緣: `{upper_txt}` (預期最大漲幅 {em_up})\n🛡️ 合約兩平點: **`${breakeven:.2f}`**\n{safety_text}\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "BTO_PUT" in strategy:
        breakeven = data["strike"] - data.get("ask", 0)
        lower_txt = fmt_or_dash(em_lower if em_ok else None, ".2f", prefix="$")
        em_info = f"1σ 預期下緣: `{lower_txt}` (預期最大跌幅 {em_dn})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n✅ 目標跌破此防線即開始獲利\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)

    elif "BTO_CALL" in strategy:
        breakeven = data["strike"] + data.get("ask", 0)
        upper_txt = fmt_or_dash(em_upper if em_ok else None, ".2f", prefix="$")
        em_info = f"1σ 預期上緣: `{upper_txt}` (預期最大漲幅 {em_up})\n🛡️ 損益兩平點: **`${breakeven:.2f}`**\n✅ 目標突破此防線即開始獲利\n\u200b"
        embed.add_field(name="🎯 機率圓錐 (1σ 預期波動)", value=em_info, inline=False)


def _add_liquidity_fields(embed: Any, data: Any):  # type: ignore
    """添加報價與流動性分析欄位"""
    bid_v = _to_float_or_none(data.get("bid"))
    ask_v = _to_float_or_none(data.get("ask"))
    mid_v = _to_float_or_none(data.get("mid_price"))
    if mid_v is None and bid_v is not None and ask_v is not None:
        mid_v = (bid_v + ask_v) / 2
    liq_status = data.get("liq_status", "N/A")
    liq_msg = data.get("liq_msg", "")

    spread_info = (
        f"**Bid:** `{fmt_or_dash(bid_v, '.2f')}` ｜ **Ask:** `{fmt_or_dash(ask_v, '.2f')}` (價差 `{fmt_or_dash(data.get('spread_ratio'), '.1f', suffix='%')}`)\n"
        f"**狀態:** {liq_status} {liq_msg}\n"
        f"🎯 **Limit (中價掛單建議):** `{fmt_or_dash(mid_v, '.2f')}`\n\u200b"
    )
    embed.add_field(name="💱 報價與流動性分析", value=spread_info, inline=False)


def _add_strategy_upgrade_fields(embed: Any, data: Any, strategy: Any):  # type: ignore
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


def _add_risk_optimization_fields(embed: Any, data: Any, user_capital: Any = None):  # type: ignore
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
        sim_lines = [
            f"{_ANSI_R}成交後預期總曝險: {projected_pct:+.1f}%{_ANSI_0}",
            f"{_ANSI_R}超過 {RISK_THRESHOLD}% 宏觀紅線{_ANSI_0}",
        ]
    else:
        sim_status = "✅ 狀態：風險受控"
        sim_lines = [
            f"{_ANSI_G}成交後的預期總曝險: {projected_pct:+.1f}%{_ANSI_0}",
            f"{_ANSI_G}符合資產組合平衡標準{_ANSI_0}",
        ]

    _add_ansi_field_safely(
        embed,
        f"🛡️ What-if 曝險模擬 | {sim_status}",
        _ansi_tree_lines(sim_lines),
    )

    # NRO 風控警示 (含 VIX/SPY 資料不足的 fail-closed 說明)
    nro_warnings = data.get("nro_warnings") or []
    if nro_warnings:
        embed.add_field(
            name="⚠️ 風控警示",
            value="\n".join(f"• {w}" for w in nro_warnings)[:1000] + "\n\u200b",
            inline=False,
        )

    # 2. Nexus Risk Optimizer 自動優化建議
    if suggested > safe_qty:
        opt_title = "⚖️ Nexus Risk Optimizer (自動優化建議)"

        spy_p = data.get("spy_price")

        actions = [
            f"{_ANSI_R}❌ 原始建議: {suggested} 口{_ANSI_0}",
            f"{_ANSI_G}✅ 安全成交: {safe_qty} 口 (符合風控){_ANSI_0}",
        ]

        if safe_qty == 0 and hedge_spy != 0:
            actions.append(f"{_ANSI_R}⚠️ 警告: 即使下 1 口也過載{_ANSI_0}")
            direction = "賣出" if hedge_spy > 0 else "買入"
            spy_label = f"@${float(spy_p):.1f}" if spy_p else "SPY 現價 --"
            actions.append(
                f"🛡️ 建議對沖: {direction} {abs(hedge_spy):.1f} 股 SPY ({spy_label})"
            )

        opt_lines = [" 偵測到風險超標，執行自動降規"] + _ansi_tree_lines(actions)
        _add_ansi_field_safely(embed, opt_title, opt_lines)


def _add_hedge_unlock_fields(embed: Any, data: Any):  # type: ignore
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


def _add_ai_verification_fields(embed: Any, data: Any):  # type: ignore
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


def get_ema_signal_ui(ema_signals: List[Dict[str, Any]], *, ansi: bool = False) -> Any:
    """將 EMA 訊號清單轉化為 Discord 友善的文字流。

    預設回傳 markdown 字串（/x 面板沿用）；`ansi=True` 回傳 ansi 樹狀行清單
    （交給 `_add_ansi_field_safely`），空清單時回傳單行提示。
    """
    if not ema_signals:
        if ansi:
            return _ansi_tree_lines(["⚪ 暫無關鍵 EMA 觸碰訊號"])
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

        if ansi:
            ui_lines.append(
                f"{icon} {_ANSI_W}EMA {window} {action}{_ANSI_0} (偏離: {_ANSI_W}{dist}%{_ANSI_0})"
            )
        else:
            ui_lines.append(f"{icon} **EMA {window} {action}** (偏離: `{dist}%`)")

    if ansi:
        return _ansi_tree_lines(ui_lines)
    return "\n".join(ui_lines)


def _add_trend_and_support_fields(embed: Any, data: Any):  # type: ignore
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


def _add_sentiment_fields(embed: Any, data: Any):  # type: ignore
    """添加情緒指標欄位 (used by create_scan_embed)"""
    pcr = data.get("pcr")
    if pcr is None:
        # PCR 缺值：顯示 `--`，不以 0.8/0.0 冒充
        pcr_value = f"{_ANSI_W}--{_ANSI_0} 資料不足"
    else:
        pcr_label = "🐂 偏多" if pcr < 0.7 else ("🐻 偏空" if pcr > 1.0 else "⚖️ 中性")
        pcr_value = f"{_ANSI_W}{pcr:.2f}{_ANSI_0} {pcr_label}"

    skew = _to_float_or_none(data.get("skew"))
    if skew is None:
        # Skew 缺值：顯示 `--`，不以 0 冒充「平衡」
        skew_value = f"{_ANSI_W}--{_ANSI_0} 資料不足"
    else:
        skew_label = (
            "🌪️ 下行恐懼" if skew > 5 else ("💫 上行熱情" if skew < -5 else "⚖️ 平衡")
        )
        skew_value = f"{_ANSI_W}{skew:.2f}{_ANSI_0} {skew_label}"

    iv_rank_raw = data.get("iv_rank")
    # 歷史 IV 樣本不足時 iv_rank 為 None：顯示「未知」，不可當成 0% 低 IVR
    if isinstance(iv_rank_raw, (int, float)):
        iv_rank = float(iv_rank_raw)
        ivr_label = (
            "🔥 高 IVR"
            if iv_rank >= 50
            else ("⚡ 中 IVR" if iv_rank >= 30 else "🧊 低 IVR")
        )
        ivr_value = f"{_ANSI_W}{iv_rank:.1f}%{_ANSI_0} {ivr_label}"
    else:
        ivr_value = f"{_ANSI_W}--%{_ANSI_0} ⏳ 樣本積累中"
    rows = [
        f"P/C Ratio : {pcr_value}",
        f"Skew      : {skew_value}",
        f"IV Rank   : {ivr_value}",
    ]
    _add_ansi_field_safely(embed, "🧭 情緒指標", _ansi_tree_lines(rows))


def get_embed_length(embed: discord.Embed) -> int:
    """計算 Embed 的總字元數，用以評估是否超出 Discord 的 6000 字元限制。"""
    total = 0
    if embed.title:
        total += len(embed.title)
    if embed.description:
        total += len(embed.description)
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    if embed.author and embed.author.name:
        total += len(embed.author.name)
    for field in embed.fields:
        if field.name:
            total += len(field.name)
        if field.value:
            total += len(field.value)
    return total


def chunk_embeds(
    embeds: list[discord.Embed], max_size: int = 5500, max_count: int = 10
) -> list[list[discord.Embed]]:
    """
    將一組 Embed 依照總字元長度限制與最大數量限制，切分成多個符合 Discord 限制的子列表（Chunks）。
    每組子列表的累計字元數不超過 max_size，且 Embed 數量不超過 max_count。
    """
    chunks: list[list[discord.Embed]] = []
    current_chunk: list[discord.Embed] = []
    current_size = 0

    for embed in embeds:
        embed_len = get_embed_length(embed)
        if current_chunk and (
            current_size + embed_len > max_size or len(current_chunk) >= max_count
        ):
            chunks.append(current_chunk)
            current_chunk = [embed]
            current_size = embed_len
        else:
            current_chunk.append(embed)
            current_size += embed_len

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def fedwatch_source_note(source: Any) -> str:
    """依 FedWatch 明細的 `source` 欄位產生資料源說明（edge 端 Primary 為 Atlanta
    Fed MPT，失敗時改用 CBOT ZQ 期貨反推；兩者皆失敗為備援估算）。"""
    src = str(source or "")
    if "Atlanta Fed" in src:
        return (
            "⚠️ FedWatch 資料源: Atlanta Fed 市場機率追蹤 (MPT)，"
            "方法論與 CME 官網 FedWatch 工具不同，數字可能存在落差。"
        )
    if "ZQ" in src:
        return (
            "⚠️ FedWatch 資料源: CBOT 30 天期聯邦基金期貨 (ZQ) 反推，"
            "起始利率取自期貨隱含均價而非實際 EFFR，與 CME 官網 FedWatch 可能有落差。"
        )
    return "⚠️ FedWatch 資料源: 即時來源無法取得，以下為備援估算值。"


_PSQ_TF_ORDER = ("W", "3D", "D", "65m", "15m", "5m")
_SQZ_SHORT = {
    "High": "🔴高",
    "Mid": "🟠中",
    "Normal": "🟡一般",
    "Release": "⚪解除",
}
_MOM_SHORT = {
    "LightBlue": "▲淺藍",
    "DarkBlue": "▽深藍",
    "Red": "▼紅",
    "Golden": "△金",
    "Neutral": "·",
}


def format_psq_matrix_lines(matrix: Any) -> list[str]:
    """多時間框架擠壓矩陣（皆為已收盤 K 棒），供 /x 擠壓欄位使用。

    以 getattr 讀取 `TimeframeState`；缺框架顯示「資料不足」。matrix 為 None 或
    空時回傳空清單（呼叫端據此略過整段）。
    """
    if not matrix:
        return []
    lines = [" 多時間框架 (已收盤 K 棒)"]
    last = len(_PSQ_TF_ORDER) - 1
    for i, tf in enumerate(_PSQ_TF_ORDER):
        prefix = " └─" if i == last else " ├─"
        st = matrix.get(tf) if hasattr(matrix, "get") else None
        if st is None:
            lines.append(f"{prefix} {tf:<3} │ —（資料不足）")
            continue
        sqz = _SQZ_SHORT.get(str(getattr(st, "squeeze_level", "")), "—")
        color = str(getattr(st, "momentum_color", "Neutral"))
        mom = _MOM_SHORT.get(color, color)
        try:
            mv = f"{float(getattr(st, 'momentum_value', 0.0)):+.2f}"
        except (TypeError, ValueError):
            mv = "--"
        ago = getattr(st, "green_dot_bars_ago", None)
        if getattr(st, "green_dot", False) and ago is not None:
            flag = f"GD {ago} 根前"
        elif getattr(st, "turbo", False):
            flag = "Turbo"
        else:
            flag = "—"
        ts = str(getattr(st, "bar_ts", "") or "")
        # 日內框架只顯示時分，日／3D／週顯示月日（bar_ts 形如 "2026-10-07 11:00" 或 "2026-10-07"）
        if tf in ("65m", "15m", "5m") and " " in ts:
            ts_short = ts.split(" ")[-1][:5]
        else:
            ts_short = ts[5:10] if len(ts) >= 10 else ts
        row = f"{prefix} {tf:<3} │ {sqz} │ {mom} {mv} │ {flag} │ {ts_short}"
        prev = getattr(st, "preview_squeeze_level", None)
        # 成型中那一根與已收盤等級相同時不重複顯示（省字數，避免 embed 超過 6000）
        if prev and str(prev) != str(getattr(st, "squeeze_level", "")):
            row += f"（成型中:{_SQZ_SHORT.get(str(prev), str(prev))}）"
        lines.append(row)
    return lines


def macro_iv_status_text(
    iv_source: Optional[str],
    event_loading_applied: bool,
    *,
    omit_loading_note: bool = False,
) -> str:
    """臨近總經事件時的 IV 狀態文字；只有真的套用 1.4x 事件加載才宣稱「已校正」。

    `omit_loading_note=True`：呼叫端的值行已揭露 ×1.4 事件加載時，省略括號註記。
    """
    if event_loading_applied:
        if omit_loading_note:
            return "⚠️ 臨近總經大事件"
        return "⚠️ 臨近總經大事件（快取／HV 代理已套用 1.4x 事件加載）"
    if iv_source == "LIVE_IV":
        return "⚠️ 臨近總經大事件（即時 IV 已含事件定價）"
    return "⚠️ 臨近總經大事件（快取 IV，可能未反映事件）"
