import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import discord

from cogs.calendar import CalendarCog


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    return bot


@pytest.fixture
def mock_interaction():
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.id = 12345
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.mark.asyncio
@patch("database.watchlist.get_user_watchlist")
@patch("cogs.calendar.calendar_service.get_high_impact_events")
@patch("cogs.calendar.calendar_service.get_symbol_earnings_batch")
async def test_calendar_command_high_rate(
    mock_get_earnings, mock_get_macro, mock_get_watchlist, mock_bot, mock_interaction
):
    cog = CalendarCog(mock_bot)

    mock_get_watchlist.return_value = [("AAPL", 1), ("TSLA", 1)]

    mock_macro = [
        MagicMock(
            event="FOMC",
            country="US",
            impact="high",
            tte_hours=24.0,
            time="2026-06-27T14:00:00Z",
            fedwatch_probability=0.75,
        )
    ]
    mock_get_macro.return_value = mock_macro

    mock_get_earnings.return_value = {
        "AAPL": MagicMock(symbol="AAPL", date="2026-07-01", tte_hours=120.0),
        "TSLA": None,
    }

    await cog.calendar.callback(cog, mock_interaction)

    mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    mock_interaction.followup.send.assert_awaited_once()

    call_args = mock_interaction.followup.send.call_args[1]
    embed = call_args["embed"]

    assert "🗓️ 總經與財報事件日曆" in embed.title
    assert embed.color == discord.Color(0xE74C3C)
    assert any("逃頂窗口已動態前移" in f.value for f in embed.fields)
    assert any("FOMC" in f.value for f in embed.fields)
    assert any("AAPL" in f.value for f in embed.fields)


@pytest.mark.asyncio
@patch("database.watchlist.get_user_watchlist")
@patch("cogs.calendar.calendar_service.get_high_impact_events")
@patch("cogs.calendar.calendar_service.get_symbol_earnings_batch")
async def test_calendar_command_rate_cut(
    mock_get_earnings, mock_get_macro, mock_get_watchlist, mock_bot, mock_interaction
):
    cog = CalendarCog(mock_bot)

    mock_get_watchlist.return_value = []

    mock_macro = [
        MagicMock(
            event="CPI",
            country="US",
            impact="high",
            tte_hours=48.0,
            time="2026-06-28T14:00:00Z",
            fedwatch_probability=0.45,
        )
    ]
    mock_get_macro.return_value = mock_macro

    mock_get_earnings.return_value = {}

    await cog.calendar.callback(cog, mock_interaction)

    mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    mock_interaction.followup.send.assert_awaited_once()

    call_args = mock_interaction.followup.send.call_args[1]
    embed = call_args["embed"]

    assert embed.color == discord.Color(0x3498DB)
    assert any("逃頂窗口已後推 5 天" in f.value for f in embed.fields)
