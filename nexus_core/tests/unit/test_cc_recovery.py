import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import pandas as pd
import discord
from cogs.cc_recovery import CoveredCallRecoveryCog
from market_analysis.trading_orchestration import filter_cc_recovery_targets


@pytest.mark.asyncio
async def test_filter_cc_recovery_targets_success():
    # Mock database and market data services
    mock_market_cache = {
        "reference_spot_price": 100.0,
        "max_pain": 95.0,
        "expected_move_lower": 90.0,
        "expected_move_upper": 110.0,
    }

    mock_calls = pd.DataFrame(
        [
            {
                "strike": 115.0,
                "lastPrice": 2.0,
                "bid": 1.9,
                "ask": 2.1,
                "impliedVolatility": 0.3,
                "contractSymbol": "TEST115C",
            }
        ]
    )

    class MockOptionChain:
        def __init__(self, calls):
            self.calls = calls
            self.puts = pd.DataFrame()

    with patch(
        "database.market_cache.get_market_cache", return_value=mock_market_cache
    ), patch(
        "market_analysis.trading_orchestration.get_all_option_expiries",
        new_callable=AsyncMock,
    ) as mock_expiries, patch(
        "market_analysis.trading_orchestration.get_option_chain", new_callable=AsyncMock
    ) as mock_chain, patch(
        "market_analysis.trading_orchestration.SentimentEngine.get_last_stored_iv",
        return_value=0.35,
    ):
        from datetime import datetime, timedelta

        mock_date = (datetime.now() + timedelta(days=40)).strftime("%Y-%m-%d")
        mock_expiries.return_value = [mock_date]  # >30 days from now
        mock_chain.return_value = MockOptionChain(mock_calls)

        res = await filter_cc_recovery_targets("AAPL")

        assert res is not None
        assert res["symbol"] == "AAPL"
        assert res["current_price"] == 100.0
        assert len(res["recommendations"]) > 0
        rec = res["recommendations"][0]
        assert rec["strike"] == 115.0
        assert rec["annualized_yield"] >= 10.0


@pytest.mark.asyncio
async def test_cc_recovery_slash_command():
    bot = MagicMock()
    cog = CoveredCallRecoveryCog(bot)

    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.message = None

    mock_res = {
        "symbol": "AAPL",
        "current_price": 100.0,
        "fallback_iv": 0.35,
        "recommendations": [
            {
                "expiration": "2026-08-15",
                "strike": 105.0,
                "delta": 0.12,
                "premium": 2.0,
                "bid": 1.9,
                "ask": 2.1,
                "annualized_yield": 21.0,
                "contractSymbol": "TEST105C",
            }
        ],
    }

    with patch(
        "cogs.cc_recovery.filter_cc_recovery_targets",
        new_callable=AsyncMock,
        return_value=mock_res,
    ):
        await cog.cc_recovery.callback(cog, interaction, "AAPL")

        interaction.response.defer.assert_called_once_with(ephemeral=False)
        interaction.followup.send.assert_called_once()
        args, kwargs = interaction.followup.send.call_args
        assert "embed" in kwargs
        embed = kwargs["embed"]
        assert embed.title == "🛡️ AAPL Covered Call 防禦性收租篩選結果"
