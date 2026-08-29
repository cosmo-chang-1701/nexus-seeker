from typing import Any, Tuple
import pytest
import pandas as pd
from unittest.mock import AsyncMock, patch, MagicMock
from services.trading_service import TradingService


@pytest.fixture
def trading_service() -> Any:
    bot = MagicMock()
    return TradingService(bot)


def test_clean_market_condition_inputs(trading_service: Any) -> Any:
    # Test normal inputs
    ma20, atr, rsi = trading_service._clean_market_condition_inputs(
        100.0, 98.5, 2.5, 65.0
    )
    assert ma20 == 98.5
    assert atr == 2.5
    assert rsi == 65.0

    # Test None inputs
    ma20, atr, rsi = trading_service._clean_market_condition_inputs(
        100.0, None, None, None
    )
    assert ma20 == 100.0
    assert atr == 2.0  # 2% of price (100.0)
    assert rsi == 50.0

    # Test NaN inputs
    ma20, atr, rsi = trading_service._clean_market_condition_inputs(
        100.0, float("nan"), float("nan"), float("nan")
    )
    assert ma20 == 100.0
    assert atr == 2.0
    assert rsi == 50.0

    # Test out of bounds inputs
    ma20, atr, rsi = trading_service._clean_market_condition_inputs(
        100.0, 95.0, -1.0, 150.0
    )
    assert ma20 == 95.0
    assert atr == 2.0
    assert rsi == 50.0


@pytest.mark.asyncio
async def test_run_market_scan_unpacks_correctly(trading_service: Any):  # type: ignore
    # Mock database watchlist to return a list of 3-element tuples
    # (user_id, symbol, use_llm)
    mock_watchlists = [(1, "AAPL", 1)]
    mock_holdings = [{"user_id": 1, "symbol": "AAPL", "avg_cost": 150.0}]

    # Mock market data service calls
    mock_spy_df = pd.DataFrame(
        {"Close": [670.0]}, index=pd.date_range("2026-05-20", periods=1)
    )
    mock_macro = {"vix": 15.0, "oil": 75.0, "vix_change": 0.0}

    # AAPL K-line history (both 60d/1h and 1y/1d)
    # We populate it with a short history (5 rows) so SMA20/ATR14/RSI14 will evaluate to NaN,
    # thereby triggering the cleaning and fallback logic.
    mock_aapl_df_short = pd.DataFrame(
        {
            "Open": [150.0] * 5,
            "High": [152.0] * 5,
            "Low": [148.0] * 5,
            "Close": [150.0] * 5,
            "Volume": [1000] * 5,
        },
        index=pd.date_range("2026-05-20", periods=5),
    )

    with patch("database.get_all_watchlist", return_value=mock_watchlists), patch(
        "database.holdings.get_all_holdings", return_value=mock_holdings
    ), patch(
        "services.market_data_service.get_spy_history_df",
        new_callable=AsyncMock,
        return_value=mock_spy_df,
    ), patch(
        "services.market_data_service.get_macro_environment",
        new_callable=AsyncMock,
        return_value=mock_macro,
    ), patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as mock_get_hist, patch("database.get_full_user_context") as mock_user_ctx, patch(
        "market_analysis.portfolio.refresh_portfolio_greeks", new_callable=AsyncMock
    ) as mock_refresh:
        # mock_get_hist is called for "5d/1d" (Gap), "60d/1h" (EMA), and "1y/1d" (PSQ/scan)
        # return mock_aapl_df_short for all of them
        mock_get_hist.return_value = mock_aapl_df_short

        # Mock user context
        mock_context = MagicMock()
        mock_context.capital = 50000.0
        mock_context.total_weighted_delta = 0.0
        mock_context.option_alert_mode = 1
        mock_user_ctx.return_value = mock_context

        # Call run_market_scan
        # Since AAPL history has only 5 rows, SMA20, ATR14, RSI14 will be NaN.
        # This will test both:
        # 1. 3-element unpacking from all_watchlists.
        # 2. Indicators fallback cleaning (so MarketCondition doesn't raise validation error).
        res = await trading_service.run_market_scan(is_auto=True)

        # Assert no unpacking exception was raised, and it processed successfully
        assert isinstance(res, dict)
        mock_refresh.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_get_execution_decision_handles_none_skew_without_crash(
    trading_service: Any,
) -> None:
    """A degraded SentimentEngine.calculate_skew() result (skew=None, e.g. for an
    illiquid ADR like SBGSY/BXDC with no listed options) must not crash
    get_execution_decision via `None / float`."""
    df_hist = pd.DataFrame(
        {
            "Open": [100.0 + i * 0.1 for i in range(30)],
            "High": [101.0 + i * 0.1 for i in range(30)],
            "Low": [99.0 + i * 0.1 for i in range(30)],
            "Close": [100.0 + i * 0.1 for i in range(30)],
            "Volume": [1000] * 30,
        },
        index=pd.date_range("2026-06-01", periods=30),
    )

    sentinel_decision = object()

    with patch(
        "services.market_data_service.get_macro_environment",
        new_callable=AsyncMock,
        return_value={"vix": 18.0},
    ), patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=df_hist,
    ), patch(
        "market_analysis.sentiment.SentimentEngine.calculate_skew",
        new_callable=AsyncMock,
        return_value={
            "symbol": "SBGSY",
            "skew": None,
            "skew_percentile": None,
            "state": "N/A",
            "is_fallback": False,
            "error": "No option expiries returned",
        },
    ), patch(
        "market_analysis.sentiment.SentimentEngine.detect_uoa",
        new_callable=AsyncMock,
        return_value=[],
    ), patch.object(
        trading_service.execution_router,
        "evaluate_market",
        return_value=sentinel_decision,
    ):
        result = await trading_service.get_execution_decision("SBGSY")

    # Reaching evaluate_market (rather than the outer except-and-return-None path)
    # proves the None-skew division didn't raise a TypeError.
    assert result is sentinel_decision


def _make_trade_asset(
    id_: int,
    symbol: str,
    strike: float,
    expiry: str,
    opt_type: str,
    entry_price: float,
    quantity: float,
) -> Any:
    asset = MagicMock()
    asset.id = id_
    asset.symbol = symbol
    asset.entry_price = entry_price
    asset.metadata = {
        "opt_type": opt_type,
        "strike": strike,
        "expiry": expiry,
        "entry_price": entry_price,
        "quantity": quantity,
    }
    return asset


@pytest.mark.asyncio
async def test_get_portfolio_pnl_maps_each_trade_to_its_own_mid_price() -> None:
    """Regression test for the Semaphore(3)+asyncio.gather batching refactor in
    TradingService.get_portfolio_pnl: each trade's unrealized PnL must still be
    computed from *its own* option-chain mid price once the mid-price fetches run
    concurrently instead of sequentially (a mixed-up zip/order bug would make one
    trade's PnL bleed into another's)."""
    assets = [
        _make_trade_asset(1, "AAPL", 150.0, "2026-01-16", "call", 5.0, 1),
        _make_trade_asset(2, "TSLA", 250.0, "2026-01-16", "put", 8.0, -2),
        _make_trade_asset(3, "NVDA", 900.0, "2026-01-16", "call", 20.0, 3),
    ]

    # Distinct mid price per symbol so a mixed-up mapping would be caught.
    mid_by_symbol = {"AAPL": 6.0, "TSLA": 7.0, "NVDA": 25.0}

    async def _fake_get_option_chain_mid_iv(
        symbol: str, expiry: Any, strike: Any, opt_type: Any
    ) -> Tuple[float, float]:
        return mid_by_symbol[symbol], 0.3

    with (
        patch("services.asset_manager.AssetManager.get_assets", return_value=assets),
        patch(
            "market_analysis.portfolio.get_option_chain_mid_iv",
            side_effect=_fake_get_option_chain_mid_iv,
        ),
    ):
        service = TradingService(MagicMock())
        result = await service.get_portfolio_pnl(user_id=1)

    trades_by_symbol = {t["symbol"]: t for t in result["trades"]}

    assert trades_by_symbol["AAPL"]["current_price"] == 6.0
    assert trades_by_symbol["AAPL"]["unrealized_pnl"] == pytest.approx(
        (6.0 - 5.0) * 100 * 1
    )

    assert trades_by_symbol["TSLA"]["current_price"] == 7.0
    # Short put (quantity < 0): PnL = (entry - mid) * 100 * abs(quantity)
    assert trades_by_symbol["TSLA"]["unrealized_pnl"] == pytest.approx(
        (8.0 - 7.0) * 100 * 2
    )

    assert trades_by_symbol["NVDA"]["current_price"] == 25.0
    assert trades_by_symbol["NVDA"]["unrealized_pnl"] == pytest.approx(
        (25.0 - 20.0) * 100 * 3
    )

    expected_total = (
        (6.0 - 5.0) * 100 * 1 + (8.0 - 7.0) * 100 * 2 + (25.0 - 20.0) * 100 * 3
    )
    assert result["total_unrealized_pnl"] == pytest.approx(expected_total)


@pytest.mark.asyncio
async def test_get_portfolio_pnl_fetches_mid_prices_concurrently() -> None:
    """Ensures the refactor actually batches the per-trade option-chain lookups
    (Semaphore(3) + asyncio.gather) instead of quietly regressing back to a
    fully serial await-in-a-for-loop, which was the original latency cause behind
    /list_trades feeling slow for accounts with many positions."""
    import asyncio

    assets = [
        _make_trade_asset(i, f"SYM{i}", 100.0, "2026-01-16", "call", 1.0, 1)
        for i in range(5)
    ]

    in_flight = 0
    max_in_flight = 0

    async def _fake_get_option_chain_mid_iv(
        symbol: str, expiry: Any, strike: Any, opt_type: Any
    ) -> Tuple[float, float]:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return 1.5, 0.3

    with (
        patch("services.asset_manager.AssetManager.get_assets", return_value=assets),
        patch(
            "market_analysis.portfolio.get_option_chain_mid_iv",
            side_effect=_fake_get_option_chain_mid_iv,
        ),
    ):
        service = TradingService(MagicMock())
        await service.get_portfolio_pnl(user_id=1)

    # With 5 trades and a Semaphore(3) cap, at least 2 lookups must overlap.
    assert max_in_flight >= 2
