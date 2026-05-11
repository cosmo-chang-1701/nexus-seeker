import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import sys
import os
import pandas as pd

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from cogs.unified_terminal import (
    UnifiedTerminalCog,
    SymbolHubView,
    PortfolioHubView,
    PulseHubView,
)


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_symbol_hub_command(mock_interaction, mock_bot):
    cog = UnifiedTerminalCog(mock_bot)

    with patch(
        "services.market_data_service.validate_symbol", new_callable=AsyncMock
    ) as mock_val, patch(
        "services.market_data_service.get_spy_history_df", new_callable=AsyncMock
    ) as mock_spy_hist, patch(
        "services.market_data_service.get_macro_environment", new_callable=AsyncMock
    ) as mock_macro, patch(
        "market_math.analyze_symbol", new_callable=AsyncMock
    ) as mock_analyze, patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
        new_callable=AsyncMock,
    ) as mock_skew, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as mock_hist, patch("database.get_full_user_context") as mock_user_ctx:
        mock_val.return_value = True
        mock_spy_hist.return_value = pd.DataFrame({"Close": [500.0]})
        mock_macro.return_value = {"vix": 15.0}

        # Complete mock data to satisfy create_scan_embed
        mock_analyze.return_value = {
            "symbol": "NVDA",
            "strategy": "STO_PUT",
            "strike": 100,
            "price": 120.0,
            "rsi": 50.0,
            "sma20": 110.0,
            "hv_rank": 40.0,
            "weighted_delta": 0.5,
            "delta": -0.2,
            "iv": 0.3,
            "aroc": 15.0,
            "target_date": "2024-06-21",
            "ts_ratio": 1.0,
            "ts_state": "Normal",
            "v_skew": 1.2,
            "v_skew_state": "Normal",
            "bid": 2.0,
            "ask": 2.2,
            "expected_move": 5.0,
            "em_lower": 115.0,
            "em_upper": 125.0,
        }
        mock_skew.return_value = {"skew": 5.0}
        mock_hist.return_value = pd.DataFrame({"Close": [100.0, 105.0]})

        mock_ctx = MagicMock()
        mock_ctx.capital = 100000.0
        mock_ctx.risk_limit = 15.0
        mock_user_ctx.return_value = mock_ctx

        await cog.symbol_hub.callback(cog, mock_interaction, symbol="NVDA")

        assert mock_interaction.followup.send.called
        _, kwargs = mock_interaction.followup.send.call_args
        if "view" not in kwargs:
            # Print content if error message was sent instead of the hub
            print(
                f"Followup content: {kwargs.get('content') or [a for a in mock_interaction.followup.send.call_args[0]]}"
            )

        assert "view" in kwargs
        assert isinstance(kwargs["view"], SymbolHubView)


@pytest.mark.asyncio
async def test_portfolio_hub_command(mock_interaction, mock_bot):
    cog = UnifiedTerminalCog(mock_bot)

    with patch(
        "services.trading_service.TradingService.get_portfolio_pnl",
        new_callable=AsyncMock,
    ) as mock_pnl, patch("database.get_full_user_context") as mock_user_ctx:
        mock_pnl.return_value = {"trades": [], "total_unrealized_pnl": 0.0}
        mock_ctx = MagicMock()
        mock_ctx.capital = 100000.0
        mock_user_ctx.return_value = mock_ctx

        await cog.portfolio_hub.callback(cog, mock_interaction)

        mock_interaction.followup.send.assert_called_once()
        _, kwargs = mock_interaction.followup.send.call_args
        assert "view" in kwargs
        assert isinstance(kwargs["view"], PortfolioHubView)


@pytest.mark.asyncio
async def test_pulse_hub_command(mock_interaction, mock_bot):
    cog = UnifiedTerminalCog(mock_bot)

    with patch(
        "services.calendar_service.calendar_service.get_portfolio_events",
        new_callable=AsyncMock,
    ) as mock_events:
        mock_events.return_value = []

        await cog.pulse_hub.callback(cog, mock_interaction)

        mock_interaction.followup.send.assert_called_once()
        _, kwargs = mock_interaction.followup.send.call_args
        assert "view" in kwargs
        assert isinstance(kwargs["view"], PulseHubView)
