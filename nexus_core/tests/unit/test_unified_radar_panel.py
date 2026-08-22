from typing import Any
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
from cogs.unified_terminal.batch_scan_view import (
    BatchScanPaginatedView,
    BatchScanWarningButton,
)


@pytest.fixture
def mock_bot() -> Any:
    bot = MagicMock()
    return bot


@pytest.fixture
def mock_interaction() -> Any:
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
async def test_symbol_hub_opens_radar_panel(mock_bot: Any, mock_interaction: Any):  # type: ignore
    """
    測試當 `/x` 未帶任何參數時，正確展開 Unified Radar Panel (UI 模式)
    """
    cog = UnifiedTerminalCog(mock_bot)

    with patch(
        "cogs.embed_builders.scan_embeds.build_unified_radar_panel_embed"
    ) as mock_build_embed:
        mock_build_embed.return_value = discord.Embed(title="Panel Embed")

        await cog.symbol_hub.callback(  # type: ignore
            cog,  # type: ignore
            mock_interaction,
            symbol=None,
            scan_type=None,
            tag=None,
            squeeze=None,
        )

        mock_interaction.response.defer.assert_called_once_with(ephemeral=True)
        # Should instantiate UnifiedRadarView and send
        call_kwargs = mock_interaction.followup.send.call_args.kwargs
        assert "view" in call_kwargs
        assert isinstance(call_kwargs["view"], UnifiedRadarView)
        assert call_kwargs["embed"].title == "Panel Embed"


@pytest.mark.asyncio
async def test_symbol_hub_bypass_ui(mock_bot: Any, mock_interaction: Any):  # type: ignore
    """
    測試當 `/x` 帶有 scan_type 參數時，能跳過 UI 面板並直接執行 execute_unified_scan (進階用戶 Bypass)
    """
    cog = UnifiedTerminalCog(mock_bot)
    cog.execute_unified_scan = AsyncMock()

    scan_type = Choice(name="ALL", value="ALL")
    await cog.symbol_hub.callback(  # type: ignore
        cog,  # type: ignore
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
    assert "squeeze_mode" in state["quant_filters"]


@pytest.mark.asyncio
async def test_radar_view_interactions(mock_bot: Any, mock_interaction: Any):  # type: ignore
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
async def test_execute_unified_scan_filters(mock_bot: Any, mock_interaction: Any):  # type: ignore
    """
    測試 execute_unified_scan 是否正確根據 state 的進階條件過濾標的
    """
    cog = UnifiedTerminalCog(mock_bot)

    state = {
        "scope": "ALL",  # 使用 ALL 避免針對 WATCHLIST 等特定情境進行 mock
        "quant_filters": ["dp_skew_defense", "exclude_martial_law"],
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
        def mock_to_thread_side_effect(func: Any, *args, **kwargs):  # type: ignore
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
            async def fake_fetch(sym: Any):  # type: ignore
                if sym == "AAPL":
                    # 符合條件：無極端派發 (skew >= -0.3), max_pain distance < 10%
                    return {
                        "symbol": "AAPL",
                        "skew": -0.2,
                        "max_pain": {"distance_pct": 0.05},  # 5% < 10%
                    }
                elif sym == "NVDA":
                    # 違反 dp_skew_defense (skew < -0.3)
                    return {
                        "symbol": "NVDA",
                        "skew": -0.4,
                        "max_pain": {"distance_pct": 0.05},
                    }
                elif sym == "TSLA":
                    # 違反 exclude_martial_law (distance_pct == 0.15 > 0.10)
                    return {
                        "symbol": "TSLA",
                        "skew": -0.2,
                        "max_pain": {"distance_pct": 0.15},
                    }
                return None

            cog._fetch_sym_radar_data_fast = fake_fetch  # type: ignore

            # Mock build_radar_scan_embed to capture the filtered result
            with patch(
                "cogs.unified_terminal.cog.build_radar_scan_embed"
            ) as mock_builder:
                mock_builder.return_value = discord.Embed(title="Radar Scan")

                # Mock BatchScanPaginatedView
                with patch(
                    "cogs.unified_terminal.cog.BatchScanPaginatedView"
                ) as MockView:
                    MockView.return_value = discord.ui.View()

                    await cog.execute_unified_scan(mock_interaction, state, 12345)

                    # Assert what was passed to build_radar_scan_embed
                    mock_builder.assert_called_once()
                    filtered_results = mock_builder.call_args.args[0]
                    assert len(filtered_results) == 1
                    assert filtered_results[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_execute_unified_scan_squeeze_mode_filter(
    mock_bot: Any, mock_interaction: Any
) -> None:
    """
    測試 execute_unified_scan 的 squeeze_mode 會正確套用為 require_squeeze_firing 過濾。
    """
    cog = UnifiedTerminalCog(mock_bot)

    state = {
        "scope": "ALL",
        "quant_filters": ["squeeze_mode"],
        "params": {
            "max_pain_threshold": 10.0,
            "abs_support_tolerance": 1.0,
            "silent_period_days": 5,
        },
        "selected_tag": None,
    }

    with patch("cogs.unified_terminal.cog.asyncio.to_thread") as mock_thread:

        def mock_to_thread_side_effect(func: Any, *args, **kwargs):  # type: ignore
            if "get_user_portfolio" in func.__name__:
                return [(123, "AAPL"), (123, "TSLA")]
            return [{"symbol": "AAPL"}, {"symbol": "TSLA"}]

        mock_thread.side_effect = mock_to_thread_side_effect

        with patch("services.asset_manager.AssetManager.get_assets", return_value=[]):

            async def fake_fetch(sym: Any):  # type: ignore
                if sym == "AAPL":
                    return {
                        "symbol": "AAPL",
                        "quote": {"c": 105.0},
                        "max_pain": {"max_pain": 100.0},
                        "psq_result": {"is_squeezing": True, "momentum_value": 1.2},
                        "gex_profile_data": {"put_wall": 100.0},
                        "uoa": [],
                    }
                if sym == "TSLA":
                    return {
                        "symbol": "TSLA",
                        "quote": {"c": 105.0},
                        "max_pain": {"max_pain": 100.0},
                        "psq_result": {"is_squeezing": False, "momentum_value": 1.2},
                        "gex_profile_data": {"put_wall": 100.0},
                        "uoa": [],
                    }
                return None

            cog._fetch_sym_radar_data_fast = fake_fetch  # type: ignore

            with patch(
                "cogs.unified_terminal.cog.build_radar_scan_embed"
            ) as mock_builder:
                mock_builder.return_value = discord.Embed(title="Radar Scan")
                with patch(
                    "cogs.unified_terminal.cog.BatchScanPaginatedView"
                ) as MockView:
                    MockView.return_value = discord.ui.View()
                    await cog.execute_unified_scan(mock_interaction, state, 12345)

                    mock_builder.assert_called_once()
                    filtered_results = mock_builder.call_args.args[0]
                    assert len(filtered_results) == 1
                    assert filtered_results[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_execute_unified_scan_magnetic_filters(
    mock_bot: Any, mock_interaction: Any
) -> None:
    """
    測試 execute_unified_scan 是否正確根據 magnetic_filters 條件過濾標的
    """
    cog = UnifiedTerminalCog(mock_bot)

    state = {
        "scope": "ALL",
        "quant_filters": ["magnetic_filters"],
        "params": {
            "min_max_pain_dev": 0.10,
            "abs_support_tolerance": 1.0,
        },
        "selected_tag": None,
    }

    with patch("cogs.unified_terminal.cog.asyncio.to_thread") as mock_thread:

        def mock_to_thread_side_effect(func: Any, *args, **kwargs):  # type: ignore
            if "get_user_portfolio" in func.__name__:
                return [(123, "AAPL"), (123, "NVDA")]
            return [
                {"symbol": "AAPL"},
                {"symbol": "NVDA"},
                {"symbol": "TSLA"},
                {"symbol": "AMD"},
            ]

        mock_thread.side_effect = mock_to_thread_side_effect

        with patch("services.asset_manager.AssetManager.get_assets", return_value=[]):

            async def fake_fetch(sym: Any):  # type: ignore
                if sym == "AAPL":
                    # 符合條件：dev > 0.10, price >= putwall, abs(dp_poc - putwall)/putwall < 0.01
                    return {
                        "symbol": "AAPL",
                        "quote": {"c": 115.0},
                        "max_pain": {"max_pain": 100.0},  # dev = 15.0/100 = 0.15 > 0.10
                        "gex_profile_data": {"put_wall": 110.0},  # price 115 >= 110
                        "dp_poc": 110.5,  # abs(110.5 - 110)/110 = 0.0045 < 0.01
                    }
                elif sym == "NVDA":
                    # 違反 min_max_pain_dev：dev <= 0.10
                    return {
                        "symbol": "NVDA",
                        "quote": {"c": 105.0},
                        "max_pain": {"max_pain": 100.0},  # dev = 5/100 = 0.05 <= 0.10
                        "gex_profile_data": {"put_wall": 100.0},
                        "dp_poc": 100.5,
                    }
                elif sym == "TSLA":
                    # 違反 exclude_putwall_breach：price < putwall
                    return {
                        "symbol": "TSLA",
                        "quote": {"c": 95.0},
                        "max_pain": {"max_pain": 80.0},  # dev = 15/80 > 0.10
                        "gex_profile_data": {"put_wall": 100.0},  # price 95 < 100
                        "dp_poc": 100.5,
                    }
                elif sym == "AMD":
                    # 違反 require_absolute_support：dp_poc 差距 >= 1%
                    return {
                        "symbol": "AMD",
                        "quote": {"c": 115.0},
                        "max_pain": {"max_pain": 100.0},  # dev = 0.15 > 0.10
                        "gex_profile_data": {"put_wall": 110.0},
                        "dp_poc": 115.0,  # abs(115 - 110)/110 = 5/110 = 0.045 >= 0.01
                    }
                return None

            cog._fetch_sym_radar_data_fast = fake_fetch  # type: ignore

            with patch(
                "cogs.unified_terminal.cog.build_radar_scan_embed"
            ) as mock_builder:
                mock_builder.return_value = discord.Embed(title="Radar Scan")
                with patch(
                    "cogs.unified_terminal.cog.BatchScanPaginatedView"
                ) as MockView:
                    MockView.return_value = discord.ui.View()
                    await cog.execute_unified_scan(mock_interaction, state, 12345)

                    mock_builder.assert_called_once()
                    filtered_results = mock_builder.call_args.args[0]
                    assert len(filtered_results) == 1
                    assert filtered_results[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_batch_scan_alpha_filters_and_pagination(
    mock_bot: Any, mock_interaction: Any
) -> None:
    """
    測試:
    1. Alpha 訊號 (TDP, UOA) 正確過濾標的。
    2. 當符合條件的標的超過 10 個、產生多頁 embeds 時，cog 只送出「一次」
       followup.send，並將所有分頁封裝進單一則訊息的 BatchScanPaginatedView，
       翻頁交由使用者點擊 ◀/▶ 按鈕就地編輯同一則訊息，藉此繞過 Discord
       單一互動的 followup 訊息數量上限。
    """
    cog = UnifiedTerminalCog(mock_bot)
    state: dict[str, Any] = {
        "scope": "WATCHLIST",
        "quant_filters": ["tdp_mode", "uoa_mode"],
        "params": {},
        "selected_tag": None,
    }

    # 模擬 15 檔標的資料，前 12 檔符合 Alpha 條件
    async def mock_fetch_sym(sym: str) -> dict[str, Any]:
        idx = int(sym.replace("SYM_", ""))
        is_valid = idx <= 12
        return {
            "symbol": sym,
            "quote": {"c": 90.0 if is_valid else 110.0},
            "ma20": 100.0,
            "max_pain": {"max_pain": 100.0},
            "dp_poc": 100.0,
            "uoa": [{"trade_type": "SWEEP", "delta": 1.5 if is_valid else 0.5}],
            "skew": -0.1,  # 符合 dark_pool_skew_floor (-0.2)
        }

    cog._fetch_sym_radar_data_fast = mock_fetch_sym  # type: ignore

    with patch("cogs.unified_terminal.cog.asyncio.to_thread") as mock_thread:
        # 模擬 WATCHLIST 有 15 檔

        def mock_to_thread_side_effect(func: Any, *args: Any, **kwargs: Any):  # type: ignore
            if "get_user_watchlist" in func.__name__:
                return [[f"SYM_{i}"] for i in range(1, 16)]
            return []

        mock_thread.side_effect = mock_to_thread_side_effect

        with patch("cogs.unified_terminal.cog.build_radar_scan_embed") as mock_builder:
            page_1 = discord.Embed(title="Radar Scan (第 1/2 頁)")
            page_2 = discord.Embed(title="Radar Scan (第 2/2 頁)")
            mock_builder.return_value = [page_1, page_2]

            await cog.execute_unified_scan(mock_interaction, state, 12345)

            mock_builder.assert_called_once()
            filtered_results = mock_builder.call_args.args[0]
            assert len(filtered_results) == 12
            assert filtered_results[0]["symbol"] == "SYM_1"

            # 只送出一次 followup，翻頁改由 BatchScanPaginatedView 就地編輯訊息
            assert mock_interaction.followup.send.call_count == 1

            _, kwargs = mock_interaction.followup.send.call_args
            assert kwargs["embed"].title == "Radar Scan (第 1/2 頁)"
            assert isinstance(kwargs["view"], BatchScanPaginatedView)
            assert kwargs["view"].embeds == [page_1, page_2]


@pytest.mark.asyncio
async def test_batch_scan_reports_error_when_send_fails(
    mock_bot: Any, mock_interaction: Any
) -> None:
    """
    測試:單一 followup.send（帶著整批分頁的 BatchScanPaginatedView）失敗時，
    外層例外處理應補發一則錯誤通知，而不是讓例外往外拋出、整個指令悄無聲息地
    失敗（回歸「第 6/7 頁卻沒有第 7 頁」臭蟲——修正後已不存在「逐頁分別發送」
    這個失敗模式，但仍需確保單次發送失敗時使用者收得到錯誤提示）。
    """
    cog = UnifiedTerminalCog(mock_bot)
    state: dict[str, Any] = {
        "scope": "WATCHLIST",
        "quant_filters": [],
        "params": {},
        "selected_tag": None,
    }

    async def mock_fetch_sym(sym: str) -> dict[str, Any]:
        return {
            "symbol": sym,
            "quote": {"c": 100.0},
            "ma20": 100.0,
            "max_pain": {"max_pain": 100.0},
            "dp_poc": 100.0,
            "uoa": [],
            "skew": 0.0,
        }

    cog._fetch_sym_radar_data_fast = mock_fetch_sym  # type: ignore

    with patch("cogs.unified_terminal.cog.asyncio.to_thread") as mock_thread:

        def mock_to_thread_side_effect(func: Any, *args: Any, **kwargs: Any):  # type: ignore
            if "get_user_watchlist" in func.__name__:
                return [[f"SYM_{i}"] for i in range(1, 4)]
            return []

        mock_thread.side_effect = mock_to_thread_side_effect

        with patch("cogs.unified_terminal.cog.build_radar_scan_embed") as mock_builder:
            mock_builder.return_value = [
                discord.Embed(title="Radar Scan (第 1/3 頁)"),
                discord.Embed(title="Radar Scan (第 2/3 頁)"),
                discord.Embed(title="Radar Scan (第 3/3 頁)"),
            ]

            async def send_side_effect(*args: Any, **kwargs: Any) -> Any:
                if kwargs.get("view") is not None:
                    raise discord.HTTPException(MagicMock(status=400), "boom")
                return MagicMock()

            mock_interaction.followup.send = AsyncMock(side_effect=send_side_effect)

            await cog.execute_unified_scan(mock_interaction, state, 12345)

            # 第一次呼叫（帶 view，失敗）+ 外層例外處理補發的錯誤通知 = 2 次呼叫
            assert mock_interaction.followup.send.call_count == 2

            call_1, call_2 = mock_interaction.followup.send.call_args_list
            assert "view" in call_1.kwargs

            assert "view" not in call_2.kwargs
            assert "執行批次掃描時發生錯誤" in call_2.kwargs["embed"].description


@pytest.mark.asyncio
async def test_btn_return_panel_switches_back_to_radar_view(
    mock_bot: Any,
    mock_interaction: Any,
) -> None:
    """測試 BatchScanPaginatedView 點擊『🔄 返回控制面板』按鈕時，原地切換回 UnifiedRadarView。"""
    cog = UnifiedTerminalCog(mock_bot)
    embeds = [discord.Embed(title="Radar Page 1")]
    view = BatchScanPaginatedView(embeds, cog, mock_bot, total_items=1)

    with patch(
        "cogs.embed_builders.scan_embeds.build_unified_radar_panel_embed"
    ) as mock_panel_embed:
        mock_panel_embed.return_value = discord.Embed(title="Panel Embed")

        await view.btn_return_panel.callback(mock_interaction)

        mock_interaction.response.edit_message.assert_called_once()
        kwargs = mock_interaction.response.edit_message.call_args.kwargs
        assert kwargs["embed"].title == "Panel Embed"
        assert isinstance(kwargs["view"], UnifiedRadarView)


@pytest.mark.asyncio
async def test_batch_scan_warning_button_concurrent_semaphore(
    mock_bot: Any,
    mock_interaction: Any,
) -> None:
    """測試 BatchScanWarningButton 點擊時能解析警示標的並使用 Semaphore(3) 併發分析。"""
    cog = UnifiedTerminalCog(mock_bot)
    cog._run_single_symbol_hub = AsyncMock()

    # 模擬 Embed 帶有即時聯動警示
    embed = discord.Embed(title="Radar")
    embed.add_field(
        name="💡 即時聯動警示 (Real-time Insights)",
        value="• 🚀 NVDA: 價格逼近波動下緣\n• 🚀 TSLA: 價格逼近波動下緣",
        inline=False,
    )
    mock_interaction.message = MagicMock()
    mock_interaction.message.embeds = [embed]

    embeds = [embed]
    view = BatchScanPaginatedView(embeds, cog, mock_bot, total_items=1)
    warning_btn = [c for c in view.children if isinstance(c, BatchScanWarningButton)][0]

    await warning_btn.callback(mock_interaction)

    # 驗證兩個標的皆被呼叫分析
    assert cog._run_single_symbol_hub.call_count == 2
    called_symbols = [
        call.args[1] for call in cog._run_single_symbol_hub.call_args_list
    ]
    assert "NVDA" in called_symbols
    assert "TSLA" in called_symbols


@pytest.mark.asyncio
async def test_fetch_sym_radar_data_fast_stitches_month_max_pains_and_ma20(
    mock_bot: Any,
) -> None:
    """測試 _fetch_sym_radar_data_fast_raw 正確從快取中縫合 month_max_pains 與 ma20。"""
    cog = UnifiedTerminalCog(mock_bot)

    with (
        patch(
            "services.market_data_service.get_quote",
            return_value={"c": 150.0, "volume": 1000000},
        ),
        patch(
            "database.cache.get_kv_cache",
            side_effect=lambda key: (
                {
                    "ma20": 145.0,
                    "month_max_pains": [{"expiry": "2026-08-28", "max_pain": 148.0}],
                }
                if "radar_terminal_NVDA" in key
                else None
            ),
        ),
        patch("database.market_cache.get_market_cache", return_value={}),
        patch(
            "database.squeeze_cache.get_squeeze_cache",
            return_value={
                "momentum": 1.5,
                "direction": "🟢",
                "is_squeezing": False,
            },
        ),
    ):
        data = await cog._fetch_sym_radar_data_fast_raw("NVDA")
        assert data["symbol"] == "NVDA"
        assert data["ma20"] == 145.0
        assert len(data["month_max_pains"]) == 1
        assert data["month_max_pains"][0]["max_pain"] == 148.0


@pytest.mark.asyncio
async def test_exclude_martial_law_putwall_breach_and_neg_gex(
    mock_bot: Any,
    mock_interaction: Any,
) -> None:
    """測試 exclude_martial_law 能正確過濾跌破 PutWall 與落入負 Gamma 的標的。"""
    cog = UnifiedTerminalCog(mock_bot)

    state = {
        "scope": "ALL",
        "quant_filters": ["exclude_martial_law"],
        "params": {
            "max_pain_threshold": 10.0,
            "abs_support_tolerance": 1.0,
            "silent_period_days": 5,
        },
        "selected_tag": None,
    }

    with (
        patch("cogs.unified_terminal.cog.asyncio.to_thread") as mock_thread,
        patch("services.asset_manager.AssetManager.get_assets", return_value=[]),
    ):

        def mock_to_thread_side_effect(func: Any, *args: Any, **kwargs: Any) -> Any:
            if "get_user_portfolio" in getattr(func, "__name__", ""):
                return [(123, "NVDA"), (123, "TSLA"), (123, "AAPL")]
            return [{"symbol": "NVDA"}, {"symbol": "TSLA"}, {"symbol": "AAPL"}]

        mock_thread.side_effect = mock_to_thread_side_effect

        async def fake_fetch(sym: str) -> dict[str, Any]:
            if sym == "NVDA":
                # 跌破 PutWall: spot 100 < put_wall 105
                return {
                    "symbol": "NVDA",
                    "quote": {"c": 100.0},
                    "max_pain": {"distance_pct": 0.02},
                    "gex_profile_data": {
                        "put_wall": 105.0,
                        "net_gex": 100000.0,
                    },
                }
            elif sym == "TSLA":
                # 負 Gamma: net_gex < 0
                return {
                    "symbol": "TSLA",
                    "quote": {"c": 200.0},
                    "max_pain": {"distance_pct": 0.02},
                    "gex_profile_data": {
                        "put_wall": 190.0,
                        "net_gex": -500000.0,
                    },
                }
            elif sym == "AAPL":
                # 正常標的: spot 220 > put_wall 200, net_gex > 0, dist 2% < 10%
                return {
                    "symbol": "AAPL",
                    "quote": {"c": 220.0},
                    "max_pain": {"distance_pct": 0.02},
                    "gex_profile_data": {
                        "put_wall": 200.0,
                        "net_gex": 500000.0,
                    },
                }
            return {}

        cog._fetch_sym_radar_data_fast = fake_fetch  # type: ignore

        with patch("cogs.unified_terminal.cog.build_radar_scan_embed") as mock_builder:
            mock_builder.return_value = [discord.Embed(title="Radar Page")]

            await cog.execute_unified_scan(mock_interaction, state, 12345)

            # 斷言只有 AAPL 通過過濾
            mock_builder.assert_called_once()
            passed_results = mock_builder.call_args.args[0]
            passed_symbols = [r["symbol"] for r in passed_results]
            assert passed_symbols == ["AAPL"]
