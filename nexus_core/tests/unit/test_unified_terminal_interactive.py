import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import sys
import os

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from cogs.unified_terminal import (
    SymbolHubView,
    PortfolioHubView,
    PulseHubView,
)


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_symbol_hub_interactions(mock_interaction, mock_bot):
    """測試 /x 指令按鈕互動"""
    view = SymbolHubView(symbol="AAPL", user_id=123, bot=mock_bot)

    # 1. 測試新聞按鈕
    with patch(
        "services.news_service.fetch_recent_news", new_callable=AsyncMock
    ) as mock_news:
        mock_news.return_value = "Mock News"
        await view.btn_news.callback(mock_interaction)
        mock_interaction.response.defer.assert_called()
        mock_interaction.followup.send.assert_called()
        args, kwargs = mock_interaction.followup.send.call_args
        assert "embed" in kwargs
        assert "📰 AAPL 官方新聞掃描" in kwargs["embed"].title

    # 2. 測試 Reddit 按鈕
    mock_interaction.followup.send.reset_mock()
    with patch(
        "services.reddit_service.get_reddit_context", new_callable=AsyncMock
    ) as mock_reddit:
        mock_reddit.return_value = "Mock Reddit"
        await view.btn_reddit.callback(mock_interaction)
        args, kwargs = mock_interaction.followup.send.call_args
        assert "embed" in kwargs
        assert "🔥 AAPL 散戶情緒優勢" in kwargs["embed"].title

    # 3. 測試情緒掃描按鈕
    mock_interaction.followup.send.reset_mock()
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
    ) as mock_max_pain:
        mock_skew.return_value = {"skew": 5, "state": "Normal"}
        mock_pcr.return_value = {"pcr": 0.8, "state": "Normal"}
        mock_uoa.return_value = []
        mock_max_pain.return_value = {"max_pain": 150, "is_converging": True}

        await view.btn_sentiment.callback(mock_interaction)
        args, kwargs = mock_interaction.followup.send.call_args
        assert "embed" in kwargs
        assert "📊 AAPL 期權情緒掃描" in kwargs["embed"].title


@pytest.mark.asyncio
async def test_portfolio_hub_interactions(mock_interaction, mock_bot):
    """測試 /dash 指令分頁互動"""
    view = PortfolioHubView(user_id=123, bot=mock_bot)

    # 測試現貨持倉按鈕
    with patch(
        "services.asset_manager.AssetManager.get_assets"
    ) as mock_get_assets, patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch(
        "database.user_settings.get_full_user_context"
    ) as mock_user_ctx:
        mock_asset = MagicMock()
        mock_asset.symbol = "AAPL"
        mock_asset.metadata = {"quantity": 10, "avg_cost": 150}
        mock_get_assets.return_value = [mock_asset]
        mock_quote.return_value = {"c": 160}
        mock_user_ctx.return_value = MagicMock(capital=100000)

        await view.btn_holdings.callback(mock_interaction)
        mock_interaction.edit_original_response.assert_called()
        _, kwargs = mock_interaction.edit_original_response.call_args
        assert "embed" in kwargs
        assert "現貨持倉清單" in kwargs["embed"].title


@pytest.mark.asyncio
async def test_pulse_hub_interactions(mock_interaction, mock_bot):
    """測試 /market 指令互動"""
    view = PulseHubView(user_id=123, bot=mock_bot)

    # 測試預測市場按鈕
    mock_bot.polymarket_service = MagicMock()
    mock_bot.polymarket_service.get_active_markets.return_value = [
        {"question": "Test?", "tokens": []}
    ]

    await view.btn_poly.callback(mock_interaction)
    mock_interaction.edit_original_response.assert_called()
    _, kwargs = mock_interaction.edit_original_response.call_args
    assert "embed" in kwargs
    assert "Polymarket 巨鯨意圖圖譜" in kwargs["embed"].title
