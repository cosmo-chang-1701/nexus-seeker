from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.sentiment import SentimentCog


@pytest.mark.asyncio
async def test_max_pain_uses_builder(mock_interaction):
    bot = MagicMock()
    cog = SentimentCog(bot)
    embed = object()
    data = {
        "expiry": "2099-01-02",
        "max_pain": 500,
        "current_price": 498.5,
        "distance_pct": -0.3,
        "is_converging": True,
    }

    with patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
        new=AsyncMock(return_value=data),
    ), patch(
        "cogs.sentiment.create_max_pain_embed", return_value=embed
    ) as mock_builder:
        await cog.max_pain.callback(cog, mock_interaction, symbol="spy")

    mock_builder.assert_called_once_with("SPY", data)
    mock_interaction.followup.send.assert_called_once_with(embed=embed)
