"""財報與產業資金流輪動報告 Embed 建構函式。"""

import discord

from datetime import datetime, timezone

from cogs.embed_builders._ansi_utils import (
    _render_aligned_table,
    _truncate_with_boundary,
    _to_float_or_none,
    _visual_truncate,
    fmt_or_dash,
)
from cogs.embed_builders._embed_helpers import (
    _add_ansi_table_fields,
    _safe_embed_field_value,
    _safe_embed_codeblock_value,
    _build_watchlist_style_panel,
    _report_embed_color,
    _extract_report_batch,
    _parse_ai_report_sections,
    _append_ai_report_fields,
)
from cogs.embed_builders._core import NexusEmbed


def create_earnings_report_embed(
    report_type: str, report_content: str, raw_data: dict
) -> discord.Embed:
    """
    建立盤前財報與估值調整 Embed，沿用欄位化戰報風格。
    """
    upcoming = raw_data.get("upcoming_earnings", {})
    sentiment = raw_data.get("earnings_sentiment_scan", {})
    analyzed_symbols = int(raw_data.get("analyzed_symbols", 0) or 0)
    batch_label = _extract_report_batch(report_type)

    embed = NexusEmbed(
        title="📊 Nexus Seeker 盤前財報與估值調整",
        description=(
            f"**更新批次：** {batch_label}\n"
            f"**掃描標的：** `{analyzed_symbols}` 檔 ｜ "
            f"**即將財報：** `{len(upcoming)}` 檔\n"
            "盤前聚焦財報日期、情緒與估值風險，維持與其他核心戰報一致的欄位式呈現。"
        ),
        color=_report_embed_color(report_content),
        timestamp=datetime.now(timezone.utc),
    )

    if upcoming:
        earnings_rows: list[list[str]] = []
        for sym, events in upcoming.items():
            for event in events:
                date = str(event.get("date") or "未知日期")
                sentiment_status = "新聞+社群" if sym in sentiment else "日曆"
                earnings_rows.append(
                    [
                        _visual_truncate(sym, 8),
                        _visual_truncate(date, 14),
                        sentiment_status,
                    ]
                )
        header_line, divider, *data_lines = _render_aligned_table(
            ["標的", "財報日", "情緒覆蓋"], earnings_rows, sep=" | "
        )
        # 以 _chunk_ansi_table 切分，每塊自帶完整收尾 fence，不會被 1024 截斷切掉
        _add_ansi_table_fields(
            embed, "📅 即將發布財報標的", header_line, divider, data_lines
        )
    else:
        embed.add_field(
            name="📅 即將發布財報標的",
            value=_safe_embed_field_value(
                "近期無需調整倉位的財報事件。", "近期無財報事件"
            ),
            inline=False,
        )

    sentiment_lines = []
    for sym, payload in list(sentiment.items())[:3]:
        news = str(payload.get("news", "無相關資訊"))
        reddit = str(payload.get("reddit_sentiment", "無相關資訊"))
        sentiment_lines.append(f"**{sym}**")
        sentiment_lines.append(f"📰 新聞：{_truncate_with_boundary(news, 140)}")
        sentiment_lines.append(f"💬 社群：{_truncate_with_boundary(reddit, 140)}")
        sentiment_lines.append("")
    if sentiment_lines:
        sentiment_lines.pop()
    else:
        sentiment_lines = ["目前無額外新聞 / Reddit 情緒補充。"]
    embed.add_field(
        name="🧠 情緒 / 估值快照",
        value=_safe_embed_field_value("\n".join(sentiment_lines), "目前無額外情緒資訊"),
        inline=False,
    )

    note = raw_data.get("note", "")
    if note:
        embed.add_field(
            name="🧾 掃描備註",
            value=_safe_embed_field_value(str(note), "無補充備註"),
            inline=False,
        )

    _append_ai_report_fields(embed, report_content)
    embed.set_footer(text="AI Analyst | 盤前財報與估值調整")
    return embed


def create_sector_flow_report_embed(
    report_type: str, report_content: str, raw_data: dict
) -> discord.Embed:
    """建立收盤資金流向與板塊輪動報告 Embed。"""
    batch_label = _extract_report_batch(report_type)
    vix = _to_float_or_none(raw_data.get("vix"))
    spy_price = _to_float_or_none(raw_data.get("spy_price"))
    degraded_suffix = " [降級模式]" if vix is None or spy_price is None else ""
    vix_tier_name = str(raw_data.get("vix_tier_name", "Unknown"))
    sectors = raw_data.get("sectors", []) or []
    poly_events = raw_data.get("poly_events", []) or []
    spy_max_pain = raw_data.get("spy_max_pain", {}) or {}

    embed = NexusEmbed(
        title=f"📊 Nexus Seeker 收盤資金流向與板塊輪動報告{degraded_suffix}",
        description=(
            f"**更新批次：** {batch_label}\n"
            f"**SPY 現價：** `{fmt_or_dash(spy_price, prefix='$')}` ｜ "
            f"**VIX：** `{fmt_or_dash(vix)}` (`{vix_tier_name}`)\n"
            "沿用欄位式收盤戰報版型，彙整板塊輪動、事件定價與 AI 收斂結論。"
        ),
        color=_report_embed_color(report_content),
        timestamp=datetime.now(timezone.utc),
    )

    market_panel_body = "\n".join(
        [
            f"SPY 現價: {fmt_or_dash(spy_price, prefix='$')}",
            f"VIX: {fmt_or_dash(vix)} ({vix_tier_name})",
            f"掃描板塊數: {len(sectors)}",
            f"Polymarket 訊號: {len(poly_events)}",
        ]
    )
    market_panel = _build_watchlist_style_panel(
        "🌐 收盤市場快照 (Close Snapshot)",
        market_panel_body,
        width=45,
        empty_msg="暫無市場快照",
    )
    embed.add_field(
        name="🌐 收盤市場快照",
        value=_safe_embed_codeblock_value(market_panel, "暫無市場快照", lang="ansi"),
        inline=False,
    )

    if sectors:
        sorted_sectors = sorted(
            sectors,
            key=lambda item: abs(_to_float_or_none(item.get("pct_change")) or 0.0),
            reverse=True,
        )
        sector_rows = [
            [
                _visual_truncate(str(item.get("symbol", "N/A")), 5),
                _visual_truncate(str(item.get("name", "N/A")), 18),
                fmt_or_dash(
                    item.get("pct_change"), "+.2f", suffix="%", placeholder="--%"
                ),
                fmt_or_dash(item.get("rel_vol"), ".2f", suffix="x", placeholder="--x"),
                fmt_or_dash(item.get("skew"), "+.1f"),
                str(int(item.get("uoa_count", 0))),
            ]
            for item in sorted_sectors
        ]
        header_line, divider, *data_lines = _render_aligned_table(
            ["ETF", "板塊", "日變動", "量比", "Skew", "UOA"],
            sector_rows,
            ["left", "left", "right", "right", "right", "right"],
            sep=" | ",
        )
        _add_ansi_table_fields(
            embed, "🔄 板塊輪動快照", header_line, divider, data_lines
        )
    else:
        sector_panel = _build_watchlist_style_panel(
            "🔄 板塊輪動快照 (Sector Rotation)",
            "",
            width=45,
            empty_msg="目前無板塊輪動資料。",
        )
        embed.add_field(
            name="🔄 板塊輪動快照",
            value=_safe_embed_field_value(
                f"```ansi\n{sector_panel}\n```", "目前無板塊輪動資料。"
            ),
            inline=False,
        )

    event_bullets: list[str] = []
    max_pain_value = spy_max_pain.get("max_pain")
    if max_pain_value is not None:
        event_bullets.append(f"SPY Max Pain: {fmt_or_dash(max_pain_value, prefix='$')}")

    for event in poly_events[:3]:
        question = _truncate_with_boundary(str(event.get("question", "N/A")), 140)
        event_bullets.append(f"Polymarket: {question}")

    event_panel = _build_watchlist_style_panel(
        "🐋 事件定價與關鍵參考 (Event Pricing)",
        "\n".join(event_bullets),
        width=45,
        empty_msg="目前無顯著 Polymarket / Max Pain 補充訊號。",
    )
    embed.add_field(
        name="🐋 事件定價與關鍵參考",
        value=_safe_embed_codeblock_value(
            event_panel, "目前無事件定價資料", lang="ansi"
        ),
        inline=False,
    )

    sections = _parse_ai_report_sections(report_content)
    if sections:
        for header, content in sections:
            panel = _build_watchlist_style_panel(
                header,
                content,
                width=45,
                empty_msg="無詳細資訊",
            )
            embed.add_field(
                name=header,
                value=_safe_embed_codeblock_value(panel, "無詳細資訊", lang="ansi"),
                inline=False,
            )
    else:
        panel = _build_watchlist_style_panel(
            "🤖 AI 分析摘要 (AI Summary)",
            report_content,
            width=45,
            empty_msg="無詳細資訊",
        )
        embed.add_field(
            name="🤖 AI 分析摘要",
            value=_safe_embed_codeblock_value(panel, "無詳細資訊", lang="ansi"),
            inline=False,
        )
    embed.set_footer(text="AI Analyst | 收盤資金流向與板塊輪動")
    return embed
