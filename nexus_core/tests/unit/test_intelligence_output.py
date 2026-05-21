from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.intelligence import IntelligenceCog


@pytest.mark.asyncio
async def test_poly_status_uses_builder(mock_interaction):
    bot = MagicMock()
    bot.polymarket_service = MagicMock()
    bot.polymarket_service.get_status.return_value = {
        "connected": True,
        "running": True,
        "asset_count": 42,
        "last_message": "2026-05-21 17:00:00",
        "errors": 1,
    }
    cog = IntelligenceCog(bot)
    embed = object()

    with patch(
        "cogs.intelligence.create_polymarket_status_embed",
        return_value=embed,
    ) as mock_builder:
        await cog.poly_status.callback(cog, mock_interaction)

    mock_builder.assert_called_once_with(bot.polymarket_service.get_status.return_value)
    mock_interaction.response.send_message.assert_called_once_with(
        embed=embed, ephemeral=True
    )


@pytest.mark.asyncio
async def test_quote_uses_builder(mock_interaction):
    bot = MagicMock()
    cog = IntelligenceCog(bot)
    embed = object()
    quote = {"c": 150.0, "dp": 1.3, "h": 155.0, "l": 145.0, "pc": 148.0}

    with patch(
        "services.market_data_service.get_quote",
        new=AsyncMock(return_value=quote),
    ), patch(
        "cogs.intelligence.create_quote_embed", return_value=embed
    ) as mock_builder:
        await cog.quote.callback(cog, mock_interaction, symbol="aapl")

    mock_builder.assert_called_once_with("AAPL", quote)
    mock_interaction.followup.send.assert_called_once_with(embed=embed, ephemeral=True)
