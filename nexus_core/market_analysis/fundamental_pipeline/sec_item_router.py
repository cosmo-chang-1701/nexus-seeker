"""SEC EDGAR 申報項目路由與時段分類器。

依據 SEC Form 類型與 8-K Items 將申報分流至對應的分析管線：
- 8-K Item 2.02 -> EARNINGS (財務預期差)
- 8-K Item 1.01 -> MATERIAL_AGREEMENT (重大協議)
- 8-K Item 1.02 -> TERMINATION_AGREEMENT (重大終止)
- 8-K Item 2.05 -> RESTRUCTURING (重組與處分)
- 8-K Item 4.02 -> GOVERNANCE_CRITICAL (財報重編/非信賴性)
- 8-K Item 5.02 -> GOVERNANCE_HIGH (高管變動)
- Form 4 -> INSIDER_TRANSACTION (內部人交易)
- SC 13D/G -> ACTIVIST_13D / PASSIVE_13G (大股東申報)
- 10-K / 10-Q -> FINANCIAL_REPORT (定期財務報告)
"""

from __future__ import annotations

from datetime import datetime, time, timezone
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


def classify_filing_session(accepted_at: str | datetime) -> FilingSession:
    """將 SEC EDGAR 申報受理時間分類為美東時區的四個交易時段。

    時段劃分標準（美東時間 US/Eastern）：
    - BMO (盤前): 04:00 <= ET < 09:30
    - RTH (常規交易): 09:30 <= ET < 16:00
    - AMC (盤後): 16:00 <= ET < 20:00
    - OVERNIGHT (夜間): 20:00 <= ET < 04:00 (次日)
    """
    if isinstance(accepted_at, str):
        cleaned = accepted_at.strip()
        # 處理緊湊格式 YYYYMMDDHHMMSS
        if len(cleaned) == 14 and cleaned.isdigit():
            dt = datetime.strptime(cleaned, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        else:
            # 處理 ISO 8601 或一般字串
            norm_str = cleaned.replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(norm_str)
            except ValueError:
                # 備援：截取至秒數解析
                dt = datetime.strptime(cleaned[:19], "%Y-%m-%d %H:%M:%S")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = accepted_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

    et_dt = dt.astimezone(_ET_ZONE)
    t = et_dt.time()

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
