"""/x 標的分析中心修正的回歸測試。

涵蓋：代號正規化、symbol/scan_type 二擇一、tag 正規化與非 WATCHLIST 忽略、
例外細節不外洩、一鍵對沖 IVR 未知與負 Gamma 禁售、標籤下拉選單 25 上限、
報價失敗時 Max Pain 偏離的假 -100%、雷達洞察 IVR 單位。
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import discord
import pytest
from discord.app_commands import Choice

from cogs.embed_builders.portfolio_embeds import create_tactical_hedge_embed
from cogs.unified_terminal.cog import UnifiedTerminalCog
from cogs.unified_terminal.radar_data import _KvSnapshot
from cogs.unified_terminal.radar_view import UnifiedRadarView
from cogs.unified_terminal.symbol_view import (
    SymbolHubView,
    _HEDGE_BUY_PROTECTION,
    _HEDGE_SELL_PUT_SPREAD,
    _is_negative_gamma_zone,
    _recommend_hedge_strategy,
)


@pytest.fixture
def interaction() -> Any:
    inter = MagicMock()
    inter.user.id = 4242
    inter.response = MagicMock()
    inter.response.defer = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.response.is_done.return_value = True
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    inter.edit_original_response = AsyncMock()
    return inter


@pytest.fixture
def cog() -> Any:
    bot = MagicMock(spec=[])  # 無 memory_manager，跳過預熱 hook
    return UnifiedTerminalCog(bot)


def _sent_embed(interaction: Any) -> discord.Embed:
    _, kwargs = interaction.followup.send.call_args
    embed = kwargs["embed"]
    assert isinstance(embed, discord.Embed)
    return embed


# ---------------------------------------------------------------------------
# 1. 代號正規化
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,expected",
    [
        (" nvda", "NVDA"),
        ("$NVDA", "NVDA"),
        ("nvda  ", "NVDA"),
        ("brk.b", "BRK.B"),
    ],
)
async def test_run_single_symbol_hub_normalizes_symbol(
    cog: Any, interaction: Any, raw: str, expected: str
) -> None:
    cog._process_symbol_hub_data = AsyncMock(return_value={"symbol": expected})
    with patch(
        "services.market_data_service.validate_symbol",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_validate, patch(
        "services.single_flight.SingleFlightManager.run",
        new_callable=AsyncMock,
        return_value={},
    ) as mock_run, patch(
        "cogs.unified_terminal.symbol_deep_dive.create_tactical_symbol_embed",
        return_value=discord.Embed(title="ok"),
    ):
        await cog.symbol_hub.callback(cog, interaction, symbol=raw)

    mock_validate.assert_awaited_once_with(expected)
    key, _fn, sym_arg = mock_run.call_args.args
    assert key == f"single_hub_{expected}"
    assert sym_arg == expected
    assert cog._process_symbol_hub_data.call_args.args[0] == expected
    _, kwargs = interaction.followup.send.call_args
    assert isinstance(kwargs["view"], SymbolHubView)
    assert kwargs["view"].symbol == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["$", "   ", "$$"])
async def test_run_single_symbol_hub_rejects_empty_symbol(
    cog: Any, interaction: Any, raw: str
) -> None:
    with patch(
        "services.market_data_service.validate_symbol", new_callable=AsyncMock
    ) as mock_validate:
        await cog.symbol_hub.callback(cog, interaction, symbol=raw)

    mock_validate.assert_not_called()
    embed = _sent_embed(interaction)
    assert embed.title == "❌ 輸入錯誤"
    assert "請輸入有效的股票代號" in (embed.description or "")


# ---------------------------------------------------------------------------
# 2. symbol 與 scan_type 二擇一；tag 正規化
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_symbol_and_scan_type_together_is_rejected(
    cog: Any, interaction: Any
) -> None:
    cog._run_single_symbol_hub = AsyncMock()
    cog.execute_unified_scan = AsyncMock()

    await cog.symbol_hub.callback(
        cog,
        interaction,
        symbol="NVDA",
        scan_type=Choice(name="Watchlist", value="WATCHLIST"),
    )

    cog._run_single_symbol_hub.assert_not_called()
    cog.execute_unified_scan.assert_not_called()
    embed = _sent_embed(interaction)
    assert embed.title == "❌ 輸入錯誤"
    assert "擇一" in (embed.description or "")


@pytest.mark.asyncio
async def test_watchlist_tag_is_normalized(cog: Any, interaction: Any) -> None:
    cog.execute_unified_scan = AsyncMock()
    await cog.symbol_hub.callback(
        cog,
        interaction,
        scan_type=Choice(name="Watchlist", value="WATCHLIST"),
        tag="  tech ",
    )
    state = cog.execute_unified_scan.call_args.args[1]
    assert state["selected_tag"] == "TECH"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["ALL", "HOLDINGS", "ORDERS", "OPTIONS"])
async def test_tag_ignored_for_non_watchlist_scan(
    cog: Any, interaction: Any, scope: str
) -> None:
    cog.execute_unified_scan = AsyncMock()
    await cog.symbol_hub.callback(
        cog, interaction, scan_type=Choice(name=scope, value=scope), tag="TECH"
    )
    state = cog.execute_unified_scan.call_args.args[1]
    assert state["scope"] == scope
    assert state["selected_tag"] is None


# ---------------------------------------------------------------------------
# 3. 例外細節不外洩給使用者
# ---------------------------------------------------------------------------

_SECRET = "sqlite3.OperationalError /app/data/nexus.db"


@pytest.mark.asyncio
async def test_outer_error_does_not_leak_exception(cog: Any, interaction: Any) -> None:
    cog.execute_unified_scan = AsyncMock(side_effect=RuntimeError(_SECRET))
    await cog.symbol_hub.callback(
        cog, interaction, scan_type=Choice(name="ALL", value="ALL")
    )
    desc = _sent_embed(interaction).description or ""
    assert "未預期錯誤" in desc
    assert _SECRET not in desc


@pytest.mark.asyncio
async def test_single_symbol_error_does_not_leak_exception(
    cog: Any, interaction: Any
) -> None:
    with patch(
        "services.market_data_service.validate_symbol",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "services.single_flight.SingleFlightManager.run",
        new_callable=AsyncMock,
        side_effect=RuntimeError(_SECRET),
    ):
        await cog.symbol_hub.callback(cog, interaction, symbol="NVDA")

    desc = _sent_embed(interaction).description or ""
    assert "NVDA" in desc
    assert _SECRET not in desc


@pytest.mark.asyncio
async def test_batch_scan_error_does_not_leak_exception(
    cog: Any, interaction: Any
) -> None:
    with patch(
        "services.asset_manager.AssetManager.get_assets",
        side_effect=RuntimeError(_SECRET),
    ):
        await cog.execute_unified_scan(
            interaction, {"scope": "HOLDINGS", "quant_filters": [], "params": {}}, 1
        )
    desc = _sent_embed(interaction).description or ""
    assert "執行批次掃描時發生錯誤" in desc
    assert _SECRET not in desc


@pytest.mark.asyncio
async def test_symbol_view_error_does_not_leak_exception(interaction: Any) -> None:
    view = SymbolHubView(symbol="NVDA", user_id=4242, bot=MagicMock())
    view.base_data = {"symbol": "NVDA"}
    with patch(
        "cogs.unified_terminal.symbol_view.create_tactical_symbol_embed",
        side_effect=RuntimeError(_SECRET),
    ):
        await view.btn_home.callback(interaction)
    desc = _sent_embed(interaction).description or ""
    assert "恢復主頁失敗" in desc
    assert _SECRET not in desc


# ---------------------------------------------------------------------------
# 4. 一鍵對沖：IVR 未知不捏造、負 Gamma 賣方禁售
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ivr,neg_gamma,expected",
    [
        (70.0, False, _HEDGE_SELL_PUT_SPREAD),
        (70.0, True, _HEDGE_BUY_PROTECTION),
        (50.0, False, _HEDGE_BUY_PROTECTION),
        (5.0, False, _HEDGE_BUY_PROTECTION),
        (None, False, _HEDGE_BUY_PROTECTION),
    ],
)
def test_recommend_hedge_strategy(
    ivr: float | None, neg_gamma: bool, expected: str
) -> None:
    assert _recommend_hedge_strategy(ivr, neg_gamma) == expected


@pytest.mark.parametrize(
    "base_data,expected",
    [
        ({"gex_profile_data": {"net_gex": -1_000_000.0, "put_wall": 90.0}}, True),
        (
            {
                "gex_profile_data": {"net_gex": 5_000_000.0, "put_wall": 100.0},
                "quote": {"c": 95.0},
            },
            True,
        ),
        (
            {
                "gex_profile_data": {"net_gex": 5_000_000.0, "put_wall": 100.0},
                "quote": {},
                "price": 95.0,
            },
            True,
        ),
        (
            {
                "gex_profile_data": {"net_gex": 5_000_000.0, "put_wall": 100.0},
                "quote": {"c": 105.0},
            },
            False,
        ),
        ({"gex_profile_data": None}, False),
        ({}, False),
    ],
)
def test_is_negative_gamma_zone(base_data: dict, expected: bool) -> None:
    assert _is_negative_gamma_zone(base_data) is expected


@pytest.mark.asyncio
async def test_btn_hedge_unknown_ivr_passes_none(interaction: Any) -> None:
    view = SymbolHubView(symbol="NVDA", user_id=4242, bot=MagicMock())
    view.base_data = {"symbol": "NVDA", "iv_rank": None}
    with patch(
        "cogs.unified_terminal.symbol_view.create_tactical_hedge_embed"
    ) as mock_builder:
        mock_builder.return_value = MagicMock(spec=discord.Embed)
        await view.btn_hedge.callback(interaction)
    mock_builder.assert_called_once_with("NVDA", None, _HEDGE_BUY_PROTECTION)


@pytest.mark.asyncio
async def test_btn_hedge_negative_gamma_blocks_credit_spread(interaction: Any) -> None:
    view = SymbolHubView(symbol="NVDA", user_id=4242, bot=MagicMock())
    view.base_data = {
        "symbol": "NVDA",
        "iv_rank": 85.0,
        "gex_profile_data": {"net_gex": -2_000_000.0, "put_wall": 100.0},
        "quote": {"c": 110.0},
    }
    with patch(
        "cogs.unified_terminal.symbol_view.create_tactical_hedge_embed"
    ) as mock_builder:
        mock_builder.return_value = MagicMock(spec=discord.Embed)
        await view.btn_hedge.callback(interaction)
    mock_builder.assert_called_once_with("NVDA", 85.0, _HEDGE_BUY_PROTECTION)


def test_tactical_hedge_embed_unknown_ivr() -> None:
    embed = create_tactical_hedge_embed("NVDA", None, _HEDGE_BUY_PROTECTION)
    value = embed.fields[0].value or ""
    assert "--% (資料不足)" in value
    assert "50.0%" not in value

    embed_known = create_tactical_hedge_embed("NVDA", 62.34, _HEDGE_SELL_PUT_SPREAD)
    assert "62.3%" in (embed_known.fields[0].value or "")


# ---------------------------------------------------------------------------
# 5. 標籤下拉選單不得超過 Discord 25 選項上限
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tag_select_respects_discord_option_limit(interaction: Any) -> None:
    view = UnifiedRadarView(MagicMock(), 4242)
    many_tags = [f"TAG{i:02d}" for i in range(30)]
    with patch.object(
        type(view.scope_select), "values", new_callable=PropertyMock
    ) as mock_values, patch(
        "database.watchlist_tags.get_user_unique_tags", return_value=many_tags
    ):
        mock_values.return_value = ["WATCHLIST"]
        await view.on_scope_change(interaction)

    assert view.tag_select is not None
    options = view.tag_select.options
    assert len(options) == 25
    assert options[0].value == "ALL_TAGS"


# ---------------------------------------------------------------------------
# 6. 報價失敗時不得產生 -100% 的 Max Pain 假偏離
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "quote,expected_dist",
    [
        ({}, 0.0),
        ({"c": 0.0}, 0.0),
        ({"c": 110.0, "volume": 1000}, 10.0),
    ],
)
async def test_fast_radar_max_pain_distance_guard(
    cog: Any, quote: dict, expected_dist: float
) -> None:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    kv = _KvSnapshot(
        {
            "radar_terminal_TESTMP": (
                {"mp_near": 100.0, "uoa": [], "skew": 0.0, "skew_percentile": 50.0},
                None,
            )
        }
    )
    market_cache = {"max_pain": 100.0, "updated_at": now_utc, "is_stale": 0}
    squeeze = {"momentum": 1.0, "is_squeezing": False, "direction": "⚪"}

    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value=quote,
    ), patch(
        "cogs.unified_terminal.radar_data._load_symbol_caches",
        return_value=(kv, market_cache, squeeze),
    ), patch(
        "market_analysis.sentiment.history_storage.get_last_stored_sentiment",
        return_value=None,
    ), patch(
        "market_analysis.sentiment.history_storage.get_last_stored_iv",
        return_value=None,
    ):
        result = await cog._fetch_sym_radar_data_fast_raw("TESTMP")

    assert result["max_pain"]["distance_pct"] == pytest.approx(expected_dist)


# ---------------------------------------------------------------------------
# 7. 雷達洞察 IVR 單位：0~100 百分點一律除以 100
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ivr,expected", [(0.9, 0.009), (1.0, 0.01), (85.0, 0.85)])
def test_radar_embed_passes_fractional_iv_rank_to_insights(
    ivr: float, expected: float
) -> None:
    from cogs.embed_builders.market_embeds import build_radar_scan_embed

    scan_results = [
        {
            "symbol": "AAPL",
            "quote": {"c": 225.0, "dp": 1.2},
            "iv_metrics": {"iv_rank": ivr, "expected_move_weekly": 5.0},
            "max_pain": {"max_pain": 220.0},
            "gex_metrics": {"put_wall": 215.0, "call_wall": 230.0, "net_gex": 5e5},
            "gex_profile_data": {
                "put_wall": 215.0,
                "call_wall": 230.0,
                "net_gex": 5e5,
            },
            "psq_result": {"momentum": 1.0, "direction": "🟢", "is_squeezing": False},
            "uoa": [],
            "skew": 0.0,
            "skew_percentile": 50.0,
        }
    ]
    captured: list[Any] = []

    def _capture(ctx: Any) -> Any:
        captured.append(ctx)
        return (None, None, None)

    with patch(
        "market_analysis.insights_engine.InsightsEngine.generate_cro_insight",
        side_effect=_capture,
    ), patch("database.cache.get_kv_cache", return_value=None):
        build_radar_scan_embed(scan_results, "WATCHLIST", 12345)

    assert captured, "generate_cro_insight 應被呼叫"
    assert captured[0].iv_rank == pytest.approx(expected)
