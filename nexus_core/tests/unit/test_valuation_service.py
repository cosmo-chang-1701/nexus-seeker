"""估值與修正動能服務單元測試。"""

from datetime import date
from typing import Any

import pytest
from database.fundamental_pipeline import (
    get_latest_fair_value,
    get_latest_revision_score,
    save_liquidity_regime,
)
from market_analysis.fundamental_pipeline.models import LiquidityReading
from services.valuation_service import ValuationService


class MockValuationDataProvider:
    def __init__(self) -> None:
        self.metrics: dict[str, Any] = {
            "fcfPerShareTTM": 6.5,
            "beta": 1.1,
            "epsGrowthTTMYoy": 15.0,
            "epsNormalizedAnnual": 6.8,
            "peTTM": 28.0,
        }
        self.peers = ["MSFT", "GOOGL", "NVDA", "AMZN"]
        self.spot_price = 180.0

    async def get_company_metrics(self, symbol: str) -> dict[str, Any]:
        if symbol in self.peers:
            return {"peTTM": 30.0}
        return self.metrics

    async def get_peers(self, symbol: str) -> list[str]:
        return self.peers

    async def get_spot_price(self, symbol: str) -> float | None:
        return self.spot_price


@pytest.mark.asyncio
async def test_valuation_service_compute_and_save(db_conn: Any) -> None:
    # 預先存入一筆流動性體制記錄
    reading = LiquidityReading(
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
    await save_liquidity_regime(reading)

    mock_provider = MockValuationDataProvider()
    service = ValuationService(data_provider=mock_provider)

    fv_res, rev_res = await service.compute_and_save_valuation(
        symbol="AAPL", as_of_date=date(2026, 10, 5)
    )

    # 驗證回傳物件
    assert fv_res.fair_value is not None
    assert fv_res.fair_value > 0
    assert fv_res.margin_of_safety is not None
    assert rev_res.score_30d is not None

    # 驗證資料庫持久化記錄
    saved_fv = get_latest_fair_value("AAPL")
    assert saved_fv is not None
    assert saved_fv.symbol == "AAPL"
    assert saved_fv.trading_date == "2026-10-05"
    assert saved_fv.fair_value == fv_res.fair_value

    saved_rev = get_latest_revision_score("AAPL")
    assert saved_rev is not None
    assert saved_rev.symbol == "AAPL"
    assert saved_rev.trading_date == "2026-10-05"
    assert saved_rev.score_30d == rev_res.score_30d


@pytest.mark.asyncio
async def test_valuation_service_snapshot_fwd_eps_fallback_and_governance(
    db_conn: Any,
) -> None:
    from database.fundamental_pipeline import (
        save_eps_estimate_snapshots,
        save_governance_flags,
    )
    from market_analysis.fundamental_pipeline.models import (
        EPSEstimateSnapshotRecord,
        GovernanceFlagRecord,
    )

    # 1. 預存分析師共識快照 (0y = 5.0)
    snaps = [
        EPSEstimateSnapshotRecord(
            symbol="TEST",
            snapshot_date="2026-10-05",
            horizon="0y",
            source="finnhub",
            eps_mean=5.0,
        ),
    ]
    await save_eps_estimate_snapshots(snaps)

    # 2. 存入 REVIEW 治理紅旗
    gov_flag = GovernanceFlagRecord(
        symbol="TEST",
        source_accession="000999-26-00003",
        flag_kind="INSIDER_SALE",
        severity="REVIEW",
        detail_json="{}",
        expires_at="2026-11-05",
    )
    await save_governance_flags([gov_flag])

    class NoFwdEpsProvider:
        async def get_company_metrics(self, symbol: str) -> dict[str, Any]:
            if symbol == "TEST":
                return {
                    "fcfPerShareTTM": 0.0,  # 強制 DCF 失效，依賴 Comps
                    "epsGrowth3Y": 12.0,  # 備援成長率
                    # 無 epsNormalizedAnnual / epsTTM
                }
            return {"peTTM": 20.0}

        async def get_peers(self, symbol: str) -> list[str]:
            return ["P1", "P2", "P3"]

        async def get_spot_price(self, symbol: str) -> float | None:
            return 70.0

    service = ValuationService(data_provider=NoFwdEpsProvider())
    fv_res, rev_res = await service.compute_and_save_valuation(
        symbol="TEST", as_of_date=date(2026, 10, 5)
    )

    # 驗證成功從快照 0y 補足 forward_eps = 5.0，Comps 估值成功
    assert fv_res.comps_value is not None
    assert fv_res.fair_value is not None
    assert fv_res.method == "COMPS_ONLY"
    assert pytest.approx(fv_res.comps_value, rel=1e-2) == 5.0 * 20.0

    # 驗證治理問題成功壓制 deep value
    assert fv_res.is_deep_value is False
    assert "GOVERNANCE_RISK" in fv_res.flags
