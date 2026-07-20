import pytest
from unittest.mock import AsyncMock, patch, MagicMock, PropertyMock
import discord
from discord.app_commands import Choice
import sys
import os

sys.path.append(os.path.join(os.getcwd(), "nexus_core"))
sys.path.append(os.getcwd())

from cogs.unified_terminal.cog import UnifiedTerminalCog
from cogs.unified_terminal.radar_view import UnifiedRadarView, FilterParamsModal


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    return bot


@pytest.fixture
def mock_interaction():
    interaction = AsyncMock()
    interaction.user.id = 12345
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.is_done.return_value = False
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_symbol_hub_opens_radar_panel(mock_bot, mock_interaction):
    """
    測試當 `/x` 未帶任何參數時，正確展開 Unified Radar Panel (UI 模式)
    """
    cog = UnifiedTerminalCog(mock_bot)

    with patch(
        "cogs.embed_builders.scan_embeds.build_unified_radar_panel_embed"
    ) as mock_build_embed:
        mock_build_embed.return_value = discord.Embed(title="Panel Embed")

        await cog.symbol_hub.callback(
            cog, mock_interaction, symbol=None, scan_type=None, tag=None, squeeze=None
        )

        mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
        # Should instantiate UnifiedRadarView and send
        call_kwargs = mock_interaction.followup.send.call_args.kwargs
        assert "view" in call_kwargs
        assert isinstance(call_kwargs["view"], UnifiedRadarView)
        assert call_kwargs["embed"].title == "Panel Embed"


@pytest.mark.asyncio
async def test_symbol_hub_bypass_ui(mock_bot, mock_interaction):
    """
    測試當 `/x` 帶有 scan_type 參數時，能跳過 UI 面板並直接執行 execute_unified_scan (進階用戶 Bypass)
    """
    cog = UnifiedTerminalCog(mock_bot)
    cog.execute_unified_scan = AsyncMock()

    scan_type = Choice(name="ALL", value="ALL")
    await cog.symbol_hub.callback(
        cog,
        mock_interaction,
        symbol=None,
        scan_type=scan_type,
        tag="TECH",
        squeeze=True,
    )

    # 預期能直接呼叫 execute_unified_scan，並且將舊參數轉為新的 state
    cog.execute_unified_scan.assert_called_once()
    state = cog.execute_unified_scan.call_args.args[1]

    assert state["scope"] == "ALL"
    assert state["selected_tag"] == "TECH"
    assert "require_tdp_signal" in state["quant_filters"]


@pytest.mark.asyncio
async def test_radar_view_interactions(mock_bot, mock_interaction):
    """
    測試 UnifiedRadarView 介面互動與狀態更新邏輯
    """
    cog = UnifiedTerminalCog(mock_bot)
    view = UnifiedRadarView(cog, 12345)

    # 1. Scope Selector Change (非 WATCHLIST 不讀 DB)
    with patch.object(
        type(view.scope_select), "values", new_callable=PropertyMock
    ) as mock_values:
        mock_values.return_value = ["HOLDINGS"]
        await view.on_scope_change(mock_interaction)
    assert view.scope == "HOLDINGS"
    mock_interaction.response.edit_message.assert_called_once()

    # 2. Filter Selector Change
    mock_interaction.response.edit_message.reset_mock()
    with patch.object(
        type(view.filter_select), "values", new_callable=PropertyMock
    ) as mock_values:
        mock_values.return_value = ["exclude_martial_law", "strict_liquidity"]
        await view.on_filter_change(mock_interaction)
    assert "exclude_martial_law" in view.quant_filters
    assert "strict_liquidity" in view.quant_filters
    mock_interaction.response.edit_message.assert_called_once()

    # 3. Parameter Modal Popup
    await view.on_adjust_params(mock_interaction)
    mock_interaction.response.send_modal.assert_called_once()
    modal = mock_interaction.response.send_modal.call_args.args[0]
    assert isinstance(modal, FilterParamsModal)

    # 4. Execute Scan Route
    cog.execute_unified_scan = AsyncMock()
    mock_interaction.response.is_done.return_value = (
        True  # 假設在 interaction 中被 defer
    )
    await view.on_execute_scan(mock_interaction)
    mock_interaction.response.defer.assert_called()
    cog.execute_unified_scan.assert_called_once_with(
        mock_interaction, view.get_state_dict(), 12345
    )


@pytest.mark.asyncio
async def test_execute_unified_scan_filters(mock_bot, mock_interaction):
    """
    測試 execute_unified_scan 是否正確根據 state 的進階條件過濾標的
    """
    cog = UnifiedTerminalCog(mock_bot)

    state = {
        "scope": "ALL",  # 使用 ALL 避免針對 WATCHLIST 等特定情境進行 mock
        "quant_filters": ["require_tdp_signal", "exclude_martial_law"],
        "params": {
            "max_pain_threshold": 10.0,  # 10% 限制
            "abs_support_tolerance": 1.0,
            "silent_period_days": 5,
        },
        "selected_tag": None,
    }

    # Mocking target symbols gathering to return fake tickers
    with patch("cogs.unified_terminal.cog.asyncio.to_thread") as mock_thread:
        # Mock active orders, holdings, portfolio
        # Active orders uses dict access (o["symbol"]), portfolio uses tuple index (row[1])
        def mock_to_thread_side_effect(func, *args, **kwargs):
            if "get_user_portfolio" in func.__name__:
                return [(123, "AAPL"), (123, "TSLA")]
            return [{"symbol": "AAPL"}, {"symbol": "TSLA"}]

        mock_thread.side_effect = mock_to_thread_side_effect

        # Mock AssetManager
        class FakeAsset:
            symbol = "NVDA"

        with patch(
            "services.asset_manager.AssetManager.get_assets", return_value=[FakeAsset()]
        ):
            # Setup fetch radar data responses
            async def fake_fetch(sym):
                if sym == "AAPL":
                    # 符合條件：有 squeeze, max_pain distance < 10%
                    return {
                        "symbol": "AAPL",
                        "psq_result": {"is_squeezing": True},
                        "max_pain": {"distance_pct": 0.05},  # 5% < 10%
                    }
                elif sym == "NVDA":
                    # 違反 require_tdp_signal (is_squeezing == False)
                    return {
                        "symbol": "NVDA",
                        "psq_result": {"is_squeezing": False},
                        "max_pain": {"distance_pct": 0.05},
                    }
                elif sym == "TSLA":
                    # 違反 exclude_martial_law (distance_pct == 0.15 > 0.10)
                    return {
                        "symbol": "TSLA",
                        "psq_result": {"is_squeezing": True},
                        "max_pain": {"distance_pct": 0.15},
                    }
                return None

            cog._fetch_sym_radar_data = fake_fetch

            # Mock build_radar_scan_embed to capture the filtered result
            with patch(
                "cogs.unified_terminal.cog.build_radar_scan_embed"
            ) as mock_builder:
                mock_builder.return_value = discord.Embed(title="Radar Scan")

                # Mock BatchScanView
                with patch("cogs.unified_terminal.cog.BatchScanView") as MockView:
                    MockView.return_value = discord.ui.View()

                    await cog.execute_unified_scan(mock_interaction, state, 12345)

                    # Assert what was passed to build_radar_scan_embed
                    mock_builder.assert_called_once()
                    filtered_results = mock_builder.call_args.args[0]
                    assert len(filtered_results) == 1
                    assert filtered_results[0]["symbol"] == "AAPL"
