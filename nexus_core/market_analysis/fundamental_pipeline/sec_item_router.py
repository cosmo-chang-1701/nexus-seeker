"""SEC EDGAR 申報項目路由與時段分類器。

依據 SEC Form 類型與 8-K Items 將申報分流至對應的分析管線：
- 8-K Item 2.02 -> EARNINGS (財務預期差)
- 8-K Item 1.01 -> MATERIAL_AGREEMENT (重大協議)
- 8-K Item 1.02 -> TERMINATION_AGREEMENT (重大終止)
- 8-K Item 2.05 -> RESTRUCTURING (重組與處分)
- 8-K Item 4.02 -> GOVERNANCE_CRITICAL (財報重編/非信賴性)
- 8-K Item 5.02 -> GOVERNANCE_HIGH (高管變動路由鍵；實際嚴重度由 governance_gate 判定，預設 REVIEW)
- Form 4 -> INSIDER_TRANSACTION (內部人交易)
- SC 13D/G -> ACTIVIST_13D / PASSIVE_13G (大股東申報)
- 10-K / 10-Q -> FINANCIAL_REPORT (定期財務報告)
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
import re
from typing import Sequence
from zoneinfo import ZoneInfo

from market_analysis.fundamental_pipeline.models import FilingSession

_ET_ZONE = ZoneInfo("America/New_York")

# 8-K Item 對應路由鍵表
_ITEM_ROUTE_MAP: dict[str, str] = {
    "2.02": "EARNINGS",
    "1.01": "MATERIAL_AGREEMENT",
    "1.02": "TERMINATION_AGREEMENT",
    "2.05": "RESTRUCTURING",
    "4.02": "GOVERNANCE_CRITICAL",
    "5.02": "GOVERNANCE_HIGH",
}


# SEC 申報 SGML 表頭（`{accession}.hdr.sgml` / `-index-headers.html`）中的受理時間標籤；
# 值為「美東牆上時間」緊湊格式 YYYYMMDDHHMMSS，與 EDGAR index 頁的 Accepted 欄一致。
_SGML_ACCEPTANCE_RE = re.compile(r"<ACCEPTANCE-DATETIME>\s*(\d{14})")

# submissions JSON 的 acceptanceDateTime 與真實受理時間的最大可能正偏差：
# 實測部分公司（如 AAPL 全數、AMZN 舊筆）的值為「真實 UTC + 美東偏移」，
# 冬令最多 +5 小時；其餘公司則為真實 UTC（偏差 0）。真實時間必然落在
# [JSON 值 - 5h, JSON 值] 區間內。
SEC_JSON_ACCEPTANCE_MAX_SKEW = timedelta(hours=5)


def parse_sec_acceptance_datetime(accepted_at: str | datetime) -> datetime:
    """將 SEC 受理時間正規化為帶時區之美東 datetime。

    解讀規則（2026-10 以真實樣本驗證，見
    tests/unit/fixtures/sec/submissions_acceptance_samples.json）：
    - 緊湊格式 ``YYYYMMDDHHMMSS``（SGML 表頭 ``<ACCEPTANCE-DATETIME>``）：**美東牆上時間**。
    - 無時區之 ``YYYY-MM-DD HH:MM:SS``（EDGAR index 頁 Accepted 欄）：美東牆上時間。
    - 帶時區之 ISO 8601（含 ``Z``）：依其標示之時區換算。
    - ``datetime``：帶時區者依其時區換算；無時區者視為美東牆上時間。

    注意：submissions JSON 的 ``acceptanceDateTime`` 雖標示 ``Z``，實測**並非一律為真實
    UTC**——AAPL 等公司的值比真實 UTC 多出美東偏移（夏令 +4h、冬令 +5h），MSFT 等公司則
    正確，同一公司新舊筆亦可能不一致，無法以固定公式還原。因此精確時段分類必須改用
    SGML 表頭（``parse_sec_header_acceptance``），JSON 值只能當作「真實時間的上界」
    （見 ``SEC_JSON_ACCEPTANCE_MAX_SKEW``）。
    """
    if isinstance(accepted_at, datetime):
        if accepted_at.tzinfo is None:
            return accepted_at.replace(tzinfo=_ET_ZONE)
        return accepted_at.astimezone(_ET_ZONE)

    cleaned = accepted_at.strip()
    if len(cleaned) == 14 and cleaned.isdigit():
        return datetime.strptime(cleaned, "%Y%m%d%H%M%S").replace(tzinfo=_ET_ZONE)

    norm_str = cleaned.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(norm_str)
    except ValueError:
        # 備援：截取至秒數解析（無時區，視為美東牆上時間）
        dt = datetime.strptime(cleaned[:19], "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_ET_ZONE)
    return dt.astimezone(_ET_ZONE)


def parse_sec_header_acceptance(header_text: str) -> datetime:
    """從 SEC 申報 SGML 表頭文字擷取權威受理時間（美東）。

    找不到 ``<ACCEPTANCE-DATETIME>`` 標籤時拋出 ``ValueError``，由呼叫端視為處理失敗。
    """
    match = _SGML_ACCEPTANCE_RE.search(header_text)
    if match is None:
        raise ValueError("SEC 表頭缺少 <ACCEPTANCE-DATETIME> 標籤")
    return parse_sec_acceptance_datetime(match.group(1))


def classify_filing_session(accepted_at: str | datetime) -> FilingSession:
    """將 SEC EDGAR 申報受理時間分類為美東時區的四個交易時段。

    時段劃分標準（美東時間 US/Eastern）：
    - BMO (盤前): 04:00 <= ET < 09:30
    - RTH (常規交易): 09:30 <= ET < 16:00
    - AMC (盤後): 16:00 <= ET < 20:00
    - OVERNIGHT (夜間): 20:00 <= ET < 04:00 (次日)

    時間字串解讀規則見 ``parse_sec_acceptance_datetime``；請勿直接傳入 submissions
    JSON 的 ``acceptanceDateTime``（其 ``Z`` 標示不可信），應傳入 SGML 表頭時間。
    """
    t = parse_sec_acceptance_datetime(accepted_at).time()

    if time(4, 0) <= t < time(9, 30):
        return "BMO"
    elif time(9, 30) <= t < time(16, 0):
        return "RTH"
    elif time(16, 0) <= t < time(20, 0):
        return "AMC"
    else:
        return "OVERNIGHT"


def extract_8k_items(items_raw: str | Sequence[str] | None) -> list[str]:
    """從 8-K Items 字串或清單中正規化提取項目代號（如 '2.02', '4.02'）。"""
    if not items_raw:
        return []
    if isinstance(items_raw, str):
        matches = re.findall(r"\b(\d+\.\d+)\b", items_raw)
        return sorted(list(set(matches)))
    extracted: set[str] = set()
    for item in items_raw:
        found = re.findall(r"\b(\d+\.\d+)\b", item)
        extracted.update(found)
    return sorted(list(extracted))


def route_filing(form: str, items: str | Sequence[str] | None = None) -> list[str]:
    """依據 Form 與 Items 回傳分流之處理管線鍵值清單。"""
    form_norm = form.strip().upper()
    if form_norm.startswith("FORM "):
        form_norm = form_norm[5:].strip()
    routes: list[str] = []

    if form_norm in ("4", "4/A"):
        routes.append("INSIDER_TRANSACTION")
        return routes

    if form_norm in ("SC 13D", "SC 13D/A", "SCHEDULE 13D", "13D", "13D/A"):
        routes.append("ACTIVIST_13D")
        return routes

    if form_norm in ("SC 13G", "SC 13G/A", "SCHEDULE 13G", "13G", "13G/A"):
        routes.append("PASSIVE_13G")
        return routes

    if form_norm in ("10-K", "10-K/A", "10-Q", "10-Q/A"):
        routes.append("FINANCIAL_REPORT")
        return routes

    if form_norm in ("8-K", "8-K/A"):
        parsed_items = extract_8k_items(items)
        for itm in parsed_items:
            if itm in _ITEM_ROUTE_MAP:
                routes.append(_ITEM_ROUTE_MAP[itm])
        if not routes:
            routes.append("FORM_8K")
        return routes

    routes.append("OTHER")
    return routes
