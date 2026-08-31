"""Polymarket 預測市場鯨魚追蹤與機率閃崩警報 Embed 建構函式。"""

import discord

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from cogs.embed_builders._core import NexusEmbed
from cogs.embed_builders._ansi_utils import _truncate_with_boundary


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
        full_desc = _truncate_with_boundary(full_desc, 3900)

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
    embed.add_field(name="🐋 市場名稱", value=f"**{market}**", inline=False)
    embed.add_field(
        name="📊 機率變化",
        value=f"`{old_prob*100:.1f}%` ➔ `{new_prob*100:.1f}%`",
        inline=True,
    )
    embed.add_field(name="📐 Delta", value=f"`{delta:+.1f}%`", inline=True)
    embed.add_field(
        name="🔍 可能原因",
        value="📰 **突發新聞、重大事件落地、或大戶倒貨重新定價**",
        inline=False,
    )
    embed.set_footer(text="Polymarket AI Monitor | Nexus Seeker")
    return embed
