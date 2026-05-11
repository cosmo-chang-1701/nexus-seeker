import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from cogs.terminal import TerminalCog
from cogs.sentiment import SentimentCog
from cogs.hedging import HedgingCog
from cogs.trading import SchedulerCog
from cogs.intelligence import IntelligenceCog
from database.user_settings import get_full_user_context


@pytest.mark.asyncio
async def test_command_settings(mock_interaction, db_conn):
    bot = MagicMock()
    cog = TerminalCog(bot)

    # Execute /settings command using .callback
    await cog.update_settings.callback(
        cog, mock_interaction, capital=100000.0, risk_limit=15.0, enable_vtr=True
    )

    # Verify response
    mock_interaction.followup.send.assert_called_once()
    args, kwargs = mock_interaction.followup.send.call_args
    assert "✅ **帳戶設定已更新**" in args[0]
    assert "💰 總資金: `$100,000.00`" in args[0]
    assert "🛡️ 風險限制: `15.0%`" in args[0]

    # Verify database update
    context = get_full_user_context(mock_interaction.user.id)
    assert context.capital == 100000.0
    assert context.risk_limit == 15.0
    assert context.enable_vtr is True


@pytest.mark.asyncio
async def test_command_runway_check(mock_interaction, db_conn):
    bot = MagicMock()
    cog = TerminalCog(bot)

    # First set some settings
    await cog.update_settings.callback(
        cog, mock_interaction, monthly_expense=5000.0, cash_reserve=20000.0
    )

    # Execute /runway_check
    await cog.runway_check.callback(cog, mock_interaction)

    # It might use response.send_message or followup.send depending on logic
    sent = (
        mock_interaction.response.send_message.called
        or mock_interaction.followup.send.called
    )
    assert sent


@pytest.mark.asyncio
async def test_command_add_holding(mock_interaction, db_conn, mock_market_data):
    bot = MagicMock()
    cog = TerminalCog(bot)

    # Execute /add_holding
    await cog.add_holding.callback(
        cog, mock_interaction, symbol="AAPL", quantity=10, avg_cost=150.0
    )

    mock_interaction.followup.send.assert_called_once()
    assert "✅ **現貨持倉已登錄**" in mock_interaction.followup.send.call_args[0][0]

    # Verify DB
    from database.holdings import get_user_holdings

    holdings = get_user_holdings(mock_interaction.user.id)
    assert len(holdings) == 1
    assert holdings[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_command_skew_scan(mock_interaction, db_conn, mock_market_data):
    bot = MagicMock()
    cog = SentimentCog(bot)

    # Mock all 4 tasks called in gather
    with patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
        new_callable=AsyncMock,
    ) as mock_skew, patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
        new_callable=AsyncMock,
    ) as mock_pcr, patch(
        "market_analysis.sentiment_engine.SentimentEngine.detect_uoa",
        new_callable=AsyncMock,
    ) as mock_uoa, patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
        new_callable=AsyncMock,
    ) as mock_mp:
        mock_skew.return_value = {"symbol": "SPY", "skew": 5.0, "state": "Normal"}
        mock_pcr.return_value = {"symbol": "SPY", "pcr": 0.8, "state": "Normal"}
        mock_uoa.return_value = []
        mock_mp.return_value = {"max_pain": 500}

        await cog.skew_scan.callback(cog, mock_interaction, symbol="SPY")

        mock_interaction.followup.send.assert_called_once()
        assert "embed" in mock_interaction.followup.send.call_args[1]


@pytest.mark.asyncio
async def test_command_ddp_scan(mock_interaction, db_conn, mock_market_data):
    bot = MagicMock()
    cog = SchedulerCog(bot)

    # Add something to watchlist
    from database.watchlist import add_watchlist_symbol

    add_watchlist_symbol(mock_interaction.user.id, "TSLA")

    with patch(
        "market_analysis.ddp_inspector.DDPInspector.run_scan", new_callable=AsyncMock
    ) as mock_scan:
        mock_scan.return_value = [
            {
                "symbol": "TSLA",
                "signal": "BULLISH",
                "current_pe": 30.0,
                "pe_mean_3y": 45.0,
                "eps_growth": 0.2,
                "rev_accel": True,
                "confidence_score": 0.85,
                "forward_pe": 25.0,
            }
        ]

        await cog.ddp_scan.callback(cog, mock_interaction)
        assert (
            mock_interaction.response.send_message.called
            or mock_interaction.followup.send.called
        )


@pytest.mark.asyncio
async def test_command_poly_list(mock_interaction, db_conn):
    bot = MagicMock()
    bot.polymarket_service = MagicMock()
    bot.polymarket_service.get_active_markets.return_value = [
        {"title": "Test Market", "price": 0.5}
    ]

    cog = IntelligenceCog(bot)
    await cog.poly_list.callback(cog, mock_interaction)

    mock_interaction.followup.send.assert_called_once()
    assert "embed" in mock_interaction.followup.send.call_args[1]


@pytest.mark.asyncio
async def test_command_settle_hedge(mock_interaction, db_conn):
    cursor = db_conn.cursor()
    # Correct columns for hedge_alerts
    # vix_level is at index 2, hedge_contracts at index 7, status at index 10
    # Wait, let's check the schema again to be sure of indices
    # (id, user_id, vix_level, vix_stage_move, portfolio_delta, portfolio_vega, hedge_instrument, hedge_contracts, instruction_text, narration, status, created_at, executed_at)
    cursor.execute(
        """
        INSERT INTO hedge_alerts (user_id, vix_level, portfolio_delta, portfolio_vega, hedge_instrument, hedge_contracts, instruction_text, status)
        VALUES (?, 20.0, 10.0, 50.0, 'SPY', 10, 'Hedge instructions', 'PENDING')
    """,
        (mock_interaction.user.id,),
    )
    alert_id = cursor.lastrowid
    db_conn.commit()

    bot = MagicMock()
    cog = HedgingCog(bot)

    await cog.settle_hedge.callback(
        cog, mock_interaction, alert_id=alert_id, actual_qty=12
    )

    mock_interaction.followup.send.assert_called_once()
    assert "embed" in mock_interaction.followup.send.call_args[1]

    cursor.execute(
        "SELECT status, hedge_contracts FROM hedge_alerts WHERE id = ?", (alert_id,)
    )
    row = cursor.fetchone()
    assert row[0] == "EXECUTED"
    assert row[1] == 12


@pytest.mark.asyncio
async def test_command_vtr_stats(mock_interaction, db_conn):
    bot = MagicMock()
    cog = TerminalCog(bot)

    await cog.vtr_stats.callback(cog, mock_interaction)
    assert (
        mock_interaction.response.send_message.called
        or mock_interaction.followup.send.called
    )


@pytest.mark.asyncio
async def test_command_sys_health(mock_interaction):
    bot = MagicMock()
    cog = TerminalCog(bot)

    with patch("psutil.virtual_memory") as mock_mem, patch(
        "psutil.disk_usage"
    ) as mock_disk, patch("psutil.cpu_percent") as mock_cpu:
        # Case 1: Healthy
        mock_mem.return_value.percent = 50.0
        mock_mem.return_value.available = 512 * 1024 * 1024
        mock_disk.return_value.percent = 40.0
        mock_disk.return_value.free = 10 * 1024 * 1024 * 1024
        mock_cpu.return_value = 10.0

        await cog.sys_health.callback(cog, mock_interaction)
        mock_interaction.followup.send.assert_called()
        args, kwargs = mock_interaction.followup.send.call_args
        embed = kwargs["embed"]
        assert "✅ 狀態優良" in embed.fields[-1].value
        assert discord.Color.green() == embed.color

        # Case 2: Disk Full Danger
        mock_interaction.followup.send.reset_mock()
        mock_disk.return_value.percent = 96.0
        await cog.sys_health.callback(cog, mock_interaction)
        args, kwargs = mock_interaction.followup.send.call_args
        embed = kwargs["embed"]
        assert "🆘 **極度危險**" in embed.fields[-1].value
        assert "(磁碟即將滿載)" in embed.fields[-1].value
        assert discord.Color.red() == embed.color
