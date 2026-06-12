import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cogs.terminal import TerminalCog
from cogs.sentiment import SentimentCog
from cogs.hedging import HedgingCog
from cogs.trading import SchedulerCog
from cogs.intelligence import IntelligenceCog
from cogs.calendar import CalendarCog
from cogs.unified_terminal import UnifiedTerminalCog


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_all_commands_structure(mock_interaction, db_conn, mock_bot):
    """
    Smoke test to ensure command callbacks are structurally correct and compatible with parameters.
    """
    terminal = TerminalCog(mock_bot)
    sentiment = SentimentCog(mock_bot)
    hedging = HedgingCog(mock_bot)
    trading = SchedulerCog(mock_bot)
    intelligence = IntelligenceCog(mock_bot)
    calendar = CalendarCog(mock_bot)
    unified = UnifiedTerminalCog(mock_bot)

    # --- Terminal Commands ---
    await terminal.update_settings.callback(terminal, mock_interaction, risk_limit=25.0)
    assert (
        "帳戶設定已更新"
        in mock_interaction.followup.send.call_args.kwargs["embed"].description
    )
    mock_interaction.followup.send.reset_mock()

    await terminal.add_watch.callback(
        terminal, mock_interaction, symbol="NVDA", use_llm=True
    )
    assert (
        "已加入觀察清單"
        in mock_interaction.followup.send.call_args.kwargs["embed"].description
    )
    mock_interaction.followup.send.reset_mock()

    await terminal.list_watch.callback(terminal, mock_interaction)
    assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    with patch("psutil.virtual_memory") as mem, patch(
        "psutil.disk_usage"
    ) as disk, patch("psutil.cpu_percent") as cpu:
        mem.return_value.percent = 40.0
        mem.return_value.available = 1024 * 1024 * 1024
        disk.return_value.percent = 30.0
        disk.return_value.free = 50 * 1024 * 1024 * 1024
        cpu.return_value = 5.0
        await terminal.sys_health.callback(terminal, mock_interaction)
        assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    # --- Sentiment Commands ---
    with patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
        new_callable=AsyncMock,
    ) as m_skew, patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
        new_callable=AsyncMock,
    ) as m_pcr, patch(
        "market_analysis.sentiment_engine.SentimentEngine.detect_uoa",
        new_callable=AsyncMock,
    ) as m_uoa, patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
        new_callable=AsyncMock,
    ) as m_mp:
        m_skew.return_value = {"symbol": "TSLA", "skew": 1.0, "state": "Normal"}
        m_pcr.return_value = {"symbol": "TSLA", "pcr": 1.0, "state": "Normal"}
        m_uoa.return_value = []
        m_mp.return_value = {"max_pain": 200}
        await sentiment.skew_scan.callback(sentiment, mock_interaction, symbol="TSLA")
        assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    # --- Intelligence Commands ---
    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote:
        # Finnhub quote requires c, d, dp, h, l, o, pc
        m_quote.return_value = {
            "c": 150.0,
            "d": 2.0,
            "dp": 1.3,
            "h": 155.0,
            "l": 145.0,
            "o": 148.0,
            "pc": 148.0,
        }
        await intelligence.quote.callback(intelligence, mock_interaction, symbol="AAPL")
        assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    with patch(
        "services.news_service.fetch_recent_news", new_callable=AsyncMock
    ) as m_news:
        m_news.return_value = "Test News Content"
        await intelligence.scan_news.callback(
            intelligence, mock_interaction, symbol="AAPL"
        )
        assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    # --- Calendar Commands ---
    with patch(
        "services.market_data_service.get_earnings_calendar", new_callable=AsyncMock
    ) as m_cal:
        m_cal.return_value = [{"date": "2026-05-15", "symbol": "AAPL"}]
        await calendar.calendar.callback(calendar, mock_interaction)
        assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    # --- Unified Terminal Commands ---
    with patch(
        "market_analysis.portfolio.refresh_portfolio_greeks", new_callable=AsyncMock
    ), patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote:
        m_quote.return_value = {"c": 500.0}
        await unified.symbol_hub.callback(unified, mock_interaction, symbol="SPY")
        assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    # --- Hedging Commands ---
    await hedging.hedge_list.callback(hedging, mock_interaction)
    assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()

    # --- Trading Commands ---
    with patch(
        "market_analysis.ddp_inspector.DDPInspector.run_scan", new_callable=AsyncMock
    ) as m_ddp:
        m_ddp.return_value = []
        await trading.ddp_scan.callback(trading, mock_interaction)
        assert mock_interaction.followup.send.called
    mock_interaction.followup.send.reset_mock()


@pytest.mark.asyncio
async def test_command_remove_watch(mock_interaction, db_conn, mock_bot):
    terminal = TerminalCog(mock_bot)
    from database.watchlist import add_watchlist_symbol

    add_watchlist_symbol(mock_interaction.user.id, "AMD")
    await terminal.remove_watch.callback(terminal, mock_interaction, symbol="AMD")
    assert (
        "已移除觀察標的"
        in mock_interaction.followup.send.call_args.kwargs["embed"].description
    )


@pytest.mark.asyncio
async def test_command_event_impact(mock_interaction, db_conn, mock_bot):
    cal_cog = CalendarCog(mock_bot)
    from database.portfolio import add_portfolio_record

    # (user_id, symbol, opt_type, strike, expiry, entry_price, quantity, stock_cost, delta, theta, gamma, category)
    add_portfolio_record(
        mock_interaction.user.id,
        "AAPL",
        "CALL",
        150.0,
        "2026-12-17",
        5.0,
        1,
        0.0,
        0.5,
        -0.1,
        0.01,
    )

    with patch("market_analysis.greeks.calculate_vanna", return_value=0.01), patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as m_quote:
        m_quote.return_value = {"c": 100.0}
        await cal_cog.event_impact.callback(
            cal_cog, mock_interaction, symbol="AAPL", vol_move=25.0
        )
        assert mock_interaction.followup.send.called
