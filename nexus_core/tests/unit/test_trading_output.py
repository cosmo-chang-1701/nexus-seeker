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
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
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
async def test_monitor_real_portfolio_task_skips_when_memory_unsafe() -> None:
    """當 is_memory_safe() 回傳 False（RAM+Swap 水位 > 85%）時，
    monitor_real_portfolio_task 應該直接跳過本輪審計，不呼叫
    audit_real_portfolio_risk 或發送任何 DM。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("services.llm_service.is_memory_safe", return_value=False):
        await cog.monitor_real_portfolio_task()

    cog.trading_service.audit_real_portfolio_risk.assert_not_called()
    bot.queue_dm.assert_not_called()


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
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
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
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
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
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ):
        await cog.monitor_real_portfolio_task()

    cog.rollover_engine.evaluate_margin_defense.assert_awaited_once()
    await_args = cog.rollover_engine.evaluate_margin_defense.await_args
    assert await_args is not None
    assert await_args.kwargs["already_flagged_symbols"] == {
        ("NVDA", "SPOT"),
        ("AAPL", "SPOT"),
    }


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
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
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
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ):
        await cog.monitor_real_portfolio_task()

    cog.rollover_engine.evaluate_core_deployment.assert_awaited_once()
    await_args = cog.rollover_engine.evaluate_core_deployment.await_args
    assert await_args is not None
    assert await_args.kwargs["precomputed_entry_confirmation"] == entry_confirmation


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_dispatches_covered_call_overlay_embed() -> (
    None
):
    """Phase C 回歸鎖定：evaluate_covered_call_overlay 產生的 instruction
    (is_covered_call_overlay=True) 必須經由專屬的 create_covered_call_overlay_embed
    推播，而非誤用 create_dynamic_rollover_embed (該函式的 is_hold 判斷會將
    sell_ratio==0 的本情境誤渲染為「安全續抱、無需任何手動操作」)。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "VOO",
        "metadata": "{}",
        "quantity": 500.0,
        "avg_cost": 470.0,
    }

    overlay_instruction = {
        "symbol": "VOO",
        "action": "HOLD",
        "sell_ratio": 0.0,
        "target_core": "VOO",
        "reason": "test reason",
        "suggested_strategy": "Covered Call (STO)",
        "scenario": "CORE_DEPLOYMENT",
        "is_manual_override_required": False,
        "cash_impact": "$350",
        "limit_price": 465.0,
        "strike": "$465.00C",
        "expiry": "2026-09-18",
        "direction": "STO",
        "is_covered_call_overlay": True,
    }

    cog.rollover_engine.check_satellite_rebalancing = AsyncMock(return_value=[])  # type: ignore
    cog.rollover_engine.evaluate_opportunity_cost_for_satellites = AsyncMock(  # type: ignore
        return_value=([], None)
    )
    cog.rollover_engine.evaluate_core_deployment = AsyncMock(return_value=[])  # type: ignore
    cog.rollover_engine.evaluate_covered_call_overlay = AsyncMock(  # type: ignore
        return_value=[overlay_instruction]
    )
    cog.rollover_engine.evaluate_margin_defense = AsyncMock(return_value=[])  # type: ignore

    overlay_embed = object()
    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ), patch("database.is_notification_enabled", return_value=True), patch(
        "database.get_kv_cache", return_value=None
    ), patch("database.save_kv_cache", new_callable=AsyncMock), patch(
        "database.log_rollover_instruction", new_callable=AsyncMock
    ), patch(
        "cogs.trading.portfolio_monitor.create_covered_call_overlay_embed",
        return_value=overlay_embed,
    ) as mock_overlay_embed, patch(
        "cogs.trading.portfolio_monitor.create_dynamic_rollover_embed"
    ) as mock_rotation_embed:
        await cog.monitor_real_portfolio_task()

    cog.rollover_engine.evaluate_covered_call_overlay.assert_awaited_once()
    mock_overlay_embed.assert_called_once()
    mock_overlay_embed.assert_called_once_with(
        symbol="VOO",
        reason="test reason",
        strike="$465.00C",
        expiry="2026-09-18",
        cash_impact="$350",
        trigger_condition_text=None,
        is_manual_override_required=False,
    )
    mock_rotation_embed.assert_not_called()
    bot.queue_dm.assert_awaited_once_with(1, embed=overlay_embed)


def _mock_all_rollover_scenarios(cog: PortfolioMonitorCog) -> None:
    """所有測試共用：預設全部六大情境 + Covered Call Profit Lock 回傳空結果，
    測試僅需覆寫關心的特定情境 mock，避免真實引擎邏輯在測試環境意外執行
    (例如 evaluate_macro_top_escape_defense 的總經資料抓取)。"""
    cog.rollover_engine.check_satellite_rebalancing = AsyncMock(  # type: ignore
        return_value=[]
    )
    cog.rollover_engine.evaluate_opportunity_cost_for_satellites = AsyncMock(  # type: ignore
        return_value=([], None)
    )
    cog.rollover_engine.evaluate_core_deployment = AsyncMock(return_value=[])  # type: ignore
    cog.rollover_engine.evaluate_covered_call_overlay = AsyncMock(  # type: ignore
        return_value=[]
    )
    cog.rollover_engine.evaluate_margin_defense = AsyncMock(return_value=[])  # type: ignore
    cog.rollover_engine.evaluate_macro_top_escape_defense = AsyncMock(  # type: ignore
        return_value=[]
    )
    cog.rollover_engine.evaluate_covered_call_profit_lock = AsyncMock(  # type: ignore
        return_value=[]
    )


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_options_ingestion_gate_off_skips_option_trades() -> (
    None
):
    """config.ENABLE_OPTIONS_ROLLOVER_INGESTION 為 False（預設值）時，
    不應呼叫 get_all_trade_positions，且進入動態轉倉引擎的 portfolio_assets
    不應包含任何 OPTIONS_CONTRACT 部位——僅現貨持倉應流入評估迴圈。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore
    _mock_all_rollover_scenarios(cog)
    mock_check_satellite = AsyncMock(return_value=[])
    cog.rollover_engine.check_satellite_rebalancing = mock_check_satellite  # type: ignore

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "NVDA",
        "metadata": "{}",
        "quantity": 10.0,
        "avg_cost": 200.0,
    }

    mock_get_trade_positions = MagicMock(
        return_value=[
            {
                "user_id": 1,
                "symbol": "AAPL",
                "quantity": 1.0,
                "opt_type": "call",
                "expiry": "2026-01-16",
                "strike": 150.0,
            }
        ]
    )

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ), patch("config.ENABLE_OPTIONS_ROLLOVER_INGESTION", False), patch(
        "database.portfolio.get_all_trade_positions", mock_get_trade_positions
    ):
        await cog.monitor_real_portfolio_task()

    mock_get_trade_positions.assert_not_called()
    mock_check_satellite.assert_awaited_once()
    await_args = mock_check_satellite.await_args
    assert await_args is not None
    portfolio_assets = await_args.args[1]
    assert all(a.get("instrument_type") != "OPTIONS_CONTRACT" for a in portfolio_assets)


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_options_ingestion_gate_on_splits_long_and_short_call_trades() -> (
    None
):
    """config.ENABLE_OPTIONS_ROLLOVER_INGESTION 為 True 時，多頭期權部位
    (quantity>0) 應併入 check_satellite_rebalancing 的 portfolio_assets，
    空頭 CALL 部位 (quantity<0, opt_type=="call") 應併入
    evaluate_covered_call_profit_lock 的 short_call_positions，空頭 PUT
    則兩邊都不應出現。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore
    _mock_all_rollover_scenarios(cog)
    mock_check_satellite = AsyncMock(return_value=[])
    cog.rollover_engine.check_satellite_rebalancing = mock_check_satellite  # type: ignore
    mock_profit_lock = AsyncMock(return_value=[])
    cog.rollover_engine.evaluate_covered_call_profit_lock = mock_profit_lock  # type: ignore

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "NVDA",
        "metadata": "{}",
        "quantity": 10.0,
        "avg_cost": 200.0,
    }

    long_call = {
        "user_id": 1,
        "symbol": "AAPL",
        "quantity": 1.0,
        "opt_type": "call",
        "expiry": "2026-01-16",
        "strike": 150.0,
        "entry_price": 5.0,
    }
    short_call = {
        "user_id": 1,
        "symbol": "VOO",
        "quantity": -1.0,
        "opt_type": "call",
        "expiry": "2026-02-20",
        "strike": 480.0,
        "entry_price": 2.5,
    }
    short_put = {
        "user_id": 1,
        "symbol": "TSLA",
        "quantity": -1.0,
        "opt_type": "put",
        "expiry": "2026-03-20",
        "strike": 200.0,
        "entry_price": 3.0,
    }

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ), patch("config.ENABLE_OPTIONS_ROLLOVER_INGESTION", True), patch(
        "database.portfolio.get_all_trade_positions",
        return_value=[long_call, short_call, short_put],
    ), patch(
        "market_analysis.portfolio.get_option_chain_mid_iv",
        new_callable=AsyncMock,
        return_value=(4.5, 0.35, 4.4, 4.6),
    ):
        await cog.monitor_real_portfolio_task()

    check_call_args = mock_check_satellite.await_args
    assert check_call_args is not None
    portfolio_assets = check_call_args.args[1]
    option_symbols = {
        a["symbol"]
        for a in portfolio_assets
        if a.get("instrument_type") == "OPTIONS_CONTRACT"
    }
    assert option_symbols == {"AAPL"}

    profit_lock_call = mock_profit_lock.await_args
    assert profit_lock_call is not None
    short_call_positions = profit_lock_call.args[1]
    assert len(short_call_positions) == 1
    assert short_call_positions[0]["symbol"] == "VOO"


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_options_dry_run_suppresses_dm_but_logs_audit_trail() -> (
    None
):
    """config.OPTIONS_ROLLOVER_DRY_RUN 為 True（預設值）時，instrument_type
    =="OPTIONS" 的指令不應觸發 DM 推播，但 dedup key 與
    database.log_rollover_instruction 審計軌跡仍應正常寫入——期權轉倉是
    尚未經過生產流量驗證的新分支，dry-run 期間僅記錄不推播。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore
    _mock_all_rollover_scenarios(cog)

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "NVDA",
        "metadata": "{}",
        "quantity": 10.0,
        "avg_cost": 200.0,
    }

    options_instruction = {
        "symbol": "NVDA",
        "action": "LIQUIDATE",
        "sell_ratio": 1.0,
        "target_core": "VOO",
        "reason": "test reason",
        "scenario": "SATELLITE_REBALANCE",
        "instrument_type": "OPTIONS",
    }
    cog.rollover_engine.check_satellite_rebalancing = AsyncMock(  # type: ignore
        return_value=[options_instruction]
    )

    log_mock = AsyncMock()
    save_kv_mock = AsyncMock()

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ), patch("database.is_notification_enabled", return_value=True), patch(
        "database.get_kv_cache", return_value=None
    ), patch("database.save_kv_cache", save_kv_mock), patch(
        "database.log_rollover_instruction", log_mock
    ), patch("config.OPTIONS_ROLLOVER_DRY_RUN", True):
        await cog.monitor_real_portfolio_task()

    bot.queue_dm.assert_not_called()
    log_mock.assert_awaited_once()
    log_call = log_mock.await_args
    assert log_call is not None
    assert log_call.kwargs["symbol"] == "NVDA"
    assert log_call.kwargs["scenario"] == "SATELLITE_REBALANCE"
    assert log_call.kwargs["action"] == "LIQUIDATE"
    save_kv_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_options_dry_run_off_sends_dm_for_options() -> (
    None
):
    """config.OPTIONS_ROLLOVER_DRY_RUN 為 False 時，期權轉倉指令應正常推播
    DM——證明 dry-run 開關雙向都有效，而非「期權轉倉恆被跳過」的假陽性。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore
    _mock_all_rollover_scenarios(cog)

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "NVDA",
        "metadata": "{}",
        "quantity": 10.0,
        "avg_cost": 200.0,
    }

    options_instruction = {
        "symbol": "NVDA",
        "action": "LIQUIDATE",
        "sell_ratio": 1.0,
        "target_core": "VOO",
        "reason": "test reason",
        "scenario": "SATELLITE_REBALANCE",
        "instrument_type": "OPTIONS",
    }
    cog.rollover_engine.check_satellite_rebalancing = AsyncMock(  # type: ignore
        return_value=[options_instruction]
    )

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ), patch("database.is_notification_enabled", return_value=True), patch(
        "database.get_kv_cache", return_value=None
    ), patch("database.save_kv_cache", new_callable=AsyncMock), patch(
        "database.log_rollover_instruction", new_callable=AsyncMock
    ), patch("config.OPTIONS_ROLLOVER_DRY_RUN", False):
        await cog.monitor_real_portfolio_task()

    bot.queue_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_notif_key_routes_margin_defense_vs_default() -> (
    None
):
    """MARGIN_DEFENSE 情境的通知開關應查詢 "defense_margin_call"
    （帳戶生存等級警訊，獨立於例行轉倉靜音設定），其餘情境一律查詢
    "defense_option_rollover"。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore
    _mock_all_rollover_scenarios(cog)

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "NVDA",
        "metadata": "{}",
        "quantity": 10.0,
        "avg_cost": 200.0,
    }

    cog.rollover_engine.check_satellite_rebalancing = AsyncMock(  # type: ignore
        return_value=[
            {
                "symbol": "NVDA",
                "action": "REDUCE",
                "sell_ratio": 0.3,
                "target_core": "VOO",
                "reason": "test reason",
                "scenario": "SATELLITE_REBALANCE",
            }
        ]
    )
    cog.rollover_engine.evaluate_margin_defense = AsyncMock(  # type: ignore
        return_value=[
            {
                "symbol": "SPY",
                "action": "LIQUIDATE",
                "sell_ratio": 1.0,
                "target_core": "CASH",
                "reason": "test reason",
                "scenario": "MARGIN_DEFENSE",
            }
        ]
    )

    notif_mock = MagicMock(return_value=True)

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ), patch("database.is_notification_enabled", notif_mock), patch(
        "database.get_kv_cache", return_value=None
    ), patch("database.save_kv_cache", new_callable=AsyncMock), patch(
        "database.log_rollover_instruction", new_callable=AsyncMock
    ):
        await cog.monitor_real_portfolio_task()

    call_args_list = [c.args for c in notif_mock.call_args_list]
    assert (1, "defense_margin_call") in call_args_list
    assert (1, "defense_option_rollover") in call_args_list


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_dedup_suppresses_second_dm_same_day() -> (
    None
):
    """同一使用者、標的、情境、動作，當日已發送過（database.get_kv_cache
    回傳真值）時，不應再次推播 DM，也不應重複寫入 dedup key 或審計軌跡——
    現行測試皆固定 mock get_kv_cache 回傳 None，從未驗證過 dedup 真的生效。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    from cogs.trading.portfolio_monitor import ny_tz as _ny_tz
    from datetime import datetime as _datetime

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore
    _mock_all_rollover_scenarios(cog)

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "NVDA",
        "metadata": "{}",
        "quantity": 10.0,
        "avg_cost": 200.0,
    }

    cog.rollover_engine.check_satellite_rebalancing = AsyncMock(  # type: ignore
        return_value=[
            {
                "symbol": "NVDA",
                "action": "REDUCE",
                "sell_ratio": 0.3,
                "target_core": "VOO",
                "reason": "test reason",
                "scenario": "SATELLITE_REBALANCE",
            }
        ]
    )

    today_str = _datetime.now(_ny_tz).strftime("%Y%m%d")
    expected_dedup_key = (
        f"rollover_alert_1_NVDA_SPOT_SATELLITE_REBALANCE_REDUCE_{today_str}"
    )

    save_kv_mock = AsyncMock()
    log_mock = AsyncMock()

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ), patch("database.is_notification_enabled", return_value=True), patch(
        "database.get_kv_cache",
        side_effect=lambda key: 1 if key == expected_dedup_key else None,
    ), patch("database.save_kv_cache", save_kv_mock), patch(
        "database.log_rollover_instruction", log_mock
    ):
        await cog.monitor_real_portfolio_task()

    bot.queue_dm.assert_not_called()
    save_kv_mock.assert_not_called()
    log_mock.assert_not_called()


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_covered_call_profit_lock_dedup_key_includes_strike_expiry() -> (
    None
):
    """同一標的但不同履約價/到期日的兩筆 Covered Call 權利金衰減停利指令，
    dedup key 必須各自獨立（納入 strike/expiry），不能讓其中一筆意外壓制
    另一筆——兩筆都應正常推播。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore
    _mock_all_rollover_scenarios(cog)

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "AAPL",
        "metadata": "{}",
        "quantity": 200.0,
        "avg_cost": 140.0,
    }

    cog.rollover_engine.evaluate_covered_call_profit_lock = AsyncMock(  # type: ignore
        return_value=[
            {
                "symbol": "AAPL",
                "action": "LIQUIDATE",
                "sell_ratio": 1.0,
                "target_core": "AAPL",
                "reason": "test reason",
                "scenario": "COVERED_CALL_PROFIT_LOCK",
                "instrument_type": "OPTIONS",
                "is_covered_call_profit_lock": True,
                "entry_premium": 2.5,
                "current_premium": 0.4,
                "decay_pct": 0.84,
                "dte": 10,
                "strike": "$150.00C",
                "expiry": "2026-01-16",
            },
            {
                "symbol": "AAPL",
                "action": "LIQUIDATE",
                "sell_ratio": 1.0,
                "target_core": "AAPL",
                "reason": "test reason",
                "scenario": "COVERED_CALL_PROFIT_LOCK",
                "instrument_type": "OPTIONS",
                "is_covered_call_profit_lock": True,
                "entry_premium": 3.0,
                "current_premium": 0.5,
                "decay_pct": 0.83,
                "dte": 20,
                "strike": "$160.00C",
                "expiry": "2026-02-20",
            },
        ]
    )

    get_kv_mock = MagicMock(return_value=None)

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ), patch("database.is_notification_enabled", return_value=True), patch(
        "database.get_kv_cache", get_kv_mock
    ), patch("database.save_kv_cache", new_callable=AsyncMock), patch(
        "database.log_rollover_instruction", new_callable=AsyncMock
    ), patch(
        "cogs.trading.portfolio_monitor.create_covered_call_profit_lock_embed",
        return_value=object(),
    ), patch("config.OPTIONS_ROLLOVER_DRY_RUN", False):
        await cog.monitor_real_portfolio_task()

    dedup_keys = [c.args[0] for c in get_kv_mock.call_args_list]
    profit_lock_keys = [k for k in dedup_keys if "COVERED_CALL_PROFIT_LOCK" in k]
    assert len(profit_lock_keys) == 2
    assert len(set(profit_lock_keys)) == 2
    assert any("$150.00C" in k and "2026-01-16" in k for k in profit_lock_keys)
    assert any("$160.00C" in k and "2026-02-20" in k for k in profit_lock_keys)
    assert bot.queue_dm.await_count == 2


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_dispatches_covered_call_profit_lock_embed() -> (
    None
):
    """evaluate_covered_call_profit_lock 產生的指令
    (is_covered_call_profit_lock=True) 必須經由專屬的
    create_covered_call_profit_lock_embed 推播，而非誤用
    create_dynamic_rollover_embed。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore
    _mock_all_rollover_scenarios(cog)

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "VOO",
        "metadata": "{}",
        "quantity": 500.0,
        "avg_cost": 470.0,
    }

    profit_lock_instruction = {
        "symbol": "VOO",
        "action": "LIQUIDATE",
        "sell_ratio": 1.0,
        "target_core": "VOO",
        "reason": "test reason",
        "scenario": "COVERED_CALL_PROFIT_LOCK",
        "instrument_type": "OPTIONS",
        "is_covered_call_profit_lock": True,
        "entry_premium": 2.50,
        "current_premium": 0.40,
        "decay_pct": 0.84,
        "dte": 10,
        "strike": "$465.00C",
        "expiry": "2026-01-16",
        "cash_impact": "$4,000",
    }
    cog.rollover_engine.evaluate_covered_call_profit_lock = AsyncMock(  # type: ignore
        return_value=[profit_lock_instruction]
    )

    profit_lock_embed = object()
    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ), patch("database.is_notification_enabled", return_value=True), patch(
        "database.get_kv_cache", return_value=None
    ), patch("database.save_kv_cache", new_callable=AsyncMock), patch(
        "database.log_rollover_instruction", new_callable=AsyncMock
    ), patch(
        "cogs.trading.portfolio_monitor.create_covered_call_profit_lock_embed",
        return_value=profit_lock_embed,
    ) as mock_profit_lock_embed, patch(
        "cogs.trading.portfolio_monitor.create_dynamic_rollover_embed"
    ) as mock_rotation_embed, patch("config.OPTIONS_ROLLOVER_DRY_RUN", False):
        await cog.monitor_real_portfolio_task()

    mock_profit_lock_embed.assert_called_once_with(
        symbol="VOO",
        reason="test reason",
        entry_premium=2.50,
        current_premium=0.40,
        decay_pct=0.84,
        btc_ratio=1.0,
        dte=10,
        strike="$465.00C",
        expiry="2026-01-16",
        cash_impact="$4,000",
    )
    mock_rotation_embed.assert_not_called()
    bot.queue_dm.assert_awaited_once_with(1, embed=profit_lock_embed)


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_invokes_macro_top_escape_defense() -> None:
    """Scenario 6 (宏觀逃頂前瞻防禦) 必須被實際掛進 monitor_real_portfolio_task
    的評估迴圈，且接收到的 already_flagged_symbols 應是 Scenario 2/3/4/5
    累積後的完整集合（在六大情境中排最後一位，永遠享有最低優先權）。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore
    _mock_all_rollover_scenarios(cog)

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
    cog.rollover_engine.evaluate_margin_defense = AsyncMock(  # type: ignore
        return_value=[{"symbol": "AAPL", "action": "LIQUIDATE"}]
    )
    mock_macro_top_escape = AsyncMock(return_value=[])
    cog.rollover_engine.evaluate_macro_top_escape_defense = mock_macro_top_escape  # type: ignore

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ):
        await cog.monitor_real_portfolio_task()

    mock_macro_top_escape.assert_awaited_once()
    await_args = mock_macro_top_escape.await_args
    assert await_args is not None
    assert await_args.kwargs["already_flagged_symbols"] == {
        ("NVDA", "SPOT"),
        ("AAPL", "SPOT"),
    }


@pytest.mark.asyncio
async def test_monitor_real_portfolio_task_logs_rollover_instruction_with_correct_args() -> (
    None
):
    """database.log_rollover_instruction 必須以正確的
    user_id/symbol/scenario/action/sell_ratio/target_core/suggested_price/
    cash_impact 呼叫——釘死簽章，避免未來重構不小心漏改某個 kwarg。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    bot.get_cog = MagicMock(return_value=None)

    with patch("discord.ext.tasks.Loop.start"):
        cog = PortfolioMonitorCog(bot)

    cog.trading_service.audit_real_portfolio_risk = AsyncMock(return_value=[])  # type: ignore
    _mock_all_rollover_scenarios(cog)

    holding = {
        "id": 1,
        "user_id": 1,
        "symbol": "NVDA",
        "metadata": "{}",
        "quantity": 10.0,
        "avg_cost": 200.0,
    }

    cog.rollover_engine.check_satellite_rebalancing = AsyncMock(  # type: ignore
        return_value=[
            {
                "symbol": "NVDA",
                "action": "REDUCE",
                "sell_ratio": 0.3,
                "target_core": "VOO",
                "reason": "test reason",
                "scenario": "SATELLITE_REBALANCE",
                "cash_impact": "$600",
            }
        ]
    )

    log_mock = AsyncMock()

    with patch(
        "cogs.trading.portfolio_monitor.market_time.is_market_open", return_value=True
    ), patch("services.llm_service.is_memory_safe", return_value=True), patch(
        "database.holdings.get_all_holdings", return_value=[holding]
    ), patch("database.watchlist.get_user_watchlist", return_value=[]), patch(
        "market_analysis.trading_orchestration.recommend_covered_calls",
        new_callable=AsyncMock,
        return_value={"recommendations": []},
    ), patch("database.is_notification_enabled", return_value=True), patch(
        "database.get_kv_cache", return_value=None
    ), patch("database.save_kv_cache", new_callable=AsyncMock), patch(
        "database.log_rollover_instruction", log_mock
    ):
        await cog.monitor_real_portfolio_task()

    log_mock.assert_awaited_once()
    log_await_args = log_mock.await_args
    assert log_await_args is not None
    call_kwargs = log_await_args.kwargs
    assert call_kwargs["user_id"] == 1
    assert call_kwargs["symbol"] == "NVDA"
    assert call_kwargs["scenario"] == "SATELLITE_REBALANCE"
    assert call_kwargs["action"] == "REDUCE"
    assert call_kwargs["sell_ratio"] == 0.3
    assert call_kwargs["target_core"] == "VOO"
    assert call_kwargs["suggested_price"] == "Market"
    assert call_kwargs["cash_impact"] == "$600"
