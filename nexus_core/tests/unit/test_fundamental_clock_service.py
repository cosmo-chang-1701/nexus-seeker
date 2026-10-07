"""基本面每日共識快照、估值與次日候選名單服務單元測試。"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from database.fundamental_pipeline import (
    get_eps_estimate_snapshots,
    get_latest_watch_candidates,
    get_prior_eps_estimate_snapshots,
    save_governance_flags,
)
from market_analysis.fundamental_pipeline.models import (
    EPSEstimateSnapshotRecord,
    FairValueResult,
    GovernanceFlagRecord,
    RevisionMomentumResult,
)
from services.fundamental_clock_service import (
    EstimateSnapshotRunner,
    ValuationJobRunner,
    composite_score,
    generate_and_save_watch_candidates,
    refresh_universe_estimate_snapshots,
)
from services.valuation_service import ValuationService

_SVC = "services.fundamental_clock_service"
ny_tz = ZoneInfo("America/New_York")
_WED_2000 = datetime(2026, 10, 7, 20, 0, tzinfo=ny_tz)


def _future_expiry(days: int = 30) -> str:
    """治理旗標有效期以「現在」為基準，避免寫死日期過期後測試失效。"""
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _fv(
    fair_value: float | None, mos: float | None, method: str = "BLENDED"
) -> FairValueResult:
    return FairValueResult(
        fair_value=fair_value,
        margin_of_safety=mos,
        dcf_value=fair_value,
        comps_value=fair_value,
        discount_rate=0.08,
        equity_risk_premium=0.045,
        is_deep_value=False,
        flags=[],
        method=method,
    )


def _rev(score: float | None, pead: bool = False) -> RevisionMomentumResult:
    return RevisionMomentumResult(
        score_30d=score,
        breadth_ratio=None,
        is_pead_aligned=pead,
        slopes={},
        up_count=0,
        down_count=0,
        details={"flags": []},
    )


def _service(results: dict[str, tuple[FairValueResult, RevisionMomentumResult]]) -> Any:
    svc = MagicMock()

    async def _compute(symbol: str, as_of_date: Any = None) -> Any:
        if symbol not in results:
            raise RuntimeError(f"finnhub 429 {symbol}")
        return results[symbol]

    svc.compute_and_save_valuation = AsyncMock(side_effect=_compute)
    svc.refresh_estimate_snapshots = AsyncMock(return_value=[])
    return svc


def _leader_bot() -> Any:
    return SimpleNamespace(_is_leader_instance=True)


def test_composite_score_ignores_invalid_inputs_and_has_no_governance_bonus() -> None:
    # 動能 40% + MOS 40% + PEAD 20 分；docs 未規範治理加減分
    assert composite_score(_fv(150.0, 0.30), _rev(55.0, pead=True)) == pytest.approx(
        55.0 * 0.4 + 30.0 * 0.4 + 20.0
    )
    # 無有效公允價值 / 無動能 → 該項 0，不以哨兵值參與
    assert composite_score(_fv(None, None, "NONE"), _rev(None)) == 0.0


@pytest.mark.asyncio
async def test_generate_watch_candidates_governance_and_ranking(db_conn: Any) -> None:
    """CRITICAL 排除；HIGH 不排除但上限 WATCH；ETF 無 FV 不得為 CANDIDATE；單檔例外隔離。"""
    await save_governance_flags(
        [
            GovernanceFlagRecord(
                symbol="SMCI",
                source_accession="000123-26-00001",
                flag_kind="4.02",
                severity="CRITICAL",
                detail_json="{}",
                expires_at=_future_expiry(),
            ),
            GovernanceFlagRecord(
                symbol="INTC",
                source_accession="000456-26-00002",
                flag_kind="AUDITOR_CHANGE",
                severity="HIGH",
                detail_json="{}",
                expires_at=_future_expiry(),
            ),
            GovernanceFlagRecord(
                symbol="AAPL",
                source_accession="000789-26-00003",
                flag_kind="5.02",
                severity="REVIEW",
                detail_json="{}",
                expires_at=_future_expiry(),
            ),
        ]
    )
    svc = _service(
        {
            "NVDA": (_fv(150.0, 0.30), _rev(55.0, pead=True)),
            "INTC": (_fv(40.0, 0.40), _rev(60.0, pead=True)),
            "SPY": (_fv(None, None, "NONE"), _rev(80.0)),
            "AAPL": (_fv(100.0, 0.05), _rev(10.0)),
        }
    )
    with patch(
        f"{_SVC}.get_fundamental_universe",
        AsyncMock(return_value=["NVDA", "INTC", "SPY", "AAPL", "BAD", "SMCI"]),
    ):
        records = await generate_and_save_watch_candidates(
            as_of_date=date(2026, 10, 5),
            valuation_service=svc,
            memory_check=lambda: True,
        )

    by_sym = {r.symbol: r for r in records}
    assert "BAD" not in by_sym  # 例外隔離：不寫入、不影響其他標的
    assert by_sym["SMCI"].status == "EXCLUDED"
    assert "CRITICAL" in (by_sym["SMCI"].excluded_reason or "")
    assert svc.compute_and_save_valuation.await_count == 5  # SMCI 不估值

    assert by_sym["INTC"].status == "WATCH"  # HIGH：分數最高也不晉升
    assert "HIGH" in by_sym["INTC"].reasons_json
    assert by_sym["NVDA"].status == "CANDIDATE"
    assert by_sym["AAPL"].status == "CANDIDATE"  # REVIEW 不影響
    assert by_sym["SPY"].status == "WATCH"  # 無有效 FV
    # 有效 FV 者排前，ETF 雖動能高仍排在後面
    assert by_sym["SPY"].rank > by_sym["AAPL"].rank
    assert len(get_latest_watch_candidates(10)) == 5
    svc.refresh_estimate_snapshots.assert_not_awaited()  # 快照由 19:00 工作負責


@pytest.mark.asyncio
async def test_generate_watch_candidates_aborts_on_memory_without_saving(
    db_conn: Any,
) -> None:
    svc = _service({"NVDA": (_fv(150.0, 0.30), _rev(55.0))})
    checks = iter([True, False])
    with patch(
        f"{_SVC}.get_fundamental_universe", AsyncMock(return_value=["NVDA", "AAPL"])
    ):
        records = await generate_and_save_watch_candidates(
            as_of_date=date(2026, 10, 5),
            valuation_service=svc,
            memory_check=lambda: next(checks),
        )
    assert records == []
    assert get_latest_watch_candidates(10) == []
    assert svc.compute_and_save_valuation.await_count == 1


class _DatedConsensus:
    """回傳指定快照日的假共識提供者（模擬每日 19:00 呼叫）。"""

    def __init__(self) -> None:
        self.day = "2026-09-05"
        self.fail: set[str] = set()

    async def get_consensus(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def get_estimate_snapshots(
        self, symbol: str
    ) -> list[EPSEstimateSnapshotRecord]:
        if symbol in self.fail:
            raise RuntimeError("finnhub 429")
        bump = 0.0 if self.day == "2026-09-05" else 0.2
        return [
            EPSEstimateSnapshotRecord(
                symbol=symbol,
                snapshot_date=self.day,
                horizon="0q",
                source="finnhub_calendar",
                eps_mean=1.0 + bump,
                fiscal_period="2027-Q1",
            )
        ]


@pytest.mark.asyncio
async def test_snapshot_refresh_writes_daily_snapshots_with_fiscal_period(
    db_conn: Any,
) -> None:
    """每日刷新各寫一份當日快照（不覆寫前一日），t-30 窗口查得到前一份。"""
    provider = _DatedConsensus()
    svc = ValuationService(data_provider=MagicMock(), consensus_provider=provider)
    with patch(
        f"{_SVC}.get_fundamental_universe", AsyncMock(return_value=["MSFT", "BAD"])
    ):
        provider.fail = {"BAD"}
        stats1 = await refresh_universe_estimate_snapshots(svc, lambda: True)
        provider.day = "2026-10-05"
        stats2 = await refresh_universe_estimate_snapshots(svc, lambda: True)

    assert stats1 == {
        "symbols": 2,
        "refreshed": 1,
        "empty": 0,
        "failed": 1,
        "aborted": 0,
    }
    assert stats2["refreshed"] == 1
    current = get_eps_estimate_snapshots("MSFT", None, "2026-10-05")
    assert [(s.snapshot_date, s.fiscal_period, s.eps_mean) for s in current] == [
        ("2026-10-05", "2027-Q1", pytest.approx(1.2))
    ]
    prior = get_prior_eps_estimate_snapshots("MSFT", "2026-08-31", "2026-09-07")
    assert [s.snapshot_date for s in prior] == ["2026-09-05"]


@pytest.mark.asyncio
async def test_snapshot_refresh_stops_when_memory_unsafe() -> None:
    svc = MagicMock()
    svc.refresh_estimate_snapshots = AsyncMock(return_value=[MagicMock()])
    with patch(
        f"{_SVC}.get_fundamental_universe", AsyncMock(return_value=["A", "B", "C"])
    ):
        checks = iter([True, False])
        stats = await refresh_universe_estimate_snapshots(svc, lambda: next(checks))
    assert stats["aborted"] == 1
    assert stats["refreshed"] == 1
    assert svc.refresh_estimate_snapshots.await_count == 1


@pytest.mark.asyncio
async def test_runners_skip_when_not_leader_or_memory_unsafe() -> None:
    follower = SimpleNamespace(_is_leader_instance=False)
    with (
        patch(f"{_SVC}.is_memory_safe", return_value=True),
        patch(f"{_SVC}.refresh_universe_estimate_snapshots", AsyncMock()) as refresh,
        patch(f"{_SVC}.generate_and_save_watch_candidates", AsyncMock()) as gen,
    ):
        assert await EstimateSnapshotRunner(follower).trigger(_WED_2000) is False
        assert await ValuationJobRunner(follower).trigger(_WED_2000) is False
    with (
        patch(f"{_SVC}.is_memory_safe", return_value=False),
        patch(f"{_SVC}.refresh_universe_estimate_snapshots", AsyncMock()) as refresh2,
        patch(f"{_SVC}.generate_and_save_watch_candidates", AsyncMock()) as gen2,
    ):
        assert await EstimateSnapshotRunner(_leader_bot()).trigger(_WED_2000) is False
        assert await ValuationJobRunner(_leader_bot()).trigger(_WED_2000) is False
    for mock in (refresh, gen, refresh2, gen2):
        mock.assert_not_called()


@pytest.mark.asyncio
async def test_valuation_runner_runs_in_background_and_prevents_overlap() -> None:
    """trigger 立即返回（背景 Task）；上一輪未完成時略過；逐檔閘門含 leader 與記憶體。"""
    release = asyncio.Event()
    captured: dict[str, Any] = {}

    async def _slow(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        await release.wait()
        return []

    bot = _leader_bot()
    runner = ValuationJobRunner(bot, service=MagicMock())
    with (
        patch(f"{_SVC}.is_memory_safe", return_value=True),
        patch(
            f"{_SVC}.generate_and_save_watch_candidates", AsyncMock(side_effect=_slow)
        ) as gen,
    ):
        assert await runner.trigger(_WED_2000) is True
        await asyncio.sleep(0)
        assert runner.is_running()
        assert await runner.trigger(_WED_2000) is False  # 防重疊
        assert gen.await_count == 1
        assert captured["as_of_date"] == date(2026, 10, 7)
        check = captured["memory_check"]
        assert check() is True
        bot._is_leader_instance = False
        assert check() is False  # 長時間執行中失去 leader → 下一檔前中止
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert not runner.is_running()


@pytest.mark.asyncio
async def test_valuation_runner_waits_for_snapshot_refresh() -> None:
    """20:00 估值在 19:00 快照刷新仍執行時，等待其完成後才開始。"""
    order: list[str] = []
    release = asyncio.Event()

    async def _refresh(**kwargs: Any) -> dict[str, int]:
        await release.wait()
        order.append("snapshot_done")
        return {}

    async def _generate(**kwargs: Any) -> list[Any]:
        order.append("valuation_start")
        return []

    snap = EstimateSnapshotRunner(_leader_bot(), service=MagicMock())
    val = ValuationJobRunner(_leader_bot(), service=MagicMock(), snapshot_runner=snap)
    with (
        patch(f"{_SVC}.is_memory_safe", return_value=True),
        patch(
            f"{_SVC}.refresh_universe_estimate_snapshots",
            AsyncMock(side_effect=_refresh),
        ),
        patch(
            f"{_SVC}.generate_and_save_watch_candidates",
            AsyncMock(side_effect=_generate),
        ),
    ):
        assert await snap.trigger(_WED_2000) is True
        assert await val.trigger(_WED_2000) is True
        for _ in range(5):
            await asyncio.sleep(0)
        assert order == []  # 估值仍在等待快照
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
    assert order == ["snapshot_done", "valuation_start"]


@pytest.mark.asyncio
async def test_runner_logs_exception_without_raising(caplog: Any) -> None:
    runner = EstimateSnapshotRunner(_leader_bot(), service=MagicMock())
    with (
        patch(f"{_SVC}.is_memory_safe", return_value=True),
        patch(
            f"{_SVC}.refresh_universe_estimate_snapshots",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        assert await runner.trigger(_WED_2000) is True
        assert await runner.wait_until_idle(1.0) is True
    assert "共識快照刷新失敗" in caplog.text
    assert not runner.is_running()
