"""新聞、Reddit 與媒體輿情社群 Embed 建構函式。"""

import re
import discord

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._embed_helpers import (
    add_news_field,
    add_reddit_field,
    _add_ansi_field_safely,
)


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


def create_media_sentiment_embed(
    symbol: Any,
    news_text: Any = None,
    reddit_text: Any = None,
    *,
    polymarket_odds: Any = None,
    reddit_posts: Optional[List[Dict[str, Any]]] = None,
    reddit_sentiment_score: Optional[str] = None,
    news_items: Optional[List[Dict[str, Any]]] = None,
    polymarket_summary: Optional[str] = None,
    skew_val: Optional[float] = None,
    skew_percentile: Optional[float] = None,
    pcr_val: Optional[float] = None,
) -> discord.Embed:
    """建構輿情與社群 (Media & Social) 掃描結果的統一 Embed"""
    embed = NexusEmbed(
        title=f"🎭 {symbol} 輿情與社群大盤掃描 (Media & Social)",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    # 1. 📊 輿情與期權共振雷達 (ANSI Panel)
    poly_summary_raw = polymarket_summary or polymarket_odds
    if poly_summary_raw and str(poly_summary_raw).strip() != "N/A":
        poly_ansi_summary = re.sub(
            r"\[([^\]]+)\]\([^\)]+\)", r"\1", str(poly_summary_raw)
        ).split("\n")[0]
        poly_status_line = f" └─ 狀態: \u001b[1;34m{poly_ansi_summary}\u001b[0m"
    else:
        poly_status_line = " └─ 狀態: \u001b[1;30m暫無預測市場數據\u001b[0m"

    reddit_score_str = str(reddit_sentiment_score or "⚖️ 中性")
    if (
        "🚀" in reddit_score_str
        or "樂觀" in reddit_score_str
        or "Bullish" in reddit_score_str
    ):
        sentiment_color = "\u001b[1;32m"
        is_retail_bullish = True
        is_retail_bearish = False
    elif (
        "💀" in reddit_score_str
        or "恐慌" in reddit_score_str
        or "悲觀" in reddit_score_str
        or "Bearish" in reddit_score_str
    ):
        sentiment_color = "\u001b[1;31m"
        is_retail_bullish = False
        is_retail_bearish = True
    else:
        sentiment_color = "\u001b[1;33m"
        is_retail_bullish = False
        is_retail_bearish = False

    post_count = len(reddit_posts) if reddit_posts else 0
    tag = f" (Top {post_count} 焦點監控)" if post_count > 0 else ""
    reddit_status_line = f" └─ 狀態: {sentiment_color}{reddit_score_str}\u001b[0m{tag}"

    skew_color = (
        "\u001b[1;35m"
        if skew_percentile is not None and skew_percentile > 80.0
        else "\u001b[1;36m"
    )
    pcr_color = (
        "\u001b[1;31m"
        if pcr_val is not None and pcr_val > 1.2
        else (
            "\u001b[1;32m" if pcr_val is not None and pcr_val < 0.7 else "\u001b[1;37m"
        )
    )
    skew_val_str = f"{skew_val:.2f}%" if skew_val is not None else "--%"
    skew_per_str = f"{skew_percentile:.1f}%" if skew_percentile is not None else "--%"
    pcr_val_str = f"{pcr_val:.2f}" if pcr_val is not None else "--"
    greeks_line = f" └─ Skew 值: {skew_color}{skew_val_str}\u001b[0m (分位: {skew_color}{skew_per_str}\u001b[0m) | Volume PCR: {pcr_color}{pcr_val_str}\u001b[0m"

    is_skew_high = skew_percentile is not None and skew_percentile > 80.0
    is_skew_low = skew_percentile is not None and skew_percentile < 20.0
    is_whale_bullish = "🟢" in str(poly_summary_raw) or "看多" in str(poly_summary_raw)
    is_whale_bearish = "🔴" in str(poly_summary_raw) or "偏空" in str(poly_summary_raw)

    if is_retail_bullish and is_skew_high:
        resonance_rating = (
            "\u001b[1;31m⚠️ 散戶極度 FOMO 但機構買 Put 避險 (Skew > 80%)\u001b[0m"
        )
        resonance_guide = "短線嚴禁追高買權；現貨建議逢高落袋或建立保護性賣權"
    elif is_retail_bearish and (is_skew_low or is_whale_bullish):
        resonance_rating = (
            "\u001b[1;32m💡 散戶恐慌拋售 vs 機構偏斜低廉/巨鯨護航\u001b[0m"
        )
        resonance_guide = "勿盲目殺跌；具左側反彈潛力，可評估逢低分批接刀或賣出 Put"
    elif is_retail_bullish and (is_whale_bullish or not is_skew_high):
        resonance_rating = "\u001b[1;32m💎 巨鯨散戶同步看多，期權結構健康\u001b[0m"
        resonance_guide = "現貨續抱；做多期權優先選擇平值或順勢牛市價差策略"
    elif is_retail_bearish and (
        is_whale_bearish or (pcr_val is not None and pcr_val > 1.2)
    ):
        resonance_rating = "\u001b[1;31m💀 多重共振偏空，期權沽購比升溫\u001b[0m"
        resonance_guide = "防守為上；降低多頭曝險，嚴守支撐位並配置保護性頭寸"
    else:
        resonance_rating = "\u001b[1;33m⚖️ 輿情多空分歧，期權籌碼處於平衡區間\u001b[0m"
        resonance_guide = "保持觀察；等待催化事件或方向性共振突破"

    radar_lines = [
        "```ansi",
        " 巨鯨定價 (Polymarket)",
        poly_status_line,
        " 散戶風向 (Reddit)",
        reddit_status_line,
        " 期權微觀結構 (Greeks & Skew)",
        greeks_line,
        " 輿情籌碼共振 (Resonance Check)",
        f" └─ 評級: {resonance_rating}",
        f" └─ 指引: \u001b[1;37m{resonance_guide}\u001b[0m",
        "```",
    ]
    _add_ansi_field_safely(embed, "📊 輿情與期權共振雷達", radar_lines)

    # 2. 🐋 Polymarket 預測事件
    if polymarket_odds and str(polymarket_odds).strip() != "N/A":
        poly_raw_str = str(polymarket_odds).strip()
        lines: List[str] = []
        for line in poly_raw_str.split("\n"):
            line_clean = line.strip()
            if not line_clean:
                continue
            if not line_clean.startswith("•"):
                line_clean = f"• {line_clean}"
            lines.append(line_clean)
        if lines:
            embed.add_field(
                name="🐋 Polymarket 預測事件",
                value="\n".join(lines[:4]) + "\n\u200b",
                inline=False,
            )

    # 3. 🔥 Reddit 社群熱門討論 (純文章清單，移除重複情緒指標)
    if reddit_posts and isinstance(reddit_posts, list) and len(reddit_posts) > 0:
        reddit_lines: List[str] = []
        for p in reddit_posts[:3]:
            if isinstance(p, dict):
                sub = p.get("subreddit", "reddit")
                raw_title = str(p.get("title", "")).strip()
                url = p.get("url", "")
                if url:
                    reddit_lines.append(f"• `[r/{sub}]` [{raw_title}]({url})")
                else:
                    reddit_lines.append(f"• `[r/{sub}]` {raw_title}")

        if reddit_lines:
            embed.add_field(
                name="🔥 Reddit 社群熱門討論",
                value="\n".join(reddit_lines) + "\n\u200b",
                inline=False,
            )
    elif reddit_text:
        reddit_str = str(reddit_text).strip()
        if reddit_str:
            if (
                "無相關討論" in reddit_str
                or "尚未配置" in reddit_str
                or "連線異常" in reddit_str
                or "錯誤" in reddit_str
            ):
                embed.add_field(
                    name="🔥 Reddit 社群熱門討論",
                    value=f"• {reddit_str}\n\u200b",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="🔥 Reddit 社群熱門討論",
                    value=f"```{reddit_str}\n\u200b```\n\u200b",
                    inline=False,
                )

    # 4. 📰 即時市場新聞與權威報導
    if news_items and isinstance(news_items, list) and len(news_items) > 0:
        news_lines: List[str] = []
        for n in news_items[:3]:
            if isinstance(n, dict):
                src = n.get("source") or "News"
                headline = str(n.get("headline", "")).strip()
                url = n.get("url", "")
                time_tag = n.get("time_tag", "")
                time_suffix = f" · *{time_tag}*" if time_tag else ""
                if url:
                    news_lines.append(f"• `[{src}]` [{headline}]({url}){time_suffix}")
                else:
                    news_lines.append(f"• `[{src}]` {headline}{time_suffix}")

        if news_lines:
            embed.add_field(
                name="📰 即時市場新聞與權威報導",
                value="\n".join(news_lines) + "\n\u200b",
                inline=False,
            )
    elif news_text:
        news_str = str(news_text).strip()
        if news_str:
            if "▪️" in news_str or "\n" in news_str:
                raw_lines = [
                    line.strip() for line in news_str.split("\n") if line.strip()
                ]
                fmt_lines = []
                for line in raw_lines[:4]:
                    clean_line = line.lstrip("▪️").lstrip("•").strip()
                    fmt_lines.append(f"• {clean_line}")
                embed.add_field(
                    name="📰 即時市場新聞與權威報導",
                    value="\n".join(fmt_lines) + "\n\u200b",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="📰 即時市場新聞與權威報導",
                    value=f"• {news_str}\n\u200b",
                    inline=False,
                )

    embed.set_footer(
        text="Nexus Seeker 輿情中心 • 資料來源: Finnhub, Polymarket & Reddit (WSB/Stocks/Options)"
    )
    return embed
