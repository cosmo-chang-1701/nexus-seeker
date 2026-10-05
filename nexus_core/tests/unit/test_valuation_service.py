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
