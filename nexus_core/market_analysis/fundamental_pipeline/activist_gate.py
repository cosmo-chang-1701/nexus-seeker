"""Schedule 13D 激進投資人意圖識別與及時性審查閘門。

依據 SEC Rule 13d-1(a) 與 Item 4 (Purpose of Transaction) 條款：
1. 激進意圖關鍵字審查：董事會席次、戰略重整、出售拆分、代理人委託書爭奪。
2. 5 營業日及時性檢查：跨越 5% 持股門檻後，逾期申報（> 5 營業日）標記延遲紅旗。

營業日定義依 Exchange Act Rule 14d-1(g)(3)（Rule 13d-1 沿用）：週六、週日與聯邦假日
以外的日子。注意這與 NYSE 交易日不同（耶穌受難日 NYSE 休市但 SEC 上班；哥倫布日、
退伍軍人節 SEC 休息但 NYSE 開市），因此使用聯邦假日曆而非 nyse_calendar。

接線狀態：`services/filing_event_service.py` 在每小時 SEC 申報同步中，遇到結構化
Schedule 13D / 13D/A（EDGAR 自 2024-12-18 起強制的 `primary_doc.xml`）時，以
`parse_schedule_13d_xml` 取出封面與 Item 4，再呼叫 `evaluate_activist_filing`。
結果目前只寫入日誌（record-only，不入庫、不推播）；13G 為被動持股、無 Item 4 意圖，
不評估；舊版 HTML / 純文字 `SC 13D` 無結構化欄位，略過。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
import re

import defusedxml.ElementTree as ET
from pandas.tseries.holiday import USFederalHolidayCalendar

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


@dataclass(frozen=True)
class Schedule13DFields:
    """結構化 Schedule 13D 申報中供激進投資人閘門使用的欄位。"""

    investor_name: str
    ownership_pct: float
    item_4_text: str
    event_date: str  # ISO 日期 (YYYY-MM-DD)；無法解析時為空字串


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _iter_local(root: ET.Element, name: str) -> list[ET.Element]:
    return [node for node in root.iter() if _local_name(node.tag) == name]


def _first_text(root: ET.Element, name: str) -> str:
    for node in _iter_local(root, name):
        text = (node.text or "").strip()
        if text:
            return text
    return ""


def _normalize_event_date(raw: str) -> str:
    """封面 `dateOfEvent` 為 MM/DD/YYYY，正規化為 ISO；亦接受已是 ISO 的值。"""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def parse_schedule_13d_xml(xml_text: str) -> Schedule13DFields | None:
    """解析 EDGAR 結構化 Schedule 13D `primary_doc.xml`（命名空間無關）。

    - 投資人名稱：第一位申報人 `reportingPersonName`。
    - 持股比例：各申報人 `percentOfClass` 取最大值（共同申報人持股多為重疊計算，不可加總）。
    - Item 4：`item4/transactionPurpose`；修正申報未修改 Item 4 時為空字串。
    - 事件日：封面 `dateOfEvent`。
    非 XML 或缺少申報人名稱時回傳 None（例如舊版 HTML 申報）。
    """
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return None

    investor_name = _first_text(root, "reportingPersonName")
    if not investor_name:
        return None

    pcts: list[float] = []
    for node in _iter_local(root, "percentOfClass"):
        try:
            pcts.append(float((node.text or "").strip().rstrip("%")))
        except ValueError:
            continue

    return Schedule13DFields(
        investor_name=investor_name,
        ownership_pct=max(pcts) if pcts else 0.0,
        item_4_text=_first_text(root, "transactionPurpose"),
        event_date=_normalize_event_date(_first_text(root, "dateOfEvent")),
    )


@lru_cache(maxsize=16)
def _federal_holidays(year: int) -> frozenset[date]:
    """指定年度的美國聯邦假日（含週末移至週五 / 週一的補假日）。"""
    holidays = USFederalHolidayCalendar().holidays(
        start=f"{year}-01-01", end=f"{year}-12-31"
    )
    return frozenset(ts.date() for ts in holidays)


def is_sec_business_day(day: date) -> bool:
    """`day` 是否為 SEC 營業日（排除週末與聯邦假日）。"""
    # 0=Mon, ..., 4=Fri, 5=Sat, 6=Sun
    if day.weekday() >= 5:
        return False
    return day not in _federal_holidays(day.year)


def count_business_days(start_date: date, end_date: date) -> int:
    """計算兩日期之間的 SEC 營業日天數（排除週末與聯邦假日，不含 start_date 當日）。"""
    if start_date >= end_date:
        return 0
    cur = start_date + timedelta(days=1)
    b_days = 0
    while cur <= end_date:
        if is_sec_business_day(cur):
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
