"""Schedule 13D 激進投資人意圖識別與及時性審查閘門。

依據 SEC Rule 13d-1(a) 與 Item 4 (Purpose of Transaction) 條款：
1. 激進意圖關鍵字審查：董事會席次、戰略重整、出售拆分、代理人委託書爭奪。
2. 5 營業日及時性檢查：跨越 5% 持股門檻後，逾期申報（> 5 營業日）標記延遲紅旗。
"""

from __future__ import annotations

from datetime import date, timedelta
import re

from market_analysis.fundamental_pipeline.models import ActivistSignal

# 激進投資人意圖核心關鍵字與正則
_ACTIVIST_INTENT_PATTERNS = {
    "BOARD_SEAT": re.compile(
        r"board\s+representation|nominate\s+directors?|board\s+seat|board\s+composition",
        re.IGNORECASE,
    ),
    "STRATEGIC_REVIEW": re.compile(
        r"strategic\s+alternatives?|maximize\s+shareholder\s+value|strategic\s+review",
        re.IGNORECASE,
    ),
    "SALE_OR_MERGER": re.compile(
        r"sale\s+of\s+(the\s+)?company|merger|acquisition|take-private|going-private",
        re.IGNORECASE,
    ),
    "SPINOFF_OR_SPLIT": re.compile(
        r"spin-off|divestiture|split-off|carve-out|asset\s+sale", re.IGNORECASE
    ),
    "PROXY_CONTEST": re.compile(
        r"proxy\s+contest|solicit\s+proxies|consent\s+solicitation", re.IGNORECASE
    ),
    "CAPITAL_ALLOCATION": re.compile(
        r"share\s+repurchase|special\s+dividend|excess\s+cash|capital\s+allocation",
        re.IGNORECASE,
    ),
    "OPERATIONAL_RESTRUCTURING": re.compile(
        r"operational\s+(improvements?|changes?)|cost\s+reduction|headcount\s+reduction",
        re.IGNORECASE,
    ),
    "UNDERVALUED_STANCE": re.compile(
        r"undervalued|attractive\s+investment\s+opportunity|trading\s+at\s+a\s+discount",
        re.IGNORECASE,
    ),
}


def count_business_days(start_date: date, end_date: date) -> int:
    """計算兩日期之間的營業日天數（排除週六與週日，不含 start_date 當日）。"""
    if start_date >= end_date:
        return 0
    cur = start_date + timedelta(days=1)
    b_days = 0
    while cur <= end_date:
        # 0=Mon, ..., 4=Fri, 5=Sat, 6=Sun
        if cur.weekday() < 5:
            b_days += 1
        cur += timedelta(days=1)
    return b_days


def evaluate_activist_filing(
    symbol: str,
    accession: str,
    investor_name: str,
    ownership_pct: float,
    item_4_text: str,
    event_date: str,
    filing_date: str,
) -> ActivistSignal:
    """解析評估 Schedule 13D 申報事件，產出 ActivistSignal。"""
    sym_upper = symbol.strip().upper()
    key_intents: list[str] = []

    # 1. 關鍵意圖掃描
    for intent_key, pattern in _ACTIVIST_INTENT_PATTERNS.items():
        if pattern.search(item_4_text):
            key_intents.append(intent_key)

    # 2. 5 營業日及時性審查 (SEC 2024 新規 Rule 13d-1(a))
    is_delayed = False
    try:
        e_dt = date.fromisoformat(event_date[:10])
        f_dt = date.fromisoformat(filing_date[:10])
        b_days = count_business_days(e_dt, f_dt)
        if b_days > 5:
            is_delayed = True
    except Exception:
        # 若時間解析異常，維持安全中性
        b_days = 0

    # 3. 繁體中文摘要
    intent_desc_map = {
        "BOARD_SEAT": "爭取董事會席次",
        "STRATEGIC_REVIEW": "要求戰略評估",
        "SALE_OR_MERGER": "推動出售或併購",
        "SPINOFF_OR_SPLIT": "要求業務拆分",
        "PROXY_CONTEST": "發動委託書爭奪",
        "CAPITAL_ALLOCATION": "敦促資本配置/回購",
        "OPERATIONAL_RESTRUCTURING": "要求營運精簡重組",
        "UNDERVALUED_STANCE": "主張價值嚴重低估",
    }
    desc_intents = [intent_desc_map[k] for k in key_intents if k in intent_desc_map]
    intents_summary = "、".join(desc_intents) if desc_intents else "未明確提出實質訴求"

    summary_text = f"激進投資人 {investor_name} 申報持股 {ownership_pct:.1f}%；訴求包含：{intents_summary}。"
    if is_delayed:
        summary_text += (
            f" ⚠️ 申報逾期（距事件日歷經 {b_days} 個營業日，超過法規 5 日上限）。"
        )

    return ActivistSignal(
        symbol=sym_upper,
        accession=accession,
        investor_name=investor_name,
        ownership_pct=ownership_pct,
        is_delayed_filing=is_delayed,
        key_intents=key_intents,
        summary_text=summary_text,
    )
