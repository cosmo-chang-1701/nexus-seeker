"""公司治理審查閘門（Governance Review Gate）。

依據 SEC 8-K 重大申報與法規監管日誌評估標的治理風險：
- 8-K Item 4.02: 財報重大重編 / 審計意見不可信賴性 -> 🔴 CRITICAL (風控審查 30 天)
- 8-K Item 5.02: 核心高管/董事非正常解職離任 -> 🟠 HIGH (風控審查 30 天)
- 計算綜合治理狀態 (GovernanceStatus) 與最高風險等級。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Mapping, Sequence

from market_analysis.fundamental_pipeline.models import (
    GovernanceFlagRecord,
    GovernanceSeverity,
    GovernanceStatus,
)

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
    expires_dt = base_time + timedelta(days=review_days)
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
    """從 8-K Items 中檢測是否觸發治理審查紅旗並產生 Flag 記錄。"""
    flags: list[GovernanceFlagRecord] = []
    sym_upper = symbol.strip().upper()

    if isinstance(accepted_at, str):
        try:
            norm_str = accepted_at.strip().replace("Z", "+00:00")
            base_time = datetime.fromisoformat(norm_str)
        except Exception:
            base_time = datetime.now(timezone.utc)
    else:
        base_time = accepted_at

    for item in items:
        clean_item = item.strip()
        if "4.02" in clean_item:
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

        elif "5.02" in clean_item:
            # 8-K Item 5.02: 高管/董事離任或變動 (HIGH)
            detail = {
                "item": "5.02",
                "title": "Departure of Directors or Principal Officers",
                "snippet": text_snippet[:500]
                if text_snippet
                else "核心管理層/董事變更或離職",
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
