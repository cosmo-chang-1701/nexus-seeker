"""估值與修正動能服務單元測試。

Fixture 使用 Finnhub `/stock/metric?metric=all` 的真實鍵名（2026-10 實測 MSFT / AAPL 回 133 個鍵，
沒有 `fcfPerShareTTM`；每股 FCF 以 `現價 / pfcfShareTTM` 推得）。數值取自 MSFT 實測回應。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from database.fundamental_pipeline import (
    get_eps_estimate_snapshots,
    get_fair_value,
    get_latest_fair_value,
    get_latest_processed_earnings_surprise,
    get_latest_revision_score,
    get_prior_eps_estimate_snapshots,
    save_earnings_surprises,
    save_eps_estimate_snapshots,
    save_governance_flags,
    save_liquidity_regime,
)
from market_analysis.fundamental_pipeline.fair_value import (
    FLAG_BETA_DEFAULT,
    FLAG_FCF_UNAVAILABLE,
    FLAG_FORWARD_EPS_TRAILING,
    FLAG_G_EST_DEFAULT,
    FLAG_G_EST_HISTORICAL,
    FLAG_GOVERNANCE_RISK,
    FLAG_NFCI_MISSING,
    FLAG_PEER_PE_TRAILING,
    FLAG_US10Y_DEFAULT,
)
from market_analysis.fundamental_pipeline.models import (
    EarningsSurpriseDTO,
    EPSEstimateSnapshotRecord,
    GovernanceFlagRecord,
    LiquidityReading,
)
from services.valuation_service import (
    ValuationService,
    derive_fcf_per_share,
    derive_forward_eps,
    derive_growth_estimate,
    peer_pe,
)

# MSFT 實測（2026-10-07）：quote c = 527.98
MSFT_SPOT = 527.98
MSFT_METRICS: dict[str, Any] = {
    "pfcfShareTTM": 58.911,
    "pfcfShareAnnual": 58.911,
    "cashFlowPerShareTTM": 12.3339,  # 營運現金流口徑，不是 FCF，不得使用
    "forwardPE": 24.19122,
    "epsTTM": 17.9466,
    "epsExclExtraItemsTTM": 17.9466,
    "beta": 1.0602406,
    "epsGrowthTTMYoy": 31.56,
    "epsGrowth3Y": 22.83,
    "revenueGrowthTTMYoy": 17.79,
    "peTTM": 29.505,
}
# SPY 實測只回 19 個鍵，估值相關僅 beta
ETF_METRICS: dict[str, Any] = {"beta": 1.0172414}


def _future_expiry(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


class FakeProvider:
    def __init__(
        self,
        metrics: dict[str, Any],
        spot: float | None = MSFT_SPOT,
        peer_metrics: dict[str, Any] | None = None,
        peers: list[str] | None = None,
    ) -> None:
        self.metrics = metrics
        self.spot = spot
        self.peer_metrics = (
            peer_metrics if peer_metrics is not None else {"forwardPE": 26.0}
        )
        self.peers = peers if peers is not None else ["AAPL", "GOOGL", "ORCL", "AMZN"]

    async def get_company_metrics(self, symbol: str) -> dict[str, Any]:
        if symbol in self.peers:
            return self.peer_metrics
        return self.metrics

    async def get_peers(self, symbol: str) -> list[str]:
        return self.peers

    async def get_spot_price(self, symbol: str) -> float | None:
        return self.spot


async def _save_liquidity() -> None:
    await save_liquidity_regime(
        LiquidityReading(
            trading_date=date(2026, 10, 5),
            nfci=-0.25,
            anfci=-0.20,
            net_liquidity_bn=6200.0,
            net_liquidity_chg_13w_pct=2.5,
            reserves_chg_13w_pct=1.8,
            us10y=4.15,
            regime="EASY",
            equity_risk_premium=0.0425,
        )
    )


def test_derivations_use_real_finnhub_keys() -> None:
    fcf = derive_fcf_per_share(MSFT_METRICS, MSFT_SPOT)
    assert fcf == pytest.approx(MSFT_SPOT / 58.911)  # ≈ 8.96，非 cashFlowPerShareTTM
    fwd = derive_forward_eps(MSFT_METRICS, MSFT_SPOT)
    assert fwd is not None
    assert fwd == pytest.approx(MSFT_SPOT / 24.19122)
    g, flag = derive_growth_estimate(MSFT_METRICS, fwd)
    assert flag is None
    assert g == pytest.approx(fwd / 17.9466 - 1.0)
    assert peer_pe({"forwardPE": 26.0}) == (26.0, False)
    assert peer_pe({"peTTM": 30.0}) == (30.0, True)
    assert peer_pe({}) == (None, False)


def test_derivations_handle_missing_and_zero() -> None:
    assert derive_fcf_per_share({}, MSFT_SPOT) is None
    assert derive_fcf_per_share({"pfcfShareTTM": 0}, MSFT_SPOT) is None
    assert derive_fcf_per_share(MSFT_METRICS, 0.0) is None
    # P/FCF 為負 → FCF 為負（由 DCF 判定 FCF_NON_POSITIVE）
    neg = derive_fcf_per_share({"pfcfShareTTM": -20.0}, 100.0)
    assert neg == pytest.approx(-5.0)
    # 歷史成長率 0.0 是有效值（is not None），不得被吞掉退回預設
    g, flag = derive_growth_estimate({"epsGrowthTTMYoy": 0.0}, None)
    assert (g, flag) == (0.0, FLAG_G_EST_HISTORICAL)
    g_def, flag_def = derive_growth_estimate({}, None)
    assert flag_def == FLAG_G_EST_DEFAULT
    assert g_def == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_compute_and_save_blended_with_real_keys(db_conn: Any) -> None:
    await _save_liquidity()
    service = ValuationService(data_provider=FakeProvider(MSFT_METRICS))

    fv_res, rev_res = await service.compute_and_save_valuation(
        symbol="MSFT", as_of_date=date(2026, 10, 5)
    )

    assert fv_res.method == "BLENDED"
    assert fv_res.dcf_value is not None and fv_res.dcf_value > 0
    expected_comps = (MSFT_SPOT / 24.19122) * 26.0
    assert fv_res.comps_value is not None
    assert fv_res.comps_value > expected_comps  # NFCI < 0 → 流動性溢價 > 1
    for flag in (
        FLAG_US10Y_DEFAULT,
        FLAG_NFCI_MISSING,
        FLAG_BETA_DEFAULT,
        FLAG_G_EST_DEFAULT,
        FLAG_FCF_UNAVAILABLE,
        FLAG_FORWARD_EPS_TRAILING,
    ):
        assert flag not in fv_res.flags
    assert rev_res.score_30d is None  # 尚無快照

    saved = get_latest_fair_value("MSFT")
    assert saved is not None
    assert saved.method == "BLENDED"
    assert saved.spot_price == MSFT_SPOT
    assert saved.fair_value == fv_res.fair_value
    saved_rev = get_latest_revision_score("MSFT")
    assert saved_rev is not None
    assert saved_rev.score_30d is None


@pytest.mark.asyncio
async def test_etf_without_fundamentals_saves_null_not_zero(db_conn: Any) -> None:
    await _save_liquidity()
    service = ValuationService(
        data_provider=FakeProvider(ETF_METRICS, spot=570.0, peers=[])
    )
    fv_res, _ = await service.compute_and_save_valuation(
        symbol="SPY", as_of_date=date(2026, 10, 5)
    )
    assert fv_res.method == "NONE"
    assert fv_res.fair_value is None
    assert fv_res.margin_of_safety is None
    assert FLAG_FCF_UNAVAILABLE in fv_res.flags

    saved = get_fair_value("SPY", "2026-10-05")
    assert saved is not None
    assert saved.fair_value is None
    assert saved.margin_of_safety is None
    assert saved.method == "NONE"


@pytest.mark.asyncio
async def test_default_inputs_are_flagged(db_conn: Any) -> None:
    """無流動性讀數 → 10 年債套用 4.25% 預設；無 forwardPE → 近四季 EPS；同業退回 trailing PE。"""
    metrics = {
        "pfcfShareTTM": 25.0,
        "epsTTM": 4.0,
        "epsGrowth3Y": 12.0,
    }
    service = ValuationService(
        data_provider=FakeProvider(
            metrics, spot=100.0, peer_metrics={"peTTM": 20.0}, peers=["P1", "P2", "P3"]
        )
    )
    fv_res, _ = await service.compute_and_save_valuation(
        symbol="TEST", as_of_date=date(2026, 10, 5)
    )
    for flag in (
        FLAG_US10Y_DEFAULT,
        FLAG_NFCI_MISSING,
        FLAG_BETA_DEFAULT,
        FLAG_FORWARD_EPS_TRAILING,
        FLAG_G_EST_HISTORICAL,
        FLAG_PEER_PE_TRAILING,
    ):
        assert flag in fv_res.flags, flag
    assert fv_res.comps_value == pytest.approx(4.0 * 20.0)

    saved = get_fair_value("TEST", "2026-10-05")
    assert saved is not None
    assert FLAG_US10Y_DEFAULT in json.loads(saved.flags_json)


@pytest.mark.asyncio
async def test_high_governance_suppresses_deep_value_but_review_does_not(
    db_conn: Any,
) -> None:
    await _save_liquidity()
    await save_governance_flags(
        [
            GovernanceFlagRecord(
                symbol="HIGHCO",
                source_accession="0001-26-000001",
                flag_kind="AUDITOR_CHANGE",
                severity="HIGH",
                detail_json="{}",
                expires_at=_future_expiry(),
            ),
            GovernanceFlagRecord(
                symbol="REVCO",
                source_accession="0001-26-000002",
                flag_kind="5.02",
                severity="REVIEW",
                detail_json="{}",
                expires_at=_future_expiry(),
            ),
        ]
    )
    # 低 P/FCF、低前瞻本益比（相對同業 26 倍）→ 深度折價；MOS 只取決於倍數，與現價水準無關
    cheap_metrics = {
        "pfcfShareTTM": 8.0,
        "forwardPE": 8.0,
        "epsTTM": 17.86,
        "beta": 1.06,
    }
    cheap = FakeProvider(cheap_metrics, spot=150.0)
    service = ValuationService(data_provider=cheap)

    fv_high, _ = await service.compute_and_save_valuation(
        "HIGHCO", as_of_date=date(2026, 10, 5)
    )
    assert fv_high.is_deep_value is False
    assert FLAG_GOVERNANCE_RISK in fv_high.flags
    assert "DEEP_VALUE_SUPPRESSED_BY_GOVERNANCE" in fv_high.flags

    fv_review, _ = await service.compute_and_save_valuation(
        "REVCO", as_of_date=date(2026, 10, 5)
    )
    assert fv_review.is_deep_value is True
    assert FLAG_GOVERNANCE_RISK not in fv_review.flags


def _snapshot(
    day: str, horizon: str, eps: float, period: str
) -> EPSEstimateSnapshotRecord:
    return EPSEstimateSnapshotRecord(
        symbol="MSFT",
        snapshot_date=day,
        horizon=horizon,  # type: ignore[arg-type]
        source="finnhub_calendar",
        eps_mean=eps,
        fiscal_period=period,
    )


@pytest.mark.asyncio
async def test_snapshot_queries_respect_as_of_and_prior_window(db_conn: Any) -> None:
    await save_eps_estimate_snapshots(
        [
            _snapshot("2026-08-20", "0q", 3.00, "2027-Q1"),  # 窗口外（t-46）
            _snapshot("2026-09-05", "0q", 3.20, "2027-Q1"),  # t-30
            _snapshot("2026-10-05", "0q", 3.52, "2027-Q1"),  # t
            _snapshot("2026-10-06", "0q", 9.99, "2027-Q1"),  # 晚於 as_of
        ]
    )
    curr = get_eps_estimate_snapshots("MSFT", None, "2026-10-05")
    assert [s.snapshot_date for s in curr] == ["2026-10-05"]
    assert curr[0].fiscal_period == "2027-Q1"
    assert get_eps_estimate_snapshots("MSFT")[0].snapshot_date == "2026-10-06"
    prior = get_prior_eps_estimate_snapshots("MSFT", "2026-08-31", "2026-09-07")
    assert [s.snapshot_date for s in prior] == ["2026-09-05"]


@pytest.mark.asyncio
async def test_revision_uses_fiscal_period_pairing_and_trading_day_pead(
    db_conn: Any,
) -> None:
    """t 與 t-30 同財期配對（跨季換期）；PEAD 只取 PROCESSED 並以 NYSE 交易日計算。"""
    await _save_liquidity()
    await save_eps_estimate_snapshots(
        [
            _snapshot("2026-09-05", "0q", 3.00, "2027-Q1"),
            _snapshot("2026-09-05", "+1q", 3.00, "2027-Q2"),
            _snapshot("2026-10-05", "0q", 3.60, "2027-Q2"),  # 換季後 0q = 舊 +1q
            _snapshot("2026-10-05", "+1q", 3.90, "2027-Q3"),
        ]
    )
    await save_earnings_surprises(
        [
            EarningsSurpriseDTO(
                symbol="MSFT",
                fiscal_period="2027-Q1",
                composite_score=40.0,
                status="PROCESSED",
                announced_on="2026-09-21",  # 週一
            ),
            EarningsSurpriseDTO(
                symbol="MSFT",
                fiscal_period="2027-Q2",
                composite_score=None,
                status="PENDING",
                announced_on="2026-10-02",
            ),
        ]
    )
    latest = get_latest_processed_earnings_surprise("MSFT", "2026-10-05")
    assert latest is not None
    assert latest.fiscal_period == "2027-Q1"

    service = ValuationService(data_provider=FakeProvider(MSFT_METRICS))
    _, rev = await service.compute_and_save_valuation(
        "MSFT", as_of_date=date(2026, 10, 5)
    )
    assert set(rev.slopes) == {"0q"}
    assert rev.slopes["0q"] == pytest.approx(0.20)
    assert rev.score_30d == pytest.approx(100.0)
    # 2026-09-21（週一）之後到 2026-10-05：9/22–10/5 共 10 個交易日
    assert rev.details["days_since_surprise"] == 10
    assert rev.is_pead_aligned is True
    assert rev.details["current_snapshot_date"] == "2026-10-05"
