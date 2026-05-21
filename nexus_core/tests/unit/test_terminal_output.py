from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.terminal import TerminalCog


@pytest.mark.asyncio
async def test_runway_check_uses_builder(mock_interaction):
    bot = MagicMock()
    cog = TerminalCog(bot)
    embed = object()
    ctx = SimpleNamespace(
        monthly_expense=5000.0, cash_reserve=20000.0, total_theta=100.0
    )
    holding = SimpleNamespace(
        symbol="AAPL", metadata={"quantity": 10, "avg_cost": 150.0}
    )

    with (
        patch("market_analysis.portfolio.refresh_portfolio_greeks", new=AsyncMock()),
        patch("cogs.terminal.get_full_user_context", return_value=ctx),
        patch(
            "market_analysis.pro_management.calculate_financial_runway",
            side_effect=[120.0, 180.0],
        ),
        patch("services.asset_manager.AssetManager") as mock_manager_cls,
        patch(
            "services.market_data_service.get_quote",
            new=AsyncMock(return_value={"c": 200.0}),
        ),
        patch(
            "cogs.terminal.create_financial_runway_embed", return_value=embed
        ) as mock_builder,
    ):
        mock_manager_cls.return_value.get_assets.return_value = [holding]
        await cog.runway_check.callback(cog, mock_interaction)

    mock_builder.assert_called_once()
    kwargs = mock_builder.call_args.kwargs
    assert kwargs["backup_liquidity"] == 1600.0
    assert kwargs["total_holding_value"] == 2000.0
    assert kwargs["ratio"] == pytest.approx(0.6)
    mock_interaction.followup.send.assert_called_once_with(embed=embed, ephemeral=True)


@pytest.mark.asyncio
async def test_sys_health_uses_builder(mock_interaction):
    bot = MagicMock()
    bot.polymarket_service = SimpleNamespace(
        _market_cache={1: 1}, _order_books={1: 1, 2: 2}
    )
    cog = TerminalCog(bot)
    embed = object()

    with (
        patch("psutil.virtual_memory") as mock_mem,
        patch("psutil.disk_usage") as mock_disk,
        patch("psutil.cpu_percent", return_value=10.0),
        patch("psutil.Process") as mock_process,
        patch("services.market_data_service._sma_cache", {1: 1, 2: 2}),
        patch("services.market_data_service._ema_cache", {1: 1}),
        patch(
            "cogs.terminal.create_system_health_embed", return_value=embed
        ) as mock_builder,
    ):
        mock_mem.return_value.percent = 50.0
        mock_mem.return_value.available = 512 * 1024 * 1024
        mock_disk.return_value.percent = 40.0
        mock_disk.return_value.free = 10 * 1024 * 1024 * 1024
        mock_process.return_value.memory_info.return_value.rss = 256 * 1024 * 1024

        await cog.sys_health.callback(cog, mock_interaction)

    mock_builder.assert_called_once()
    kwargs = mock_builder.call_args.kwargs
    assert kwargs["sma_cache_size"] == 2
    assert kwargs["poly_cache_size"] == 1
    assert kwargs["orderbook_size"] == 2
    mock_interaction.followup.send.assert_called_once_with(embed=embed, ephemeral=True)


@pytest.mark.asyncio
async def test_promote_watch_uses_builder(mock_interaction):
    bot = MagicMock()
    cog = TerminalCog(bot)
    embed = object()

    with (
        patch(
            "services.market_data_service.validate_symbol",
            new=AsyncMock(return_value=True),
        ),
        patch("services.asset_manager.AssetManager") as mock_manager_cls,
        patch("market_analysis.portfolio.refresh_portfolio_greeks", new=AsyncMock()),
        patch(
            "cogs.terminal.create_asset_promotion_embed", return_value=embed
        ) as mock_builder,
    ):
        mock_manager_cls.return_value.promote_to_trade.return_value = True
        await cog.promote_watch.callback(
            cog,
            mock_interaction,
            symbol="aapl",
            opt_type="call",
            strike=150.0,
            expiry="2026-06-19",
            price=5.5,
            qty=2,
        )

    mock_builder.assert_called_once_with(
        symbol="AAPL",
        expiry="2026-06-19",
        strike=150.0,
        opt_type="call",
        quantity=2,
        price=5.5,
    )
    mock_interaction.followup.send.assert_called_once_with(embed=embed, ephemeral=True)


@pytest.mark.asyncio
async def test_transition_sim_uses_builder(mock_interaction):
    bot = MagicMock()
    cog = TerminalCog(bot)
    embed = object()
    result = SimpleNamespace(
        initial_pnl=2500.0,
        additional_capital_required=7500.0,
        adjusted_cost_basis=92.5,
        projected_aroc=18.0,
        capital_efficiency_gain=2.7,
    )

    with (
        patch(
            "services.market_data_service.get_quote",
            new=AsyncMock(return_value={"c": 100.0}),
        ),
        patch(
            "market_analysis.pro_management.simulate_pro_transition",
            return_value=result,
        ),
        patch(
            "cogs.terminal.create_transition_simulation_embed", return_value=embed
        ) as mock_builder,
    ):
        await cog.transition_sim.callback(
            cog,
            mock_interaction,
            symbol="nvda",
            current_option_pnl=2500.0,
            target_cc_strike=110.0,
            target_cc_premium=2.5,
        )

    mock_builder.assert_called_once_with(
        symbol="NVDA",
        current_price=100.0,
        initial_pnl=2500.0,
        additional_capital_required=7500.0,
        adjusted_cost_basis=92.5,
        target_cc_strike=110.0,
        target_cc_premium=2.5,
        projected_aroc=18.0,
        capital_efficiency_gain=2.7,
    )
    mock_interaction.followup.send.assert_called_once_with(embed=embed)
