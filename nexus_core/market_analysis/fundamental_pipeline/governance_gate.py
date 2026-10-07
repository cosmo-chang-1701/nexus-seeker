"""公司治理審查閘門（Governance Review Gate）。

依據 SEC 8-K 重大申報與法規監管日誌評估標的治理風險：
- 8-K Item 4.02: 財報重大重編 / 審計意見不可信賴性 -> 🔴 CRITICAL (風控審查 30 天)
- 8-K Item 5.02: 高管/董事變動。5.02 同時涵蓋離任 (b)、新任 (c)、董事選任 (d) 與
  薪酬協議 (e)，僅憑 item code 無法區分，因此預設為 🟡 REVIEW（僅供人工複核，不推播、
  不作為候選排除依據）；只有在呼叫端提供的內文明確指出離任 / 辭職 / 解職時，才升為
  🟠 HIGH (風控審查 30 天)。目前 FilingEventService 只取得 submissions JSON 的 item code，
  未下載 8-K 內文，因此 5.02 一律為 REVIEW。
- 計算綜合治理狀態 (GovernanceStatus) 與最高風險等級。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
from typing import Mapping, Sequence

from market_analysis.fundamental_pipeline.models import (
    GovernanceFlagRecord,
    GovernanceSeverity,
    GovernanceStatus,
)
from market_analysis.fundamental_pipeline.sec_item_router import (
    extract_8k_items,
    parse_sec_acceptance_datetime,
)

# 5.02 標題本身即含 "Departure of Directors or Certain Officers"，判斷前先移除標題，
# 避免任何 5.02 都被誤判為離任。
_ITEM_502_TITLE_RE = re.compile(
    r"departure\s+of\s+directors\s+or\s+(certain|principal)\s+officers",
    re.IGNORECASE,
)
# 5.02(b) 離任語意：辭職、退休、解職、卸任、終止聘僱、離職。
_ITEM_502_DEPARTURE_RE = re.compile(
    r"\bresign(?:s|ed|ation)?\b|\bretire(?:s|d|ment)?\b|\bstep(?:s|ped)?\s+down\b"
    r"|\bterminat(?:e|ed|ion)\b|\bdepart(?:s|ed|ure)?\b|\bseparation\b"
    r"|辭職|辭任|退休|解職|離任|卸任|離職",
    re.IGNORECASE,
)


def is_item_502_departure(text_snippet: str) -> bool:
    """判斷 8-K Item 5.02 內文是否明確指出高管 / 董事離任（5.02(b)）。

    無內文時回傳 False（無法判斷，不得推定為離任）。
    """
    if not text_snippet or not text_snippet.strip():
        return False
    body = _ITEM_502_TITLE_RE.sub(" ", text_snippet)
    return bool(_ITEM_502_DEPARTURE_RE.search(body))


_SEVERITY_ORDER: dict[GovernanceSeverity, int] = {
    "INFO": 1,
    "REVIEW": 2,
    "HIGH": 3,
    "CRITICAL": 4,
}


def create_governance_flag(
    symbol: str,
    source_accession: str,
    flag_kind: str,
    severity: GovernanceSeverity,
    detail: Mapping[str, object] | None = None,
    review_days: int = 30,
    base_time: datetime | None = None,
) -> GovernanceFlagRecord:
    """建立治理審查旗標記錄，並設置審查到期日。"""
    if base_time is None:
        base_time = datetime.now(timezone.utc)
    elif base_time.tzinfo is None:
        # 無時區者視為美東牆上時間（SEC 受理時間慣例）
        base_time = parse_sec_acceptance_datetime(base_time)
    # expires_at 以 UTC 字串儲存，與 get_active_governance_flags 的 UTC as_of 比較一致
    expires_dt = base_time.astimezone(timezone.utc) + timedelta(days=review_days)
    expires_str = expires_dt.strftime("%Y-%m-%d %H:%M:%S")

    detail_json = json.dumps(detail, ensure_ascii=False) if detail else None

    return GovernanceFlagRecord(
        symbol=symbol.strip().upper(),
        source_accession=source_accession.strip(),
        flag_kind=flag_kind.strip(),
        severity=severity,
        detail_json=detail_json,
        expires_at=expires_str,
    )


def generate_governance_flags_from_8k(
    symbol: str,
    accession: str,
    items: Sequence[str],
    accepted_at: str | datetime,
    text_snippet: str = "",
    review_days: int = 30,
) -> list[GovernanceFlagRecord]:
    """從 8-K Items 中檢測是否觸發治理審查紅旗並產生 Flag 記錄。

    `accepted_at` 應為 SGML 表頭之權威受理時間（美東），解讀規則見
    `sec_item_router.parse_sec_acceptance_datetime`；審查期由此起算。
    """
    flags: list[GovernanceFlagRecord] = []
    sym_upper = symbol.strip().upper()

    try:
        base_time = parse_sec_acceptance_datetime(accepted_at)
    except (ValueError, TypeError):
        base_time = datetime.now(timezone.utc)

    for clean_item in extract_8k_items(list(items)):
        if clean_item == "4.02":
            # 8-K Item 4.02: 財報重編/非信賴性 (CRITICAL)
            detail = {
                "item": "4.02",
                "title": "Non-Reliance on Previously Issued Financial Statements",
                "snippet": text_snippet[:500]
                if text_snippet
                else "會計財務報表重大不信賴性",
            }
            flags.append(
                create_governance_flag(
                    symbol=sym_upper,
                    source_accession=accession,
                    flag_kind="ITEM_4_02_RESTATEMENT",
                    severity="CRITICAL",
                    detail=detail,
                    review_days=review_days,
                    base_time=base_time,
                )
            )

        elif clean_item == "5.02":
            # 8-K Item 5.02: 只有內文明確指出離任 (5.02(b)) 才升為 HIGH；
            # 僅有 item code 或屬新任 / 選任 / 薪酬協議 (5.02(c)(d)(e)) 時降為 REVIEW。
            if is_item_502_departure(text_snippet):
                detail = {
                    "item": "5.02",
                    "title": "Departure of Directors or Principal Officers",
                    "snippet": text_snippet[:500],
                }
                flags.append(
                    create_governance_flag(
                        symbol=sym_upper,
                        source_accession=accession,
                        flag_kind="ITEM_5_02_OFFICER_DEPARTURE",
                        severity="HIGH",
                        detail=detail,
                        review_days=review_days,
                        base_time=base_time,
                    )
                )
            else:
                detail = {
                    "item": "5.02",
                    "title": "Directors or Officers Change (unclassified)",
                    "snippet": text_snippet[:500]
                    if text_snippet
                    else "高管 / 董事異動（僅有 item code，未能判定是否為離任，待人工複核）",
                }
                flags.append(
                    create_governance_flag(
                        symbol=sym_upper,
                        source_accession=accession,
                        flag_kind="ITEM_5_02_OFFICER_CHANGE",
                        severity="REVIEW",
                        detail=detail,
                        review_days=review_days,
                        base_time=base_time,
                    )
                )

    return flags


def evaluate_governance_status(
    symbol: str, active_flags: Sequence[GovernanceFlagRecord]
) -> GovernanceStatus:
    """根據當前生效之治理旗標綜合評估標的治理審查狀態。"""
    sym_upper = symbol.strip().upper()
    flags_list = [f for f in active_flags if f.symbol.upper() == sym_upper]

    if not flags_list:
        return GovernanceStatus(
            symbol=sym_upper,
            is_clean=True,
            max_severity=None,
            active_flags=[],
        )

    # 尋找最高嚴重等級
    max_sev: GovernanceSeverity = "INFO"
    max_order = 0
    for f in flags_list:
        order = _SEVERITY_ORDER.get(f.severity, 0)
        if order > max_order:
            max_order = order
            max_sev = f.severity

    return GovernanceStatus(
        symbol=sym_upper,
        is_clean=False,
        max_severity=max_sev,
        active_flags=flags_list,
    )
