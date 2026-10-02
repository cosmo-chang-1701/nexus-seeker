from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from cogs.embed_builders.rollover_embeds import (
    create_dynamic_rollover_embed,
)
from market_analysis.dynamic_rollover import (
    DynamicRolloverEngine,
    RolloverScenario,
)


@pytest.fixture
def engine() -> DynamicRolloverEngine:
    return DynamicRolloverEngine()


@pytest.fixture(autouse=True)
def _mock_target_reference_live_quote() -> Any:
    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={},
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_entry_condition1_vwap() -> Any:
    """條件一新增 Session VWAP 站穩確認；全域 mock 為遠低於本檔案各演練情境
    收盤價的常數，維持既有演練情境的通過/攔截語意不變。"""
    with patch(
        "market_analysis.vwap_utils.fetch_session_vwap",
        new_callable=AsyncMock,
        return_value=50.0,
    ):
        yield


# ==============================================================================
# 情境一：NVDA 轉弱，SPCX 轉強符合轉倉條件 (Opportunity Cost Rotation)
# ==============================================================================


# ==============================================================================
# 情境二：NVDA 轉弱，沒有標的符合轉倉條件 (No Candidate / Hold / Breakdown / Margin)
# ==============================================================================


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=True,
)
async def test_drill_scenario_2c_nvda_structural_breakdown_retreat_to_voo(
    mock_cliff: AsyncMock,
    mock_user_ctx: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """
    子情境 2C：
    NVDA 15m 實體收盤跌破 Stop Loss ($184.00 < $185.50)，
    觸發結構破位 (Structural Breakdown)。
    機構風控鐵律：強制 100% 清倉 (LIQUIDATE) 撤退回防核心大盤 VOO，嚴禁追逐高波衛星標的。
    """
    mock_user_ctx.return_value = MagicMock(can_trade_spreads=False)

    portfolio_assets: List[Dict[str, Any]] = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "spot_price": 184.0,
            "price_15m_close": 184.0,  # 實體跌破防守線 $185.50
            "avg_cost": 180.0,
            "quantity": 77.0,
            "current_value": 14168.0,
            "put_wall": 190.0,
            "call_wall": 210.0,
            "gamma_flip": 192.0,
            "atr_14": 3.0,
            "atr_15m": 3.0,
            "sqz_mom": -0.8,
            "skew": -0.2,
            "max_allocation_pct": 0.30,
            "target_allocation_pct": 0.20,
        }
    ]

    instructions = await engine.check_satellite_rebalancing(
        user_id=101,
        portfolio_assets=portfolio_assets,
        total_account_value=50000.0,
    )

    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["symbol"] == "NVDA"
    assert ins["action"] == "LIQUIDATE"
    assert ins["sell_ratio"] == 1.0
    assert ins["target_core"] == "VOO"
    assert ins["scenario"] == RolloverScenario.SATELLITE_REBALANCE.value
    assert "SL-結構失效" in ins["reason"]

    # 驗證 Embed 渲染
    embed = create_dynamic_rollover_embed(
        rollover_type="核心衛星再平衡",
        sell_symbol=ins["symbol"],
        sell_ratio=ins["sell_ratio"],
        buy_symbol=ins["target_core"],
        reason=ins["reason"],
        suggested_strategy=ins["suggested_strategy"],
        suggested_price="Market",
        strike="N/A",
        expiry="N/A",
        direction="BUY",
        sell_action="SELL",
        scenario=ins["scenario"],
        cash_impact=ins["cash_impact"],
        asset_class="SPOT",
    )

    assert embed.title == "🔄 核心衛星再平衡: NVDA → VOO"
    assert "🚨【執行轉倉指令】" in str(embed.description)
    assert "SELL (賣出現貨)" in str(embed.fields[0].value)
    assert "VOO" in str(embed.fields[1].value)
    assert "到期日" not in str(embed.fields[2].value)
    assert "履約價" not in str(embed.fields[2].value)


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="SHORT_GAMMA_CRITICAL",
)
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=True,
)
@patch(
    "market_analysis.dynamic_rollover.margin_defense.confirm_inverse_hedge_spot_momentum",
    new_callable=AsyncMock,
    return_value=False,
)
async def test_drill_scenario_2d_systemic_margin_defense_retreat_to_boxx(
    mock_inverse_confirm: AsyncMock,
    mock_cliff: AsyncMock,
    mock_user_ctx: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """
    子情境 2D：
    大盤觸發 SHORT_GAMMA_CRITICAL 負 Gamma 踩踏模式，且帳戶存在保證金赤字壓力。
    NVDA 經檢查無邊際優勢 (No-Edge)，強制 100% 清倉並轉入純現金等價物 BOXX。
    """
    mock_user_ctx.return_value = MagicMock(
        has_margin_pressure=True,
        can_trade_spreads=False,
        cash_reserve=1000.0,
    )

    portfolio_assets: List[Dict[str, Any]] = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "spot_price": 184.0,
            "price_15m_close": 184.0,
            "avg_cost": 180.0,
            "quantity": 77.0,
            "current_value": 14168.0,
            "put_wall": 190.0,
            "call_wall": 210.0,
            "gamma_flip": 192.0,
            "atr_14": 3.0,
            "atr_15m": 3.0,
            "sqz_mom": -1.2,
            "skew": -0.35,
        }
    ]

    instructions = await engine.evaluate_margin_defense(
        user_id=101,
        portfolio_assets=portfolio_assets,
        already_flagged_symbols=set(),
    )

    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["symbol"] == "NVDA"
    assert ins["action"] == "LIQUIDATE"
    assert ins["sell_ratio"] == 1.0
    assert ins["target_core"] == "BOXX"
    assert ins["scenario"] == RolloverScenario.MARGIN_DEFENSE.value
    assert ins["buy_action_label"] == "轉入 BOXX（鎖定無風險利息）"

    # 驗證 Embed 渲染 (紅色警戒)
    embed = create_dynamic_rollover_embed(
        rollover_type="槓桿與保證金防禦",
        sell_symbol=ins["symbol"],
        sell_ratio=ins["sell_ratio"],
        buy_symbol=ins["target_core"],
        reason=ins["reason"],
        suggested_strategy=ins["suggested_strategy"],
        suggested_price="Market",
        strike="N/A",
        expiry="N/A",
        direction="BUY",
        sell_action="SELL",
        buy_action_label=ins["buy_action_label"],
        scenario=ins["scenario"],
        cash_impact=ins["cash_impact"],
        asset_class="SPOT",
    )

    assert embed.title == "🚨 保證金防禦強制平倉: 槓桿與保證金防禦"
    assert embed.color == discord.Color.red()
    assert "SELL (賣出現貨)" in str(embed.fields[0].value)
    assert "BOXX" in str(embed.fields[1].value)
    assert "轉入 BOXX" in str(embed.fields[1].value)
    assert "到期日" not in str(embed.fields[2].value)
    assert "履約價" not in str(embed.fields[2].value)
