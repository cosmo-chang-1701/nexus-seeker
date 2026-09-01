from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from market_analysis.dynamic_rollover import DynamicRolloverEngine
from cogs.embed_builders.rollover_embeds import create_dynamic_rollover_embed


@pytest.fixture
def engine() -> DynamicRolloverEngine:
    return DynamicRolloverEngine()


@pytest.fixture(autouse=True)
def _mock_target_reference_live_quote() -> Any:
    """_resolve_target_reference_price (BOXX 參考價格) 的即時報價備援層 mock，
    避免單元測試觸發真實外部網路呼叫，比照 test_dynamic_rollover.py 的既有
    同名 fixture。"""
    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={},
    ):
        yield


_CRITICAL_PATCHES = dict(
    regime="SHORT_GAMMA_CRITICAL",
    vts={"is_valid": True, "vts_ratio": 1.05},
    fear_greed={"fear_greed": 80.0},
    prob=0.75,
)


def _satellite_asset(**overrides: Any) -> dict:
    base = {
        "symbol": "NVDA",
        "asset_class": "SATELLITE",
        "current_value": 5000.0,
        "quantity": 10.0,
        "instrument_type": "SPOT",
        "spot_price": 500.0,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_macro_top_escape_defense_opt_in_gate_off_no_action(
    mock_ctx: MagicMock, engine: DynamicRolloverEngine
) -> None:
    """Gate 1: 使用者未開啟 enable_macro_top_escape_defense -> 即使評分達 CRITICAL
    也必須恆為 no-op，不應觸發任何額外的宏觀數據抓取或減碼動作。"""
    mock_ctx.return_value = MagicMock(enable_macro_top_escape_defense=False)
    result = await engine.evaluate_macro_top_escape_defense(1, [_satellite_asset()])
    assert result == []


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="NORMAL",
)
@patch(
    "services.market_data_service.get_vix_term_structure",
    new_callable=AsyncMock,
    return_value={"is_valid": True, "vts_ratio": 1.05},
)
@patch(
    "market_analysis.index_microstructure.fetch_core_macro_metrics",
    new_callable=AsyncMock,
    return_value={"fear_greed": 80.0},
)
@patch("database.cache.get_kv_cache", return_value=0.50)
@patch("database.orders.get_user_active_orders", return_value=[])
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_macro_top_escape_defense_score_below_critical_no_action(
    mock_ctx: MagicMock,
    mock_orders: MagicMock,
    mock_kv: MagicMock,
    mock_fear_greed: AsyncMock,
    mock_vts: AsyncMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """Gate 2: 已開啟 opt-in，但綜合評分僅到 ELEVATED (未達 CRITICAL 門檻) ->
    不應觸發任何減碼動作。"""
    mock_ctx.return_value = MagicMock(enable_macro_top_escape_defense=True)
    result = await engine.evaluate_macro_top_escape_defense(1, [_satellite_asset()])
    assert result == []


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value=_CRITICAL_PATCHES["regime"],
)
@patch(
    "services.market_data_service.get_vix_term_structure",
    new_callable=AsyncMock,
    return_value=_CRITICAL_PATCHES["vts"],
)
@patch(
    "market_analysis.index_microstructure.fetch_core_macro_metrics",
    new_callable=AsyncMock,
    return_value=_CRITICAL_PATCHES["fear_greed"],
)
@patch("database.cache.get_kv_cache", return_value=_CRITICAL_PATCHES["prob"])
@patch("database.orders.get_user_active_orders", return_value=[])
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_macro_top_escape_defense_triggers_bounded_trim_to_boxx(
    mock_ctx: MagicMock,
    mock_orders: MagicMock,
    mock_kv: MagicMock,
    mock_fear_greed: AsyncMock,
    mock_vts: AsyncMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """兩道 Gate 皆通過 (opt-in 開啟 + 評分達 CRITICAL) -> 對 SATELLITE 持倉
    觸發有界 25% 防禦性減碼，轉入 BOXX，且遠低於 Scenario 3/4 的 90%/100%。"""
    mock_ctx.return_value = MagicMock(enable_macro_top_escape_defense=True)
    result = await engine.evaluate_macro_top_escape_defense(1, [_satellite_asset()])
    assert len(result) == 1
    assert result[0]["symbol"] == "NVDA"
    assert result[0]["action"] == "LIQUIDATE"
    assert result[0]["sell_ratio"] == 0.25
    assert result[0]["target_core"] == "BOXX"
    assert result[0]["sell_action"] == "STC"
    assert "BOXX" in (result[0]["buy_action_label"] or "")
    assert result[0]["is_manual_override_required"] is True
    assert result[0]["scenario"] == "MACRO_TOP_ESCAPE_DEFENSE"
    assert "逃頂確認" in result[0]["reason"]


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value=_CRITICAL_PATCHES["regime"],
)
@patch(
    "services.market_data_service.get_vix_term_structure",
    new_callable=AsyncMock,
    return_value=_CRITICAL_PATCHES["vts"],
)
@patch(
    "market_analysis.index_microstructure.fetch_core_macro_metrics",
    new_callable=AsyncMock,
    return_value=_CRITICAL_PATCHES["fear_greed"],
)
@patch("database.cache.get_kv_cache", return_value=_CRITICAL_PATCHES["prob"])
@patch("database.orders.get_user_active_orders", return_value=[])
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_macro_top_escape_defense_skips_already_flagged_symbols(
    mock_ctx: MagicMock,
    mock_orders: MagicMock,
    mock_kv: MagicMock,
    mock_fear_greed: AsyncMock,
    mock_vts: AsyncMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """已被 Scenario 2/3/4/5 標記過的標的，Scenario 6 應跳過以避免矛盾指令。"""
    mock_ctx.return_value = MagicMock(enable_macro_top_escape_defense=True)
    result = await engine.evaluate_macro_top_escape_defense(
        1,
        [_satellite_asset()],
        already_flagged_symbols={("NVDA", "SPOT")},
    )
    assert result == []


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value=_CRITICAL_PATCHES["regime"],
)
@patch(
    "services.market_data_service.get_vix_term_structure",
    new_callable=AsyncMock,
    return_value=_CRITICAL_PATCHES["vts"],
)
@patch(
    "market_analysis.index_microstructure.fetch_core_macro_metrics",
    new_callable=AsyncMock,
    return_value=_CRITICAL_PATCHES["fear_greed"],
)
@patch("database.cache.get_kv_cache", return_value=_CRITICAL_PATCHES["prob"])
@patch("database.orders.get_user_active_orders", return_value=[])
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_macro_top_escape_defense_excludes_core_etfs(
    mock_ctx: MagicMock,
    mock_orders: MagicMock,
    mock_kv: MagicMock,
    mock_fear_greed: AsyncMock,
    mock_vts: AsyncMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """CORE_DEFENSE_ETF_SYMBOLS (QQQ/SPY/VOO/VXX/IVV/VTI) 即使被誤標為
    SATELLITE，也永遠不應被本情境標記減碼。"""
    mock_ctx.return_value = MagicMock(enable_macro_top_escape_defense=True)
    result = await engine.evaluate_macro_top_escape_defense(
        1, [_satellite_asset(symbol="VOO")]
    )
    assert result == []


def test_macro_top_escape_defense_scenario_renders_orange() -> None:
    """安全性回歸測試：MACRO_TOP_ESCAPE_DEFENSE (宏觀逃頂前瞻防禦) 無論
    action/sell_ratio 為何，embed 顏色都必須固定為策展警示橙色 (0xF39C12)，
    且不可與 MARGIN_DEFENSE 的危急紅色 (0xE74C3C) 混淆——本情境是前瞻/機率性
    的領先訊號減碼，而非已經價格/保證金雙重確認的危急情境。"""
    embed = create_dynamic_rollover_embed(
        rollover_type="宏觀逃頂前瞻防禦",
        sell_symbol="NVDA",
        sell_ratio=0.25,
        buy_symbol="BOXX",
        reason="多項宏觀領先訊號同時觸發，先行有界防禦性減碼",
        suggested_strategy="STC 25% 轉倉 BOXX (逃頂前瞻防禦)",
        suggested_price="Market",
        strike="N/A",
        expiry="N/A",
        direction="STC",
        scenario="MACRO_TOP_ESCAPE_DEFENSE",
    )
    assert embed.color == discord.Color(
        0xF39C12
    ), f"MACRO_TOP_ESCAPE_DEFENSE 必須恆為策展警示橙色，實際為 {embed.color}"
    assert embed.color != discord.Color(0xE74C3C)
    assert "宏觀逃頂前瞻防禦" in str(embed.title)
