"""基本面次日候選名單與事件時鐘服務單元測試。"""

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from database.fundamental_pipeline import (
    get_latest_watch_candidates,
    save_governance_flags,
)
from market_analysis.fundamental_pipeline.event_clock import ClockJobRegistry
from market_analysis.fundamental_pipeline.models import (
    FairValueResult,
    GovernanceFlagRecord,
    RevisionMomentumResult,
)
from services.fundamental_clock_service import (
    generate_and_save_watch_candidates,
    register_valuation_clock_jobs,
)


@pytest.mark.asyncio
async def test_generate_and_save_watch_candidates(db_conn: Any) -> None:
    # 建立一筆 CRITICAL 治理紅旗標的 SMCI
    crit_flag = GovernanceFlagRecord(
        symbol="SMCI",
        source_accession="000123-26-00001",
        flag_kind="4.02",
        severity="CRITICAL",
        detail_json="{}",
        expires_at="2026-11-05",
    )
    await save_governance_flags([crit_flag])

    mock_val_service = AsyncMock()

    async def mock_compute(symbol: str, as_of_date: Any = None) -> tuple[Any, Any]:
        if symbol == "NVDA":
            fv = FairValueResult(
                fair_value=150.0,
                margin_of_safety=0.30,
                dcf_value=155.0,
                comps_value=145.0,
                discount_rate=0.08,
                equity_risk_premium=0.045,
                is_deep_value=True,
                flags=["DEEP_VALUE"],
                method="BLENDED",
            )
            rev = RevisionMomentumResult(
                score_30d=55.0,
                breadth_ratio=0.8,
                is_pead_aligned=True,
                slopes={},
                up_count=4,
                down_count=0,
                details={},
            )
            return fv, rev
        else:
            fv = FairValueResult(
                fair_value=100.0,
                margin_of_safety=0.05,
                dcf_value=100.0,
                comps_value=100.0,
                discount_rate=0.08,
                equity_risk_premium=0.045,
                is_deep_value=False,
                flags=["FAIRLY_VALUED"],
                method="BLENDED",
            )
            rev = RevisionMomentumResult(
                score_30d=10.0,
                breadth_ratio=0.0,
                is_pead_aligned=False,
                slopes={},
                up_count=2,
                down_count=2,
                details={},
            )
            return fv, rev

    mock_val_service.compute_and_save_valuation.side_effect = mock_compute

    with patch(
        "services.fundamental_clock_service.get_fundamental_universe",
        AsyncMock(return_value=["NVDA", "AAPL", "SMCI"]),
    ):
        candidates = await generate_and_save_watch_candidates(
            as_of_date=date(2026, 10, 5),
            valuation_service=mock_val_service,
        )

        assert len(candidates) == 3

        # NVDA 應該是第 1 名 CANDIDATE
        c_nvda = next(c for c in candidates if c.symbol == "NVDA")
        assert c_nvda.rank == 1
        assert c_nvda.status == "CANDIDATE"

        # AAPL 應該是正常候選
        c_aapl = next(c for c in candidates if c.symbol == "AAPL")
        assert c_aapl.status in ("CANDIDATE", "WATCH")

        # SMCI 觸發 CRITICAL 應該列入 EXCLUDED
        c_smci = next(c for c in candidates if c.symbol == "SMCI")
        assert c_smci.status == "EXCLUDED"
        assert c_smci.excluded_reason is not None
        assert "CRITICAL" in c_smci.excluded_reason

        # 驗證資料庫持久化讀取
        latest_cands = get_latest_watch_candidates(10)
        assert len(latest_cands) == 3


def test_register_valuation_clock_jobs() -> None:
    register_valuation_clock_jobs()
    job = ClockJobRegistry.get("fundamental_watch_candidate_2000")
    assert job is not None
    assert job.job_id == "fundamental_watch_candidate_2000"
    assert job.priority == 50
