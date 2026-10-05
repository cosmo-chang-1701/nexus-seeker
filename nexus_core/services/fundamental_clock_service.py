"""基本面次日候選名單生成與事件時鐘服務 (Fundamental Clock Service)。

職責：
1. 於盤後 (18:00–22:00 ET，定錨平日 20:00 ET) 遍歷全域基本面標的池。
2. 評定公司治理閘門、內在公允價值安全邊際 (MOS) 與分析師修正動能。
3. 計算多因子綜合評估分，產出次日基本面候選排名 (fundamental_watch_candidate)。
4. 註冊至全域事件時鐘 (ClockJobRegistry)。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from database.fundamental_pipeline import (
    get_active_governance_flags,
    save_watch_candidates,
)
from market_analysis.fundamental_pipeline.event_clock import (
    ClockJob,
    ClockJobRegistry,
    weekday_at,
)
from market_analysis.fundamental_pipeline.governance_gate import (
    evaluate_governance_status,
)
from market_analysis.fundamental_pipeline.models import (
    WatchCandidateRecord,
    WatchCandidateStatus,
)
from services.fundamental_universe import get_fundamental_universe
from services.valuation_service import ValuationService

logger = logging.getLogger(__name__)
_ET_ZONE = ZoneInfo("America/New_York")


async def generate_and_save_watch_candidates(
    as_of_date: date | None = None,
    valuation_service: ValuationService | None = None,
) -> list[WatchCandidateRecord]:
    """生成並持久化次日基本面候選名單。"""
    now_et = datetime.now(timezone.utc).astimezone(_ET_ZONE)
    target_date = as_of_date if as_of_date is not None else now_et.date()
    date_str = target_date.isoformat()

    val_service = (
        valuation_service if valuation_service is not None else ValuationService()
    )

    universe = await get_fundamental_universe()
    if not universe:
        logger.warning("[FundamentalClockService] 基本面標的池為空，略過候選名單生成")
        empty_list: list[WatchCandidateRecord] = []
        return empty_list

    scored_candidates: list[dict[str, Any]] = []
    excluded_candidates: list[dict[str, Any]] = []

    for sym in universe:
        sym_upper = sym.strip().upper()
        try:
            # 1. 公司治理審查
            flags = await asyncio.to_thread(get_active_governance_flags, sym_upper)
            gov_status = evaluate_governance_status(sym_upper, flags)

            # 若觸發 CRITICAL 級別重大治理紅旗，直接列入排除名單
            if not gov_status.is_clean and gov_status.max_severity == "CRITICAL":
                excluded_candidates.append(
                    {
                        "symbol": sym_upper,
                        "status": "EXCLUDED",
                        "excluded_reason": "CRITICAL_GOVERNANCE_FLAG: 觸發重大治理風控審查 (4.02/5.02)",
                        "reasons": {
                            "governance_clean": False,
                            "severity": gov_status.max_severity,
                        },
                    }
                )
                continue

            # 2. 估值與動能計算
            fv_res, rev_res = await val_service.compute_and_save_valuation(
                sym_upper, as_of_date=target_date
            )

            mos = fv_res.margin_of_safety or 0.0
            score_rev = rev_res.score_30d
            pead_bonus = 20.0 if rev_res.is_pead_aligned else 0.0
            gov_penalty = -30.0 if not gov_status.is_clean else 10.0

            # 綜合多因子評分 (動能 40% + 安全邊際 40% + PEAD 共振 20分 + 治理狀態)
            composite_score = (
                (score_rev * 0.40)
                + (max(-100.0, min(100.0, mos * 100.0)) * 0.40)
                + pead_bonus
                + gov_penalty
            )

            reasons_dict = {
                "fair_value": fv_res.fair_value,
                "margin_of_safety": round(mos, 4),
                "revision_score": round(score_rev, 2),
                "pead_aligned": rev_res.is_pead_aligned,
                "governance_clean": gov_status.is_clean,
                "composite_score": round(composite_score, 2),
                "valuation_method": fv_res.method,
            }

            scored_candidates.append(
                {
                    "symbol": sym_upper,
                    "composite_score": composite_score,
                    "reasons": reasons_dict,
                }
            )
        except Exception as e:
            logger.error(
                f"[FundamentalClockService] 處理標的 {sym_upper} 候選評定失敗: {e}"
            )

    # 依綜合分數降冪排序
    scored_candidates.sort(key=lambda x: float(x["composite_score"]), reverse=True)

    records: list[WatchCandidateRecord] = []
    current_rank = 1

    for item in scored_candidates:
        c_score = float(item["composite_score"])
        # 前 5 名且綜合分數 > 0 評為 CANDIDATE，其餘評為 WATCH
        status_val: WatchCandidateStatus = (
            "CANDIDATE" if current_rank <= 5 and c_score > 0.0 else "WATCH"
        )
        records.append(
            WatchCandidateRecord(
                trading_date=date_str,
                symbol=str(item["symbol"]),
                rank=current_rank,
                status=status_val,
                reasons_json=json.dumps(item["reasons"], ensure_ascii=False),
                excluded_reason=None,
            )
        )
        current_rank += 1

    # 追加排除標的至末尾
    for ex in excluded_candidates:
        records.append(
            WatchCandidateRecord(
                trading_date=date_str,
                symbol=str(ex["symbol"]),
                rank=current_rank,
                status="EXCLUDED",
                reasons_json=json.dumps(ex["reasons"], ensure_ascii=False),
                excluded_reason=str(ex["excluded_reason"]),
            )
        )
        current_rank += 1

    if records:
        await save_watch_candidates(records)
        logger.info(
            f"[FundamentalClockService] 次日基本面候選名單生成完成 ({date_str}): "
            f"共 {len(records)} 檔 (CANDIDATE: {sum(1 for r in records if r.status == 'CANDIDATE')}, "
            f"WATCH: {sum(1 for r in records if r.status == 'WATCH')}, "
            f"EXCLUDED: {sum(1 for r in records if r.status == 'EXCLUDED')})"
        )

    return records


async def _run_watch_candidates_job(now_et: datetime) -> None:
    """事件時鐘工作處理函式：平日 20:00 ET 執行。"""
    try:
        await generate_and_save_watch_candidates(as_of_date=now_et.date())
    except Exception:
        logger.exception("[FundamentalClockService] 排程執行次日候選名單失敗")


def register_valuation_clock_jobs() -> None:
    """向 ClockJobRegistry 註冊盤後 20:00 次日基本面候選名單工作。"""
    ClockJobRegistry.register(
        ClockJob(
            job_id="fundamental_watch_candidate_2000",
            name="20:00 基本面次日候選名單生成",
            schedule_desc="平日 20:00 ET (盤後 18:00–22:00 估值掃描窗口)",
            handler=_run_watch_candidates_job,
            is_due_fn=weekday_at(20, 0, window_minutes=30),
            priority=50,
            description="計算全景 DCF/Comps 安全邊際與分析師修正動能，產出次日基本面觀察排名",
        )
    )
