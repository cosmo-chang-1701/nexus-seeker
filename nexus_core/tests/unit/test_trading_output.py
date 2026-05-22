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
        "cogs.trading.create_ditm_transition_alert_embed", return_value=embed
    ) as mock_builder:
        await cog.monitor_vtr_task()

    mock_builder.assert_called_once()
    kwargs = mock_builder.call_args.kwargs
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
        "cogs.trading.create_vtr_settlement_notice_embed", return_value=embed
    ) as mock_builder:
        await cog.monitor_vtr_task()

    mock_builder.assert_called_once()
    kwargs = mock_builder.call_args.kwargs
    assert kwargs["status_icon"] == "🔄 [轉倉完成]"
    assert kwargs["symbol"] == "QQQ"
    assert kwargs["regime"] == "Balanced"
    bot.queue_dm.assert_awaited_once_with(1, embed=embed)


@pytest.mark.asyncio
async def test_dispatch_watchlist_heartbeat_sends_all_watchlist_symbols():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = SchedulerCog(bot)

    eval_aapl = MagicMock()
    eval_aapl.metrics.symbol = "AAPL"
    eval_aapl.metrics.option_skew = 3.2
    eval_aapl.metrics.option_skew_state = "正常"
    eval_aapl.tactical.alert_level = "green"
    eval_aapl.event_context.summary = "未偵測到近期需調整參數的重大事件。"

    eval_nvda = MagicMock()
    eval_nvda.metrics.symbol = "NVDA"
    eval_nvda.metrics.option_skew = 6.8
    eval_nvda.metrics.option_skew_state = "⚠️ 預警性對沖 (Put 昂貴)"
    eval_nvda.tactical.alert_level = "yellow"
    eval_nvda.event_context.summary = (
        "CPI 倒數 12.0 小時 ｜ 先縮口數，優先定義風險的 Debit Spread / 保護性部位。"
    )

    with patch(
        "services.calendar_service.calendar_service.get_next_high_impact_event",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(
            event="CPI", time="2026-05-22T12:30:00Z", tte_hours=12.0
        ),
    ), patch(
        "services.calendar_service.calendar_service.get_symbol_earnings_batch",
        new_callable=AsyncMock,
        return_value={"AAPL": None, "NVDA": None},
    ), patch(
        "market_analysis.intraday_pipeline.evaluate_watchlist_symbol",
        new_callable=AsyncMock,
        side_effect=[eval_aapl, eval_nvda],
    ), patch(
        "database.get_full_user_context",
        side_effect=[
            SimpleNamespace(capital=100000.0, risk_limit=15.0, option_alert_mode=1),
        ],
    ), patch(
        "database.is_symbol_in_portfolio",
        side_effect=[False, True],
    ), patch(
        "database.get_user_holdings",
        return_value=[{"symbol": "NVDA", "quantity": 100.0, "avg_cost": 900.0}],
    ), patch(
        "ui.formatter.generate_ansi_watchlist_report",
        side_effect=["AAPL report", "NVDA report"],
    ), patch(
        "market_analysis.intraday_pipeline.derive_watchlist_option_guidance",
        side_effect=["AAPL guidance", "NVDA guidance"],
    ) as mock_guidance, patch(
        "market_analysis.intraday_pipeline.build_watchlist_option_plan",
        new_callable=AsyncMock,
        side_effect=[object(), object()],
    ), patch(
        "services.llm_service.generate_watchlist_skew_commentary",
        new_callable=AsyncMock,
        side_effect=["AAPL skew", "NVDA skew"],
    ), patch(
        "services.llm_service.generate_watchlist_roundup_commentary",
        new_callable=AsyncMock,
        return_value="本輪先留意 NVDA 的事件風險與偏左 skew，AAPL 仍以例行追蹤為主。",
    ), patch(
        "cogs.trading.create_watchlist_overview_embed",
        return_value=object(),
    ) as mock_overview_builder, patch(
        "cogs.trading.create_watchlist_signal_embed",
        side_effect=[object(), object()],
    ) as mock_builder:
        await cog._dispatch_watchlist_heartbeat(
            [(1, "AAPL", 1), (1, "NVDA", 1), (1, "AAPL", 1)]
        )

    assert mock_builder.call_count == 2
    mock_overview_builder.assert_called_once()
    assert bot.queue_dm.await_count == 3
    assert mock_guidance.call_args_list[0].kwargs["has_position"] is False
    assert mock_guidance.call_args_list[1].kwargs["has_position"] is True
    assert mock_builder.call_args_list[0].kwargs["option_guidance"] == "AAPL guidance"
    assert mock_builder.call_args_list[0].kwargs["skew_commentary"] == "AAPL skew"
    assert mock_builder.call_args_list[0].kwargs["has_position"] is False
    assert mock_builder.call_args_list[0].kwargs["holding_quantity"] is None
    assert mock_builder.call_args_list[1].kwargs["skew_commentary"] == "NVDA skew"
    assert mock_builder.call_args_list[1].kwargs["has_position"] is True
    assert mock_builder.call_args_list[1].kwargs["holding_quantity"] == 100.0
    assert mock_builder.call_args_list[1].kwargs["holding_avg_cost"] == 900.0


@pytest.mark.asyncio
async def test_dispatch_watchlist_heartbeat_honors_portfolio_only_mode():
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = SchedulerCog(bot)

    eval_aapl = MagicMock()
    eval_aapl.metrics.symbol = "AAPL"
    eval_aapl.metrics.option_skew = 3.2
    eval_aapl.metrics.option_skew_state = "正常"
    eval_aapl.tactical.alert_level = "green"
    eval_aapl.event_context.summary = "未偵測到近期需調整參數的重大事件。"

    eval_nvda = MagicMock()
    eval_nvda.metrics.symbol = "NVDA"
    eval_nvda.metrics.option_skew = 6.8
    eval_nvda.metrics.option_skew_state = "⚠️ 預警性對沖 (Put 昂貴)"
    eval_nvda.tactical.alert_level = "yellow"
    eval_nvda.event_context.summary = (
        "CPI 倒數 12.0 小時 ｜ 先縮口數，優先定義風險的 Debit Spread / 保護性部位。"
    )

    with patch(
        "services.calendar_service.calendar_service.get_next_high_impact_event",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(
            event="CPI", time="2026-05-22T12:30:00Z", tte_hours=12.0
        ),
    ), patch(
        "services.calendar_service.calendar_service.get_symbol_earnings_batch",
        new_callable=AsyncMock,
        return_value={"AAPL": None, "NVDA": None},
    ), patch(
        "market_analysis.intraday_pipeline.evaluate_watchlist_symbol",
        new_callable=AsyncMock,
        side_effect=[eval_aapl, eval_nvda],
    ), patch(
        "database.get_full_user_context",
        return_value=SimpleNamespace(
            capital=100000.0, risk_limit=15.0, option_alert_mode=2
        ),
    ), patch(
        "database.is_symbol_in_portfolio",
        side_effect=[False, True],
    ), patch(
        "database.get_user_holdings",
        return_value=[{"symbol": "NVDA", "quantity": 100.0, "avg_cost": 900.0}],
    ), patch(
        "ui.formatter.generate_ansi_watchlist_report",
        return_value="NVDA report",
    ), patch(
        "market_analysis.intraday_pipeline.derive_watchlist_option_guidance",
        return_value="NVDA holding guidance",
    ) as mock_guidance, patch(
        "market_analysis.intraday_pipeline.build_watchlist_option_plan",
        new_callable=AsyncMock,
        return_value=object(),
    ), patch(
        "services.llm_service.generate_watchlist_skew_commentary",
        new_callable=AsyncMock,
        side_effect=["AAPL skew", "NVDA skew"],
    ), patch(
        "services.llm_service.generate_watchlist_roundup_commentary",
        new_callable=AsyncMock,
        return_value="本輪僅推送持倉內標的。",
    ), patch(
        "cogs.trading.create_watchlist_overview_embed",
        return_value=object(),
    ), patch(
        "cogs.trading.create_watchlist_signal_embed",
        return_value=object(),
    ) as mock_builder:
        await cog._dispatch_watchlist_heartbeat([(1, "AAPL", 1), (1, "NVDA", 1)])

    mock_guidance.assert_called_once_with(
        eval_nvda.metrics,
        eval_nvda.tactical,
        event_context=eval_nvda.event_context,
        has_position=True,
    )
    mock_builder.assert_called_once()
    assert mock_builder.call_args.kwargs["holding_quantity"] == 100.0
    assert mock_builder.call_args.kwargs["holding_avg_cost"] == 900.0
    assert bot.queue_dm.await_count == 2
