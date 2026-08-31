"""財報與產業資金流輪動報告 Embed 建構函式。"""

import discord

from datetime import datetime, timezone

from cogs.embed_builders._ansi_utils import (
    _pad_string,
    _truncate_with_boundary,
    _safe_float,
    _visual_truncate,
)
from cogs.embed_builders._embed_helpers import (
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
        earnings_lines = ["```ansi"]
        headers = ["標的", "財報日", "情緒覆蓋"]
        widths = [8, 14, 12]
        earnings_lines.append(
            " | ".join(_pad_string(h, w) for h, w in zip(headers, widths))
        )
        earnings_lines.append("-" * (sum(widths) + 3 * (len(widths) - 1)))
        for sym, events in upcoming.items():
            for event in events:
                date = event.get("date", "未知日期")
                sentiment_status = "新聞+社群" if sym in sentiment else "日曆"
                earnings_lines.append(
                    " | ".join(
                        [
                            _pad_string(_visual_truncate(sym, widths[0]), widths[0]),
                            _pad_string(_visual_truncate(date, widths[1]), widths[1]),
                            _pad_string(
                                _visual_truncate(sentiment_status, widths[2]), widths[2]
                            ),
                        ]
                    )
                )
        earnings_lines.append("```")
        embed.add_field(
            name="📅 即將發布財報標的",
            value=_safe_embed_field_value("\n".join(earnings_lines), "近期無財報事件"),
            inline=False,
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
    embed.set_footer(text="Nexus Seeker AI Analyst | 盤前財報與估值調整")
    return embed


def create_sector_flow_report_embed(
    report_type: str, report_content: str, raw_data: dict
) -> discord.Embed:
    """建立收盤資金流向與板塊輪動報告 Embed。"""
    batch_label = _extract_report_batch(report_type)
    vix = _safe_float(raw_data.get("vix"))
    spy_price = _safe_float(raw_data.get("spy_price"))
    vix_tier_name = str(raw_data.get("vix_tier_name", "Unknown"))
    sectors = raw_data.get("sectors", []) or []
    poly_events = raw_data.get("poly_events", []) or []
    spy_max_pain = raw_data.get("spy_max_pain", {}) or {}

    embed = NexusEmbed(
        title="📊 Nexus Seeker 收盤資金流向與板塊輪動報告",
        description=(
            f"**更新批次：** {batch_label}\n"
            f"**SPY 現價：** `${spy_price:.2f}` ｜ "
            f"**VIX：** `{vix:.2f}` (`{vix_tier_name}`)\n"
            "沿用欄位式收盤戰報版型，彙整板塊輪動、事件定價與 AI 收斂結論。"
        ),
        color=_report_embed_color(report_content),
        timestamp=datetime.now(timezone.utc),
    )

    market_panel_body = "\n".join(
        [
            f"SPY 現價: ${spy_price:.2f}",
            f"VIX: {vix:.2f} ({vix_tier_name})",
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
        sector_lines = ["```ansi"]
        sector_lines.append(" 🔄 板塊輪動快照 (Sector Rotation)")
        sector_lines.append(" ----------------------------------")
        headers = ["ETF", "板塊", "日變動", "量比", "Skew", "UOA"]
        widths = [5, 18, 8, 6, 8, 4]
        sector_lines.append(
            " ".join(
                [
                    " | ".join(_pad_string(h, w) for h, w in zip(headers, widths)),
                ]
            )
        )
        sector_lines.append("-" * (sum(widths) + 3 * (len(widths) - 1)))
        sorted_sectors = sorted(
            sectors,
            key=lambda item: abs(_safe_float(item.get("pct_change"))),
            reverse=True,
        )
        for item in sorted_sectors:
            sector_lines.append(
                " | ".join(
                    [
                        _pad_string(
                            _visual_truncate(str(item.get("symbol", "N/A")), widths[0]),
                            widths[0],
                        ),
                        _pad_string(
                            _visual_truncate(str(item.get("name", "N/A")), widths[1]),
                            widths[1],
                        ),
                        _pad_string(
                            f"{_safe_float(item.get('pct_change')):+.2f}%",
                            widths[2],
                            "right",
                        ),
                        _pad_string(
                            f"{_safe_float(item.get('rel_vol')):.2f}x",
                            widths[3],
                            "right",
                        ),
                        _pad_string(
                            f"{_safe_float(item.get('skew')):+.1f}",
                            widths[4],
                            "right",
                        ),
                        _pad_string(
                            str(int(item.get("uoa_count", 0))), widths[5], "right"
                        ),
                    ]
                )
            )
        sector_lines.append("```")
        sector_value = "\n".join(sector_lines)
    else:
        sector_panel = _build_watchlist_style_panel(
            "🔄 板塊輪動快照 (Sector Rotation)",
            "",
            width=45,
            empty_msg="目前無板塊輪動資料。",
        )
        sector_value = f"```ansi\n{sector_panel}\n```"
    embed.add_field(
        name="🔄 板塊輪動快照",
        value=_safe_embed_field_value(sector_value, "目前無板塊輪動資料。"),
        inline=False,
    )

    event_bullets: list[str] = []
    max_pain_value = spy_max_pain.get("max_pain")
    if max_pain_value is not None:
        event_bullets.append(f"SPY Max Pain: ${_safe_float(max_pain_value):.2f}")

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
    embed.set_footer(text="Nexus Seeker AI Analyst | 收盤資金流向與板塊輪動")
    return embed
