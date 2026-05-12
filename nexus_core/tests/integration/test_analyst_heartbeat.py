import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import sys
import os
import datetime

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from cogs.analyst_agent import AnalystAgent


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    bot.queue_dm = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_dispatch_intraday_guide_phases(mock_bot):
    cog = AnalystAgent(mock_bot)

    with patch("psutil.virtual_memory") as mock_vmem, patch(
        "market_analysis.portfolio.refresh_portfolio_greeks", new_callable=AsyncMock
    ), patch(
        "services.asset_manager.AssetManager.get_assets"
    ) as mock_get_assets, patch(
        "market_analysis.pro_management.calculate_financial_runway"
    ) as mock_runway, patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
        new_callable=AsyncMock,
    ) as mock_skew, patch("database.get_all_user_ids") as mock_users, patch(
        "database.get_full_user_context"
    ) as mock_ctx, patch("cogs.analyst_agent.datetime") as mock_datetime:
        # Mock standard data
        mock_mem = MagicMock()
        mock_mem.percent = 50.0  # Normal memory
        mock_vmem.return_value = mock_mem

        # Mock assets
        mock_trade_asset = MagicMock()
        mock_trade_asset.metadata = {
            "opt_type": "call",
            "strike": 150.0,
            "expiry": "2024-12-31",
            "entry_price": 5.0,
            "quantity": 1,
            "weighted_delta": 100.0,
            "vanna": 10.0,
        }
        mock_get_assets.side_effect = (
            lambda uid, context_type: [mock_trade_asset]
            if context_type.name == "TRADE"
            else []
        )
        mock_runway.return_value = 365.0
        mock_skew.return_value = {"skew": 3.0, "state": "Normal"}

        mock_users.return_value = [1]
        mock_user_ctx = MagicMock()
        mock_user_ctx.enable_analyst_agent = True
        mock_user_ctx.capital = 100000.0
        mock_user_ctx.cash_reserve = 10000.0
        mock_user_ctx.monthly_expense = 1000.0
        mock_user_ctx.total_theta = 50.0
        mock_ctx.return_value = mock_user_ctx

        # Override _fetch_macro_data on the cog instance
        cog._fetch_macro_data = AsyncMock(return_value={"vix": 15.0})

        # Test Phase A (hour < 11)
        mock_now = datetime.datetime(2024, 1, 1, 10, 0, 0)
        mock_datetime.now.return_value = mock_now
        mock_datetime.side_effect = lambda *args, **kwargs: datetime.datetime(
            *args, **kwargs
        )

        await cog.dispatch_intraday_guide()

        # Verify
        mock_bot.queue_dm.assert_called_once()
        args, kwargs = mock_bot.queue_dm.call_args
        embed = kwargs["embed"]
        assert "Phase A" in embed.title
        assert "早盤流動性" in embed.fields[2].value  # Active Signal field

        # Reset
        mock_bot.queue_dm.reset_mock()

        # Test Phase B (11 <= hour < 14)
        mock_now = datetime.datetime(2024, 1, 1, 12, 0, 0)
        mock_datetime.now.return_value = mock_now

        await cog.dispatch_intraday_guide()

        mock_bot.queue_dm.assert_called_once()
        args, kwargs = mock_bot.queue_dm.call_args
        embed = kwargs["embed"]
        assert "Phase B" in embed.title
        assert "板塊輪動" in embed.fields[2].value

        # Reset
        mock_bot.queue_dm.reset_mock()

        # Test Phase C (hour >= 14)
        mock_now = datetime.datetime(2024, 1, 1, 15, 0, 0)
        mock_datetime.now.return_value = mock_now

        await cog.dispatch_intraday_guide()

        mock_bot.queue_dm.assert_called_once()
        args, kwargs = mock_bot.queue_dm.call_args
        embed = kwargs["embed"]
        assert "Phase C" in embed.title
        assert "強制對沖建議" in embed.fields[2].value


@pytest.mark.asyncio
async def test_dispatch_intraday_guide_memory_gate(mock_bot):
    cog = AnalystAgent(mock_bot)

    with patch("psutil.virtual_memory") as mock_vmem, patch(
        "database.get_all_user_ids"
    ) as mock_users, patch("database.get_full_user_context") as mock_ctx:
        # Trigger memory gate
        mock_mem = MagicMock()
        mock_mem.percent = 90.0  # > 85.0 triggers gate
        mock_vmem.return_value = mock_mem

        mock_users.return_value = [1]
        mock_user_ctx = MagicMock()
        mock_user_ctx.enable_analyst_agent = True
        mock_ctx.return_value = mock_user_ctx

        cog._fetch_macro_data = AsyncMock(return_value={"vix": 15.0})

        await cog.dispatch_intraday_guide()

        mock_bot.queue_dm.assert_called_once()
        args, kwargs = mock_bot.queue_dm.call_args
        embed = kwargs["embed"]

        assert "Memory Safety Gate Active" in embed.description
        assert "90.0%" in embed.fields[0].value
