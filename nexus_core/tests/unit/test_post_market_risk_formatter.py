"""宏觀風險 Theta/Vega/Vanna 文案與板塊相關性「資料不足」語意測試。"""

from typing import Any, Dict, List
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from market_analysis.report_formatter import (
    format_correlation_report,
    format_macro_risk_report,
)
from market_analysis.risk_engine import analyze_sector_correlation


def _metrics(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "exposure_pct": 0.0,
        "net_exposure_dollars": 0.0,
        "total_beta_delta": 0.0,
        "total_gamma": 0.0,
        "gamma_threshold": 10.0,
        "theta_yield": 0.1,
        "total_theta": 5.0,
        "portfolio_heat": 10.0,
        "total_margin_used": 1000.0,
        "total_vega": 12.5,
        "total_vanna": 250.0,
    }
    base.update(overrides)
    return base


def _text(metrics: Dict[str, Any]) -> str:
    return "".join(format_macro_risk_report(metrics, 500.0))


def test_theta_negative_is_time_decay_not_low_yield() -> None:
    text = _text(_metrics(total_theta=-20.0, theta_yield=-0.02))
    assert "時間價值流失" in text
    assert "收益率過低" not in text


def test_theta_zero_is_no_position() -> None:
    text = _text(_metrics(total_theta=0.0, theta_yield=0.0))
    assert "無 Theta 部位" in text
    assert "收益率過低" not in text


def test_theta_positive_tiers() -> None:
    assert "收益率過低" in _text(_metrics(total_theta=5.0, theta_yield=0.01))
    assert "現金流健康" in _text(_metrics(total_theta=5.0, theta_yield=0.1))
    assert "過度收租" in _text(_metrics(total_theta=5.0, theta_yield=0.5))


def test_vega_vanna_labels() -> None:
    text = _text(_metrics(total_vega=12.5, total_vanna=250.0))
    assert "`$+12.50` / IV +1 點" in text
    assert "IV 上升 1 個百分點時的估計損益" in text
    # Vanna 為 SPY 等效股數 (除以 100)，不帶 $
    assert "`+2.50` 股 / IV +1 點" in text
    assert "$+2.50" not in text
    assert "$+250.00" not in text
    assert "SPY 等效 Delta 的變動" in text


def test_correlation_none_is_insufficient_data() -> None:
    text = "".join(format_correlation_report(None, 3))
    assert "資料不足，未評估" in text
    assert "分散性良好" not in text
    assert "60 個交易日 Pearson 係數" in text


def test_correlation_empty_is_diversified() -> None:
    text = "".join(format_correlation_report([], 3))
    assert "分散性良好" in text


def _frame(n: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start="2024-01-01", periods=n)
    return pd.DataFrame(
        {"Close": 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))}, index=dates
    )


@pytest.mark.asyncio
async def test_correlation_single_symbol_returns_empty() -> None:
    assert await analyze_sector_correlation(["AAPL"]) == []


@pytest.mark.asyncio
async def test_correlation_exception_returns_none() -> None:
    with patch(
        "services.market_data_service.get_history_df", autospec=True
    ) as mock_hist:
        mock_hist.side_effect = RuntimeError("boom")
        assert await analyze_sector_correlation(["AAPL", "MSFT"]) is None


@pytest.mark.asyncio
async def test_correlation_short_sample_returns_none() -> None:
    with patch(
        "services.market_data_service.get_history_df", autospec=True
    ) as mock_hist:
        mock_hist.side_effect = [_frame(30), _frame(30, 1)]
        assert await analyze_sector_correlation(["AAPL", "MSFT"]) is None


@pytest.mark.asyncio
async def test_correlation_single_usable_series_returns_none() -> None:
    with patch(
        "services.market_data_service.get_history_df", autospec=True
    ) as mock_hist:
        mock_hist.side_effect = [_frame(100), pd.DataFrame()]
        assert await analyze_sector_correlation(["AAPL", "MSFT"]) is None


@pytest.mark.asyncio
async def test_correlation_requests_6mo_and_uses_last_61_closes() -> None:
    calls: List[Any] = []

    async def fake(sym: str, period: str) -> pd.DataFrame:
        calls.append((sym, period))
        return _frame(120, hash(sym) % 7)

    with patch("services.market_data_service.get_history_df", side_effect=fake):
        result = await analyze_sector_correlation(["AAPL", "MSFT"])
    assert result is not None
    assert {p for _, p in calls} == {"6mo"}
