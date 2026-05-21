from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.analyst_agent import AnalystAgent


@pytest.mark.asyncio
async def test_dispatch_intraday_guide_uses_builder():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = AnalystAgent(bot)

    embed = object()

    with patch("psutil.virtual_memory") as mock_vmem, patch(
        "market_analysis.portfolio.refresh_portfolio_greeks", new_callable=AsyncMock
    ), patch(
        "services.asset_manager.AssetManager.get_assets"
    ) as mock_get_assets, patch(
        "market_analysis.pro_management.calculate_financial_runway", return_value=365.0
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
        new_callable=AsyncMock,
        return_value={"skew": 3.0, "state": "Normal"},
    ), patch("database.get_all_user_ids", return_value=[1]), patch(
        "database.get_full_user_context"
    ) as mock_ctx, patch(
        "cogs.analyst_agent.create_intraday_execution_guide_embed",
        return_value=embed,
    ) as mock_builder:
        mock_mem = MagicMock()
        mock_mem.percent = 50.0
        mock_vmem.return_value = mock_mem

        trade_asset = MagicMock()
        trade_asset.metadata = {
            "opt_type": "call",
            "strike": 150.0,
            "expiry": "2024-12-31",
            "entry_price": 5.0,
            "quantity": 1,
            "weighted_delta": 100.0,
            "vanna": 10.0,
        }
        mock_get_assets.side_effect = (
            lambda uid, context_type: [trade_asset]
            if context_type.name == "TRADE"
            else []
        )

        user_ctx = MagicMock()
        user_ctx.enable_analyst_agent = True
        user_ctx.capital = 100000.0
        user_ctx.cash_reserve = 10000.0
        user_ctx.monthly_expense = 1000.0
        user_ctx.total_theta = 50.0
        mock_ctx.return_value = user_ctx

        cog._fetch_macro_data = AsyncMock(return_value={"vix": 15.0})

        await cog.dispatch_intraday_guide()

    mock_builder.assert_called_once()
    kwargs = mock_builder.call_args.kwargs
    assert kwargs["is_memory_gated"] is False
    assert kwargs["phase_name"] == "流動性與開盤波動 (Phase A)"
    assert "早盤流動性" in kwargs["active_signal_content"]
    bot.queue_dm.assert_awaited_once_with(1, embed=embed)


@pytest.mark.asyncio
async def test_dispatch_intraday_guide_memory_gate_uses_builder():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = AnalystAgent(bot)

    embed = object()

    with patch("psutil.virtual_memory") as mock_vmem, patch(
        "database.get_all_user_ids", return_value=[1]
    ), patch("database.get_full_user_context") as mock_ctx, patch(
        "cogs.analyst_agent.create_intraday_execution_guide_embed",
        return_value=embed,
    ) as mock_builder:
        mock_mem = MagicMock()
        mock_mem.percent = 90.0
        mock_vmem.return_value = mock_mem

        user_ctx = MagicMock()
        user_ctx.enable_analyst_agent = True
        mock_ctx.return_value = user_ctx

        cog._fetch_macro_data = AsyncMock(return_value={"vix": 15.0})

        await cog.dispatch_intraday_guide()

    mock_builder.assert_called_once()
    kwargs = mock_builder.call_args.kwargs
    assert kwargs["is_memory_gated"] is True
    assert kwargs["memory_percent"] == 90.0
    bot.queue_dm.assert_awaited_once_with(1, embed=embed)
