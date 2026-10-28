"""估值讀取流動性體制的 us10y 退回測試：最新列 us10y 為 NULL 時改用較早的非 NULL 讀數。"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from database.fundamental_pipeline import (
    get_latest_liquidity_regime,
    save_liquidity_regime,
)
from market_analysis.fundamental_pipeline.fair_value import (
    FLAG_NFCI_MISSING,
    FLAG_US10Y_DEFAULT,
    calculate_cost_of_equity,
)
from market_analysis.fundamental_pipeline.models import LiquidityReading
from services.valuation_service import ValuationService

SPOT = 100.0
BETA = 1.2
METRICS: dict[str, Any] = {
    "pfcfShareTTM": 20.0,
    "forwardPE": 20.0,
    "epsTTM": 5.0,
    "beta": BETA,
    "revenueGrowth5Y": 10.0,
    "peTTM": 20.0,
}


class _Provider:
    async def get_company_metrics(self, symbol: str) -> dict[str, Any]:
        return METRICS

    async def get_peers(self, symbol: str) -> list[str]:
        return ["AAPL", "GOOGL", "ORCL", "AMZN"]

    async def get_spot_price(self, symbol: str) -> float | None:
        return SPOT


async def _save_normal_row() -> None:
    await save_liquidity_regime(
        LiquidityReading(
            trading_date=date(2026, 10, 20),
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


async def _save_null_row() -> None:
    await save_liquidity_regime(
        LiquidityReading(
            trading_date=date(2026, 10, 21),
            nfci=None,
            anfci=None,
            net_liquidity_bn=None,
            net_liquidity_chg_13w_pct=None,
            reserves_chg_13w_pct=None,
            us10y=None,
            regime="UNKNOWN",
            equity_risk_premium=None,
        )
    )


@pytest.mark.asyncio
async def test_latest_liquidity_default_returns_newest_row_even_if_null(
    db_conn: Any,
) -> None:
    await _save_normal_row()
    await _save_null_row()

    reading = get_latest_liquidity_regime()

    assert reading is not None
    assert reading.trading_date == date(2026, 10, 21)
    assert reading.us10y is None
    assert reading.regime == "UNKNOWN"


@pytest.mark.asyncio
async def test_latest_liquidity_require_us10y_skips_null_rows(db_conn: Any) -> None:
    await _save_normal_row()
    await _save_null_row()

    reading = get_latest_liquidity_regime(require_us10y=True)

    assert reading is not None
    assert reading.trading_date == date(2026, 10, 20)
    assert reading.us10y == pytest.approx(4.15)
    assert reading.nfci == pytest.approx(-0.25)


@pytest.mark.asyncio
async def test_latest_liquidity_require_us10y_none_when_all_null(
    db_conn: Any,
) -> None:
    await _save_null_row()

    assert get_latest_liquidity_regime(require_us10y=True) is None


@pytest.mark.asyncio
async def test_valuation_uses_last_non_null_us10y(db_conn: Any) -> None:
    await _save_normal_row()
    await _save_null_row()
    service = ValuationService(data_provider=_Provider())

    fv_res, _ = await service.compute_and_save_valuation(
        symbol="ZZCO", as_of_date=date(2026, 10, 21)
    )

    assert FLAG_US10Y_DEFAULT not in fv_res.flags
    assert FLAG_NFCI_MISSING not in fv_res.flags
    expected_r, _ = calculate_cost_of_equity(us10y=4.15, nfci=-0.25, beta=BETA)
    assert fv_res.discount_rate == pytest.approx(expected_r, abs=1e-4)


@pytest.mark.asyncio
async def test_valuation_still_defaults_when_no_us10y_history(db_conn: Any) -> None:
    await _save_null_row()
    service = ValuationService(data_provider=_Provider())

    fv_res, _ = await service.compute_and_save_valuation(
        symbol="ZZCO", as_of_date=date(2026, 10, 21)
    )

    assert FLAG_US10Y_DEFAULT in fv_res.flags
