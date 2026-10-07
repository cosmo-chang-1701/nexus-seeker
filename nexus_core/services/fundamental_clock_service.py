"""基本面每日共識快照、估值與次日候選名單服務 (Fundamental Valuation Clock Service)。

職責（規格：docs/valuation_pricing/06_revision_momentum_and_fair_value.md §3、§5.5）：
1. NYSE 交易日 19:00 ET `eps_estimate_snapshot_1900` → `EstimateSnapshotRunner`：逐檔抓取
   分析師 EPS 共識並寫入當日快照（eps_estimate_snapshot，帶財期），供修正動能以財期配對 t-30d。
2. NYSE 交易日 20:00 ET `fundamental_watch_candidate_2000` → `ValuationJobRunner`：快照刷新若仍在
   執行先等待其完成，再逐檔治理閘門 → DCF / Comps 估值 → 修正動能與 PEAD → 次日候選排名。
3. 兩個執行器皆為背景 `asyncio.Task`（不阻塞 5 分鐘時鐘輪詢；Finnhub 背景限流等待只卡住自己）、
   上一輪未完成時略過（防重疊）、觸發時與每檔開始前複檢 leader 與 `is_memory_safe()`，
   單一標的例外隔離。
4. 綜合評估分 = 動能 40% + 安全邊際 40% + PEAD 共振 20 分。只入庫、不推播
   （docs 未規範推播），供 `/fa` 與 09:00 盤前簡報讀取。

治理規則（docs §5.3）：
- CRITICAL（8-K 4.02 等）→ EXCLUDED，不估值。
- HIGH → 不排除、分數不加減，但不可晉升 CANDIDATE（上限 WATCH），並壓制深度價值判定。
- REVIEW / INFO（含 8-K 5.02 預設 REVIEW）→ 不影響評分，只在 reasons 標註。
- 無有效公允價值（method == NONE，如 ETF）→ 不可晉升 CANDIDATE。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from database.fundamental_pipeline import (
    get_active_governance_flags,
    save_watch_candidates,
)
from market_analysis.fundamental_pipeline.governance_gate import (
    evaluate_governance_status,
)
from market_analysis.fundamental_pipeline.models import (
    FairValueResult,
    RevisionMomentumResult,
    WatchCandidateRecord,
    WatchCandidateStatus,
)
from services.fundamental_universe import get_fundamental_universe
from services.llm_service import is_memory_safe
from services.valuation_service import ValuationService

logger = logging.getLogger(__name__)
_ET_ZONE = ZoneInfo("America/New_York")

# 綜合評估分權重（docs §3 流程圖：動能 40% + MOS 40% + PEAD 20 分）
REVISION_WEIGHT: float = 0.40
MOS_WEIGHT: float = 0.40
PEAD_BONUS: float = 20.0
MAX_CANDIDATES: int = 5
# 20:00 估值等待 19:00 快照刷新完成的上限；逾時則以既有最新快照（as_of 以前）估值
SNAPSHOT_WAIT_TIMEOUT_SECONDS: float = 3600.0


def _has_valid_fair_value(fv: FairValueResult) -> bool:
    return (
        fv.method != "NONE"
        and fv.fair_value is not None
        and fv.fair_value > 0
        and fv.margin_of_safety is not None
    )


def composite_score(fv: FairValueResult, rev: RevisionMomentumResult) -> float:
    """綜合評估分；無效的動能或安全邊際不計入（貢獻為 0，不以哨兵值參與）。"""
    rev_term = rev.score_30d * REVISION_WEIGHT if rev.score_30d is not None else 0.0
    mos_term = 0.0
    if _has_valid_fair_value(fv) and fv.margin_of_safety is not None:
        mos_term = max(-100.0, min(100.0, fv.margin_of_safety * 100.0)) * MOS_WEIGHT
    pead_term = PEAD_BONUS if rev.is_pead_aligned else 0.0
    return rev_term + mos_term + pead_term


async def refresh_universe_estimate_snapshots(
    valuation_service: ValuationService | None = None,
    memory_check: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """逐檔刷新基本面標的池的當日分析師 EPS 共識快照。

    單一標的例外只計入 failed；`memory_check` 回傳 False 時中止（aborted = 1），
    已寫入的快照保留。快照日為美東當日（由 ConsensusProvider 決定）。
    """
    stats = {"symbols": 0, "refreshed": 0, "empty": 0, "failed": 0, "aborted": 0}
    val_service = (
        valuation_service if valuation_service is not None else ValuationService()
    )
    check_memory = memory_check if memory_check is not None else is_memory_safe
    universe = await get_fundamental_universe()
    stats["symbols"] = len(universe)
    for sym in universe:
        sym_upper = sym.strip().upper()
        if not check_memory():
            stats["aborted"] = 1
            logger.warning(
                f"[FundamentalClockService] 記憶體使用率超標（或已非 leader），於 {sym_upper} 前"
                "中止共識快照刷新"
            )
            break
        try:
            rows = await val_service.refresh_estimate_snapshots(sym_upper)
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            logger.warning(
                f"[FundamentalClockService] {sym_upper} 共識快照刷新失敗: {e!r}"
            )
            continue
        if rows:
            stats["refreshed"] += 1
        else:
            stats["empty"] += 1
    return stats


async def generate_and_save_watch_candidates(
    as_of_date: date | None = None,
    valuation_service: ValuationService | None = None,
    refresh_snapshots: bool = False,
    memory_check: Callable[[], bool] | None = None,
) -> list[WatchCandidateRecord]:
    """逐檔估值並持久化次日基本面候選名單。

    - 共識快照預設由 19:00 `eps_estimate_snapshot_1900` 先行刷新；`refresh_snapshots=True`
      時改為逐檔先刷新再估值（手動補跑用）。
    - `memory_check`（預設 `is_memory_safe`）於每檔開始前呼叫；回傳 False 時中止並回傳空清單
      （已完成標的的估值仍已入庫，但不寫不完整的候選排名）。
    """
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

    check_memory = memory_check if memory_check is not None else is_memory_safe
    scored: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    failed = 0

    for sym in universe:
        sym_upper = sym.strip().upper()
        if not check_memory():
            logger.warning(
                f"[FundamentalClockService] 記憶體使用率超標（或已非 leader），於 {sym_upper} 前"
                "中止本輪估值，不寫入候選名單"
            )
            aborted: list[WatchCandidateRecord] = []
            return aborted
        try:
            # 1. 公司治理審查：只有 CRITICAL 排除（docs §5.3）
            flags = await asyncio.to_thread(get_active_governance_flags, sym_upper)
            gov_status = evaluate_governance_status(sym_upper, flags)
            max_sev = gov_status.max_severity
            if max_sev == "CRITICAL":
                kinds = sorted({f.flag_kind for f in gov_status.active_flags})
                excluded.append(
                    {
                        "symbol": sym_upper,
                        "excluded_reason": (
                            "CRITICAL_GOVERNANCE_FLAG: 觸發重大治理風控審查"
                            f"（{'、'.join(kinds)}）"
                        ),
                        "reasons": {
                            "governance_clean": False,
                            "governance_max_severity": max_sev,
                            "governance_flag_kinds": kinds,
                        },
                    }
                )
                continue

            # 2. 刷新當日共識快照（失敗不影響估值，沿用既有快照）
            if refresh_snapshots:
                try:
                    await val_service.refresh_estimate_snapshots(sym_upper)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[FundamentalClockService] {sym_upper} 共識快照刷新失敗，沿用既有快照: {e!r}"
                    )

            # 3. 估值與動能計算
            fv_res, rev_res = await val_service.compute_and_save_valuation(
                sym_upper, as_of_date=target_date
            )
            valid_fv = _has_valid_fair_value(fv_res)
            score = composite_score(fv_res, rev_res)
            reasons: dict[str, Any] = {
                "fair_value": fv_res.fair_value,
                "margin_of_safety": (
                    round(fv_res.margin_of_safety, 4)
                    if fv_res.margin_of_safety is not None
                    else None
                ),
                "valuation_method": fv_res.method,
                "valuation_flags": fv_res.flags,
                "revision_score": rev_res.score_30d,
                "revision_flags": rev_res.details.get("flags", []),
                "pead_aligned": rev_res.is_pead_aligned,
                "governance_clean": gov_status.is_clean,
                "governance_max_severity": max_sev,
                "composite_score": round(score, 2),
            }
            if max_sev == "HIGH":
                reasons["governance_note"] = "HIGH 治理旗標生效中：不晉升 CANDIDATE"
            elif max_sev in ("REVIEW", "INFO"):
                reasons["governance_note"] = f"{max_sev} 治理旗標待人工複核：不影響評分"
            if not valid_fv:
                reasons["valuation_note"] = (
                    "無有效公允價值（如 ETF 或資料不足）：不晉升 CANDIDATE"
                )
            scored.append(
                {
                    "symbol": sym_upper,
                    "composite_score": score,
                    "valid_fv": valid_fv,
                    "eligible": valid_fv and max_sev != "HIGH" and score > 0.0,
                    "reasons": reasons,
                }
            )
        except Exception as e:  # noqa: BLE001
            failed += 1
            logger.error(
                f"[FundamentalClockService] 處理標的 {sym_upper} 候選評定失敗: {e!r}"
            )

    # 有效公允價值者優先，再依綜合分數降冪
    scored.sort(
        key=lambda x: (bool(x["valid_fv"]), float(x["composite_score"])), reverse=True
    )

    records: list[WatchCandidateRecord] = []
    candidate_count = 0
    rank = 1
    for item in scored:
        status_val: WatchCandidateStatus = "WATCH"
        if item["eligible"] and candidate_count < MAX_CANDIDATES:
            status_val = "CANDIDATE"
            candidate_count += 1
        records.append(
            WatchCandidateRecord(
                trading_date=date_str,
                symbol=str(item["symbol"]),
                rank=rank,
                status=status_val,
                reasons_json=json.dumps(item["reasons"], ensure_ascii=False),
                excluded_reason=None,
            )
        )
        rank += 1

    for ex in excluded:
        records.append(
            WatchCandidateRecord(
                trading_date=date_str,
                symbol=str(ex["symbol"]),
                rank=rank,
                status="EXCLUDED",
                reasons_json=json.dumps(ex["reasons"], ensure_ascii=False),
                excluded_reason=str(ex["excluded_reason"]),
            )
        )
        rank += 1

    if records:
        await save_watch_candidates(records)
        logger.info(
            f"[FundamentalClockService] 次日基本面候選名單生成完成 ({date_str}): "
            f"共 {len(records)} 檔 (CANDIDATE: {candidate_count}, "
            f"WATCH: {sum(1 for r in records if r.status == 'WATCH')}, "
            f"EXCLUDED: {len(excluded)}, 失敗: {failed})"
        )

    return records


class _BackgroundJobRunner:
    """背景排程執行器基底：單一實例、不重疊、leader-only、記憶體閘門。

    - `trigger` 由 ClockJob 呼叫：確認 leader、未重疊與記憶體後，以 `asyncio.Task`
      在背景執行 `_run` 並立即返回（有啟動回傳 True）。
    - `_should_continue` 供長時間執行時逐檔複檢 leader 與 `is_memory_safe()`。
    """

    _label: str = "背景工作"
    _task_name: str = "fundamental_background_job"

    def __init__(self, bot: Any | None = None) -> None:
        self._bot = bot
        self._task: asyncio.Task[None] | None = None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _is_leader(self) -> bool:
        return self._bot is None or bool(
            getattr(self._bot, "_is_leader_instance", False)
        )

    def _should_continue(self) -> bool:
        return self._is_leader() and is_memory_safe()

    async def trigger(self, now_et: datetime) -> bool:
        """啟動一輪背景工作；有啟動回傳 True，略過回傳 False。"""
        if not self._is_leader():
            return False
        if self.is_running():
            logger.info(
                f"[FundamentalClockService] 上一輪{self._label}仍在執行，略過本輪 "
                f"({now_et:%Y-%m-%d %H:%M} ET)"
            )
            return False
        if not is_memory_safe():
            logger.warning(
                f"[FundamentalClockService] VPS 記憶體使用率超標，略過本輪{self._label}"
            )
            return False
        self._task = asyncio.create_task(
            self._guarded_run(now_et), name=self._task_name
        )
        return True

    async def _guarded_run(self, now_et: datetime) -> None:
        started = asyncio.get_running_loop().time()
        try:
            summary = await self._run(now_et)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                f"[FundamentalClockService] {self._label}失敗 ({now_et:%Y-%m-%d %H:%M} ET)"
            )
            return
        elapsed = asyncio.get_running_loop().time() - started
        logger.info(
            f"[FundamentalClockService] {self._label}結束 ({now_et:%Y-%m-%d %H:%M} ET)，"
            f"耗時 {elapsed:.1f}s：{summary}"
        )

    async def _run(self, now_et: datetime) -> str:
        raise NotImplementedError

    async def wait_until_idle(self, timeout: float) -> bool:
        """等待進行中的一輪結束（不取消它）；逾時回傳 False，閒置或已結束回傳 True。"""
        task = self._task
        if task is None or task.done():
            return True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            return False
        except asyncio.CancelledError:
            if task.cancelled():
                return True
            raise
        except Exception:  # noqa: BLE001 — 例外已由 _guarded_run 記錄
            return True
        return True

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()


class EstimateSnapshotRunner(_BackgroundJobRunner):
    """19:00 每日分析師 EPS 共識快照刷新（`eps_estimate_snapshot_1900`）。"""

    _label = "共識快照刷新"
    _task_name = "fundamental_eps_estimate_snapshot"

    def __init__(
        self, bot: Any | None = None, service: ValuationService | None = None
    ) -> None:
        super().__init__(bot)
        self._service = service

    def _get_service(self) -> ValuationService:
        if self._service is None:
            self._service = ValuationService()
        return self._service

    async def _run(self, now_et: datetime) -> str:
        stats = await refresh_universe_estimate_snapshots(
            valuation_service=self._get_service(),
            memory_check=self._should_continue,
        )
        return str(stats)


class ValuationJobRunner(_BackgroundJobRunner):
    """20:00 估值與次日候選名單（`fundamental_watch_candidate_2000`）。

    - 19:00 快照刷新仍在執行時先等待（上限 SNAPSHOT_WAIT_TIMEOUT_SECONDS），確保修正動能
      用到當日快照；逾時則以 as_of 以前最新快照估值。
    - 跨輪重用同一個 ValuationService（同業清單快取跨日有效）。
    """

    _label = "估值與候選名單"
    _task_name = "fundamental_valuation_watch_candidates"

    def __init__(
        self,
        bot: Any | None = None,
        service: ValuationService | None = None,
        snapshot_runner: EstimateSnapshotRunner | None = None,
    ) -> None:
        super().__init__(bot)
        self._service = service
        self._snapshot_runner = snapshot_runner

    def _get_service(self) -> ValuationService:
        if self._service is None:
            self._service = ValuationService()
        return self._service

    async def _run(self, now_et: datetime) -> str:
        snap = self._snapshot_runner
        if snap is not None and snap.is_running():
            logger.info(
                "[FundamentalClockService] 共識快照刷新仍在執行，估值等待其完成後開始"
            )
            if not await snap.wait_until_idle(SNAPSHOT_WAIT_TIMEOUT_SECONDS):
                logger.warning(
                    "[FundamentalClockService] 等待共識快照刷新逾時，改以既有最新快照估值"
                )
        records = await generate_and_save_watch_candidates(
            as_of_date=now_et.date(),
            valuation_service=self._get_service(),
            memory_check=self._should_continue,
        )
        return f"寫入 {len(records)} 筆候選名單"
