from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.trading import SchedulerCog


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_uses_helpers():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = SchedulerCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(
        return_value=[
            {
                "uid": 1,
                "type": "PROFIT_LOCK",
                "symbol": "AAPL",
                "pnl_pct": 180,
                "dte": 5,
                "reason": "Delta 已接近 1.0",
            },
            {"uid": 2, "type": "GAMMA_FRAGILITY", "net_gamma": -25.5, "threshold": -20},
        ]
    )
    embed1 = object()
    embed2 = object()

    with patch("cogs.trading.market_time.is_market_open", return_value=True), patch(
        "cogs.trading.create_profit_lock_alert_embed", return_value=embed1
    ) as mock_profit, patch(
        "cogs.trading.create_gamma_fragility_embed", return_value=embed2
    ) as mock_gamma:
        await cog.monitor_real_portfolio_task()

    mock_profit.assert_called_once()
    mock_gamma.assert_called_once()
    assert bot.queue_dm.await_args_list[0].kwargs == {"embed": embed1}
    assert bot.queue_dm.await_args_list[1].kwargs == {"embed": embed2}


@pytest.mark.asyncio
async def test_pre_market_risk_monitor_uses_helper():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.fetch_user = AsyncMock(return_value=SimpleNamespace(id=1))

    with patch("discord.ext.tasks.Loop.start"):
        cog = SchedulerCog(bot)

    cog.trading_service.get_pre_market_alerts_data = AsyncMock(
        return_value={
            1: {
                "alerts": [
                    {
                        "symbol": "NVDA",
                        "is_portfolio": True,
                        "earnings_date": "2026-06-01",
                        "days_left": 3,
                    }
                ],
                "scanned_symbols": ["NVDA"],
            }
        }
    )
    embed = object()

    with patch(
        "cogs.trading.market_time.nyse_calendar.schedule",
        return_value=SimpleNamespace(empty=False),
    ), patch(
        "cogs.trading.create_pre_market_earnings_embed", return_value=embed
    ) as mock_builder:
        await cog.pre_market_risk_monitor()

    mock_builder.assert_called_once_with(
        [
            {
                "symbol": "NVDA",
                "is_portfolio": True,
                "earnings_date": "2026-06-01",
                "days_left": 3,
            }
        ],
        ["NVDA"],
        cog.EARNINGS_WARNING_DAYS,
    )
    bot.queue_dm.assert_awaited_once_with(1, embed=embed)


@pytest.mark.asyncio
async def test_monitor_vtr_task_uses_ditm_helper():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = SchedulerCog(bot)

    cog.trading_service.monitor_vtr_and_calculate_hedging = AsyncMock(
        return_value=[
            {
                "uid": 1,
                "trade_info": {
                    "symbol": "TSLA",
                    "status": "CLOSED",
                    "pnl": 1250.0,
                    "tags": ["DITM", "exit_reason:Delta 接近 1.0"],
                },
                "hedge": {"action": "賣出 10 股 SPY", "gap": 10},
                "current_total_delta": 25.0,
                "spy_price": 500.0,
                "user_capital": 100000.0,
            }
        ]
    )
    embed = object()

    with patch("cogs.trading.market_time.is_market_open", return_value=True), patch(
        "cogs.trading.create_option_defense_alert_embed", return_value=embed
    ) as mock_builder:
        await cog.monitor_vtr_task()

    mock_builder.assert_called_once()
    kwargs = mock_builder.call_args.kwargs
    assert kwargs["is_live"] is False
    assert kwargs["symbol"] == "TSLA"
    assert kwargs["action_taken"] == "已平倉 (Closed)"
    assert kwargs["exposure_pct"] == 12.5
    bot.queue_dm.assert_awaited_once_with(1, embed=embed)


@pytest.mark.asyncio
async def test_monitor_vtr_task_uses_settlement_helper_for_non_ditm():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = SchedulerCog(bot)

    cog.trading_service.monitor_vtr_and_calculate_hedging = AsyncMock(
        return_value=[
            {
                "uid": 1,
                "trade_info": {
                    "symbol": "QQQ",
                    "status": "ROLLED",
                    "pnl": 420.0,
                    "tags": [],
                },
                "hedge": {"action": "買入 3 股 SPY", "gap": 3},
                "current_total_delta": 10.0,
                "spy_price": 500.0,
                "user_capital": 100000.0,
                "regime": "Balanced",
                "target_delta": 8.0,
            }
        ]
    )
    embed = object()

    with patch("cogs.trading.market_time.is_market_open", return_value=True), patch(
        "cogs.trading.create_option_defense_alert_embed", return_value=embed
    ) as mock_builder:
        await cog.monitor_vtr_task()

    mock_builder.assert_called_once()
    kwargs = mock_builder.call_args.kwargs
    assert kwargs["is_live"] is False
    assert kwargs["status_icon"] == "🔄"
    assert kwargs["symbol"] == "QQQ"
    assert kwargs["regime"] == "Balanced"
    bot.queue_dm.assert_awaited_once_with(1, embed=embed)


@pytest.mark.asyncio
async def test_dispatch_watchlist_heartbeat_sends_all_watchlist_symbols():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    mock_terminal = MagicMock()
    mock_terminal._fetch_sym_radar_data = AsyncMock(
        side_effect=lambda sym: {
            "symbol": sym,
            "quote": {"c": 150.0, "dp": 1.2},
            "iv_metrics": {"iv_rank": 30.0, "expected_move_weekly": 4.5},
            "skew": 1.1,
            "skew_percentile": 75.0,
            "max_pain": {"max_pain": 145.0},
            "uoa": [],
        }
    )
    bot.get_cog.return_value = mock_terminal

    with patch("discord.ext.tasks.Loop.start"):
        cog = SchedulerCog(bot)

    with patch(
        "database.get_full_user_context",
        return_value=SimpleNamespace(
            capital=100000.0, risk_limit=15.0, option_alert_mode=1
        ),
    ), patch(
        "database.is_symbol_in_portfolio",
        side_effect=[False, True],
    ), patch(
        "database.is_notification_enabled",
        return_value=True,
    ), patch(
        "cogs.embed_builder.build_radar_scan_embed",
        return_value=object(),
    ) as mock_builder:
        await cog._dispatch_watchlist_heartbeat(
            [(1, "AAPL", 1), (1, "NVDA", 1), (1, "AAPL", 1)]
        )

    # AAPL is duplicate in list, so unique AAPL and NVDA are fetched
    assert mock_terminal._fetch_sym_radar_data.call_count == 2
    mock_builder.assert_called_once()
    assert bot.queue_dm.await_count == 1


@pytest.mark.asyncio
async def test_dispatch_watchlist_heartbeat_honors_portfolio_only_mode():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    mock_terminal = MagicMock()
    mock_terminal._fetch_sym_radar_data = AsyncMock(
        side_effect=lambda sym: {
            "symbol": sym,
            "quote": {"c": 150.0, "dp": 1.2},
            "iv_metrics": {"iv_rank": 30.0, "expected_move_weekly": 4.5},
            "skew": 1.1,
            "skew_percentile": 75.0,
            "max_pain": {"max_pain": 145.0},
            "uoa": [],
        }
    )
    bot.get_cog.return_value = mock_terminal

    with patch("discord.ext.tasks.Loop.start"):
        cog = SchedulerCog(bot)

    with patch(
        "database.get_full_user_context",
        return_value=SimpleNamespace(
            capital=100000.0, risk_limit=15.0, option_alert_mode=2
        ),
    ), patch(
        "database.is_symbol_in_portfolio",
        side_effect=[
            False,
            True,
        ],  # AAPL has no position (False), NVDA has position (True)
    ), patch(
        "database.is_notification_enabled",
        return_value=True,
    ), patch(
        "cogs.embed_builder.build_radar_scan_embed",
        return_value=object(),
    ) as mock_builder:
        await cog._dispatch_watchlist_heartbeat([(1, "AAPL", 1), (1, "NVDA", 1)])

    # Only NVDA has position, so only NVDA should be fetched and scanned
    mock_terminal._fetch_sym_radar_data.assert_called_once_with("NVDA")
    mock_builder.assert_called_once()
    assert bot.queue_dm.await_count == 1


@pytest.mark.asyncio
async def test_monitor_vtr_task_handles_missing_trade_info():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = SchedulerCog(bot)

    # Mock the return value to contain a transition suggestion (which lacks trade_info)
    # and a valid VTR hedging result.
    cog.trading_service.monitor_vtr_and_calculate_hedging = AsyncMock(
        return_value=[
            {
                "uid": 1,
                "type": "TRANSITION_SUGGESTION",
                "symbol": "AAPL",
                "pnl_pct": 10.0,
                "pnl_usd": 100.0,
                "transition_result": {},
                "stock_price": 175.0,
            },
            {
                "uid": 2,
                "trade_info": {
                    "symbol": "TSLA",
                    "status": "CLOSED",
                    "pnl": 1250.0,
                    "tags": ["DITM", "exit_reason:Delta 接近 1.0"],
                },
                "hedge": {"action": "賣出 10 股 SPY", "gap": 10},
                "current_total_delta": 25.0,
                "spy_price": 500.0,
                "user_capital": 100000.0,
            },
        ]
    )
    embed = object()

    with patch("cogs.trading.market_time.is_market_open", return_value=True), patch(
        "cogs.trading.create_option_defense_alert_embed", return_value=embed
    ) as mock_builder:
        # This call should execute successfully and not crash with KeyError
        await cog.monitor_vtr_task()

    # Verify only the valid trade (uid 2) triggered an alert and queued a DM
    mock_builder.assert_called_once()
    bot.queue_dm.assert_awaited_once_with(2, embed=embed)


@pytest.mark.asyncio
async def test_dispatch_order_telemetry_alignment_alert_success():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = SchedulerCog(bot)

    mock_orders = [
        {
            "id": 100,
            "user_id": 1,
            "symbol": "AAPL",
            "quantity": 10.0,
            "order_type": "LIMIT",
            "limit_price": 150.0,
            "side": "BUY",
        }
    ]

    mock_alignment_item = {
        "symbol": "AAPL",
        "order_id": 100,
        "order_type": "LIMIT",
        "price_label": "掛單限價",
        "current_price": 150.0,
        "original_qty": 10,
        "suggested_price": 145.0,
        "suggested_qty": 8,
        "is_size_down": True,
        "holding_type_label": "LEVERAGED",
        "holding_shares": 0,
        "holding_status": "空倉待命",
        "avg_cost": 0.0,
        "live_price": 152.0,
        "gain_loss_pct": 0.0,
        "put_wall": 140.0,
        "wall_dist_pct": 7.89,
        "wall_status": "上方緩衝",
        "skew_val": 1.2,
        "skew_pct": 50.0,
        "skew_status": "平穩",
        "iv_val": 35.0,
        "iv_rank": 30.0,
        "iv_status": "Normal",
        "proximity_pct": 1.31,
        "radar_status": "偏離擴大",
        "system_status_flag": "TELEMETRY ACTIVE",
        "system_instruction_directive": "通過實時防線，維持紀律掛單。",
        "is_premarket": False,
        "iv_source": "LIVE_IV",
        "side": "BUY",
    }

    mock_embed = object()

    with patch(
        "database.orders.get_all_active_orders", return_value=mock_orders
    ), patch(
        "services.calendar_service.calendar_service.get_high_impact_events",
        new=AsyncMock(return_value=[]),
    ), patch("database.is_notification_enabled", return_value=True), patch(
        "database.get_user_holdings", return_value=[]
    ), patch("database.get_user_portfolio", return_value=[]), patch(
        "services.order_telemetry_service.resolve_holding_type_and_rows",
        return_value=("LEVERAGED", {}),
    ), patch(
        "services.order_telemetry_service.build_telemetry_alignment_items",
        new=AsyncMock(return_value=([mock_alignment_item], False)),
    ), patch(
        "cogs.trading.create_telemetry_alignment_embeds",
        return_value=[mock_embed],
    ) as mock_embed_builder:
        await cog._dispatch_order_telemetry_alignment_alert()

    mock_embed_builder.assert_called_once_with(
        [mock_alignment_item],
        truncated=False,
        include_apply_button_hint=False,
        scheduled_mode=True,
    )
    bot.queue_dm.assert_awaited_once_with(1, embed=mock_embed)
