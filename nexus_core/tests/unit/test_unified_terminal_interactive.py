import pytest
import discord
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
    """測試 /x 指令按鈕互動與讀取狀態"""
    view = SymbolHubView(symbol="AAPL", user_id=123, bot=mock_bot)
    # 準備 base_data 以供 btn_home 使用
    view.base_data = {"symbol": "AAPL", "vix": 15.0, "spy_price": 500.0}

    # 測試新聞按鈕的狀態轉換 (應使用 edit_original_response)
    with patch(
        "services.news_service.fetch_recent_news", new_callable=AsyncMock
    ) as mock_news:
        mock_news.return_value = "Mock News"

        # 執行 Callback
        await view.btn_news.callback(mock_interaction)

        # 驗證 1: 呼叫了 defer
        mock_interaction.response.defer.assert_called_once()

        # 驗證 2: 呼叫了兩次 edit_original_response (一次設為 loading，一次恢復並更新 Embed)
        assert mock_interaction.edit_original_response.call_count == 2

        # 驗證 3: 最後一次呼叫時按鈕應為啟用狀態，且帶有 Embed
        _, last_kwargs = mock_interaction.edit_original_response.call_args
        assert last_kwargs["view"].children[0].disabled is False
        assert "AAPL 官方新聞掃描" in last_kwargs["embed"].title

    # 測試 Home 按鈕 (應恢復主頁)
    mock_interaction.edit_original_response.reset_mock()
    with patch(
        "services.market_data_service.get_quote", new_callable=AsyncMock
    ) as mock_quote, patch(
        "database.user_settings.get_full_user_context"
    ) as mock_user_ctx:
        mock_quote.return_value = {"dp": 1.0, "c": 150.0}
        mock_user_ctx.return_value = MagicMock(capital=100000)

        await view.btn_home.callback(mock_interaction)
        assert mock_interaction.edit_original_response.call_count == 2
        _, last_kwargs = mock_interaction.edit_original_response.call_args
        assert "標的分析中心: AAPL" in last_kwargs["embed"].title


@pytest.mark.asyncio
async def test_portfolio_hub_interactions(mock_interaction, mock_bot):
    """測試 /dash 指令分頁互動與讀取狀態"""
    view = PortfolioHubView(user_id=123, bot=mock_bot)

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

        # 驗證呼叫了兩次 edit_original_response (一次禁用，一次恢復並帶入資料)
        assert mock_interaction.edit_original_response.call_count == 2

        # 檢查最後一次呼叫的參數
        _, last_kwargs = mock_interaction.edit_original_response.call_args
        assert last_kwargs["embed"] is not None
        assert "現貨持倉清單" in last_kwargs["embed"].title
        assert last_kwargs["view"].children[0].disabled is False


@pytest.mark.asyncio
async def test_pulse_hub_interactions(mock_interaction, mock_bot):
    """測試 /market 指令互動與讀取狀態"""
    view = PulseHubView(user_id=123, bot=mock_bot)

    # 測試預測市場按鈕
    mock_bot.polymarket_service = MagicMock()
    mock_bot.polymarket_service.get_active_markets.return_value = [
        {"question": "Test?", "tokens": []}
    ]

    with patch("cogs.unified_terminal.create_polymarket_list_embed") as mock_embed_gen:
        mock_embed_gen.return_value = MagicMock(spec=discord.Embed)

        await view.btn_poly.callback(mock_interaction)

        # 驗證讀取狀態切換
        assert mock_interaction.edit_original_response.call_count == 2
        _, last_kwargs = mock_interaction.edit_original_response.call_args
        assert last_kwargs["view"].children[0].disabled is False
