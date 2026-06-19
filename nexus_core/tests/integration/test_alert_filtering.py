import pytest
from unittest.mock import AsyncMock, patch
from cogs.trading import SchedulerCog


@pytest.mark.asyncio
async def test_alert_filtering_logic(mock_interaction, db_conn):
    bot = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    bot.queue_dm = AsyncMock()
    cog = SchedulerCog(bot)

    user_id = 12345
    symbol_in_port = "AAPL"
    symbol_not_in_port = "TSLA"

    # 1. Setup portfolio and watchlist
    from database.portfolio import add_portfolio_record
    from database.watchlist import add_watchlist_symbol
    from database.user_settings import upsert_user_config

    # Add AAPL to portfolio
    add_portfolio_record(
        user_id, symbol_in_port, "call", 150, "2036-06-19", 5.0, 1, 150.0
    )
    # Add TSLA to watchlist only
    add_watchlist_symbol(user_id, symbol_not_in_port)

    # Case A: Mode = 1 (ALL)
    upsert_user_config(user_id, option_alert_mode=1)
    assert await cog._should_send_alert(user_id, symbol_in_port, 1) is True
    assert await cog._should_send_alert(user_id, symbol_not_in_port, 1) is True

    # Case B: Mode = 2 (PORTFOLIO_ONLY)
    upsert_user_config(user_id, option_alert_mode=2)
    assert await cog._should_send_alert(user_id, symbol_in_port, 2) is True
    assert await cog._should_send_alert(user_id, symbol_not_in_port, 2) is False

    # Case C: Mode = 0 (OFF)
    upsert_user_config(user_id, option_alert_mode=0)
    assert await cog._should_send_alert(user_id, symbol_in_port, 0) is False
    assert await cog._should_send_alert(user_id, symbol_not_in_port, 0) is False


@pytest.mark.asyncio
async def test_run_market_scan_logic_filtering(mock_interaction, db_conn):
    bot = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    bot.queue_dm = AsyncMock()
    cog = SchedulerCog(bot)

    user_id = mock_interaction.user.id
    symbol_watch = "TSLA"

    from database.watchlist import add_watchlist_symbol
    from database.user_settings import upsert_user_config

    add_watchlist_symbol(user_id, symbol_watch)

    # Mock trading_service.run_market_scan to return a TSLA alert
    cog.trading_service = AsyncMock()
    cog.trading_service.run_market_scan.return_value = {
        user_id: [
            {
                "symbol": symbol_watch,
                "alert_type": "OPTION",
                "ai_decision": "APPROVE",
                "price": 200.0,
                "macro_vix": 20.0,
                "strategy": "BTO_CALL",
            }
        ]
    }
    cog.trading_service.execute_vtr_auto_entry = AsyncMock()

    # Mock should_send_priority_alert to return True
    with patch(
        "cogs.trading.should_send_priority_alert", new_callable=AsyncMock
    ) as mock_priority:
        mock_priority.return_value = (True, "Priority")

        # Test Case 1: Mode ALL (1) -> Should queue DM
        upsert_user_config(user_id, option_alert_mode=1)
        await cog._run_market_scan_logic(is_auto=True)
        assert bot.queue_dm.called
        bot.queue_dm.reset_mock()

        # Test Case 2: Mode PORTFOLIO_ONLY (2) and not in portfolio -> Should NOT queue DM
        upsert_user_config(user_id, option_alert_mode=2)
        await cog._run_market_scan_logic(is_auto=True)
        assert not bot.queue_dm.called
