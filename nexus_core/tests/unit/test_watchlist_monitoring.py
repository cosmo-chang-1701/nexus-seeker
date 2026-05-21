import pytest
import pandas as pd
from pydantic import ValidationError
from unittest.mock import AsyncMock, patch

from market_analysis.intraday_pipeline import (
    _WATCHLIST_METRICS_CACHE,
    build_enhanced_watchlist_metrics,
    evaluate_watchlist_symbol,
)
from models.quant import IVMetrics
from models.schemas import EnhancedWatchlistMetrics
from risk_engine.nro import WatchlistRiskController
from ui.formatter import generate_ansi_watchlist_report


def _sample_metrics(**overrides):
    payload = {
        "symbol": "NVDA",
        "exchange": "NASDAQ",
        "current_price": 132.0,
        "buy_zone_status": "🟢 買點：趨勢支撐 (VIX 修正)",
        "buy_price_phase1": 130.0,
        "buy_price_phase2": 124.0,
        "buy_price_phase3": 118.0,
        "sell_zone_status": "🟢 賣點：第一壓力帶",
        "sell_price_phase1": 136.0,
        "sell_price_phase2": 142.0,
        "sell_price_phase3": 148.0,
        "pe_ratio": 42.5,
        "rsi_14": 56.4,
        "atr_14": 6.0,
        "beta": 1.4,
        "ma20": 128.0,
        "ma50": 122.0,
        "ma200": 110.0,
        "bias_ma20": 999.0,
        "iv_rank": 72.0,
        "volume_poc": 126.5,
        "gex_max_put_wall": 120.0,
        "vanna_sensitivity": 0.35,
        "relative_strength_spy": 0.08,
    }
    payload.update(overrides)
    return EnhancedWatchlistMetrics(**payload)


@pytest.fixture(autouse=True)
def clear_watchlist_metrics_cache():
    _WATCHLIST_METRICS_CACHE.clear()
    yield
    _WATCHLIST_METRICS_CACHE.clear()


def test_enhanced_watchlist_metrics_computes_bias_and_support_distance():
    metrics = _sample_metrics()
    assert metrics.bias_ma20 == pytest.approx((132.0 / 128.0) - 1.0)
    assert metrics.distance_to_absolute_support == pytest.approx((132.0 / 118.0) - 1.0)


def test_enhanced_watchlist_metrics_rejects_invalid_phase_order():
    with pytest.raises(ValidationError):
        _sample_metrics(buy_price_phase1=120.0, buy_price_phase2=124.0)


def test_watchlist_risk_controller_routes_premium_harvest():
    tactical = WatchlistRiskController.process_metrics(
        _sample_metrics(current_price=129.0, iv_rank=78.0)
    )
    assert tactical.scenario == "premium-harvest"
    assert tactical.alert_level == "yellow"
    assert "Cash-Secured Put" in tactical.action_guideline
    assert tactical.dynamic_grid_step == 3.0


def test_watchlist_risk_controller_routes_hard_hedge():
    tactical = WatchlistRiskController.process_metrics(
        _sample_metrics(current_price=123.0, beta=1.5, vanna_sensitivity=0.4)
    )
    assert tactical.scenario == "hard-hedge"
    assert tactical.alert_level == "red"
    assert tactical.hidden_delta_risk == pytest.approx(60.0)
    assert tactical.hedge_allocation_shares == 60
    assert "放空 60 股 SPY" in tactical.hedge_instruction


def test_watchlist_risk_controller_routes_wait():
    tactical = WatchlistRiskController.process_metrics(
        _sample_metrics(current_price=136.0, iv_rank=40.0)
    )
    assert tactical.scenario == "wait"
    assert tactical.alert_level == "green"
    assert tactical.hidden_delta_risk == 0.0
    assert tactical.hedge_instruction is None


def test_generate_ansi_watchlist_report_contains_sections():
    metrics = _sample_metrics(current_price=123.0, beta=1.5, vanna_sensitivity=0.4)
    tactical = WatchlistRiskController.process_metrics(metrics)
    report = generate_ansi_watchlist_report(metrics, tactical)
    assert report.startswith("```ansi")
    assert "技術 / 防禦牆" in report
    assert "SDDM / 對沖" in report
    assert "NVDA | NASDAQ" in report


@pytest.mark.asyncio
async def test_build_enhanced_watchlist_metrics_assembles_quant_fields():
    dates = pd.date_range("2025-01-01", periods=90, freq="D")
    stock_df = pd.DataFrame(
        {
            "Open": [100.0 + i * 0.8 for i in range(90)],
            "High": [101.5 + i * 0.8 for i in range(90)],
            "Low": [99.0 + i * 0.8 for i in range(90)],
            "Close": [100.5 + i * 0.8 for i in range(90)],
            "Volume": [1_000_000 + i * 1000 for i in range(90)],
        },
        index=dates,
    )
    spy_df = pd.DataFrame(
        {
            "Open": [400.0 + i * 0.4 for i in range(90)],
            "High": [401.0 + i * 0.4 for i in range(90)],
            "Low": [399.0 + i * 0.4 for i in range(90)],
            "Close": [400.5 + i * 0.4 for i in range(90)],
            "Volume": [2_000_000 + i * 2000 for i in range(90)],
        },
        index=dates,
    )

    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 172.5},
    ), patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=stock_df,
    ), patch(
        "services.market_data_service.get_spy_history_df",
        new_callable=AsyncMock,
        return_value=spy_df,
    ), patch(
        "services.market_data_service.get_basic_financials",
        new_callable=AsyncMock,
        return_value={"peTTM": 31.2},
    ), patch(
        "services.market_data_service.get_company_profile",
        new_callable=AsyncMock,
        return_value={"exchange": "NASDAQ"},
    ), patch(
        "services.market_data_service.get_dividend_yield",
        new_callable=AsyncMock,
        return_value=0.01,
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
        new_callable=AsyncMock,
        return_value=IVMetrics(
            symbol="MSFT",
            current_iv=0.32,
            iv_rank=68.0,
            iv_percentile=64.0,
            expected_move_weekly=8.4,
            iv_status="High",
        ),
    ), patch(
        "market_analysis.intraday_pipeline._estimate_options_wall_metrics",
        new_callable=AsyncMock,
        return_value=(165.0, 0.44),
    ), patch(
        "market_analysis.risk_engine.calculate_beta",
        return_value=1.23,
    ):
        metrics = await build_enhanced_watchlist_metrics("msft")

    assert metrics is not None
    assert metrics.symbol == "MSFT"
    assert metrics.exchange == "NASDAQ"
    assert metrics.current_price == 172.5
    assert metrics.pe_ratio == 31.2
    assert metrics.iv_rank == 68.0
    assert metrics.gex_max_put_wall == 165.0
    assert metrics.vanna_sensitivity == 0.44
    assert metrics.beta == 1.23
    assert (
        metrics.buy_price_phase1 >= metrics.buy_price_phase2 >= metrics.buy_price_phase3
    )
    assert (
        metrics.sell_price_phase1
        <= metrics.sell_price_phase2
        <= metrics.sell_price_phase3
    )


@pytest.mark.asyncio
async def test_evaluate_watchlist_symbol_returns_wait_snapshot():
    metrics = _sample_metrics(current_price=136.0, iv_rank=40.0)

    with patch(
        "market_analysis.intraday_pipeline.build_enhanced_watchlist_metrics",
        new_callable=AsyncMock,
        return_value=metrics,
    ):
        evaluation = await evaluate_watchlist_symbol("NVDA")

    assert evaluation is not None
    assert evaluation.metrics.symbol == "NVDA"
    assert evaluation.tactical.scenario == "wait"
    assert evaluation.tactical.sddm_route == "WAIT (觀望 / 待機)"
