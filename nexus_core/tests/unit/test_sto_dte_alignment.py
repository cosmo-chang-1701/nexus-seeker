"""STO 進場 DTE 必須大於賣方出場門檻 (DTE <= 21)，避免採納當日即被平倉。"""

import itertools
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from market_analysis.dynamic_rollover import constants as ro_constants
from market_analysis.risk_engine import evaluate_defense_status
from market_analysis.strategy.indicators import (
    SHORT_OPTION_EXIT_DTE,
    SHORT_OPTION_MIN_ENTRY_DTE,
    _determine_strategy_signal,
)


def test_strategy_signal_sto_min_dte_above_exit_threshold() -> None:
    grid = itertools.product(
        [100.0],
        [20.0, 34.9, 35.0, 42.0, 50.0, 57.0, 65.0, 65.1, 80.0],
        [0.0, 29.9, 30.0, 49.9, 50.0, 90.0],
        [95.0, 100.0, 105.0],
        [-1.0, 0.0, 1.0],
        [None, 90.0],
    )
    seen = 0
    for price, rsi, hv, sma, macd, ivr in grid:
        result = _determine_strategy_signal(
            {
                "price": price,
                "rsi": rsi,
                "hv_rank": hv,
                "sma20": sma,
                "macd_hist": macd,
            },
            ivr=ivr,
        )
        name, _, _, min_dte, max_dte = result
        if name and name.startswith("STO"):
            seen += 1
            assert min_dte > SHORT_OPTION_EXIT_DTE
            assert min_dte <= max_dte
    assert seen > 0


def test_covered_call_overlay_window_above_exit_threshold() -> None:
    assert ro_constants._COVERED_CALL_MIN_DTE > SHORT_OPTION_EXIT_DTE
    assert ro_constants._COVERED_CALL_MIN_DTE <= ro_constants._COVERED_CALL_MAX_DTE
    assert ro_constants._FORCED_SETTLEMENT_ROLL_MIN_DTE > SHORT_OPTION_EXIT_DTE


@pytest.mark.asyncio
async def test_option_guidance_covered_call_min_dte() -> None:
    from models.schemas import WatchlistTacticalPlan
    from market_analysis.option_guidance import build_watchlist_option_plan
    from tests.unit.test_watchlist_monitoring import _sample_metrics

    metrics = _sample_metrics(current_price=108.99, iv_rank=74.7)
    metrics.option_skew = -5.10
    tactical = WatchlistTacticalPlan(
        scenario="premium-harvest",
        sddm_route="SHIELD (全面防禦中)",
        dynamic_grid_step=4.65,
        action_guideline="test",
        alert_level="yellow",
    )
    with patch(
        "market_analysis.strategy.find_best_contract", new_callable=AsyncMock
    ) as mock_find:
        mock_find.return_value = {"strike": 115.0, "expiry": "2026-06-26", "mid": 4.15}
        plan = await build_watchlist_option_plan(
            metrics, tactical, capital=100000.0, risk_limit=15.0, has_position=True
        )
    assert plan is not None
    assert mock_find.await_count >= 1
    for call in mock_find.await_args_list:
        assert call.args[1] == "STO_CALL"
        assert call.args[3] > SHORT_OPTION_EXIT_DTE


@pytest.mark.asyncio
async def test_core_deployment_covered_call_min_dte() -> None:
    from market_analysis.dynamic_rollover import DynamicRolloverEngine

    signal = {"is_capped": True, "swamp_strike": 460.0}
    portfolio: list[dict[str, Any]] = [
        {"symbol": "VOO", "asset_class": "CORE", "quantity": 500.0, "avg_cost": 450.0}
    ]
    with (
        patch(
            "market_analysis.index_microstructure.get_spx_capped_from_above_signal",
            new_callable=AsyncMock,
            return_value=signal,
        ),
        patch(
            "market_analysis.strategy.find_lowest_strike_call_above_floor",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_find,
    ):
        await DynamicRolloverEngine().evaluate_covered_call_overlay(1, portfolio, set())
    mock_find.assert_awaited_once()
    assert mock_find.await_args is not None
    assert mock_find.await_args.args[2] > SHORT_OPTION_EXIT_DTE


def test_seller_at_min_entry_dte_not_flagged_gamma_trap() -> None:
    status = evaluate_defense_status(
        quantity=-1,
        opt_type="put",
        pnl_pct=0.0,
        current_delta=-0.2,
        dte=SHORT_OPTION_MIN_ENTRY_DTE,
    )
    assert "DTE ≤ 21" not in status
