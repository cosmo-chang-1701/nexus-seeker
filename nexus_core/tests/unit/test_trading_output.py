from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.trading.portfolio_monitor import PortfolioMonitorCog
from cogs.trading.pre_market import PreMarketCog


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_uses_helpers() -> None:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(  # type: ignore
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

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch(
        "cogs.trading.portfolio_monitor.create_profit_lock_alert_embed",
        return_value=embed1,
    ) as mock_profit, patch(
        "cogs.trading.portfolio_monitor.create_gamma_fragility_embed",
        return_value=embed2,
    ) as mock_gamma:
        await cog.monitor_real_portfolio_task()

    mock_profit.assert_called_once()
    mock_gamma.assert_called_once()
    assert bot.queue_dm.await_args_list[0].kwargs == {"embed": embed1}
    assert bot.queue_dm.await_args_list[1].kwargs == {"embed": embed2}


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_no_rollover_dm_when_no_trigger() -> None:
    """補足動態轉倉引擎後的硬性要求：三個場景 (再平衡/機會成本/保證金防禦)
    皆未觸發時，即便持倉非空，也絕不發送任何轉倉相關 DM。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(
        return_value=None
    )  # 無 UnifiedTerminalCog -> 跳過 radar 抓取

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "NVDA",
        "metadata": "{}",
        "quantity": 10.0,
        "avg_cost": 200.0,
    }

    cog.rollover_engine.check_satellite_rebalancing = AsyncMock(return_value=[])  # type: ignore
    cog.rollover_engine.evaluate_opportunity_cost_for_satellites = AsyncMock(  # type: ignore
        return_value=([], None)
    )
    cog.rollover_engine.evaluate_margin_defense = AsyncMock(return_value=[])  # type: ignore

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("database.holdings.get_all_holdings", return_value=[holding]), patch(
        "database.watchlist.get_user_watchlist", return_value=[]
    ), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ):
        await cog.monitor_real_portfolio_task()

    cog.rollover_engine.check_satellite_rebalancing.assert_awaited_once()
    cog.rollover_engine.evaluate_opportunity_cost_for_satellites.assert_awaited_once()
    cog.rollover_engine.evaluate_margin_defense.assert_awaited_once()
    bot.queue_dm.assert_not_called()


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_omits_unset_target_allocation_pct() -> None:
    """
    target_allocation_pct 目前無 DB 欄位持久化、也無 /settings UI 可設定。
    持倉本身沒有明確數值時，餵給 rollover_engine 的 asset dict 不應強行塞入
    預設值 (曾為 0.0)，否則 check_satellite_rebalancing 既有的
    asset.get("target_allocation_pct", max_alloc) fallback 永遠不會生效，
    導致常規減倉誤判為近乎全清倉。
    """
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "NVDA",
        "metadata": "{}",
        "quantity": 10.0,
        "avg_cost": 200.0,
        # 刻意不設定 target_allocation_pct，模擬真實生產資料形狀
    }

    cog.rollover_engine.check_satellite_rebalancing = AsyncMock(return_value=[])  # type: ignore
    cog.rollover_engine.evaluate_opportunity_cost_for_satellites = AsyncMock(  # type: ignore
        return_value=([], None)
    )
    cog.rollover_engine.evaluate_margin_defense = AsyncMock(return_value=[])  # type: ignore

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("database.holdings.get_all_holdings", return_value=[holding]), patch(
        "database.watchlist.get_user_watchlist", return_value=[]
    ), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ):
        await cog.monitor_real_portfolio_task()

    cog.rollover_engine.check_satellite_rebalancing.assert_awaited_once()
    await_args = cog.rollover_engine.check_satellite_rebalancing.await_args
    assert await_args is not None
    portfolio_assets = await_args.args[1]
    assert len(portfolio_assets) == 1
    assert "target_allocation_pct" not in portfolio_assets[0]


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_margin_defense_excludes_scenario2_and_3_flags() -> (
    None
):
    """
    Scenario 4 (槓桿與保證金防禦) 呼叫時傳入的 already_flagged_symbols 必須涵蓋
    Scenario 3 (核心衛星再平衡) 與 Scenario 2 (機會成本轉倉) 兩者已標記過的標的，
    避免同一標的同一輪次收到互相矛盾的清倉指令。
    """
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "NVDA",
        "metadata": "{}",
        "quantity": 10.0,
        "avg_cost": 200.0,
    }

    cog.rollover_engine.check_satellite_rebalancing = AsyncMock(  # type: ignore
        return_value=[{"symbol": "NVDA", "action": "REDUCE"}]
    )
    cog.rollover_engine.evaluate_opportunity_cost_for_satellites = AsyncMock(  # type: ignore
        return_value=([{"symbol": "AAPL", "action": "LIQUIDATE"}], None)
    )
    cog.rollover_engine.evaluate_margin_defense = AsyncMock(return_value=[])  # type: ignore

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("database.holdings.get_all_holdings", return_value=[holding]), patch(
        "database.watchlist.get_user_watchlist", return_value=[]
    ), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ):
        await cog.monitor_real_portfolio_task()

    cog.rollover_engine.evaluate_margin_defense.assert_awaited_once()
    await_args = cog.rollover_engine.evaluate_margin_defense.await_args
    assert await_args is not None
    assert await_args.kwargs["already_flagged_symbols"] == {"NVDA", "AAPL"}


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_hold_only_flags_do_not_suppress_later_scenarios() -> (
    None
):
    """
    修正回歸測試：Scenario 3 若僅回傳 HOLD 安心防守卡（無實際賣出/減碼動作），
    不應被計入 already_flagged，否則會 silently 阻擋同一標的在同一輪次收到
    更高等級的 Scenario 2 機會成本轉倉評估，或 Scenario 4 保證金強制平倉警報。
    """
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "NVDA",
        "metadata": "{}",
        "quantity": 10.0,
        "avg_cost": 200.0,
    }

    # Scenario 3 僅回傳 HOLD（無實際動作），不應被視為「已標記」
    cog.rollover_engine.check_satellite_rebalancing = AsyncMock(  # type: ignore
        return_value=[{"symbol": "NVDA", "action": "HOLD"}]
    )
    cog.rollover_engine.evaluate_opportunity_cost_for_satellites = AsyncMock(  # type: ignore
        return_value=([], None)
    )
    cog.rollover_engine.evaluate_margin_defense = AsyncMock(return_value=[])  # type: ignore

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("database.holdings.get_all_holdings", return_value=[holding]), patch(
        "database.watchlist.get_user_watchlist", return_value=[]
    ), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ):
        await cog.monitor_real_portfolio_task()

    # Scenario 2 呼叫時傳入的 already_flagged_symbols 不應包含僅 HOLD 的 NVDA
    opp_cost_call = (
        cog.rollover_engine.evaluate_opportunity_cost_for_satellites.await_args
    )
    assert opp_cost_call is not None
    assert opp_cost_call.args[2] == set()

    # Scenario 4 呼叫時傳入的 already_flagged_symbols 同樣不應包含僅 HOLD 的 NVDA
    margin_call = cog.rollover_engine.evaluate_margin_defense.await_args
    assert margin_call is not None
    assert margin_call.kwargs["already_flagged_symbols"] == set()


@pytest.mark.asyncio
async def test_pre_market_risk_monitor_triggers_pre_warm() -> None:
    bot = MagicMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = PreMarketCog(bot)

    with patch(
        "cogs.trading.pre_market.market_time.nyse_calendar.schedule",
        return_value=SimpleNamespace(empty=False),
    ), patch.object(cog, "_pre_warm_all_targets") as mock_pre_warm:
        await cog.pre_market_risk_monitor()
        # Since it's created as a task, we need to let the event loop run a bit or assert it was called.
        # However, asyncio.create_task wraps the coroutine. We can just patch `asyncio.create_task` directly if needed,
        # but mocking the method itself is simpler.
        # When mocked, _pre_warm_all_targets() returns a MagicMock, which is fine for create_task.
        mock_pre_warm.assert_called_once()


@pytest.mark.asyncio
async def test_monitor_vtr_task_uses_ditm_helper() -> None:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.monitor_vtr_and_calculate_hedging = AsyncMock(  # type: ignore
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

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch(
        "cogs.trading.portfolio_monitor.create_option_defense_alert_embed",
        return_value=embed,
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
async def test_monitor_vtr_task_uses_settlement_helper_for_non_ditm() -> None:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.monitor_vtr_and_calculate_hedging = AsyncMock(  # type: ignore
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

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch(
        "cogs.trading.portfolio_monitor.create_option_defense_alert_embed",
        return_value=embed,
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
async def test_dispatch_watchlist_heartbeat_sends_all_watchlist_symbols() -> Any:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    mock_terminal = MagicMock()
    mock_terminal._fetch_sym_radar_data_slow = AsyncMock(
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
        from cogs.trading.heartbeat import dispatch_watchlist_heartbeat

        await dispatch_watchlist_heartbeat(
            bot, [(1, "AAPL", 1), (1, "NVDA", 1), (1, "AAPL", 1)]
        )

    # AAPL is duplicate in list, so unique AAPL and NVDA are fetched
    assert mock_terminal._fetch_sym_radar_data_slow.call_count == 2
    mock_builder.assert_called_once()
    assert bot.queue_dm.await_count == 1


@pytest.mark.asyncio
async def test_dispatch_watchlist_heartbeat_syncs_symbols_to_edge_cache() -> Any:
    """心跳前應 best-effort 同步全體去重後的自選標的清單給 edge，
    讓背景排程知道該輪詢哪些標的。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    mock_terminal = MagicMock()
    mock_terminal._fetch_sym_radar_data_slow = AsyncMock(
        side_effect=lambda sym: {"symbol": sym, "quote": {"c": 150.0}}
    )
    bot.get_cog.return_value = mock_terminal

    with patch(
        "database.get_full_user_context",
        return_value=SimpleNamespace(
            capital=100000.0, risk_limit=15.0, option_alert_mode=1
        ),
    ), patch("database.is_symbol_in_portfolio", return_value=False), patch(
        "database.is_notification_enabled", return_value=True
    ), patch("cogs.embed_builder.build_radar_scan_embed", return_value=object()), patch(
        "services.edge_cache_client.sync_watchlist_symbols", new_callable=AsyncMock
    ) as mock_sync:
        from cogs.trading.heartbeat import dispatch_watchlist_heartbeat

        await dispatch_watchlist_heartbeat(
            bot, [(1, "AAPL", 1), (1, "NVDA", 1), (1, "AAPL", 1)]
        )

    mock_sync.assert_awaited_once()
    assert mock_sync.await_args is not None
    synced_symbols = mock_sync.await_args.args[0]
    assert set(synced_symbols) == {"AAPL", "NVDA"}


@pytest.mark.asyncio
async def test_dispatch_watchlist_heartbeat_survives_edge_sync_failure() -> Any:
    """edge 同步呼叫失敗時，心跳仍應照常完成推播，不受影響。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    mock_terminal = MagicMock()
    mock_terminal._fetch_sym_radar_data_slow = AsyncMock(
        side_effect=lambda sym: {"symbol": sym, "quote": {"c": 150.0}}
    )
    bot.get_cog.return_value = mock_terminal

    with patch(
        "database.get_full_user_context",
        return_value=SimpleNamespace(
            capital=100000.0, risk_limit=15.0, option_alert_mode=1
        ),
    ), patch("database.is_symbol_in_portfolio", return_value=False), patch(
        "database.is_notification_enabled", return_value=True
    ), patch(
        "cogs.embed_builder.build_radar_scan_embed", return_value=object()
    ) as mock_builder, patch(
        "services.edge_cache_client.sync_watchlist_symbols",
        new_callable=AsyncMock,
        side_effect=RuntimeError("edge unreachable"),
    ):
        from cogs.trading.heartbeat import dispatch_watchlist_heartbeat

        await dispatch_watchlist_heartbeat(bot, [(1, "AAPL", 1)])

    mock_builder.assert_called_once()
    assert bot.queue_dm.await_count == 1


@pytest.mark.asyncio
async def test_dispatch_watchlist_heartbeat_honors_portfolio_only_mode() -> Any:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    mock_terminal = MagicMock()
    mock_terminal._fetch_sym_radar_data_slow = AsyncMock(
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
        from cogs.trading.heartbeat import dispatch_watchlist_heartbeat

        await dispatch_watchlist_heartbeat(bot, [(1, "AAPL", 1), (1, "NVDA", 1)])

    # Only NVDA has position, so only NVDA should be fetched and scanned
    mock_terminal._fetch_sym_radar_data_slow.assert_called_once_with("NVDA")
    mock_builder.assert_called_once()
    assert bot.queue_dm.await_count == 1


@pytest.mark.asyncio
async def test_monitor_vtr_task_handles_missing_trade_info() -> None:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.monitor_vtr_and_calculate_hedging = AsyncMock(  # type: ignore
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

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch(
        "cogs.trading.portfolio_monitor.create_option_defense_alert_embed",
        return_value=embed,
    ) as mock_builder:
        await cog.monitor_vtr_task()

    # Verify only the valid trade (uid 2) triggered an alert and queued a DM
    mock_builder.assert_called_once()
    bot.queue_dm.assert_awaited_once_with(2, embed=embed)


@pytest.mark.asyncio
async def test_dispatch_order_telemetry_alignment_alert_success() -> None:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

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
        "cogs.trading.telemetry.create_telemetry_alignment_embeds",
        return_value=[mock_embed],
    ) as mock_embed_builder:
        from cogs.trading.telemetry import _dispatch_order_telemetry_alignment_alert

        await _dispatch_order_telemetry_alignment_alert(bot)

    mock_embed_builder.assert_called_once_with(
        [mock_alignment_item],
        truncated=False,
        include_apply_button_hint=False,
        scheduled_mode=True,
    )
    bot.queue_dm.assert_awaited_once_with(1, embed=mock_embed)
    bot = MagicMock()


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_threads_entry_confirmation_into_core_deployment() -> (
    None
):
    """Phase 2 回歸鎖定：monitor_real_portfolio_task 應將 Scenario 2
    (evaluate_opportunity_cost_for_satellites) 回傳的 _confirm_entry_signal
    確認結果，原樣透過 precomputed_entry_confirmation 轉交 Scenario 5
    (evaluate_core_deployment)，而非各自獨立重新確認。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "NVDA",
        "metadata": "{}",
        "quantity": 10.0,
        "avg_cost": 200.0,
    }

    entry_confirmation = (True, "已確認突破")
    cog.rollover_engine.check_satellite_rebalancing = AsyncMock(return_value=[])  # type: ignore
    cog.rollover_engine.evaluate_opportunity_cost_for_satellites = AsyncMock(  # type: ignore
        return_value=([], entry_confirmation)
    )
    cog.rollover_engine.evaluate_core_deployment = AsyncMock(return_value=[])  # type: ignore
    cog.rollover_engine.evaluate_margin_defense = AsyncMock(return_value=[])  # type: ignore

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("database.holdings.get_all_holdings", return_value=[holding]), patch(
        "database.watchlist.get_user_watchlist", return_value=[]
    ), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ):
        await cog.monitor_real_portfolio_task()

    cog.rollover_engine.evaluate_core_deployment.assert_awaited_once()
    await_args = cog.rollover_engine.evaluate_core_deployment.await_args
    assert await_args is not None
    assert await_args.kwargs["precomputed_entry_confirmation"] == entry_confirmation
