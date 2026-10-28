"""基本面分析管線專屬 Discord Embed 構建器。

嚴格遵守輸出集中化規範：
- 統一透過 NexusEmbed 構建所有 Embed 物件。
- 遵循 100% 繁體中文標準。
- 零交易執行顧問性聲明。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from cogs.embed_builders._ansi_utils import _clean_ansi
from cogs.embed_builders._core import NexusEmbed
from market_analysis.fundamental_pipeline.models import GovernanceFlagRecord

_ET_ZONE = ZoneInfo("America/New_York")


# NexusEmbed 總長上限 5800；預留標題截斷與換行等誤差
FA_EMBED_TOTAL_BUDGET: int = 5750
FA_FIELD_MAX_LEN: int = 1020
FA_FIELD_MIN_LEN: int = 120  # 截短時每個欄位至少保留的長度


def _fit_field_cap(lengths: Sequence[int], budget: int) -> int:
    """回傳單一欄位長度上限 C，使 sum(min(len_i, C)) <= budget（C 盡可能大，下限 FA_FIELD_MIN_LEN）。"""
    if sum(lengths) <= budget:
        return FA_FIELD_MAX_LEN
    lo, hi = FA_FIELD_MIN_LEN, FA_FIELD_MAX_LEN
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sum(min(n, mid) for n in lengths) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


def ansi_bold(text: str) -> str:
    """以 ANSI 粗體（不上色，亮暗主題皆可讀）包裹文字，供 ```ansi 區塊內取代 Markdown 粗體。"""
    return f"\u001b[1m{text}\u001b[0m"


_ANSI_FENCE_OPEN = "```ansi\n"
_ANSI_FENCE_CLOSE = "\n```"
_ANSI_FENCE_OVERHEAD = len(_ANSI_FENCE_OPEN) + len(_ANSI_FENCE_CLOSE)


def _wrap_ansi_block(body: str) -> str:
    return f"{_ANSI_FENCE_OPEN}{body}{_ANSI_FENCE_CLOSE}"


def _fit_ansi_block(content: str, cap: int) -> str:
    """將內文包成 ```ansi 區塊並限制在 cap 字元內。

    超長時以「整行」為單位自尾端丟棄，並補上一行「…（餘 N 行略）」，fence 永遠完整；
    只有單一行就超過上限時，才先移除 ANSI 控制碼再以字元截短（避免切在跳脫序列中間）。
    """
    body = content.strip()
    if not body:
        return _wrap_ansi_block("無資料")
    wrapped = _wrap_ansi_block(body)
    if len(wrapped) <= cap:
        return wrapped

    lines = body.split("\n")
    total = len(lines)
    for keep in range(total - 1, 0, -1):
        candidate = "\n".join([*lines[:keep], f"…（餘 {total - keep} 行略）"])
        if len(candidate) + _ANSI_FENCE_OVERHEAD <= cap:
            return _wrap_ansi_block(candidate)

    # 連第一行都放不下：去除 ANSI 後字元截短
    suffix = f"\n…（餘 {total - 1} 行略）" if total > 1 else ""
    room = max(cap - _ANSI_FENCE_OVERHEAD - len(suffix) - 1, 0)
    first = _clean_ansi(lines[0])
    if len(first) > room:
        first = first[:room] + "…"
    return _wrap_ansi_block(first + suffix)


def build_fa_terminal_embed(
    symbol: str,
    company_name: str,
    sections: Sequence[tuple[str, str]],
    footer_note: str | None = None,
    *,
    degraded: bool = False,
) -> NexusEmbed:
    """構建 /fa 互動診斷終端的全景 Embed 視圖。

    各區塊內文由本函式包成 ```ansi 區塊（呼叫端不得自行加 fence）。
    各區塊總長超過 NexusEmbed 5800 上限時，以同一長度上限截短「最長的」欄位
    （以整行為單位，短欄位不受影響），避免 NexusEmbed.to_dict 從尾端整欄丟棄。
    degraded=True 表示有區塊載入失敗，標題加上提示後綴。
    """
    now_et = datetime.now(timezone.utc).astimezone(_ET_ZONE)
    now_et_str = now_et.strftime("%Y-%m-%d %H:%M")

    sym_upper = symbol.strip().upper()
    title = f"🔬 基本面事件與全景估值雷達 | {sym_upper}"
    if degraded:
        title += " [部分區塊載入失敗]"
    description = f"標的資產: {company_name} ｜ 查詢時間: {now_et_str} ET"
    footer_text = (
        footer_note if footer_note else "基本面管線純顧問診斷 • 零自動交易執行"
    )

    embed = NexusEmbed(
        title=title,
        description=description,
        color=discord.Color.blue(),
    )
    embed.set_footer(text=footer_text)

    headers = [header[:256] for header, _ in sections]
    contents = [content for _, content in sections]
    values = [_fit_ansi_block(c, FA_FIELD_MAX_LEN) for c in contents]
    # 頁尾含 NexusEmbed 自動加上的品牌前綴，以實際長度計入
    footer_len = len(embed.footer.text or "")
    fixed = len(title) + len(description) + footer_len + sum(map(len, headers))
    cap = _fit_field_cap([len(v) for v in values], FA_EMBED_TOTAL_BUDGET - fixed)
    for header, content, val in zip(headers, contents, values):
        if len(val) > cap:
            val = _fit_ansi_block(content, cap)
        embed.add_field(name=header, value=val, inline=False)

    return embed


def build_governance_flag_embed(
    symbol: str,
    flag: GovernanceFlagRecord,
) -> NexusEmbed:
    """構建重大治理風險（CRITICAL / HIGH）推播警訊 Embed。"""
    sym_upper = symbol.strip().upper()
    is_critical = flag.severity == "CRITICAL"

    title = f"🚨 【SEC 治理風控重大警訊】持倉標的觸發強制審查: {sym_upper}"
    color = discord.Color.red() if is_critical else discord.Color.orange()

    embed = NexusEmbed(
        title=title,
        description=f"標的 `{sym_upper}` 於 SEC EDGAR 申報中觸發監管審查紅旗。",
        color=color,
    )

    sev_icon = "🔴" if is_critical else "🟠"
    embed.add_field(
        name="🚨 審查等級與類型",
        value=_wrap_ansi_block(
            f"{sev_icon} {ansi_bold(flag.severity)} ｜ 代號: {flag.flag_kind}\n"
            f"有效審查期至: {flag.expires_at} UTC"
        ),
        inline=False,
    )

    # 解析事由摘錄
    snippet = "未提供詳細文字"
    if flag.detail_json:
        try:
            detail_dict = json.loads(flag.detail_json)
            snippet = str(
                detail_dict.get("snippet") or detail_dict.get("title") or snippet
            )
        except Exception:
            snippet = flag.detail_json[:300]

    # 摘錄為外部申報文字：移除 ``` 避免提前閉合 fence，並保留 800 字上限
    safe_snippet = snippet.replace("```", "").strip()[:800] or "未提供詳細文字"
    embed.add_field(
        name="📄 核心事由摘錄",
        value=_wrap_ansi_block(safe_snippet),
        inline=False,
    )

    embed.add_field(
        name="🛡️ 顧問處置建議",
        value=(
            "已啟動原型假設破滅防禦機制。請檢視多頭部位與現貨曝險。\n"
            "※ 本提示為純顧問性風控評級，絕無自動下單、平倉或對沖處置。"
        ),
        inline=False,
    )

    embed.set_footer(text="SEC 申報直連流 • 治理風控閘門")
    return embed
