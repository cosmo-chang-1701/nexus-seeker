"""估值與修正動能服務單元測試。

Fixture 使用 Finnhub `/stock/metric?metric=all` 的真實鍵名（2026-10 實測 MSFT / AAPL 回 133 個鍵，
沒有 `fcfPerShareTTM`；每股 FCF 以 `現價 / pfcfShareTTM` 推得）。數值取自 MSFT 實測回應；唯 revenueGrowth5Y／revenueGrowth3Y 兩鍵為示意值，非實測。
"""

from __future__ import annotations

import json
import math
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
    FLAG_CYCLICAL_GROWTH_CAP,
    FLAG_G_EST_DEFAULT,
    FLAG_G_EST_FORWARD,
    FLAG_GOVERNANCE_RISK,
    FLAG_NFCI_MISSING,
    FLAG_PEER_PE_TRAILING,
    FLAG_US10Y_DEFAULT,
    calculate_cost_of_equity,
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
    "revenueGrowth5Y": 14.5,  # 示意值，非實測
    "revenueGrowth3Y": 12.0,  # 示意值，非實測
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
    assert g == pytest.approx(0.145)  # 營收 5Y 優先，不再用前瞻／TTM EPS 比值
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
    # 營收成長率 0.0 是有效值（is not None），不得被吞掉退回預設
    g, flag = derive_growth_estimate({"revenueGrowth5Y": 0.0}, None)
    assert (g, flag) == (0.0, None)
    # 舊版 EPS 歷史成長鍵不再使用
    _, flag_legacy = derive_growth_estimate({"epsGrowthTTMYoy": 31.0}, None)
    assert flag_legacy == FLAG_G_EST_DEFAULT
    g_def, flag_def = derive_growth_estimate({}, None)
    assert flag_def == FLAG_G_EST_DEFAULT
    assert g_def == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_compute_and_save_blended_with_real_keys(db_conn: Any) -> None:
    await _save_liquidity()
    service = ValuationService(data_provider=FakeProvider(MSFT_METRICS))

    fv_res, rev_res = await service.compute_and_save_valuation(
        symbol="ZZCO", as_of_date=date(2026, 10, 5)
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

    saved = get_latest_fair_value("ZZCO")
    assert saved is not None
    assert saved.method == "BLENDED"
    assert saved.spot_price == MSFT_SPOT
    assert saved.fair_value == fv_res.fair_value
    saved_rev = get_latest_revision_score("ZZCO")
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
        "epsGrowth3Y": 12.0,  # 舊版歷史鍵已不使用 → 預設
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
        FLAG_G_EST_DEFAULT,
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
        symbol="ZZCO",
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
    curr = get_eps_estimate_snapshots("ZZCO", None, "2026-10-05")
    assert [s.snapshot_date for s in curr] == ["2026-10-05"]
    assert curr[0].fiscal_period == "2027-Q1"
    assert get_eps_estimate_snapshots("ZZCO")[0].snapshot_date == "2026-10-06"
    prior = get_prior_eps_estimate_snapshots("ZZCO", "2026-08-31", "2026-09-07")
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
                symbol="ZZCO",
                fiscal_period="2027-Q1",
                composite_score=40.0,
                status="PROCESSED",
                announced_on="2026-09-21",  # 週一
            ),
            EarningsSurpriseDTO(
                symbol="ZZCO",
                fiscal_period="2027-Q2",
                composite_score=None,
                status="PENDING",
                announced_on="2026-10-02",
            ),
        ]
    )
    latest = get_latest_processed_earnings_surprise("ZZCO", "2026-10-05")
    assert latest is not None
    assert latest.fiscal_period == "2027-Q1"

    service = ValuationService(data_provider=FakeProvider(MSFT_METRICS))
    _, rev = await service.compute_and_save_valuation(
        "ZZCO", as_of_date=date(2026, 10, 5)
    )
    assert set(rev.slopes) == {"0q"}
    assert rev.slopes["0q"] == pytest.approx(0.20)
    assert rev.score_30d == pytest.approx(100.0)
    # 2026-09-21（週一）之後到 2026-10-05：9/22–10/5 共 10 個交易日
    assert rev.details["days_since_surprise"] == 10
    assert rev.is_pead_aligned is True
    assert rev.details["current_snapshot_date"] == "2026-10-05"


@pytest.mark.asyncio
async def test_comps_detail_is_persisted_with_peer_symbol_pairing(db_conn: Any) -> None:
    """comps_detail_json 記錄 forward EPS、同業代號與 PE 的配對；無效 PE 的同業不列入 peers。"""

    class PerPeerProvider(FakeProvider):
        PE: dict[str, dict[str, Any]] = {
            "P1": {"forwardPE": 10.0},
            "P2": {},
            "P3": {"peTTM": 30.0},
            "P4": {"forwardPE": 20.0},
        }

        async def get_company_metrics(self, symbol: str) -> dict[str, Any]:
            if symbol in self.PE:
                return self.PE[symbol]
            return self.metrics

    await _save_liquidity()
    service = ValuationService(
        data_provider=PerPeerProvider(MSFT_METRICS, peers=["P1", "P2", "P3", "P4"])
    )
    await service.compute_and_save_valuation(
        symbol="ZZCO", as_of_date=date(2026, 10, 5)
    )
    saved = get_fair_value("ZZCO", "2026-10-05")
    assert saved is not None and saved.comps_detail_json is not None
    detail = json.loads(saved.comps_detail_json)

    assert detail["forward_eps"] == pytest.approx(MSFT_SPOT / 24.19122)
    assert detail["forward_eps_source"] == "FORWARD_PE"
    assert detail["forward_pe"] == pytest.approx(24.19122)
    assert detail["peers_requested"] == ["P1", "P2", "P3", "P4"]
    assert detail["peers"] == [
        {"symbol": "P1", "pe": 10.0, "trailing": False},
        {"symbol": "P3", "pe": 30.0, "trailing": True},
        {"symbol": "P4", "pe": 20.0, "trailing": False},
    ]
    assert detail["median_pe"] == 20.0
    assert detail["liquidity_penalty"] == pytest.approx(math.exp(0.025), abs=1e-4)
    assert detail["rejection_reason"] is None


@pytest.mark.asyncio
async def test_comps_detail_trailing_eps_source_and_rejection(db_conn: Any) -> None:
    """缺 forwardPE 時 forward_eps_source == TRAILING_EPS；同業不足時記錄拒絕原因。"""
    service = ValuationService(
        data_provider=FakeProvider(
            {"pfcfShareTTM": 25.0, "epsTTM": 4.0},
            spot=100.0,
            peer_metrics={"forwardPE": 20.0},
            peers=["P1", "P2"],
        )
    )
    await service.compute_and_save_valuation(
        symbol="TEST", as_of_date=date(2026, 10, 5)
    )
    saved = get_fair_value("TEST", "2026-10-05")
    assert saved is not None and saved.comps_detail_json is not None
    detail = json.loads(saved.comps_detail_json)
    assert detail["forward_eps"] == 4.0
    assert detail["forward_eps_source"] == "TRAILING_EPS"
    assert detail["forward_pe"] is None
    assert detail["median_pe"] is None
    assert detail["rejection_reason"] == "INSUFFICIENT_PEERS"
    assert len(detail["peers"]) == 2


@pytest.mark.asyncio
async def test_comps_detail_without_any_eps_source_is_none(db_conn: Any) -> None:
    service = ValuationService(
        data_provider=FakeProvider(ETF_METRICS, spot=570.0, peers=[])
    )
    await service.compute_and_save_valuation(symbol="SPY", as_of_date=date(2026, 10, 5))
    saved = get_fair_value("SPY", "2026-10-05")
    assert saved is not None and saved.comps_detail_json is not None
    detail = json.loads(saved.comps_detail_json)
    assert detail["forward_eps"] is None
    assert detail["forward_eps_source"] is None
    assert detail["peers"] == []
    assert detail["peers_requested"] == []


class _CountingProvider(FakeProvider):
    """記錄 get_peers 呼叫與被查詢指標的同業，並可逐檔指定 PE。"""

    def __init__(self, *args: Any, pe_by_symbol: dict[str, Any], **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.pe_by_symbol = pe_by_symbol
        self.get_peers_calls = 0
        self.metrics_calls: list[str] = []

    async def get_peers(self, symbol: str) -> list[str]:
        self.get_peers_calls += 1
        return self.peers

    async def get_company_metrics(self, symbol: str) -> dict[str, Any]:
        self.metrics_calls.append(symbol)
        if symbol in self.pe_by_symbol:
            return self.pe_by_symbol[symbol]  # type: ignore[no-any-return]
        return self.metrics


@pytest.mark.asyncio
async def test_group_symbol_uses_group_peers_without_calling_get_peers(
    db_conn: Any,
) -> None:
    await _save_liquidity()
    provider = _CountingProvider(
        MSFT_METRICS,
        peers=["X1", "X2", "X3"],
        pe_by_symbol={
            s: {"forwardPE": 10.0 + i} for i, s in enumerate(["SNDK", "SKHY", "WDC"])
        },
    )
    await ValuationService(data_provider=provider).compute_and_save_valuation(
        symbol="MU", as_of_date=date(2026, 10, 5)
    )
    assert provider.get_peers_calls == 0
    saved = get_fair_value("MU", "2026-10-05")
    assert saved is not None and saved.comps_detail_json is not None
    detail = json.loads(saved.comps_detail_json)
    assert detail["peers_requested"] == ["SNDK", "SKHY", "WDC", "STX"]
    assert detail["peer_source"] == "GROUP"
    assert detail["peer_group"] == "記憶體與儲存"
    assert detail["cyclical"] is True
    assert (
        detail["median_pe"] == 11.5
    )  # 10, 11, 12, STX 回傳預設指標 PE 24.19 → 中位 11.5


@pytest.mark.asyncio
async def test_group_with_fewer_than_three_valid_pe_is_invalid_without_finnhub_fallback(
    db_conn: Any,
) -> None:
    await _save_liquidity()
    provider = _CountingProvider(
        MSFT_METRICS,
        peers=["X1", "X2", "X3"],
        pe_by_symbol={
            "SNDK": {"forwardPE": 10.0},
            "SKHY": {"forwardPE": 12.0},
            "WDC": {},
            "STX": {},
        },
    )
    await ValuationService(data_provider=provider).compute_and_save_valuation(
        symbol="MU", as_of_date=date(2026, 10, 5)
    )
    assert provider.get_peers_calls == 0
    assert not any(c.startswith("X") for c in provider.metrics_calls)
    saved = get_fair_value("MU", "2026-10-05")
    assert saved is not None and saved.comps_detail_json is not None
    detail = json.loads(saved.comps_detail_json)
    assert detail["rejection_reason"] == "INSUFFICIENT_PEERS"
    assert saved.comps_value is None


@pytest.mark.asyncio
async def test_no_comps_symbol_skips_peers_and_flags(db_conn: Any) -> None:
    await _save_liquidity()
    provider = _CountingProvider(MSFT_METRICS, pe_by_symbol={})
    fv_res, _ = await ValuationService(
        data_provider=provider
    ).compute_and_save_valuation(symbol="TSLA", as_of_date=date(2026, 10, 5))
    assert provider.get_peers_calls == 0
    assert provider.metrics_calls == ["TSLA"]
    assert fv_res.comps_value is None
    assert "NO_COMPARABLE_PEERS" in fv_res.flags
    saved = get_fair_value("TSLA", "2026-10-05")
    assert saved is not None and saved.comps_detail_json is not None
    detail = json.loads(saved.comps_detail_json)
    assert detail["rejection_reason"] == "NO_COMPARABLE_PEERS"
    assert detail["peer_source"] == "NO_COMPS"
    assert detail["peers_requested"] == []


@pytest.mark.asyncio
async def test_finnhub_symbol_detail_has_source_fields(db_conn: Any) -> None:
    await _save_liquidity()
    provider = _CountingProvider(MSFT_METRICS, pe_by_symbol={})
    await ValuationService(data_provider=provider).compute_and_save_valuation(
        symbol="ZZCO", as_of_date=date(2026, 10, 5)
    )
    assert provider.get_peers_calls == 1
    saved = get_fair_value("ZZCO", "2026-10-05")
    assert saved is not None and saved.comps_detail_json is not None
    detail = json.loads(saved.comps_detail_json)
    assert detail["peer_source"] == "FINNHUB"
    non_cyc_flags = json.loads(saved.flags_json)
    assert not any(f.startswith("CYCLICAL_") for f in non_cyc_flags)
    assert detail["peer_group"] is None
    assert detail["cyclical"] is False


def test_growth_revenue_first_primary_5y_and_3y_fallback() -> None:
    g, flag = derive_growth_estimate(
        {"revenueGrowth5Y": 14.34, "forwardPE": 14.3, "epsTTM": 10.98}, 16.8
    )
    assert g == pytest.approx(0.1434)
    assert flag is None
    # 5Y 缺值 → 3Y
    g3, flag3 = derive_growth_estimate({"revenueGrowth3Y": 8.0}, None)
    assert (g3, flag3) == (pytest.approx(0.08), None)


def test_growth_revenue_first_zero_is_kept() -> None:
    assert derive_growth_estimate(
        {"revenueGrowth5Y": 0.0, "revenueGrowth3Y": 9.0}, 5.0
    ) == (
        0.0,
        None,
    )


def test_growth_revenue_first_forward_fallback_in_band() -> None:
    g, flag = derive_growth_estimate({"epsTTM": 4.0}, 5.0)
    assert g == pytest.approx(0.25)
    assert flag == FLAG_G_EST_FORWARD
    # 邊界含端點
    assert derive_growth_estimate({"epsTTM": 1.0}, 2.0) == (
        pytest.approx(1.0),
        FLAG_G_EST_FORWARD,
    )
    assert derive_growth_estimate({"epsTTM": 2.0}, 1.0) == (
        pytest.approx(-0.5),
        FLAG_G_EST_FORWARD,
    )


def test_growth_revenue_first_forward_outside_band_uses_default() -> None:
    # 隱含 +154%（口徑不符）
    assert derive_growth_estimate({"forwardPE": 38.0, "epsTTM": 1.60}, 4.07) == (
        0.05,
        "G_EST_DEFAULT",
    )
    # 隱含 -76%（ADR 幣別不符）
    assert derive_growth_estimate({"epsTTM": 10.0}, 2.4) == (0.05, FLAG_G_EST_DEFAULT)


def test_growth_revenue_first_all_missing_uses_default() -> None:
    assert derive_growth_estimate({}, None) == (0.05, FLAG_G_EST_DEFAULT)
    assert derive_growth_estimate({"epsTTM": -1.0}, 2.0) == (0.05, FLAG_G_EST_DEFAULT)


def _dcf_for(fcf: float, growth: float, rate: float) -> float | None:
    from market_analysis.fundamental_pipeline.fair_value import calculate_dcf_value
    from market_analysis.fundamental_pipeline.models import DCFInputs

    return calculate_dcf_value(
        DCFInputs(fcf_per_share=fcf, growth_rate_1y=growth, cost_of_equity=rate)
    ).dcf_value


@pytest.mark.asyncio
async def test_cyclical_growth_cap_revenue_first_applied_for_cyclical_group(
    db_conn: Any,
) -> None:
    await _save_liquidity()
    metrics = {**MSFT_METRICS, "revenueGrowth5Y": 40.0}
    provider = _CountingProvider(
        metrics,
        peers=["X1", "X2", "X3"],
        pe_by_symbol={
            s: {"forwardPE": 10.0 + i} for i, s in enumerate(["SNDK", "SKHY", "WDC"])
        },
    )
    await ValuationService(data_provider=provider).compute_and_save_valuation(
        symbol="MU", as_of_date=date(2026, 10, 5)
    )
    saved = get_fair_value("MU", "2026-10-05")
    assert saved is not None and saved.dcf_value is not None
    assert FLAG_CYCLICAL_GROWTH_CAP in json.loads(saved.flags_json)
    fcf = MSFT_SPOT / 58.911
    r = calculate_cost_of_equity(us10y=4.15, nfci=-0.25, beta=MSFT_METRICS["beta"])[0]
    assert saved.discount_rate == pytest.approx(r, abs=1e-4)
    assert saved.dcf_value == pytest.approx(_dcf_for(fcf, 0.025, r), rel=1e-4)


@pytest.mark.asyncio
async def test_cyclical_growth_cap_revenue_first_not_for_non_cyclical(
    db_conn: Any,
) -> None:
    await _save_liquidity()
    metrics = {**MSFT_METRICS, "revenueGrowth5Y": 40.0}
    await ValuationService(
        data_provider=FakeProvider(metrics)
    ).compute_and_save_valuation(symbol="ZZCO", as_of_date=date(2026, 10, 5))
    saved = get_fair_value("ZZCO", "2026-10-05")
    assert saved is not None and saved.dcf_value is not None
    assert FLAG_CYCLICAL_GROWTH_CAP not in json.loads(saved.flags_json)
    fcf = MSFT_SPOT / 58.911
    r = calculate_cost_of_equity(us10y=4.15, nfci=-0.25, beta=MSFT_METRICS["beta"])[0]
    assert saved.discount_rate == pytest.approx(r, abs=1e-4)
    assert saved.dcf_value == pytest.approx(_dcf_for(fcf, 0.25, r), rel=1e-4)


@pytest.mark.asyncio
async def test_cyclical_growth_cap_revenue_first_not_when_growth_below_cap(
    db_conn: Any,
) -> None:
    await _save_liquidity()
    metrics = {**MSFT_METRICS, "revenueGrowth5Y": 1.0}
    provider = _CountingProvider(
        metrics,
        peers=["X1", "X2", "X3"],
        pe_by_symbol={
            s: {"forwardPE": 10.0 + i} for i, s in enumerate(["SNDK", "SKHY", "WDC"])
        },
    )
    await ValuationService(data_provider=provider).compute_and_save_valuation(
        symbol="MU", as_of_date=date(2026, 10, 5)
    )
    saved = get_fair_value("MU", "2026-10-05")
    assert saved is not None and saved.dcf_value is not None
    assert FLAG_CYCLICAL_GROWTH_CAP not in json.loads(saved.flags_json)
    fcf = MSFT_SPOT / 58.911
    r = calculate_cost_of_equity(us10y=4.15, nfci=-0.25, beta=MSFT_METRICS["beta"])[0]
    assert saved.discount_rate == pytest.approx(r, abs=1e-4)
    assert saved.dcf_value == pytest.approx(_dcf_for(fcf, 0.01, r), rel=1e-4)
