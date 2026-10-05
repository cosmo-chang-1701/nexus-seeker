from typing import Any
import pytest
from unittest.mock import AsyncMock, patch, MagicMock, ANY
import sys
import os
import pandas as pd
import discord

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from cogs.unified_terminal import (
    UnifiedTerminalCog,
    SymbolHubView,
    PortfolioHubView,
    PulseHubView,
    BatchScanPaginatedView,
)


@pytest.fixture
def mock_bot() -> Any:
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    bot.polymarket_service = MagicMock()
    bot.polymarket_service.get_market_snapshot = AsyncMock(return_value=[])
    return bot


@pytest.mark.asyncio
@pytest.mark.slow
async def test_symbol_hub_command(mock_interaction: Any, mock_bot: Any):  # type: ignore
    cog = UnifiedTerminalCog(mock_bot)

    with (
        patch(
            "services.market_data_service.validate_symbol", new_callable=AsyncMock
        ) as mock_val,
        patch(
            "services.market_data_service.get_spy_history_df", new_callable=AsyncMock
        ) as mock_spy_hist,
        patch(
            "services.market_data_service.get_macro_environment", new_callable=AsyncMock
        ) as mock_macro,
        patch(
            "services.market_data_service.get_quote", new_callable=AsyncMock
        ) as mock_quote,
        patch("market_math.analyze_symbol", new_callable=AsyncMock) as mock_analyze,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new_callable=AsyncMock,
        ) as mock_skew,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_indicator_percentile"
        ) as mock_skew_p,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
            new_callable=AsyncMock,
        ) as mock_mp,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
            new_callable=AsyncMock,
        ) as mock_pcr,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.detect_uoa_with_physical_caps",
            new_callable=AsyncMock,
        ) as mock_uoa,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
        ) as mock_iv,
        patch(
            "services.market_data_service.get_history_df", new_callable=AsyncMock
        ) as mock_hist,
        patch(
            "services.reddit_service.get_reddit_context", new_callable=AsyncMock
        ) as mock_reddit,
        patch(
            "market_analysis.ddp_inspector.DDPInspector.inspect_symbol",
            new_callable=AsyncMock,
        ) as mock_ddp,
        patch(
            "services.polymarket_service.PolymarketService.get_market_snapshot",
            new_callable=AsyncMock,
        ) as mock_poly,
        patch("database.get_full_user_context") as mock_user_ctx,
    ):
        mock_val.return_value = True
        mock_spy_hist.return_value = pd.DataFrame({"Close": [500.0]})
        mock_macro.return_value = {"vix": 15.0}
        mock_quote.return_value = {
            "c": 120.0,
            "dp": 1.5,
            "d": 1.8,
            "o": 119.0,
            "h": 121.0,
            "l": 118.0,
            "pc": 118.2,
        }

        mock_analyze.return_value = {
            "symbol": "NVDA",
            "price": 120.0,
            "hv_rank": 40.0,
        }
        mock_skew.return_value = {"skew": 5.0}
        mock_skew_p.return_value = 85.0
        mock_mp.return_value = {"max_pain": 115.0}
        mock_pcr.return_value = {"pcr": 0.8, "state": "正常"}
        mock_uoa.return_value = ([], [])

        mock_iv_metrics = MagicMock()
        mock_iv_metrics.iv_rank = 35.0
        mock_iv_metrics.iv_percentile = 38.0
        mock_iv_metrics.current_iv = 0.45
        mock_iv_metrics.expected_move_weekly = 5.0
        mock_iv_metrics.iv_status = "Normal"
        mock_iv_metrics.is_premarket = False
        mock_iv.return_value = mock_iv_metrics

        mock_hist.return_value = pd.DataFrame({"Close": [100.0, 105.0]})
        mock_reddit.return_value = "看多情緒高漲"
        mock_ddp.return_value = {"is_ddp": True}
        poly_market = MagicMock()
        poly_market.question = "Will NVDA exceed $130?"
        poly_market.tokens = [{"outcome": "Yes", "price": "0.65"}]
        mock_poly.return_value = [poly_market]

        mock_ctx = MagicMock()
        mock_ctx.capital = 100000.0
        mock_user_ctx.return_value = mock_ctx

        await cog.symbol_hub.callback(cog, mock_interaction, symbol="NVDA")  # type: ignore

        assert mock_interaction.followup.send.called
        _, kwargs = mock_interaction.followup.send.call_args
        assert "view" in kwargs
        assert isinstance(kwargs["view"], SymbolHubView)
        embed = kwargs["embed"]
        assert "標的分析中心: NVDA" in embed.title


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        # was: test_symbol_hub_command_tolerates_string_expected_move_context
        {
            "symbol": "DDOG",
            "analyze_price": 120.0,
            "skew": {"skew": 5.0},
            "max_pain": {"max_pain": 115.0},
            "iv_rank": 35.0,
            "iv_percentile": 38.0,
            "current_iv": 0.45,
            "em_return": {
                "reference_price": "--",
                "expected_move_weekly": "--",
                "expected_move_lower": 0.0,
                "expected_move_upper": 0.0,
            },
            "is_ddp": False,
        },
        # was: test_symbol_hub_command_tolerates_non_dict_expected_move_and_string_iv
        {
            "symbol": "NVDA",
            "analyze_price": 120.0,
            "skew": {"skew": 5.0},
            "max_pain": {"max_pain": 115.0},
            "iv_rank": "--",
            "iv_percentile": "--",
            "current_iv": "--",
            "em_return": "--",
            "is_ddp": False,
        },
        # was: test_symbol_hub_command_tolerates_string_max_pain_payload
        {
            "symbol": "NVDA",
            "analyze_price": "120.0",
            "skew": {"skew": "--"},
            "max_pain": {"max_pain": "--"},
            "iv_rank": "--",
            "iv_percentile": "--",
            "current_iv": "--",
            "em_return": "--",
            "is_ddp": True,
        },
    ],
    ids=[
        "string_expected_move_context",
        "non_dict_expected_move_and_string_iv",
        "string_max_pain_payload",
    ],
)
@pytest.mark.slow
async def test_symbol_hub_command_tolerates_malformed_payloads(  # type: ignore
    mock_interaction: Any, mock_bot: Any, case: dict[str, Any]
):
    """驗證 symbol_hub 對各種降級/畸形資料型別（字串 sentinel、非 dict 結構）皆不崩潰。"""
    cog = UnifiedTerminalCog(mock_bot)
    symbol = case["symbol"]

    with (
        patch(
            "services.market_data_service.validate_symbol", new_callable=AsyncMock
        ) as mock_val,
        patch(
            "services.market_data_service.get_spy_history_df", new_callable=AsyncMock
        ) as mock_spy_hist,
        patch(
            "services.market_data_service.get_macro_environment", new_callable=AsyncMock
        ) as mock_macro,
        patch(
            "services.market_data_service.get_quote", new_callable=AsyncMock
        ) as mock_quote,
        patch("market_math.analyze_symbol", new_callable=AsyncMock) as mock_analyze,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new_callable=AsyncMock,
        ) as mock_skew,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_indicator_percentile"
        ) as mock_skew_p,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
            new_callable=AsyncMock,
        ) as mock_mp,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
            new_callable=AsyncMock,
        ) as mock_pcr,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.detect_uoa_with_physical_caps",
            new_callable=AsyncMock,
        ) as mock_uoa,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
        ) as mock_iv,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_expected_move",
            new_callable=AsyncMock,
        ) as mock_em,
        patch(
            "services.market_data_service.get_history_df", new_callable=AsyncMock
        ) as mock_hist,
        patch(
            "services.reddit_service.get_reddit_context", new_callable=AsyncMock
        ) as mock_reddit,
        patch(
            "market_analysis.ddp_inspector.DDPInspector.inspect_symbol",
            new_callable=AsyncMock,
        ) as mock_ddp,
        patch(
            "services.polymarket_service.PolymarketService.get_market_snapshot",
            new_callable=AsyncMock,
        ) as mock_poly,
        patch("database.get_full_user_context") as mock_user_ctx,
    ):
        mock_val.return_value = True
        mock_spy_hist.return_value = pd.DataFrame({"Close": [500.0]})
        mock_macro.return_value = {"vix": 15.0}
        mock_quote.return_value = {
            "c": 120.0,
            "dp": 1.5,
            "d": 1.8,
            "o": 119.0,
            "h": 121.0,
            "l": 118.0,
            "pc": 118.2,
        }

        mock_analyze.return_value = {
            "symbol": symbol,
            "price": case["analyze_price"],
            "hv_rank": 40.0,
        }
        mock_skew.return_value = case["skew"]
        mock_skew_p.return_value = 85.0
        mock_mp.return_value = case["max_pain"]
        mock_pcr.return_value = {"pcr": 0.8, "state": "正常"}
        mock_uoa.return_value = ([], [])

        mock_iv_metrics = MagicMock()
        mock_iv_metrics.iv_rank = case["iv_rank"]
        mock_iv_metrics.iv_percentile = case["iv_percentile"]
        mock_iv_metrics.current_iv = case["current_iv"]
        mock_iv_metrics.expected_move_weekly = "--"
        mock_iv_metrics.iv_status = "Normal"
        mock_iv_metrics.is_premarket = False
        mock_iv.return_value = mock_iv_metrics

        mock_em.return_value = case["em_return"]

        mock_hist.return_value = pd.DataFrame({"Close": [100.0, 105.0]})
        mock_reddit.return_value = "看多情緒高漲"
        mock_ddp.return_value = {"is_ddp": case["is_ddp"]}
        mock_poly.return_value = []

        mock_ctx = MagicMock()
        mock_ctx.capital = 100000.0
        mock_user_ctx.return_value = mock_ctx

        await cog.symbol_hub.callback(cog, mock_interaction, symbol=symbol)  # type: ignore

        assert mock_interaction.followup.send.called
        _, kwargs = mock_interaction.followup.send.call_args
        embed = kwargs["embed"]
        assert f"標的分析中心: {symbol}" in embed.title


@pytest.mark.asyncio
async def test_portfolio_hub_command(mock_interaction: Any, mock_bot: Any):  # type: ignore
    cog = UnifiedTerminalCog(mock_bot)

    with (
        patch(
            "services.trading_service.TradingService.get_portfolio_pnl",
            new_callable=AsyncMock,
        ) as mock_pnl,
        patch(
            "services.market_data_service.get_macro_environment", new_callable=AsyncMock
        ) as mock_macro,
        patch("database.get_full_user_context") as mock_user_ctx,
    ):
        mock_pnl.return_value = {"trades": [], "total_unrealized_pnl": 0.0}
        mock_macro.return_value = {"vix": 18.0}

        mock_ctx = MagicMock()
        mock_ctx.capital = 112511.0
        mock_ctx.total_theta = 50.0
        mock_ctx.monthly_expense = 1500.0
        mock_ctx.cash_reserve = 5000.0
        mock_ctx.is_professional_mode = False  # Spectator Mode
        mock_ctx.total_weighted_delta = 10.0
        mock_ctx.total_vanna = 2.0

        mock_user_ctx.return_value = mock_ctx

        await cog.portfolio_hub.callback(cog, mock_interaction)  # type: ignore

        mock_interaction.followup.send.assert_called_once()
        _, kwargs = mock_interaction.followup.send.call_args
        assert "view" in kwargs
        assert isinstance(kwargs["view"], PortfolioHubView)
        embed = kwargs["embed"]
        assert "Nexus 交易員戰略看板" in embed.title
        # Verify content reflects Spectator Mode
        assert "觀戰模式" in embed.fields[0].value


@pytest.mark.asyncio
@pytest.mark.slow
async def test_pulse_hub_command(mock_interaction: Any, mock_bot: Any):  # type: ignore
    cog = UnifiedTerminalCog(mock_bot)

    with patch(
        "services.calendar_service.calendar_service.get_portfolio_events",
        new_callable=AsyncMock,
    ) as mock_events:
        mock_events.return_value = []

        await cog.pulse_hub.callback(cog, mock_interaction)  # type: ignore

        mock_interaction.followup.send.assert_called_once()
        _, kwargs = mock_interaction.followup.send.call_args
        assert "view" in kwargs
        assert isinstance(kwargs["view"], PulseHubView)
        # 逾時停用按鈕需要可編輯原始回應的互動
        assert kwargs["view"].last_interaction is mock_interaction


@pytest.mark.asyncio
async def test_pulse_hub_command_error_sends_followup(  # type: ignore
    mock_interaction: Any, mock_bot: Any
):
    """資料層拋例外時不得讓使用者卡在「思考中」，且不外洩原始例外字串。"""
    cog = UnifiedTerminalCog(mock_bot)

    with patch(
        "cogs.unified_terminal.cog.get_macro_overview_data",
        new_callable=AsyncMock,
        side_effect=RuntimeError("secret db path /app/data"),
    ):
        await cog.pulse_hub.callback(cog, mock_interaction)  # type: ignore

    mock_interaction.response.defer.assert_awaited_once()
    mock_interaction.followup.send.assert_awaited_once()
    _, kwargs = mock_interaction.followup.send.call_args
    assert kwargs["ephemeral"] is True
    assert "view" not in kwargs
    description = kwargs["embed"].description or ""
    assert "secret db path" not in description
    assert "市場情報中心" in description


@pytest.mark.asyncio
async def test_pulse_hub_command_embed_error_sends_followup(  # type: ignore
    mock_interaction: Any, mock_bot: Any
):
    """Embed 建構失敗同樣須回覆錯誤訊息。"""
    cog = UnifiedTerminalCog(mock_bot)

    with (
        patch(
            "cogs.unified_terminal.cog.get_macro_overview_data",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "cogs.unified_terminal.cog.build_market_macro_overview_embed",
            side_effect=ValueError("bad"),
        ),
    ):
        await cog.pulse_hub.callback(cog, mock_interaction)  # type: ignore

    mock_interaction.followup.send.assert_awaited_once()
    _, kwargs = mock_interaction.followup.send.call_args
    assert kwargs["embed"].title.startswith("❌")


@pytest.mark.asyncio
async def test_macro_overview_stale_cache_expires_under_memory_pressure() -> None:
    """記憶體降級時僅回退使用 TTL 內的快取；逾期須重新計算，不得無限期提供舊數據。"""
    from cogs.unified_terminal import utils as ut

    cache: dict[str, Any] = {}
    fresh_payload = {"spx": 5000.0, "is_degraded": False, "served_stale_cache": False}

    with (
        patch.object(ut, "_macro_overview_cache", cache),
        patch.object(ut, "is_memory_safe", return_value=False),
        patch.object(ut, "_cache_clock") as mock_clock,
        patch("database.get_kv_cache", return_value=None),
        patch(
            "services.market_data_service.get_quote",
            new_callable=AsyncMock,
            side_effect=Exception("offline"),
        ),
        patch("database.calendar_cache.get_macro_events_between", return_value=[]),
        patch(
            "services.calendar_service.calendar_service.prefetch_monthly_macro_cache",
            new_callable=AsyncMock,
        ),
        patch(
            "market_analysis.index_microstructure.fetch_core_macro_metrics",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "market_analysis.index_microstructure.fetch_gex_metrics",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=AsyncMock,
            side_effect=Exception("offline"),
        ),
    ):
        # TTL 內：直接回退快取並附上年齡
        cache["overview_7"] = (1000.0, fresh_payload)
        mock_clock.return_value = 1000.0 + 120.0
        hit = await ut.get_macro_overview_data(7)
        assert hit["served_stale_cache"] is True
        assert hit["spx"] == 5000.0
        assert hit["stale_cache_age_seconds"] == pytest.approx(120.0)
        # 回傳副本，不得污染快取本體
        assert "stale_cache_age_seconds" not in fresh_payload

        # 逾期：重新計算（離線 → spx 為 None），且以新時間戳回寫快取
        mock_clock.return_value = 1000.0 + ut._MACRO_OVERVIEW_STALE_MAX_AGE_SECONDS + 1
        miss = await ut.get_macro_overview_data(7)
        assert miss["served_stale_cache"] is False
        assert miss["is_degraded"] is True
        assert miss["spx"] is None
        cached_at, _ = cache["overview_7"]
        assert cached_at == mock_clock.return_value


def test_macro_overview_embed_discloses_stale_cache_age() -> None:
    """回退快取時 Embed 須揭露資料年齡（Embed 時間戳是當下，否則會被誤讀為即時）。"""
    from cogs.embed_builders.market_embeds import build_market_macro_overview_embed

    embed = build_market_macro_overview_embed(
        {
            "is_degraded": True,
            "served_stale_cache": True,
            "stale_cache_age_seconds": 600.0,
        }
    )
    notice = next(f.value for f in embed.fields if f.name == "⚠️ 系統降級警告")
    assert "約 10 分鐘前之快取" in str(notice)


@pytest.mark.asyncio
async def test_symbol_hub_command_no_params(mock_interaction: Any, mock_bot: Any):  # type: ignore
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
        )

    assert mock_interaction.followup.send.called
    _, kwargs = mock_interaction.followup.send.call_args
    assert "view" in kwargs
    from cogs.unified_terminal.radar_view import UnifiedRadarView

    assert isinstance(kwargs["view"], UnifiedRadarView)


@pytest.mark.asyncio
async def test_symbol_hub_batch_scan_holdings(mock_interaction: Any, mock_bot: Any):  # type: ignore
    cog = UnifiedTerminalCog(mock_bot)

    # 模擬 scan_type Choice
    mock_choice = MagicMock()
    mock_choice.value = "HOLDINGS"

    # 模擬持倉
    mock_holding = MagicMock()
    mock_holding.symbol = "AAPL"

    with (
        patch("services.asset_manager.AssetManager.get_assets") as mock_get_assets,
        patch(
            "services.market_data_service.get_quote", new_callable=AsyncMock
        ) as mock_quote,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
        ) as mock_iv,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new_callable=AsyncMock,
        ) as mock_skew,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
            new_callable=AsyncMock,
        ) as mock_mp,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_indicator_percentile"
        ) as mock_skew_p,
    ):
        mock_get_assets.return_value = [mock_holding]
        mock_quote.return_value = {"c": 150.0, "dp": 1.2}

        mock_iv_metrics = MagicMock()
        mock_iv_metrics.iv_rank = 30.0
        mock_iv_metrics.expected_move_weekly = 4.5
        mock_iv.return_value = mock_iv_metrics

        mock_skew.return_value = {"skew": 1.1}
        mock_mp.return_value = {"max_pain": 145.0, "distance_pct": 3.4}
        mock_skew_p.return_value = 75.0

        await cog.symbol_hub.callback(  # type: ignore
            cog,  # type: ignore
            mock_interaction,
            symbol=None,
            scan_type=mock_choice,
        )

        assert mock_interaction.followup.send.called
        _, kwargs = mock_interaction.followup.send.call_args
        assert "view" in kwargs
        assert isinstance(kwargs["view"], BatchScanPaginatedView)
        embed = kwargs["embed"]
        assert "現貨持倉批次量化雷達 (Holdings)" in embed.title


@pytest.mark.asyncio
async def test_symbol_hub_batch_scan_all(mock_interaction: Any, mock_bot: Any):  # type: ignore
    cog = UnifiedTerminalCog(mock_bot)

    # 模擬 scan_type Choice
    mock_choice = MagicMock()
    mock_choice.value = "ALL"

    # 模擬持倉、掛單、期權
    mock_holding = MagicMock()
    mock_holding.symbol = "AAPL"

    mock_order = {"symbol": "TSLA"}
    mock_portfolio = (
        1,
        "NVDA",
        "call",
        120.0,
        "2026-06-19",
        2.5,
        1,
        118.0,
        0.5,
        -0.05,
        0.01,
        "SPECULATIVE",
    )

    with (
        patch("services.asset_manager.AssetManager.get_assets") as mock_get_assets,
        patch("database.orders.get_user_active_orders") as mock_get_orders,
        patch("database.portfolio.get_user_portfolio") as mock_get_portfolio,
        patch(
            "services.market_data_service.get_quote", new_callable=AsyncMock
        ) as mock_quote,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
        ) as mock_iv,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new_callable=AsyncMock,
        ) as mock_skew,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
            new_callable=AsyncMock,
        ) as mock_mp,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_indicator_percentile"
        ) as mock_skew_p,
    ):
        mock_get_assets.return_value = [mock_holding]
        mock_get_orders.return_value = [mock_order]
        mock_get_portfolio.return_value = [mock_portfolio]

        mock_quote.return_value = {"c": 150.0, "dp": 1.2}

        mock_iv_metrics = MagicMock()
        mock_iv_metrics.iv_rank = 30.0
        mock_iv_metrics.expected_move_weekly = 4.5
        mock_iv.return_value = mock_iv_metrics

        mock_skew.return_value = {"skew": 1.1}
        mock_mp.return_value = {"max_pain": 145.0, "distance_pct": 3.4}
        mock_skew_p.return_value = 75.0

        await cog.symbol_hub.callback(  # type: ignore
            cog,  # type: ignore
            mock_interaction,
            symbol=None,
            scan_type=mock_choice,
        )

        assert mock_interaction.followup.send.called
        _, kwargs = mock_interaction.followup.send.call_args
        assert "view" in kwargs
        assert isinstance(kwargs["view"], BatchScanPaginatedView)
        embed = kwargs["embed"]
        assert "核心 AI 暨持倉批次量化雷達 (ALL)" in embed.title


@pytest.mark.asyncio
async def test_symbol_hub_batch_scan_watchlist(mock_interaction: Any, mock_bot: Any):  # type: ignore
    cog = UnifiedTerminalCog(mock_bot)

    # 模擬 scan_type Choice
    mock_choice = MagicMock()
    mock_choice.value = "WATCHLIST"

    with (
        patch("database.get_user_watchlist") as mock_get_watchlist,
        patch(
            "services.market_data_service.get_quote", new_callable=AsyncMock
        ) as mock_quote,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
        ) as mock_iv,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new_callable=AsyncMock,
        ) as mock_skew,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
            new_callable=AsyncMock,
        ) as mock_mp,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_indicator_percentile"
        ) as mock_skew_p,
    ):
        mock_get_watchlist.return_value = [("AAPL", 1)]
        mock_quote.return_value = {"c": 150.0, "dp": 1.2}

        mock_iv_metrics = MagicMock()
        mock_iv_metrics.iv_rank = 30.0
        mock_iv_metrics.expected_move_weekly = 4.5
        mock_iv.return_value = mock_iv_metrics

        mock_skew.return_value = {"skew": 1.1}
        mock_mp.return_value = {"max_pain": 145.0, "distance_pct": 3.4}
        mock_skew_p.return_value = 75.0

        await cog.symbol_hub.callback(  # type: ignore
            cog,  # type: ignore
            mock_interaction,
            symbol=None,
            scan_type=mock_choice,
        )

        assert mock_interaction.followup.send.called
        _, kwargs = mock_interaction.followup.send.call_args
        assert "view" in kwargs
        assert isinstance(kwargs["view"], BatchScanPaginatedView)
        embed = kwargs["embed"]
        assert "自選標的批次量化雷達 (Watchlist)" in embed.title


@pytest.mark.asyncio
async def test_symbol_hub_batch_scan_watchlist_many_pages_single_followup(
    mock_interaction: Any, mock_bot: Any
) -> None:
    """大量自選股（產生超過 5-6 頁）應只送出一次 followup，並將所有分頁
    封裝進單一則訊息的 BatchScanPaginatedView，而不是逐頁分別呼叫
    interaction.followup.send() 撞上 Discord 40094 (maximum number of
    follow up messages)。"""
    cog = UnifiedTerminalCog(mock_bot)

    mock_choice = MagicMock()
    mock_choice.value = "WATCHLIST"

    # 55 個標的 -> chunk_size=10 -> 6 頁，遠超過舊版曾經在此撞牆的頁數 (7)
    watchlist_symbols = [(f"SYM{i}", i) for i in range(55)]

    with (
        patch("database.get_user_watchlist") as mock_get_watchlist,
        patch(
            "services.market_data_service.get_quote", new_callable=AsyncMock
        ) as mock_quote,
        patch(
            # _fetch_sym_radar_data_fast_raw 在 UOA/Squeeze 快取未命中時會 fallback
            # 呼叫真實的 detect_uoa / get_history_df 網路請求；55 個標的在測試沙箱
            # 無網路環境下會逐一等待逾時，需 mock 掉以避免測試掛起。
            "market_analysis.sentiment_engine.SentimentEngine.detect_uoa",
            new_callable=AsyncMock,
        ) as mock_uoa,
        patch(
            "services.market_data_service.get_history_df", new_callable=AsyncMock
        ) as mock_hist_df,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
        ) as mock_iv,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new_callable=AsyncMock,
        ) as mock_skew,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
            new_callable=AsyncMock,
        ) as mock_mp,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_indicator_percentile"
        ) as mock_skew_p,
    ):
        mock_get_watchlist.return_value = watchlist_symbols
        mock_quote.return_value = {"c": 150.0, "dp": 1.2}
        mock_uoa.return_value = []
        mock_hist_df.return_value = None

        mock_iv_metrics = MagicMock()
        mock_iv_metrics.iv_rank = 30.0
        mock_iv_metrics.expected_move_weekly = 4.5
        mock_iv.return_value = mock_iv_metrics

        mock_skew.return_value = {"skew": 1.1}
        mock_mp.return_value = {"max_pain": 145.0, "distance_pct": 3.4}
        mock_skew_p.return_value = 75.0

        await cog.symbol_hub.callback(  # type: ignore
            cog,  # type: ignore
            mock_interaction,
            symbol=None,
            scan_type=mock_choice,
        )

    total_pages = 6

    # 不管有幾頁，都只呼叫一次 followup.send，翻頁改由使用者點擊 View 上的
    # ◀/▶ 按鈕就地編輯同一則訊息，因此完全不會撞上 followup 訊息數量上限。
    assert mock_interaction.followup.send.call_count == 1

    _, kwargs = mock_interaction.followup.send.call_args
    assert isinstance(kwargs["view"], BatchScanPaginatedView)
    assert len(kwargs["view"].embeds) == total_pages
    assert kwargs["embed"] is kwargs["view"].embeds[0]


@pytest.mark.asyncio
async def test_batch_scan_warning_button_callback(mock_interaction: Any, mock_bot: Any):  # type: ignore
    from cogs.unified_terminal import BatchScanWarningButton

    cog = UnifiedTerminalCog(mock_bot)
    cog._run_single_symbol_hub = AsyncMock()

    # Case 1: No message or embeds
    btn = BatchScanWarningButton(cog, mock_bot)
    mock_interaction.message = None
    await btn.callback(mock_interaction)

    assert mock_interaction.response.send_message.called
    _, kwargs = mock_interaction.response.send_message.call_args
    assert "無法讀取當前訊息" in kwargs["embed"].description

    # Case 2: Embed has warnings
    mock_interaction.response.send_message.reset_mock()
    mock_interaction.followup.send.reset_mock()
    mock_interaction.response.edit_message.reset_mock()
    mock_interaction.edit_original_response.reset_mock()

    mock_msg = MagicMock()
    mock_embed = MagicMock()
    mock_field = MagicMock()
    mock_field.name = "💡 即時聯動警示 (Real-time Insights)"
    mock_field.value = "```ansi\n• 🚀 AAPL: 價格接近下緣\n• ⚠️ TSLA: 籌碼面異常\n```"
    mock_embed.fields = [mock_field]
    mock_embed.description = "```ansi\n```"
    mock_msg.embeds = [mock_embed]
    mock_interaction.message = mock_msg

    # Mock the button's view
    mock_view = MagicMock()
    mock_view.children = [btn]
    btn._view = mock_view

    await btn.callback(mock_interaction)

    # Verify disabled states were set and original message was updated
    assert btn.disabled is False  # Restored in finally block
    assert mock_interaction.response.edit_message.called
    assert mock_interaction.edit_original_response.called

    mock_interaction.followup.send.assert_any_call(
        "🔄 正在批次分析以下 2 個警示標的: AAPL, TSLA...", ephemeral=True
    )
    assert cog._run_single_symbol_hub.call_count == 2
    cog._run_single_symbol_hub.assert_any_call(
        mock_interaction, "AAPL", mock_interaction.user.id, embeds_accumulator=ANY
    )
    cog._run_single_symbol_hub.assert_any_call(
        mock_interaction, "TSLA", mock_interaction.user.id, embeds_accumulator=ANY
    )

    # Case 3: Embed has no warnings
    cog._run_single_symbol_hub.reset_mock()
    mock_interaction.followup.send.reset_mock()
    mock_field.value = (
        "```ansi\n• ✨ 所有標的當前價格與 Max Pain 及波動邊界皆無極端異常偏離。\n```"
    )
    await btn.callback(mock_interaction)
    assert mock_interaction.followup.send.called
    _, kwargs = mock_interaction.followup.send.call_args
    assert "當前訊息的「即時聯動警示」中沒有列出任何標的" in kwargs["embed"].description
    assert cog._run_single_symbol_hub.call_count == 0


@pytest.mark.asyncio
async def test_batch_scan_warning_button_chunking(mock_interaction: Any, mock_bot: Any):  # type: ignore
    from cogs.unified_terminal import BatchScanWarningButton
    import discord

    cog = UnifiedTerminalCog(mock_bot)

    async def mock_run_hub(
        interaction: Any, symbol: Any, user_id: Any, embeds_accumulator: Any = None
    ) -> None:
        if embeds_accumulator is not None:
            embeds_accumulator.append(discord.Embed(title=f"Mock Embed for {symbol}"))

    cog._run_single_symbol_hub = mock_run_hub

    btn = BatchScanWarningButton(cog, mock_bot)
    mock_msg = MagicMock()
    mock_embed = MagicMock()
    mock_field = MagicMock()
    mock_field.name = "💡 即時聯動警示 (Real-time Insights)"
    mock_field.value = (
        "```ansi\n" + "\n".join([f"• 🚀 SYM{i}: Test" for i in range(12)]) + "\n```"
    )
    mock_embed.fields = [mock_field]
    mock_embed.description = "```ansi\n```"
    mock_msg.embeds = [mock_embed]
    mock_interaction.message = mock_msg

    mock_view = MagicMock()
    mock_view.children = [btn]
    btn._view = mock_view

    mock_interaction.followup.send.reset_mock()
    await btn.callback(mock_interaction)

    # 12 embeds total. Under our dynamic chunking logic with default max_count=10:
    # They should be split into 2 chunks of sizes: 10, 2.
    # So followup.send should be called 1 (initial progress message) + 2 (chunks) = 3 times.
    assert mock_interaction.followup.send.call_count == 3

    calls = mock_interaction.followup.send.call_args_list
    assert "正在批次分析以下 12 個警示標的" in calls[0][0][0]

    assert "embeds" in calls[1][1]
    assert len(calls[1][1]["embeds"]) == 10

    assert "embeds" in calls[2][1]
    assert len(calls[2][1]["embeds"]) == 2


@pytest.mark.asyncio
async def test_fetch_single_symbol_data_raw_forces_live_option_data(  # type: ignore
    mock_bot: Any,
):
    """`_fetch_single_symbol_data_raw` 是 `/x symbol:`、批次掃描「⚡ 批次分析
    警示標的」按鈕與 SymbolHubView 分頁切換共用的唯一深度分析資料來源，呼叫端
    一律已透過 Discord defer 取得最長 15 分鐘的 followup 視窗。期權相關的
    Skew/PCR/UOA/Max Pain/IV/GEX 抓取必須明確帶上 force_live/force_refresh=True，
    保證略過 Edge Snapshot 與各自的記憶體/SQLite 快取層取得即時資料。"""
    from datetime import date, datetime, timedelta

    cog = UnifiedTerminalCog(mock_bot)
    # 動態計算一個必定落在「30 天內到期日」篩選窗口內的到期日，避免寫死日期
    # 隨測試執行的實際日期經過而漂移出窗口導致測試變得不穩定。
    target_expiry = (date.today() + timedelta(days=15)).strftime("%Y-%m-%d")

    with (
        patch(
            "services.market_data_service.get_spy_history_df", new_callable=AsyncMock
        ) as mock_spy_hist,
        patch(
            "services.market_data_service.get_macro_environment", new_callable=AsyncMock
        ) as mock_macro,
        patch(
            "services.market_data_service.get_quote", new_callable=AsyncMock
        ) as mock_quote,
        patch(
            "services.market_data_service.get_history_df", new_callable=AsyncMock
        ) as mock_hist,
        patch(
            "services.market_data_service.get_all_option_expiries",
            new_callable=AsyncMock,
        ) as mock_expiries,
        patch(
            "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
            new_callable=AsyncMock,
        ) as mock_gex,
        patch("market_analysis.volume_profile.calculate_volume_profile") as mock_vp,
        patch(
            "services.reddit_service.get_reddit_details", new_callable=AsyncMock
        ) as mock_reddit,
        patch(
            "market_analysis.ddp_inspector.DDPInspector.inspect_symbol",
            new_callable=AsyncMock,
        ) as mock_ddp,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
            new_callable=AsyncMock,
        ) as mock_skew,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
            new_callable=AsyncMock,
        ) as mock_pcr,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.detect_uoa_with_physical_caps",
            new_callable=AsyncMock,
        ) as mock_uoa,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.calculate_max_pain",
            new_callable=AsyncMock,
        ) as mock_mp,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
            new_callable=AsyncMock,
        ) as mock_iv,
        patch(
            "market_analysis.sentiment_engine.SentimentEngine.get_unified_max_pain",
            new_callable=AsyncMock,
        ) as mock_unified_mp,
    ):
        mock_spy_hist.return_value = pd.DataFrame({"Close": [500.0]})
        mock_macro.return_value = {"vix": 15.0}
        mock_quote.return_value = {"c": 120.0}
        mock_hist.return_value = pd.DataFrame({"Close": [100.0, 105.0]})
        mock_expiries.return_value = [target_expiry]
        mock_gex.return_value = {"put_wall": 100.0}
        mock_vp.return_value = {"hvn": 0.0, "lvn": 0.0}
        mock_reddit.return_value = ("看多情緒高漲", [])
        mock_ddp.return_value = {"is_ddp": False}
        mock_skew.return_value = {"skew": 1.0}
        mock_pcr.return_value = {"pcr": 0.9}
        mock_uoa.return_value = ([], [])
        mock_mp.return_value = {"max_pain": 115.0}
        mock_unified_mp.return_value = {"max_pain": 115.0, "distance_pct": 0.0}
        mock_iv_metrics = MagicMock()
        mock_iv_metrics.iv_rank = 35.0
        mock_iv.return_value = mock_iv_metrics

        raw = await cog._fetch_single_symbol_data_raw("NVDA")

        # 擠壓／動能以最後一根日線計算，不得吃 6 小時日線快取（盤中會凍結在
        # 盤前預熱寫入的前一日收盤）；抓取時刻一併回傳供面板標示資料時間。
        mock_hist.assert_any_await(
            "NVDA", period="1y", interval="1d", force_refresh=True
        )
        assert isinstance(raw["df_hist_fetched_at"], datetime)
        mock_gex.assert_awaited_once_with("NVDA", force_live=True)
        mock_skew.assert_awaited_once_with("NVDA", force_live=True)
        mock_pcr.assert_awaited_once_with("NVDA", force_live=True)
        mock_uoa.assert_awaited_once_with("NVDA", force_live=True)
        mock_mp.assert_awaited_once_with("NVDA", _retry=True)
        mock_iv.assert_awaited_once_with("NVDA", force_refresh=True)
        mock_unified_mp.assert_awaited_once_with(
            "NVDA", expiry=target_expiry, force_refresh=True
        )
