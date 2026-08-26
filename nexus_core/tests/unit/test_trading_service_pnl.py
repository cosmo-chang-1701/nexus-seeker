from typing import Any, Tuple
import pytest
from unittest.mock import MagicMock, patch

from services.trading_service import TradingService


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
