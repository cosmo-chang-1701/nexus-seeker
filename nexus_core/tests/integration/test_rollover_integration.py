import pytest
from unittest.mock import AsyncMock, patch
from market_analysis.dynamic_rollover import DynamicRolloverEngine
import discord
from cogs.embed_builders.rollover_embeds import (
    create_dynamic_rollover_embed,
    RolloverActionView,
)


@pytest.mark.asyncio
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=False,
)
async def test_integration_rollover_embed_generation(
    mock_cliff: AsyncMock,
) -> None:
    """
    Test that the output of DynamicRolloverEngine can correctly be pipelined
    into the Discord Embed builder (Scenario 3: Rebalancing).
    """
    engine = DynamicRolloverEngine()

    # 1. Simulate DB query returning portfolio
    portfolio = [
        {
            "symbol": "TSLA",
            "asset_class": "SATELLITE",
            "current_value": 6000.0,
            "target_allocation_pct": 0.15,
            "max_allocation_pct": 0.25,
        },
        {
            "symbol": "VOO",
            "asset_class": "CORE",
            "current_value": 4000.0,
            "target_allocation_pct": 0.85,
            "max_allocation_pct": 1.0,
        },
    ]
    total_val = 10000.0

    # 2. Engine processing
    instructions = await engine.check_satellite_rebalancing(1, portfolio, total_val)

    assert len(instructions) == 1
    ins = instructions[0]

    assert ins["symbol"] == "TSLA"
    assert ins["sell_ratio"] == 0.75  # (0.6 - 0.15)=0.45, 0.45*10k=4500, 4500/6000=0.75
    # 情境識別碼必須存在且正確，這是 embed 呈現層正確標色/標題的前提
    assert ins["scenario"] == "SATELLITE_REBALANCE"

    # 3. Embed building (真實呼叫端會將 ins["scenario"] 一併傳入)
    embed = create_dynamic_rollover_embed(
        rollover_type="核心衛星再平衡",
        sell_symbol=ins["symbol"],
        sell_ratio=ins["sell_ratio"],
        buy_symbol=ins["target_core"],
        reason=ins["reason"],
        suggested_strategy="Buy Shares",
        suggested_price="Market",
        strike="N/A",
        expiry="N/A",
        direction="BTO",
        scenario=ins["scenario"],
    )

    assert isinstance(embed, discord.Embed)
    assert embed.title == "🔄 核心衛星再平衡: 核心衛星再平衡"
    assert (
        len(embed.fields) >= 3
    ), f"Expected at least 3 fields, got {len(embed.fields)}"
    assert "TSLA" in str(embed.fields[0].value)
    assert "VOO" in str(embed.fields[1].value)
    assert "機構量化防禦與再平衡決策" in str(embed.description)

    # 4. View initialization
    view = RolloverActionView(target_symbol=ins["symbol"])
    assert len(view.children) == 2
    assert getattr(view.children[0], "label", None) == "執行試算"
    assert getattr(view.children[1], "label", None) == "忽略"
