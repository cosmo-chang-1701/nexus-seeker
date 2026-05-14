import pytest
from unittest.mock import AsyncMock, patch
from cogs.calendar import CalendarCog


@pytest.mark.asyncio
async def test_command_calendar(mock_interaction, db_conn):
    bot = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    cog = CalendarCog(bot)

    # Mock calendar_service.get_portfolio_events
    with patch(
        "services.calendar_service.calendar_service.get_portfolio_events",
        new_callable=AsyncMock,
    ) as mock_events:
        from services.calendar_service import EconomicEvent, EarningsEvent
        mock_events.return_value = [
            EconomicEvent(
                type="ECONOMIC",
                event="FOMC",
                impact="high",
                country="US",
                tte_hours=24.0,
                time="2026-05-15T18:00:00Z",
            ),
            EarningsEvent(
                type="EARNINGS",
                symbol="AAPL",
                date="2026-05-16",
                tte_hours=48.0,
            ),
        ]

        await cog.calendar.callback(cog, mock_interaction)

        mock_interaction.followup.send.assert_called_once()
        embed = mock_interaction.followup.send.call_args[1]["embed"]
        assert "重大市場事件 & 財報日曆" in embed.title
        assert "FOMC" in embed.fields[0].name
        assert "AAPL" in embed.fields[1].name


@pytest.mark.asyncio
async def test_command_iv_rank(mock_interaction, db_conn):
    bot = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    cog = CalendarCog(bot)

    # Add something to watchlist
    from database.watchlist import add_watchlist_symbol

    add_watchlist_symbol(mock_interaction.user.id, "NVDA")

    with patch.object(
        cog.vol_inspector, "run_scan", new_callable=AsyncMock
    ) as mock_scan:
        mock_scan.return_value = [
            {
                "symbol": "NVDA",
                "price": 900.0,
                "iv_current": 85.0,
                "hv_current": 50.0,
                "iv_rank": 92.5,
                "is_opportunity": False,
                "is_high_risk_vol": True,
                "tte_hours": 12.0,
                "strategy": "Defensive",
                "trigger_logic": "High IV before earnings",
                "trend": "BULLISH_STRONG",
                "runway_impact": 1.5,
            }
        ]

        await cog.iv_rank.callback(cog, mock_interaction)

        mock_interaction.followup.send.assert_called_once()
        embed = mock_interaction.followup.send.call_args[1]["embed"]
        assert "高波動 & IV Crush 風險掃描" in embed.title
        assert "NVDA" in embed.fields[0].name
        assert "92.5%" in embed.fields[0].name


@pytest.mark.asyncio
async def test_command_event_impact(mock_interaction, db_conn):
    bot = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    cog = CalendarCog(bot)

    user_id = mock_interaction.user.id
    symbol = "AAPL"

    # 1. Setup mock portfolio record
    from database.portfolio import add_portfolio_record

    add_portfolio_record(
        user_id,
        symbol,
        "call",
        150,
        "2026-06-19",
        5.0,
        1,
        150.0,
        weighted_delta=10.0,
        gamma=0.5,
    )

    # 2. Mock market data for price
    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch("market_analysis.greeks.calculate_vanna") as mock_vanna:
        mock_quote.return_value = {"c": 155.0}
        mock_vanna.return_value = 2.5  # Simulated Vanna

        await cog.event_impact.callback(
            cog, mock_interaction, symbol=symbol, vol_move=20.0
        )

        mock_interaction.followup.send.assert_called_once()
        embed = mock_interaction.followup.send.call_args[1]["embed"]
        assert f"{symbol} 事件風險模擬" in embed.title
        assert "10.00" in embed.fields[0].value  # Beta-Weighted Delta
        assert "Hidden Delta" in embed.fields[2].name
