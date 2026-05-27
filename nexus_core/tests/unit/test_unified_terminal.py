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
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch(
        "market_math.analyze_symbol", new_callable=AsyncMock
    ) as mock_analyze, patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
        new_callable=AsyncMock,
    ) as mock_skew, patch(
        "market_analysis.sentiment_engine.SentimentEngine.get_indicator_percentile"
    ) as mock_skew_p, patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
        new_callable=AsyncMock,
    ) as mock_mp, patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
        new_callable=AsyncMock,
    ) as mock_pcr, patch(
        "market_analysis.sentiment_engine.SentimentEngine.detect_uoa",
        new_callable=AsyncMock,
    ) as mock_uoa, patch(
        "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
        new_callable=AsyncMock,
    ) as mock_iv, patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as mock_hist, patch(
        "services.reddit_service.get_reddit_context", new_callable=AsyncMock
    ) as mock_reddit, patch(
        "market_analysis.ddp_inspector.DDPInspector.inspect_symbol",
        new_callable=AsyncMock,
    ) as mock_ddp, patch(
        "services.polymarket_service.PolymarketService.get_market_snapshot",
        new_callable=AsyncMock,
    ) as mock_poly, patch("database.get_full_user_context") as mock_user_ctx:
        mock_val.return_value = True
        mock_spy_hist.return_value = pd.DataFrame({"Close": [500.0]})
        mock_macro.return_value = {"vix": 15.0}
        mock_quote.return_value = {
            "c": 120.0,
            "dp": 1.5,
            "d": 1.8,
            "o": 119.0,
            "h": 121.0,
            "l": 118.0,
            "pc": 118.2,
        }

        mock_analyze.return_value = {
            "symbol": "NVDA",
            "price": 120.0,
            "hv_rank": 40.0,
        }
        mock_skew.return_value = {"skew": 5.0}
        mock_skew_p.return_value = 85.0
        mock_mp.return_value = {"max_pain": 115.0}
        mock_pcr.return_value = {"pcr": 0.8, "state": "正常"}
        mock_uoa.return_value = []

        mock_iv_metrics = MagicMock()
        mock_iv_metrics.iv_rank = 35.0
        mock_iv_metrics.iv_percentile = 38.0
        mock_iv_metrics.current_iv = 0.45
        mock_iv_metrics.expected_move_weekly = 5.0
        mock_iv_metrics.iv_status = "Normal"
        mock_iv_metrics.is_premarket = False
        mock_iv.return_value = mock_iv_metrics

        mock_hist.return_value = pd.DataFrame({"Close": [100.0, 105.0]})
        mock_reddit.return_value = "看多情緒高漲"
        mock_ddp.return_value = {"is_ddp": True}
        poly_market = MagicMock()
        poly_market.question = "Will NVDA exceed $130?"
        poly_market.tokens = [{"outcome": "Yes", "price": "0.65"}]
        mock_poly.return_value = [poly_market]

        mock_ctx = MagicMock()
        mock_ctx.capital = 100000.0
        mock_user_ctx.return_value = mock_ctx

        await cog.symbol_hub.callback(cog, mock_interaction, symbol="NVDA")

        assert mock_interaction.followup.send.called
        _, kwargs = mock_interaction.followup.send.call_args
        assert "view" in kwargs
        assert isinstance(kwargs["view"], SymbolHubView)
        embed = kwargs["embed"]
        assert "標的分析中心: NVDA" in embed.title


@pytest.mark.asyncio
async def test_portfolio_hub_command(mock_interaction, mock_bot):
    cog = UnifiedTerminalCog(mock_bot)

    with patch(
        "services.trading_service.TradingService.get_portfolio_pnl",
        new_callable=AsyncMock,
    ) as mock_pnl, patch(
        "services.market_data_service.get_macro_environment", new_callable=AsyncMock
    ) as mock_macro, patch("database.get_full_user_context") as mock_user_ctx:
        mock_pnl.return_value = {"trades": [], "total_unrealized_pnl": 0.0}
        mock_macro.return_value = {"vix": 18.0}

        mock_ctx = MagicMock()
        mock_ctx.capital = 112511.0
        mock_ctx.total_theta = 50.0
        mock_ctx.monthly_expense = 1500.0
        mock_ctx.cash_reserve = 5000.0
        mock_ctx.is_professional_mode = False  # Spectator Mode
        mock_ctx.total_weighted_delta = 10.0
        mock_ctx.total_vanna = 2.0

        mock_user_ctx.return_value = mock_ctx

        await cog.portfolio_hub.callback(cog, mock_interaction)

        mock_interaction.followup.send.assert_called_once()
        _, kwargs = mock_interaction.followup.send.call_args
        assert "view" in kwargs
        assert isinstance(kwargs["view"], PortfolioHubView)
        embed = kwargs["embed"]
        assert "Nexus 交易員戰略看板" in embed.title
        # Verify content reflects Spectator Mode
        assert "觀戰模式" in embed.fields[0].value


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
