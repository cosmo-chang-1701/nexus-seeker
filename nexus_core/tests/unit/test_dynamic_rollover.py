import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import discord
import pandas as pd
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from market_analysis.dynamic_rollover import (
    DynamicRolloverEngine,
    FundamentalThesisResult,
    CORE_DEFENSE_ETF_SYMBOLS,
    _resolve_canonical_anchor_base,
    _scan_gex_walls,
)
from cogs.embed_builders.rollover_embeds import (
    create_dynamic_rollover_embed,
    create_thesis_passed_embed,
)


@pytest.fixture
def engine() -> DynamicRolloverEngine:
    return DynamicRolloverEngine()


@pytest.fixture(autouse=True)
def _mock_target_reference_live_quote() -> Any:
    """
    _resolve_target_reference_price 在 market_cache 未命中時會嘗試即時報價
    (services.market_data_service.get_quote) 作為第二層備援。單元測試預設不應
    觸發真實外部網路呼叫，因此全域 mock 為空報價，讓測試決定性地退回具名備援
    常數 (與 mock 前的既有行為一致)；個別測試若需驗證即時報價命中路徑，
    可自行以更內層的 patch 覆寫此 fixture。
    """
    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={},
    ):
        yield


def test_evaluate_opportunity_cost(engine: DynamicRolloverEngine) -> None:
    # Scenario 1: Should rollover (EV spread > 5%, target breakout, holding decay)
    res = engine.evaluate_opportunity_cost(
        current_holding_symbol="PLTR",
        current_holding_power_squeeze=15.0,  # < 20 (decaying)
        current_holding_profit_pct=0.4,  # > 0.3 (highly profitable)
        target_watchlist_symbol="SMCI",
        target_power_squeeze=85.0,  # > 80 (breakout)
        target_expected_value=0.25,
        current_holding_expected_value=0.10,  # EV spread = 15%
    )
    assert res["should_rollover"] is True
    assert res["rollover_ratio"] == 0.5
    assert "PLTR" in res["reason"]

    # Scenario 2: Should rollover but lower ratio (profit < 30%)
    res2 = engine.evaluate_opportunity_cost(
        current_holding_symbol="PLTR",
        current_holding_power_squeeze=15.0,
        current_holding_profit_pct=0.1,
        target_watchlist_symbol="SMCI",
        target_power_squeeze=85.0,
        target_expected_value=0.25,
        current_holding_expected_value=0.10,
    )
    assert res2["should_rollover"] is True
    assert res2["rollover_ratio"] == 0.3

    # Scenario 3: Should NOT rollover (EV spread too small)
    res3 = engine.evaluate_opportunity_cost(
        current_holding_symbol="PLTR",
        current_holding_power_squeeze=15.0,
        current_holding_profit_pct=0.4,
        target_watchlist_symbol="SMCI",
        target_power_squeeze=85.0,
        target_expected_value=0.12,
        current_holding_expected_value=0.10,  # Spread = 2%
    )
    assert res3["should_rollover"] is False


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=False,
)
async def test_check_satellite_rebalancing(
    mock_cliff: AsyncMock, mock_get_user: MagicMock, engine: DynamicRolloverEngine
) -> None:
    mock_get_user.return_value = MagicMock(can_trade_spreads=False)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 4500.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.30,
        },
        {
            "symbol": "VOO",
            "asset_class": "CORE",
            "current_value": 5500.0,
            "target_allocation_pct": 0.80,
            "max_allocation_pct": 1.0,
        },
    ]
    total_val = 10000.0

    # NVDA is 45%, max is 30%, target is 20%. Excess = 45% - 20% = 25%?
    # Let's check logic: excess_alloc = current_alloc (0.45) - target_alloc (0.20) = 0.25
    # sell ratio = (0.25 * 10000) / 4500 = 2500 / 4500 = 0.555

    instructions = await engine.check_satellite_rebalancing(1, portfolio, total_val)
    assert len(instructions) == 1
    assert instructions[0]["symbol"] == "NVDA"
    assert instructions[0]["action"] == "REDUCE"
    assert instructions[0]["target_core"] == "VOO"
    assert instructions[0]["sell_ratio"] == 0.56
    assert instructions[0]["scenario"] == "SATELLITE_REBALANCE"
    assert instructions[0]["is_manual_override_required"] is False

    # Test within limits
    portfolio2 = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 2500.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.30,
        },
        {
            "symbol": "VOO",
            "asset_class": "CORE",
            "current_value": 7500.0,
            "target_allocation_pct": 0.80,
            "max_allocation_pct": 1.0,
        },
    ]
    instructions2 = await engine.check_satellite_rebalancing(1, portfolio2, 10000.0)
    assert len(instructions2) == 0


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=False,
)
async def test_satellite_rebalancing_breakdown_not_confirmed(
    mock_cliff: AsyncMock,
    mock_get_user: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    mock_get_user.return_value = MagicMock(can_trade_spreads=False)
    """結構破位待確認：15 分鐘確認未通過時不觸發清倉"""
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.50,
            "spot_price": 200.0,
            "put_wall": 210.0,
            "gamma_flip": 215.0,
            "call_wall": 250.0,
            "ivr": 30.0,
            "is_uoa_sweep": False,
            "max_pain": 220.0,
            "sqz_mom": 0.5,
            "skew": -0.1,
        },
    ]
    instructions = await engine.check_satellite_rebalancing(1, portfolio, 10000.0)
    # is_gamma_cliff_confirmed returned False → structural breakdown NOT confirmed
    # Allocation is 50% == max 50%, so no regular rebalance either
    assert all(ins.get("action") != "LIQUIDATE" for ins in instructions)


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=True,
)
async def test_satellite_rebalancing_breakdown_confirmed(
    mock_cliff: AsyncMock,
    mock_get_user: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    mock_get_user.return_value = MagicMock(can_trade_spreads=False)
    """結構破位確認：15 分鐘確認通過時觸發清倉"""
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.50,
            "spot_price": 200.0,
            "put_wall": 210.0,
            "gamma_flip": 215.0,
            "call_wall": 250.0,
            "ivr": 30.0,
            "is_uoa_sweep": False,
            "max_pain": 220.0,
            "sqz_mom": 0.5,
            "skew": -0.1,
            "price_15m_close": 185.0,
        },
    ]
    instructions = await engine.check_satellite_rebalancing(1, portfolio, 10000.0)
    # is_gamma_cliff_confirmed returned True → structural breakdown confirmed → LIQUIDATE
    liquidate_instructions = [
        ins for ins in instructions if ins.get("action") == "LIQUIDATE"
    ]
    assert len(liquidate_instructions) == 1
    assert liquidate_instructions[0]["symbol"] == "NVDA"


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.is_memory_safe", return_value=True)
@patch("market_analysis.dynamic_rollover.client")
@patch("database.market_cache.save_fundamental_cache")
async def test_evaluate_fundamental_thesis(
    mock_save_cache: MagicMock,
    mock_client: MagicMock,
    mock_mem: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    # Mock LLM Response
    mock_parsed = FundamentalThesisResult(
        is_broken=True, confidence=0.9, reasoning="Test reason"
    )
    mock_message = MagicMock()
    mock_message.parsed = mock_parsed
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_client.beta.chat.completions.parse = AsyncMock(return_value=mock_response)

    res = await engine.evaluate_fundamental_thesis("AMD", "Bad news")
    assert res is not None
    assert res.is_broken is True
    assert res.reasoning == "Test reason"

    # Verify the result is cached
    mock_save_cache.assert_called_once_with("AMD", True, 0.9, "Test reason")

    # Backward compat: no form_type/sections passed -> prompt has no
    # filing-type note block and no structured appendix.
    call_kwargs = mock_client.beta.chat.completions.parse.call_args.kwargs
    system_prompt = call_kwargs["messages"][0]["content"]
    user_prompt = call_kwargs["messages"][1]["content"]
    assert "Filing Context" not in system_prompt
    assert "Structured Filing Appendix" not in user_prompt


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.is_memory_safe", return_value=False)
async def test_evaluate_fundamental_thesis_memory_unsafe(
    mock_mem: MagicMock, engine: DynamicRolloverEngine
) -> None:
    res = await engine.evaluate_fundamental_thesis("AMD", "Bad news")
    assert res is None


def _mock_llm_client_for_thesis(mock_client: MagicMock) -> None:
    mock_parsed = FundamentalThesisResult(
        is_broken=False, confidence=0.5, reasoning="ok"
    )
    mock_message = MagicMock()
    mock_message.parsed = mock_parsed
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_client.beta.chat.completions.parse = AsyncMock(return_value=mock_response)


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.is_memory_safe", return_value=True)
@patch("market_analysis.dynamic_rollover.client")
@patch("database.market_cache.save_fundamental_cache")
async def test_evaluate_fundamental_thesis_with_form_type_10q(
    mock_save_cache: MagicMock,
    mock_client: MagicMock,
    mock_mem: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    _mock_llm_client_for_thesis(mock_client)

    await engine.evaluate_fundamental_thesis(
        "AMD",
        "some text",
        form_type="10-Q",
        sections={"quarterly_financials": "Q3 rev $1B"},
    )

    call_kwargs = mock_client.beta.chat.completions.parse.call_args.kwargs
    system_prompt = call_kwargs["messages"][0]["content"]
    user_prompt = call_kwargs["messages"][1]["content"]
    assert "Quarterly Report (10-Q)" in system_prompt
    assert "STRICT EXCLUSION RULE with extra" in system_prompt
    assert "Structured Filing Appendix" in user_prompt
    assert "Quarterly Financial Results" in user_prompt
    assert "Q3 rev $1B" in user_prompt


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.is_memory_safe", return_value=True)
@patch("market_analysis.dynamic_rollover.client")
@patch("database.market_cache.save_fundamental_cache")
async def test_evaluate_fundamental_thesis_with_form_type_8k(
    mock_save_cache: MagicMock,
    mock_client: MagicMock,
    mock_mem: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    _mock_llm_client_for_thesis(mock_client)

    await engine.evaluate_fundamental_thesis(
        "AMD",
        "some text",
        form_type="8-K",
        sections={"key_events": "[Item 5.02] CFO resigned"},
    )

    call_kwargs = mock_client.beta.chat.completions.parse.call_args.kwargs
    system_prompt = call_kwargs["messages"][0]["content"]
    user_prompt = call_kwargs["messages"][1]["content"]
    assert "EVENT-DRIVEN" in system_prompt
    assert "Item 2.05" in system_prompt
    assert "Structured Filing Appendix" in user_prompt
    assert "Key Events (8-K Item Triggers)" in user_prompt
    assert "CFO resigned" in user_prompt


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.is_memory_safe", return_value=True)
@patch("market_analysis.dynamic_rollover.client")
@patch("database.market_cache.save_fundamental_cache")
async def test_evaluate_fundamental_thesis_empty_sections_no_appendix(
    mock_save_cache: MagicMock,
    mock_client: MagicMock,
    mock_mem: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    _mock_llm_client_for_thesis(mock_client)

    sections_variants: list[Optional[Dict[str, str]]] = [None, {}]
    for sections in sections_variants:
        await engine.evaluate_fundamental_thesis(
            "AMD", "some text", form_type="10-K", sections=sections
        )
        call_kwargs = mock_client.beta.chat.completions.parse.call_args.kwargs
        user_prompt = call_kwargs["messages"][1]["content"]
        assert "Structured Filing Appendix" not in user_prompt


def test_create_thesis_passed_embed_truncates_long_reasoning() -> None:
    """reasoning 超過 4000 字元時被正確截斷，且 Embed description ≤ 4096。"""
    long_reasoning = "護城河分析" * 1000  # 5000 chars
    embed = create_thesis_passed_embed(
        symbol="AMD",
        reasoning=long_reasoning,
        source_url="https://example.com/sec",
    )
    assert embed.description is not None
    assert len(embed.description) <= 4096
    assert embed.title == "✅ AMD 基本面驗證通過"
    assert len(embed.fields) == 1
    assert embed.fields[0].value is not None
    assert "example.com" in embed.fields[0].value


def test_create_thesis_passed_embed_short_reasoning() -> None:
    """短 reasoning 原樣通過，不會被截斷，也不產生 source_url 欄位。"""
    embed = create_thesis_passed_embed(
        symbol="NVDA",
        reasoning="護城河穩固，無異常。",
    )
    assert embed.description is not None
    assert "護城河穩固" in embed.description
    assert len(embed.fields) == 0  # 無 source_url 欄位


def test_create_dynamic_rollover_embed_truncates_long_reason() -> None:
    """
    rollover embed 的 reason 實際落在 embed.description（而非 fields[0]，
    後者是固定短版型的「撤出資金/平倉」ANSI 區塊，與 reason 內容無關），
    需確認長 reason 被正確截斷且不超過 Discord description 4096 字元硬上限。
    """
    long_reason = "A" * 5000
    embed = create_dynamic_rollover_embed(
        rollover_type="原型假設破滅",
        sell_symbol="AMD",
        sell_ratio=1.0,
        buy_symbol="VOO",
        reason=long_reason,
        suggested_strategy="Buy Shares",
        suggested_price="Market",
        strike="N/A",
        expiry="N/A",
        direction="BTO",
        scenario="FUNDAMENTAL_BROKEN",
    )
    assert embed.description is not None
    assert len(embed.description) <= 4096
    assert "AAAA" in embed.description


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.DynamicRolloverEngine._find_best_rollover_target",
    return_value="AMD",
)
async def test_satellite_rebalancing_euphoria_trailing_stop(
    mock_target: MagicMock,
    mock_get_user: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """Euphoria 且動能多頭延續 (SQZ MOM > 0)，為防範 Gamma Squeeze 軋空，剩餘 10% 啟動 Trailing Stop 移動止盈"""
    mock_get_user.return_value = MagicMock(can_trade_spreads=True)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.50,
            "spot_price": 249.0,
            "put_wall": 210.0,
            "gamma_flip": 215.0,
            "call_wall": 250.0,  # 距離現價 < 1.5%
            "ivr": 30.0,
            "is_uoa_sweep": False,
            "max_pain": 220.0,
            "sqz_mom": 0.5,  # 多頭動能未衰竭
            "skew": -0.1,
            "skew_percentile": 50.0,
        },
    ]
    instructions = await engine.check_satellite_rebalancing(1, portfolio, 10000.0)
    assert len(instructions) == 2

    ins_90 = [i for i in instructions if i["sell_ratio"] == 0.9][0]
    assert ins_90["target_core"] == "AMD"
    assert ins_90["action"] == "LIQUIDATE"

    ins_10 = [i for i in instructions if i["sell_ratio"] == 0.0][0]
    assert ins_10["target_core"] == "NVDA"
    assert ins_10["action"] == "HOLD"
    assert "Trailing Stop" in ins_10["suggested_strategy"]
    assert "動能延續・移動止盈" in ins_10["reason"]


@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.DynamicRolloverEngine._find_best_rollover_target",
    return_value="AMD",
)
async def test_satellite_rebalancing_euphoria_exhaustion_bear_call_spread(
    mock_target: MagicMock,
    mock_get_user: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """Euphoria 且雙重動能衰竭確認 (SQZ MOM < 0 且 Skew >= 30%)，安全建立 10% Bear Call Spread 反向收租"""
    mock_get_user.return_value = MagicMock(can_trade_spreads=True)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.50,
            "spot_price": 249.0,
            "put_wall": 210.0,
            "gamma_flip": 215.0,
            "call_wall": 250.0,  # 距離現價 < 1.5%
            "ivr": 30.0,
            "is_uoa_sweep": False,
            "max_pain": 220.0,
            "sqz_mom": -0.8,  # 動能翻負拐頭
            "skew": -0.05,
            "skew_percentile": 40.0,  # Skew 脫離狂熱 (>= 30%)
        },
    ]
    instructions = await engine.check_satellite_rebalancing(1, portfolio, 10000.0)
    assert len(instructions) == 2

    ins_90 = [i for i in instructions if i["sell_ratio"] == 0.9][0]
    assert ins_90["target_core"] == "AMD"
    assert ins_90["action"] == "LIQUIDATE"

    ins_10 = [i for i in instructions if i["sell_ratio"] == 0.1][0]
    assert ins_10["target_core"] == "NVDA"
    assert ins_10["action"] == "REDUCE"
    assert "Bear Call Spread" in ins_10["suggested_strategy"]
    assert ins_10.get("is_manual_override_required") is True


@pytest.mark.asyncio
async def test_generate_rule_based_rebalance_report_grayscale_hold(
    engine: DynamicRolloverEngine,
) -> None:
    """測試灰階思考架構：$225 正 Gamma 彈簧床完好，盤中插針至 $224.50 但動能多頭，判定 HOLD"""
    metrics = {
        "spot_price": 224.50,
        "price_15m_close": 224.80,
        "put_wall": 227.50,  # 原始顛倒數據
        "call_wall": 225.00,  # 原始顛倒數據
        "support_wall": 225.00,
        "resistance_wall": 227.50,
        "max_pain": 217.50,
        "ivr": 35.0,
        "sqz_mom": 14.53,
        "skew": 0.12,
        "atr_14": 0.80,
        "atr_15m": 0.80,
    }
    active_orders = [
        {
            "id": 147,
            "symbol": "AMD",
            "order_type": "STOP_LIMIT",
            "side": "SELL",
            "stop_price": 223.80,
            "limit_price": 223.20,
        }
    ]

    report = await engine._generate_rule_based_rebalance_report(
        symbol="AMD",
        metrics=metrics,
        requested_action="HOLD",
        target="VOO",
        active_orders=active_orders,
        position_shares=195.0,
        current_value=43524.0,
    )

    assert report["final_action"] == "HOLD"
    assert report["final_target"] == "AMD"
    assert "委託單 #147 有效" in report["markdown_report"]
    assert "停損: $223.80" in report["markdown_report"]
    assert "限價: $223.20" in report["markdown_report"]
    assert "15m 實體 K 線過濾" in report["markdown_report"]
    assert "GEX Wall: $225.00" in report["markdown_report"]
    assert "N/A 絕對停損" not in report["markdown_report"]
    assert "$43,524" in report["markdown_report"]
    assert "VOO" in report["markdown_report"]


@pytest.mark.asyncio
async def test_generate_rule_based_rebalance_report_hard_breakdown(
    engine: DynamicRolloverEngine,
) -> None:
    """測試硬性破位條件：15m 實體收盤跌破 $223.80 觸發 100% 轉入 VOO"""
    metrics = {
        "spot_price": 223.10,
        "price_15m_close": 223.10,
        "support_wall": 225.00,
        "resistance_wall": 227.50,
        "max_pain": 217.50,
        "ivr": 35.0,
        "sqz_mom": -2.5,
        "skew": -0.35,
        "atr_14": 0.80,
        "atr_15m": 0.80,
    }

    report = await engine._generate_rule_based_rebalance_report(
        symbol="AMD",
        metrics=metrics,
        requested_action="HOLD",
        target="VOO",
        position_shares=195.0,
        current_value=43524.0,
    )

    assert report["final_action"] == "LIQUIDATE"
    assert report["final_target"] == "VOO"
    assert "15m 實體破位確認" in report["markdown_report"]
    assert "100% LIQUIDATE (轉入 VOO)" in report["options_strategy"]
    assert "$43,524" in report["markdown_report"]


@pytest.mark.asyncio
async def test_generate_rule_based_rebalance_report_dynamic_generic_ticker(
    engine: DynamicRolloverEngine,
) -> None:
    """測試完全動態通用標的（例如 NVDA 轉入 SPY，無委託單且自定義 GEX 數據）"""
    metrics = {
        "spot_price": 128.50,
        "price_15m_close": 128.60,
        "support_wall": 125.00,
        "resistance_wall": 135.00,
        "support_gex": 450000000.0,  # +450M
        "resistance_gex": -80000000.0,  # -80M
        "max_pain": 120.00,
        "ivr": 22.0,
        "sqz_mom": 5.2,
        "skew": 0.05,
        "atr_14": 1.20,
        "atr_15m": 1.20,
    }

    report = await engine._generate_rule_based_rebalance_report(
        symbol="NVDA",
        metrics=metrics,
        requested_action="HOLD",
        target="SPY",
        active_orders=[],
        position_shares=100.0,
        current_value=12850.0,
    )

    assert report["final_action"] == "HOLD"
    assert report["final_target"] == "NVDA"
    assert "建議設置防守委託單" in report["markdown_report"]
    assert "+450M" in report["markdown_report"]
    assert "-80M" in report["markdown_report"]
    assert "GEX Wall: $125.00" in report["markdown_report"]
    assert "$12,850" in report["markdown_report"]
    assert "SPY" in report["markdown_report"]
    assert "AMD" not in report["markdown_report"]
    assert "#147" not in report["markdown_report"]


@pytest.mark.asyncio
async def test_01dte_risk_parity_position_sizing(engine: DynamicRolloverEngine) -> None:
    """測試 0/1 DTE 風險平價口數動態縮放：停損擴展至 3.0x ATR，且轉倉買入股數強制縮減 50%"""
    metrics = {
        "spot_price": 100.0,
        "price_15m_close": 100.0,
        "support_wall": 100.0,
        "atr_15m": 2.0,
        "dte": 1,  # 0/1 DTE
        "ivr": 25.0,
        "sqz_mom": 1.0,
    }
    # anchor_wall = 100, base stop = 100 - (1.5 * 2) - (1.5 * 2) = 100 - 6 = 94.0
    report = await engine._generate_rule_based_rebalance_report(
        symbol="XYZ",
        metrics=metrics,
        requested_action="HOLD",
        target="VOO",
        position_shares=100.0,
        current_value=10000.0,
    )
    # VOO est price = 560. 10000 / 560 = 17 shares. With 50% risk parity scale = 8 shares.
    assert "0/1 DTE 風險平價" in report["markdown_report"]
    assert "削減 50%" in report["markdown_report"]
    assert "3.0 × ATR_15m" in report["markdown_report"]
    assert "$94.00" in report["markdown_report"]


@pytest.mark.asyncio
async def test_lvn_secondary_hvn_snapping(engine: DynamicRolloverEngine) -> None:
    """測試 LVN 拓撲吸附：絕對吸附至次級 HVN 上緣 + 0.2*ATR，禁止固定 1.5% 平移"""
    metrics = {
        "spot_price": 100.0,
        "price_15m_close": 100.0,
        "support_wall": 100.0,
        "atr_15m": 2.0,
        "lvn": 97.0,  # Base stop is 100 - 3.0 = 97.0, which lands exactly in LVN
        "secondary_hvn": 94.0,  # Below LVN
        "hvn": 94.0,
        "dte": 30,
        "ivr": 25.0,
        "sqz_mom": 1.0,
    }
    # Snapped stop should be secondary_hvn (94.0) + 0.2 * 2.0 = 94.40
    report = await engine._generate_rule_based_rebalance_report(
        symbol="XYZ",
        metrics=metrics,
        requested_action="HOLD",
        target="VOO",
        position_shares=100.0,
        current_value=10000.0,
    )
    assert "$94.40" in report["markdown_report"]


@pytest.mark.asyncio
async def test_dual_track_exit_options_vs_spot(engine: DynamicRolloverEngine) -> None:
    """測試雙軌裁決機制：期權 OPTIONS 走 3-5m 快速通道 (現價跌破即清倉)，現貨 SPOT 走 15m 實體收盤"""
    # 案例 A: 現價跌破 Stop Loss，但 15m 收盤價尚未跌破
    metrics_a = {
        "spot_price": 95.0,
        "price_15m_close": 98.0,  # 15m close still above stop loss
        "support_wall": 100.0,
        "atr_15m": 2.0,  # Stop loss = 97.0
        "dte": 10,
        "ivr": 30.0,
        "sqz_mom": 0.5,
    }
    # 現貨 SPOT: 未跌破 15m 實體收盤 -> HOLD
    report_spot = await engine._generate_rule_based_rebalance_report(
        symbol="XYZ",
        metrics=metrics_a,
        requested_action="HOLD",
        asset_class="SPOT",
    )
    assert report_spot["final_action"] == "HOLD"
    assert "15m 實體 K 線過濾" in report_spot["markdown_report"]

    # 期權 OPTIONS: 現價貫穿 Stop Loss (95.0 < 97.0) -> 3-5m 快速通道即時清倉 LIQUIDATE (拒絕等待 15m)
    report_options = await engine._generate_rule_based_rebalance_report(
        symbol="XYZ",
        metrics=metrics_a,
        requested_action="HOLD",
        asset_class="OPTIONS",
    )
    assert report_options["final_action"] == "LIQUIDATE"
    assert "期權雙軌快速通道觸發" in report_options["markdown_report"]
    assert "3-5m 快速通道" in report_options["markdown_report"]


# ==========================================
# 補足缺口測試: _find_best_rollover_target / _normalize_power_squeeze /
# evaluate_opportunity_cost_for_satellites / evaluate_margin_defense
# ==========================================


@patch("database.market_cache.get_market_cache")
@patch("database.watchlist.get_user_watchlist")
def test_find_best_rollover_target_picks_high_ev_candidate(
    mock_watchlist: MagicMock, mock_cache: MagicMock, engine: DynamicRolloverEngine
) -> None:
    mock_watchlist.return_value = [("XYZ", True)]
    mock_cache.return_value = {
        "reference_spot_price": 100.0,
        "expected_move_upper": 110.0,  # EV = 0.10 > 0.05 門檻
        "is_stale": 0,
        "is_degraded": 0,
    }
    assert engine._find_best_rollover_target(1) == "XYZ"


@patch("database.market_cache.get_market_cache")
@patch("database.watchlist.get_user_watchlist")
def test_find_best_rollover_target_ignores_stale_or_low_ev(
    mock_watchlist: MagicMock, mock_cache: MagicMock, engine: DynamicRolloverEngine
) -> None:
    mock_watchlist.return_value = [("XYZ", True)]

    # is_stale=1 -> 視為不可信快取
    mock_cache.return_value = {
        "reference_spot_price": 100.0,
        "expected_move_upper": 110.0,
        "is_stale": 1,
        "is_degraded": 0,
    }
    assert engine._find_best_rollover_target(1) == "VOO"

    # EV 未達 0.05 門檻
    mock_cache.return_value = {
        "reference_spot_price": 100.0,
        "expected_move_upper": 102.0,
        "is_stale": 0,
        "is_degraded": 0,
    }
    assert engine._find_best_rollover_target(1) == "VOO"


def test_find_best_rollover_target_no_watchlist_returns_voo(
    engine: DynamicRolloverEngine,
) -> None:
    with patch("database.watchlist.get_user_watchlist", return_value=[]):
        assert engine._find_best_rollover_target(1) == "VOO"


@pytest.mark.parametrize(
    "psq,expected",
    [
        (
            {
                "squeeze_level": "Release",
                "signal_direction": "Neutral",
                "momentum_color": "Neutral",
            },
            10.0,
        ),
        ({"squeeze_level": "Release", "signal_direction": "Long"}, 75.0),
        ({"squeeze_level": "Release", "signal_direction": "Short"}, 5.0),
        ({"squeeze_level": "Normal"}, 30.0),
        ({"squeeze_level": "Mid", "signal_direction": "Long"}, 70.0),
        ({"squeeze_level": "Mid", "signal_direction": "Short"}, 45.0),
        ({"squeeze_level": "High", "signal_direction": "Long"}, 90.0),
        ({"squeeze_level": "High", "signal_direction": "Short"}, 10.0),
        (
            {
                "squeeze_level": "High",
                "signal_direction": "Neutral",
                "is_breakout_long": True,
            },
            95.0,
        ),
        (
            {
                "squeeze_level": "High",
                "signal_direction": "Short",
                "is_breakout_short": True,
            },
            5.0,
        ),
        ({"squeeze_level": "UnknownLevel"}, 30.0),
    ],
)
def test_normalize_power_squeeze_mapping(
    psq: dict, expected: float, engine: DynamicRolloverEngine
) -> None:
    assert engine._normalize_power_squeeze(psq) == expected


@pytest.mark.asyncio
async def test_evaluate_opportunity_cost_for_satellites_no_candidate(
    engine: DynamicRolloverEngine,
) -> None:
    # candidate_symbol == "VOO" (未找到高 EV 候選標的) -> 不強制轉倉
    result = await engine.evaluate_opportunity_cost_for_satellites(
        1, [{"symbol": "NVDA", "asset_class": "SATELLITE"}], set(), "VOO", None
    )
    assert result == []

    # 有候選標的但 radar 資料為空 -> 不強制轉倉
    result2 = await engine.evaluate_opportunity_cost_for_satellites(
        1, [{"symbol": "NVDA", "asset_class": "SATELLITE"}], set(), "SMCI", None
    )
    assert result2 == []


@pytest.mark.asyncio
@patch(
    "market_analysis.dynamic_rollover.DynamicRolloverEngine._confirm_entry_signal",
    new_callable=AsyncMock,
    return_value=(True, "mocked"),
)
@patch("database.market_cache.get_market_cache")
async def test_evaluate_opportunity_cost_for_satellites_triggers(
    mock_cache: MagicMock,
    mock_entry_gate: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    def cache_side_effect(symbol: str, expiry: str = None):  # type: ignore
        if symbol.upper() == "NVDA":
            return {
                "reference_spot_price": 200.0,
                "expected_move_upper": 205.0,  # EV ≈ 0.025 (低)
                "is_stale": 0,
                "is_degraded": 0,
            }
        if symbol.upper() == "SMCI":
            return {
                "reference_spot_price": 40.0,
                "expected_move_upper": 50.0,  # EV = 0.25 (高)
                "is_stale": 0,
                "is_degraded": 0,
            }
        return None

    mock_cache.side_effect = cache_side_effect

    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "spot_price": 240.0,
            "avg_cost": 200.0,  # profit_pct = 0.2 (< 0.3)
            "psq_result": {
                "squeeze_level": "Release",
                "signal_direction": "Neutral",
            },  # normalized score = 10 (< 20 動能衰退)
        },
    ]
    candidate_radar = {
        "psq_result": {
            "squeeze_level": "High",
            "signal_direction": "Long",
            "is_breakout_long": True,
        },  # normalized score = 95 (> 80 突破待發)
        "quote": {"c": 40.0},
        "iv_metrics": {"iv_rank": 20.0},
        "gex_profile_data": {"put_wall": 0.0},
        "uoa": [],
    }

    result = await engine.evaluate_opportunity_cost_for_satellites(
        1, portfolio, set(), "SMCI", candidate_radar
    )
    assert len(result) == 1
    assert result[0]["symbol"] == "NVDA"
    assert result[0]["target_core"] == "SMCI"
    assert result[0]["action"] == "REDUCE"
    assert result[0]["sell_ratio"] == 0.3
    assert result[0]["scenario"] == "OPPORTUNITY_COST"
    assert result[0]["is_manual_override_required"] is False


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="NORMAL",
)
async def test_evaluate_margin_defense_normal_regime_no_action(
    mock_regime: AsyncMock, engine: DynamicRolloverEngine
) -> None:
    result = await engine.evaluate_margin_defense(
        1, [{"symbol": "NVDA", "asset_class": "SATELLITE", "current_value": 5000.0}]
    )
    assert result == []


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="SHORT_GAMMA_CRITICAL",
)
@patch("database.orders.get_user_active_orders", return_value=[])
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_margin_defense_critical_regime_no_margin_pressure(
    mock_ctx: MagicMock,
    mock_orders: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    mock_ctx.return_value = MagicMock(cash_reserve=100000.0)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "quantity": 10.0,
        }
    ]
    # 現金儲備 100000 遠大於 SATELLITE 總市值 5000 -> 無保證金壓力
    result = await engine.evaluate_margin_defense(1, portfolio)
    assert result == []


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="SHORT_GAMMA_CRITICAL",
)
@patch("database.orders.get_user_active_orders", return_value=[])
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_margin_defense_triggers_boxx_for_no_edge_holding(
    mock_ctx: MagicMock,
    mock_orders: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    mock_ctx.return_value = MagicMock(cash_reserve=1000.0)  # 緩衝很小

    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "quantity": 10.0,
            "instrument_type": "SPOT",
            "sqz_mom": -5.0,
            "skew": -0.5,  # 主力空頭封殺 -> 結構性無勝率
        },
    ]
    # SATELLITE 總市值 5000 > 緩衝 1000 -> 保證金壓力觸發
    result = await engine.evaluate_margin_defense(1, portfolio)
    assert len(result) == 1
    assert result[0]["symbol"] == "NVDA"
    assert result[0]["sell_action"] == "STC"
    assert result[0]["action"] == "LIQUIDATE"
    assert result[0]["sell_ratio"] == 1.0
    assert result[0]["target_core"] == "BOXX"
    assert "BOXX" in result[0]["buy_action_label"]
    assert result[0]["is_manual_override_required"] is True
    assert result[0]["scenario"] == "MARGIN_DEFENSE"


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="SHORT_GAMMA_CRITICAL",
)
@patch("database.orders.get_user_active_orders", return_value=[])
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_margin_defense_holding_with_edge_left_untouched(
    mock_ctx: MagicMock,
    mock_orders: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    mock_ctx.return_value = MagicMock(cash_reserve=1000.0)  # 緩衝很小

    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "quantity": 10.0,
            "instrument_type": "SPOT",
            "sqz_mom": 8.0,  # 動能仍為正
            "skew": 0.1,  # 未達主力空頭封殺門檻
            "put_wall": 0.0,
            "gamma_flip": 0.0,
        },
    ]
    # 保證金壓力觸發，但該持倉技術面仍有勝率 -> 不強制轉倉
    result = await engine.evaluate_margin_defense(1, portfolio)
    assert result == []


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="SHORT_GAMMA_CRITICAL",
)
@patch("database.orders.get_user_active_orders", return_value=[])
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_margin_defense_checks_every_satellite_holding(
    mock_ctx: MagicMock,
    mock_orders: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    mock_ctx.return_value = MagicMock(cash_reserve=1000.0)

    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "quantity": 10.0,
            "instrument_type": "SPOT",
            "sqz_mom": -5.0,
            "skew": -0.5,  # 無勝率
        },
        {
            "symbol": "AAPL",
            "asset_class": "SATELLITE",
            "current_value": 3000.0,
            "quantity": 5.0,
            "instrument_type": "SPOT",
            "sqz_mom": 5.0,
            "skew": 0.1,  # 仍有勝率
        },
    ]
    # 逐一檢查每檔 SATELLITE 持倉，而非只挑單一標的
    result = await engine.evaluate_margin_defense(1, portfolio)
    assert {ins["symbol"] for ins in result} == {"NVDA"}
    assert result[0]["target_core"] == "BOXX"


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="SHORT_GAMMA_CRITICAL",
)
@patch(
    "database.orders.get_user_active_orders",
    return_value=[
        {"validity": "GTC", "side": "BUY", "limit_price": 500.0, "quantity": 10.0}
    ],
)
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_margin_defense_no_eligible_asset_returns_empty(
    mock_ctx: MagicMock,
    mock_orders: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    mock_ctx.return_value = MagicMock(cash_reserve=1000.0)
    # GTC 買單現金赤字 5000 > 緩衝 1000 -> 保證金壓力觸發，
    # 但唯一持倉是核心 ETF (非 SATELLITE) -> 找不到候選資產
    portfolio = [
        {
            "symbol": "VOO",
            "asset_class": "CORE",
            "current_value": 5000.0,
            "quantity": 10.0,
        },
    ]
    result = await engine.evaluate_margin_defense(1, portfolio)
    assert result == []


def test_create_dynamic_rollover_embed_buy_action_label_override() -> None:
    embed_default = create_dynamic_rollover_embed(
        rollover_type="機會成本",
        sell_symbol="NVDA",
        sell_ratio=0.3,
        buy_symbol="SMCI",
        reason="test reason",
        suggested_strategy="Buy Shares",
        suggested_price="Market",
        strike="N/A",
        expiry="N/A",
        direction="BTO",
    )
    buy_field_default = next(
        f for f in embed_default.fields if f.name and "轉入資產" in f.name
    )
    assert buy_field_default.value is not None
    assert "(Buy To Open)" in buy_field_default.value

    embed_override = create_dynamic_rollover_embed(
        rollover_type="槓桿與保證金防禦",
        sell_symbol="NVDA",
        sell_ratio=0.5,
        buy_symbol="CASH (保證金緩衝)",
        reason="test reason",
        suggested_strategy="STC 降槓桿",
        suggested_price="Market",
        strike="N/A",
        expiry="N/A",
        direction="BTO",
        sell_action="STC",
        buy_action_label="持有現金（保證金緩衝）",
    )
    buy_field_override = next(
        f for f in embed_override.fields if f.name and "轉入資產" in f.name
    )
    assert buy_field_override.value is not None
    assert "持有現金（保證金緩衝）" in buy_field_override.value
    assert "(Buy To Open)" not in buy_field_override.value


# ==========================================
# 全面重構補強測試：情境識別碼視覺樣式 / 統一 ETF 排除清單 /
# 過期價格修正 / _compute_structural_breakdown_signals / strategy_override 互斥
# ==========================================


@pytest.mark.parametrize(
    "action,sell_ratio,direction",
    [
        ("LIQUIDATE", 1.0, "BTO"),
        ("HOLD", 0.0, "HOLD"),
    ],
)
def test_margin_defense_scenario_always_renders_red(
    action: str, sell_ratio: float, direction: str
) -> None:
    """
    安全性回歸測試：MARGIN_DEFENSE (保證金防禦強制平倉) 無論 action/sell_ratio 為何，
    embed 顏色都必須固定為危急紅色，不可退化為與一般再平衡相同的金色/青色
    (曾因 rollover_type 自由文字未包含「防禦」二字而導致此問題)。
    """
    embed = create_dynamic_rollover_embed(
        rollover_type="槓桿與保證金防禦",
        sell_symbol="NVDA",
        sell_ratio=sell_ratio,
        buy_symbol="BOXX",
        reason="大盤宏觀風控紅線亮起，個股結構無勝率",
        suggested_strategy="STC 100% 轉倉 BOXX",
        suggested_price="Market",
        strike="N/A",
        expiry="N/A",
        direction=direction,
        scenario="MARGIN_DEFENSE",
    )
    assert embed.color == discord.Color(
        0xE74C3C
    ), f"MARGIN_DEFENSE (action={action}) 必須恆為危急紅色，實際為 {embed.color}"
    assert "保證金防禦強制平倉" in str(embed.title)


def test_scenario_style_distinguishes_all_four_scenarios() -> None:
    """
    四大情境必須產生彼此不同的標題/顏色組合，讓交易者能一眼分辨是哪個引擎觸發。
    """
    embeds = {
        scenario: create_dynamic_rollover_embed(
            rollover_type="測試",
            sell_symbol="NVDA",
            sell_ratio=0.5,
            buy_symbol="VOO",
            reason="test",
            suggested_strategy="Buy Shares",
            suggested_price="Market",
            strike="N/A",
            expiry="N/A",
            direction="BTO",
            scenario=scenario,
        )
        for scenario in (
            "OPPORTUNITY_COST",
            "SATELLITE_REBALANCE",
            "MARGIN_DEFENSE",
            "FUNDAMENTAL_BROKEN",
        )
    }
    combos = {(e.title, e.color) for e in embeds.values()}
    assert len(combos) == 4, "四大情境的 (標題, 顏色) 組合必須互不相同"
    # 最危險的兩個情境 (保證金防禦 / 基本面破滅) 必須共享同一種危急紅色
    assert embeds["MARGIN_DEFENSE"].color == embeds["FUNDAMENTAL_BROKEN"].color
    assert embeds["MARGIN_DEFENSE"].color == discord.Color(0xE74C3C)


def test_create_dynamic_rollover_embed_unknown_scenario_falls_back_gracefully() -> None:
    """
    scenario 未傳入 (預設 UNKNOWN) 時應退回舊版子字串比對渲染，維持向下相容，
    而非拋出例外或渲染出無意義的空白標題。
    """
    embed = create_dynamic_rollover_embed(
        rollover_type="原型假設破滅",
        sell_symbol="AMD",
        sell_ratio=1.0,
        buy_symbol="VOO",
        reason="test",
        suggested_strategy="Buy Shares",
        suggested_price="Market",
        strike="N/A",
        expiry="N/A",
        direction="BTO",
    )
    assert embed.color == discord.Color(0xE74C3C)
    assert "破滅" in str(embed.title)


def test_core_defense_etf_symbols_unified_across_engine() -> None:
    """
    CORE_DEFENSE_ETF_SYMBOLS 為單一共用常數，VXX 必須存在於其中，
    確保機會成本轉倉與槓桿保證金防禦不再各自維護分歧的排除清單。
    """
    assert CORE_DEFENSE_ETF_SYMBOLS == frozenset(
        {"QQQ", "SPY", "VOO", "VXX", "IVV", "VTI"}
    )


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="SHORT_GAMMA_CRITICAL",
)
@patch("database.orders.get_user_active_orders", return_value=[])
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_margin_defense_excludes_vxx_as_core(
    mock_ctx: MagicMock,
    mock_orders: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """
    統一常數修正後，VXX 即使被標記為 SATELLITE 且結構無勝率，
    也必須被排除在 BOXX 強制平倉候選之外 (VXX 屬避險工具而非戰術標的)。
    """
    mock_ctx.return_value = MagicMock(cash_reserve=1000.0)
    portfolio = [
        {
            "symbol": "VXX",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "quantity": 10.0,
            "instrument_type": "SPOT",
            "sqz_mom": -5.0,
            "skew": -0.5,
        },
    ]
    result = await engine.evaluate_margin_defense(1, portfolio)
    assert result == []


@pytest.mark.asyncio
async def test_resolve_target_reference_price_uses_market_cache_over_stale_constant(
    engine: DynamicRolloverEngine,
) -> None:
    """
    目標為 VOO/SPY 時應優先讀取 market_cache 的 reference_spot_price，
    而非沿用過期的硬編碼估計值 (曾為 560.0)。
    """
    with patch("database.market_cache.get_market_cache") as mock_cache:
        mock_cache.return_value = {"reference_spot_price": 612.34}
        price = await engine._resolve_target_reference_price("VOO")
        assert price == 612.34
        assert price != 560.0


@pytest.mark.asyncio
async def test_resolve_target_reference_price_uses_market_cache_for_any_target(
    engine: DynamicRolloverEngine,
) -> None:
    """
    非 VOO/SPY 的轉倉目標 (例如 Watchlist 輪動候選標的或 BOXX) 同樣應優先查詢
    market_cache 取得其自身參考價，而非誤用「被賣出資產自身的現價」估計目標
    資產股數 (兩者價格通常無關，例如賣出 NVDA 轉倉 BOXX 絕不能用 NVDA 現價
    估算 BOXX 股數)。
    """
    with patch("database.market_cache.get_market_cache") as mock_cache:
        mock_cache.return_value = {"reference_spot_price": 101.23}
        assert await engine._resolve_target_reference_price("BOXX") == 101.23
        assert await engine._resolve_target_reference_price("SMCI") == 101.23


@pytest.mark.asyncio
async def test_resolve_target_reference_price_falls_back_when_cache_missing(
    engine: DynamicRolloverEngine,
) -> None:
    """
    任何目標快取缺失時一律退回具名備援常數，絕不誤用不相關標的的現價。
    """
    with patch("database.market_cache.get_market_cache", return_value=None):
        assert await engine._resolve_target_reference_price("VOO") == 500.0
        assert await engine._resolve_target_reference_price("SMCI") == 500.0
        assert await engine._resolve_target_reference_price("BOXX") == 500.0


@pytest.mark.asyncio
async def test_compute_structural_breakdown_signals_options_fast_path(
    engine: DynamicRolloverEngine,
) -> None:
    """
    OPTIONS 快速通道：現價貫穿 anchor_base 即時判定破位，不需等待 15m 實體收盤確認
    (不應呼叫 is_gamma_cliff_confirmed)。
    """
    with patch(
        "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
        new_callable=AsyncMock,
    ) as mock_confirm:
        (
            is_breakdown,
            is_whale_block,
            support_wall,
            resistance_wall,
            support_gex,
            resistance_gex,
        ) = await engine._compute_structural_breakdown_signals(
            symbol="AMD",
            spot=90.0,
            put_wall=100.0,
            gamma_flip=0.0,
            atr_14=2.0,
            sqz_mom=1.0,
            skew=0.0,
            price_15m_close=90.0,
            gex_profile_data=None,
            asset_class="OPTIONS",
        )
        assert is_breakdown is True
        assert is_whale_block is False
        mock_confirm.assert_not_called()


@pytest.mark.asyncio
async def test_compute_structural_breakdown_signals_whale_sto_block_only(
    engine: DynamicRolloverEngine,
) -> None:
    """主力空頭封殺 (sqz_mom<0 且 skew<-0.3) 應獨立於結構性破位觸發。"""
    (
        is_breakdown,
        is_whale_block,
        *_rest,
    ) = await engine._compute_structural_breakdown_signals(
        symbol="AMD",
        spot=100.0,
        put_wall=0.0,
        gamma_flip=0.0,
        atr_14=2.0,
        sqz_mom=-2.0,
        skew=-0.5,
        price_15m_close=100.0,
        gex_profile_data=None,
        asset_class="SPOT",
    )
    assert is_breakdown is False  # 無 anchor_base -> 無法判定結構性破位
    assert is_whale_block is True


@pytest.mark.asyncio
async def test_compute_structural_breakdown_signals_neither_triggered(
    engine: DynamicRolloverEngine,
) -> None:
    """健康部位：無結構性破位、無主力空頭封殺。"""
    (
        is_breakdown,
        is_whale_block,
        *_rest,
    ) = await engine._compute_structural_breakdown_signals(
        symbol="AMD",
        spot=110.0,
        put_wall=100.0,
        gamma_flip=95.0,
        atr_14=2.0,
        sqz_mom=5.0,
        skew=0.1,
        price_15m_close=110.0,
        gex_profile_data=None,
        asset_class="SPOT",
    )
    assert is_breakdown is False
    assert is_whale_block is False


def test_evaluate_opportunity_cost_extreme_asymmetric_forces_full_rollover(
    engine: DynamicRolloverEngine,
) -> None:
    """
    條件二「極致不對稱勝率」(低 IVR + 貼近 put_wall + UOA sweep) 觸發時，
    應強制 rollover_ratio=1.0 並採用 "Shares + ITM Call" 策略。
    """
    res = engine.evaluate_opportunity_cost(
        current_holding_symbol="PLTR",
        current_holding_power_squeeze=15.0,  # < 20 (動能衰退)
        current_holding_profit_pct=0.1,
        target_watchlist_symbol="SMCI",
        target_power_squeeze=85.0,  # > 80 (突破待發)
        target_expected_value=0.25,
        current_holding_expected_value=0.10,  # EV spread = 15% > 5%
        target_ivr=20.0,  # 0 < 20 < 30 (低 IVR)
        target_uoa_sweep=True,
        target_spot=100.0,
        target_put_wall=100.5,  # |100-100.5|/100.5 ≈ 0.5% <= 1%
    )
    assert res["should_rollover"] is True
    assert res["rollover_ratio"] == 1.0
    assert res["strategy"] == "Shares + ITM Call"


def test_evaluate_opportunity_cost_blocked_when_ev_spread_below_cost_floor(
    engine: DynamicRolloverEngine,
) -> None:
    """
    #8: EV spread 落在原始 5% 門檻之上、但扣除保守往返交易成本 (0.3%) 後低於
    實質門檻時，不應觸發轉倉，避免扣成本後實質虧損。
    """
    res = engine.evaluate_opportunity_cost(
        current_holding_symbol="PLTR",
        current_holding_power_squeeze=15.0,
        current_holding_profit_pct=0.4,
        target_watchlist_symbol="SMCI",
        target_power_squeeze=85.0,
        target_expected_value=0.152,
        current_holding_expected_value=0.10,  # EV spread = 5.2% > 5% 但 < 5.3% 成本門檻
    )
    assert res["should_rollover"] is False


def test_evaluate_opportunity_cost_extreme_asymmetric_blocked_below_cost_floor(
    engine: DynamicRolloverEngine,
) -> None:
    """
    #8: 極致不對稱勝率分支巢狀於同一 ev_spread 門檻之內，成本地板同樣適用，
    ev_spread 不足時即使低 IVR/貼近 put_wall/UOA sweep 條件齊備也不應強制全倉轉移。
    """
    res = engine.evaluate_opportunity_cost(
        current_holding_symbol="PLTR",
        current_holding_power_squeeze=15.0,
        current_holding_profit_pct=0.1,
        target_watchlist_symbol="SMCI",
        target_power_squeeze=85.0,
        target_expected_value=0.152,
        current_holding_expected_value=0.10,  # EV spread = 5.2%，低於 5.3% 成本門檻
        target_ivr=20.0,
        target_uoa_sweep=True,
        target_spot=100.0,
        target_put_wall=100.5,
    )
    assert res["should_rollover"] is False


def test_apply_ivr_strategy_overlay_override_suppresses_ivr_suffix(
    engine: DynamicRolloverEngine,
) -> None:
    """
    strategy_override 非空時應完全取代 IVR 鎖定後綴邏輯 (elif，而非疊加)，
    即使 IVR 極低本應觸發賣方鎖定後綴，也不得附加該後綴文字。
    """
    with patch(
        "market_analysis.dynamic_rollover.is_selling_locked_by_ivr", return_value=True
    ):
        result = engine._apply_ivr_strategy_overlay(
            options_strategy="100% LIQUIDATE (轉入 VOO)",
            strategy_override="Bear Call Spread (Short Call @ $105.00)",
            ivr=5.0,  # 極低 IVR，若無 override 本應觸發賣方鎖定後綴
        )
        assert result == "Bear Call Spread (Short Call @ $105.00)"
        assert "IVR 極低位" not in result

    # 未傳入 override 時應正常附加 IVR 鎖定後綴
    with patch(
        "market_analysis.dynamic_rollover.is_selling_locked_by_ivr", return_value=True
    ):
        result_no_override = engine._apply_ivr_strategy_overlay(
            options_strategy="100% LIQUIDATE (轉入 VOO)",
            strategy_override="",
            ivr=5.0,
        )
        assert "IVR 極低位" in result_no_override


# ============================================================
# Phase 1 強化測試：target_allocation_pct 生產預設值 / 停損邊界 clamp /
# anchor_base 統一解析 / GEX 重複運算與例外吞噬
# ============================================================


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=False,
)
async def test_check_satellite_rebalancing_default_target_allocation_omitted(
    mock_cliff: AsyncMock, mock_get_user: MagicMock, engine: DynamicRolloverEngine
) -> None:
    """
    模擬 portfolio_monitor.py 真實資料形狀：asset dict 完全不含
    target_allocation_pct key (該欄位無 DB 持久化、無 /settings UI)。
    修正前，呼叫端會塞入預設值 0.0，導致 excess_alloc ≈ current_alloc，
    近乎全清倉卻仍標示 REDUCE；修正後應退回 check_satellite_rebalancing
    既有的 asset.get(..., max_alloc) fallback，只修剪超出上限的部分。
    """
    mock_get_user.return_value = MagicMock(can_trade_spreads=False)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 3500.0,  # 35% of 10000
            "max_allocation_pct": 0.30,
            # 刻意不設定 target_allocation_pct，模擬生產環境真實資料形狀
        },
        {
            "symbol": "VOO",
            "asset_class": "CORE",
            "current_value": 6500.0,
            "max_allocation_pct": 1.0,
        },
    ]
    instructions = await engine.check_satellite_rebalancing(1, portfolio, 10000.0)
    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["symbol"] == "NVDA"
    assert ins["action"] == "REDUCE"
    # excess_alloc = 0.35 - max_alloc(0.30) = 0.05 -> sell_ratio = (0.05*10000)/3500 ≈ 0.14
    # 而非修正前 excess_alloc = 0.35 - 0.0 = 0.35 -> sell_ratio ≈ 1.0 (近乎全清倉)
    assert ins["sell_ratio"] == pytest.approx(0.14, abs=0.01)
    assert ins["sell_ratio"] < 0.5


def test_compute_anti_washout_stop_clamps_floor_when_anchor_far_below_spot(
    engine: DynamicRolloverEngine,
) -> None:
    """
    anchor_base 遠低於現價 (GEX 牆數據異常/過舊) 且 ATR 極小時，機制 2 算出的
    基礎停損應鉗制在 spot*0.95 下限，避免防護過鬆而失去防洗盤意義。
    """
    metrics = {"spot_price": 100.0, "atr_15m": 1.0, "dte": 30, "lvn": 0.0, "hvn": 0.0}
    stop_loss, _limit, is_01dte, _scale = engine._compute_anti_washout_stop(
        anchor_base=80.0, metrics=metrics
    )
    # raw = 80 - 1.5*1.0 = 78.5 -> 遠低於 spot*0.95=95.0 -> 鉗制至 95.0
    assert stop_loss == 95.0
    assert is_01dte is False


def test_compute_anti_washout_stop_clamps_ceiling_when_anchor_close_to_spot(
    engine: DynamicRolloverEngine,
) -> None:
    """
    anchor_base 極貼近現價且 ATR 極小時，機制 2 算出的基礎停損應鉗制在
    spot*0.98 上限，避免防護過緊而提前洗出。
    """
    metrics = {"spot_price": 100.0, "atr_15m": 0.1, "dte": 30, "lvn": 0.0, "hvn": 0.0}
    stop_loss, _limit, _is_01dte, _scale = engine._compute_anti_washout_stop(
        anchor_base=99.0, metrics=metrics
    )
    # raw = 99 - 0.15 = 98.85 -> 高於 spot*0.98=98.0 -> 鉗制至 98.0
    assert stop_loss == 98.0


def test_compute_anti_washout_stop_no_clamp_when_already_breached(
    engine: DynamicRolloverEngine,
) -> None:
    """
    回歸防護：現價已跌破 anchor_base (base_stop_loss >= spot，即雙軌裁決機制
    賴以判定「已破位」的訊號) 時，不得鉗制，否則會把停損拉回現價之下、
    誤將破位訊號抹除 (與 test_dual_track_exit_options_vs_spot 情境一致)。
    """
    metrics = {"spot_price": 95.0, "atr_15m": 2.0, "dte": 10, "lvn": 0.0, "hvn": 0.0}
    stop_loss, _limit, _is_01dte, _scale = engine._compute_anti_washout_stop(
        anchor_base=100.0, metrics=metrics
    )
    # raw = 100 - 1.5*2 = 97.0 > spot(95.0) -> 已處於破位訊號區間，不鉗制
    assert stop_loss == 97.0


def test_compute_anti_washout_stop_01dte_and_lvn_regression_unchanged(
    engine: DynamicRolloverEngine,
) -> None:
    """
    回歸防護：新增的邊界鉗制不得影響既有 0/1 DTE 風險平價與 LVN 吸附機制的
    最終輸出 (兩者刻意驗證超出 [spot*0.95, spot*0.98] 邊界的最終值)。
    """
    stop_01dte, _l1, is_01dte, scale = engine._compute_anti_washout_stop(
        anchor_base=100.0,
        metrics={"spot_price": 100.0, "atr_15m": 2.0, "dte": 1, "lvn": 0.0, "hvn": 0.0},
    )
    assert stop_01dte == 94.0
    assert is_01dte is True
    assert scale == 0.5

    stop_lvn, _l2, _is2, _s2 = engine._compute_anti_washout_stop(
        anchor_base=100.0,
        metrics={
            "spot_price": 100.0,
            "atr_15m": 2.0,
            "dte": 30,
            "lvn": 97.0,
            "secondary_hvn": 94.0,
            "hvn": 94.0,
        },
    )
    assert stop_lvn == 94.40


def test_resolve_canonical_anchor_base_topology_correction() -> None:
    """put_wall/call_wall 顛倒時應取兩者較低值作為防守錨點。"""
    assert (
        _resolve_canonical_anchor_base(
            support_wall=0.0,
            put_wall=210.0,
            call_wall=190.0,
            gamma_flip=205.0,
            hvn=150.0,
            spot=200.0,
        )
        == 190.0
    )


def test_correct_wall_topology_uses_gamma_flip_fallback_consistently_with_structural_signals(
    engine: DynamicRolloverEngine,
) -> None:
    """
    回歸防護：support_wall/put_wall 皆缺失時，_correct_wall_topology (報告顯示層)
    與 _compute_structural_breakdown_signals (清倉判定層) 過去分別維護不同的
    anchor_base 優先序 (前者只有 hvn/spot fallback，從未使用 gamma_flip)，
    導致「為何清倉」與「停損設在哪」使用不同數字。兩者現在共用
    _resolve_canonical_anchor_base，此測試鎖定 _correct_wall_topology 在此
    分支也採用 gamma_flip，而非過去被忽略、直接跳到 spot 的舊行為。
    """
    metrics = {
        "spot_price": 140.0,
        "put_wall": 0.0,
        "call_wall": 0.0,
        "support_wall": 0.0,
        "resistance_wall": 0.0,
        "gamma_flip": 150.0,
        "hvn": 0.0,
    }
    anchor_base, _effective_res_wall = engine._correct_wall_topology(metrics)
    assert anchor_base == 150.0  # 舊版行為會是 spot=140.0，忽略 gamma_flip
    assert anchor_base == _resolve_canonical_anchor_base(
        support_wall=0.0,
        put_wall=0.0,
        call_wall=0.0,
        gamma_flip=150.0,
        hvn=0.0,
        spot=140.0,
    )


@pytest.mark.asyncio
async def test_compute_structural_breakdown_signals_logs_malformed_gex_entries(
    engine: DynamicRolloverEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """
    畸形 GEX profile 項目 (無法轉為 float 的履約價/數值) 應記錄 debug log，
    而非過去的裸 except Exception: pass 靜默吞噬。
    """
    caplog.set_level(logging.DEBUG, logger="market_analysis.dynamic_rollover")
    gex_profile_data = {
        "gex_profile": {
            "not_a_strike": "also_not_a_number",
            "100.0": 500000.0,
        }
    }
    await engine._compute_structural_breakdown_signals(
        symbol="MALFORMED",
        spot=100.0,
        put_wall=0.0,
        gamma_flip=0.0,
        atr_14=1.0,
        sqz_mom=0.0,
        skew=0.0,
        price_15m_close=100.0,
        gex_profile_data=gex_profile_data,
        asset_class="SPOT",
    )
    assert any("解析失敗" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_compute_structural_breakdown_signals_memoizes_within_ttl(
    engine: DynamicRolloverEngine,
) -> None:
    """
    同一 30 分鐘週期內，Scenario 3/4 對同一標的以相同輸入重複呼叫時，
    底層 GEX 逐履約價分類掃描 (classify_gex_wall) 只應實際執行一次。
    """
    gex_profile_data = {"gex_profile": {"95.0": 500000.0, "105.0": -300000.0}}
    common_kwargs: dict[str, Any] = dict(
        symbol="NVDA",
        spot=100.0,
        put_wall=95.0,
        gamma_flip=90.0,
        atr_14=1.0,
        sqz_mom=1.0,
        skew=0.1,
        price_15m_close=100.0,
        gex_profile_data=gex_profile_data,
        asset_class="SPOT",
    )
    with patch(
        "market_analysis.index_microstructure.classify_gex_wall",
        side_effect=lambda val, max_pos, is_heavy_otm_call=False: (
            "SUPPORT_GEX_WALL" if val > 0 else "NEUTRAL"
        ),
    ) as mock_classify:
        result1 = await engine._compute_structural_breakdown_signals(**common_kwargs)
        result2 = await engine._compute_structural_breakdown_signals(**common_kwargs)

    assert result1 == result2
    # 2 個履約價 * 1 次掃描 (而非重複呼叫的 2 次掃描 = 4 次) = 2 次呼叫
    assert mock_classify.call_count == 2


# ---------------------------------------------------------------------------
# Phase 2 強化測試：情境間安全性 (#4 互斥、#5 委託單矛盾檢查、#6 淨額扣抵)
# ---------------------------------------------------------------------------


def test_net_against_existing_order_full_coverage_downgrades_to_zero(
    engine: DynamicRolloverEngine,
) -> None:
    matching_order = {"id": 42, "quantity": 10.0}
    net_ratio, note = engine._net_against_existing_order(1.0, 10.0, matching_order)
    assert net_ratio == 0.0
    assert "#42" in note
    assert "降級為觀察持有" in note


def test_net_against_existing_order_partial_coverage_reduces_ratio(
    engine: DynamicRolloverEngine,
) -> None:
    matching_order = {"id": 7, "quantity": 4.0}
    net_ratio, note = engine._net_against_existing_order(1.0, 10.0, matching_order)
    # requested_qty = 1.0 * 10 = 10, 既有委託覆蓋 4 股 -> net_qty = 6 -> net_ratio = 0.6
    assert net_ratio == 0.6
    assert "#7" in note
    assert "60%" in note


def test_net_against_existing_order_no_matching_order_is_noop(
    engine: DynamicRolloverEngine,
) -> None:
    net_ratio, note = engine._net_against_existing_order(0.5, 10.0, None)
    assert net_ratio == 0.5
    assert note == ""


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="SHORT_GAMMA_CRITICAL",
)
@patch("database.orders.get_user_active_orders", return_value=[])
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_margin_defense_skips_already_flagged_symbols(
    mock_ctx: MagicMock,
    mock_orders: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """#4: 已被 Scenario 2/3 標記過的標的，Scenario 4 應跳過以避免矛盾清倉指令"""
    mock_ctx.return_value = MagicMock(cash_reserve=1000.0)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "quantity": 10.0,
            "instrument_type": "SPOT",
            "sqz_mom": -5.0,
            "skew": -0.5,
        },
    ]
    result = await engine.evaluate_margin_defense(
        1, portfolio, already_flagged_symbols={"NVDA"}
    )
    assert result == []


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="SHORT_GAMMA_CRITICAL",
)
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_margin_defense_warns_on_gtc_buy_conflict(
    mock_ctx: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """#5: 強制清倉標的若有現存 GTC 買入網格委託單，應附加矛盾警示文字（不靜默阻擋）"""
    mock_ctx.return_value = MagicMock(cash_reserve=1000.0)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "quantity": 10.0,
            "instrument_type": "SPOT",
            "sqz_mom": -5.0,
            "skew": -0.5,
        },
    ]
    with patch(
        "database.orders.get_user_active_orders",
        return_value=[
            {
                "symbol": "NVDA",
                "side": "BUY",
                "validity": "GTC_90",
                "quantity": 5.0,
                "limit_price": 300.0,
            }
        ],
    ):
        result = await engine.evaluate_margin_defense(1, portfolio)
    assert len(result) == 1
    assert result[0]["action"] == "LIQUIDATE"
    assert result[0]["sell_ratio"] == 1.0
    assert "委託單矛盾警示" in result[0]["reason"]


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="SHORT_GAMMA_CRITICAL",
)
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_margin_defense_nets_against_existing_sell_order(
    mock_ctx: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """#5/#6: 既有 SELL 委託單已足額覆蓋建議賣出量時，降級為 HOLD 而非疊加下單"""
    mock_ctx.return_value = MagicMock(cash_reserve=1000.0)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "quantity": 10.0,
            "instrument_type": "SPOT",
            "sqz_mom": -5.0,
            "skew": -0.5,
        },
    ]
    with patch(
        "database.orders.get_user_active_orders",
        return_value=[
            {
                "id": 99,
                "symbol": "NVDA",
                "side": "SELL",
                "validity": "DAY",
                "quantity": 10.0,
                "stop_price": 90.0,
                "limit_price": 89.0,
            }
        ],
    ):
        result = await engine.evaluate_margin_defense(1, portfolio)
    assert len(result) == 1
    assert result[0]["action"] == "HOLD"
    assert result[0]["sell_ratio"] == 0.0
    assert "#99" in result[0]["reason"]
    assert "委託單淨額扣抵" in result[0]["reason"]


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=True,
)
@patch(
    "database.orders.get_user_active_orders",
    return_value=[
        {
            "id": 55,
            "symbol": "NVDA",
            "side": "SELL",
            "quantity": 30.0,
            "stop_price": 180.0,
            "limit_price": 179.0,
        }
    ],
)
async def test_check_satellite_rebalancing_liquidate_nets_against_existing_sell_order(
    mock_orders: MagicMock,
    mock_cliff: AsyncMock,
    mock_get_user: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """#6: 結構破位確認且既有委託單已足額覆蓋建議賣出量 -> 降級為 HOLD，避免重複下單"""
    mock_get_user.return_value = MagicMock(can_trade_spreads=False)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "quantity": 30.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.50,
            "spot_price": 200.0,
            "put_wall": 210.0,
            "gamma_flip": 215.0,
            "call_wall": 250.0,
            "ivr": 30.0,
            "is_uoa_sweep": False,
            "max_pain": 220.0,
            "sqz_mom": 0.5,
            "skew": -0.1,
            "price_15m_close": 185.0,
        },
    ]
    instructions = await engine.check_satellite_rebalancing(1, portfolio, 10000.0)
    nvda_instructions = [ins for ins in instructions if ins["symbol"] == "NVDA"]
    assert len(nvda_instructions) == 1
    assert nvda_instructions[0]["action"] == "HOLD"
    assert nvda_instructions[0]["sell_ratio"] == 0.0
    assert "#55" in nvda_instructions[0]["reason"]


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=False,
)
@patch(
    "database.orders.get_user_active_orders",
    return_value=[
        {
            "id": 77,
            "symbol": "NVDA",
            "side": "SELL",
            "quantity": 2.0,
            "stop_price": 180.0,
            "limit_price": 179.0,
        }
    ],
)
async def test_check_satellite_rebalancing_reduce_nets_partial_against_existing_sell_order(
    mock_orders: MagicMock,
    mock_cliff: AsyncMock,
    mock_get_user: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """#6: 常規比例控管 REDUCE 且既有委託單部分覆蓋 -> 賣出比例按淨額調整，不疊加下單"""
    mock_get_user.return_value = MagicMock(can_trade_spreads=False)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 4500.0,
            "quantity": 20.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.30,
        },
        {
            "symbol": "VOO",
            "asset_class": "CORE",
            "current_value": 5500.0,
            "target_allocation_pct": 0.80,
            "max_allocation_pct": 1.0,
        },
    ]
    total_val = 10000.0
    instructions = await engine.check_satellite_rebalancing(1, portfolio, total_val)
    nvda_instructions = [ins for ins in instructions if ins["symbol"] == "NVDA"]
    assert len(nvda_instructions) == 1
    assert nvda_instructions[0]["action"] == "REDUCE"
    # 原始 sell_ratio = round(2500/4500, 2) = 0.56 -> requested_qty = 0.56*20 = 11.2
    # 既有委託覆蓋 2 股 -> net_qty = 9.2 -> net_ratio = 0.46
    assert nvda_instructions[0]["sell_ratio"] == 0.46
    assert "#77" in nvda_instructions[0]["reason"]


# ---------------------------------------------------------------------------
# Phase 3 強化測試：流動性 / 成本閘門 (#7 Bid-Ask 流動性警告)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=True,
)
@patch("database.orders.get_user_active_orders", return_value=[])
async def test_check_satellite_rebalancing_liquidate_flags_illiquid_option_spread(
    mock_orders: MagicMock,
    mock_cliff: AsyncMock,
    mock_get_user: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """#7: 結構破位確認且標的為期權部位、bid/ask 點差過寬時，
    附加流動性警告文字並強制 is_manual_override_required=True（不靜默阻擋清倉指令）。"""
    mock_get_user.return_value = MagicMock(can_trade_spreads=False)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "instrument_type": "OPTIONS_CONTRACT",
            "current_value": 5000.0,
            "quantity": 10.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.50,
            "spot_price": 200.0,
            "put_wall": 210.0,
            "gamma_flip": 215.0,
            "call_wall": 250.0,
            "ivr": 30.0,
            "is_uoa_sweep": False,
            "max_pain": 220.0,
            "sqz_mom": 0.5,
            "skew": -0.1,
            "price_15m_close": 185.0,
            "bid": 1.00,
            "ask": 1.30,  # spread_ratio ≈ 26% > 15%
        },
    ]
    instructions = await engine.check_satellite_rebalancing(1, portfolio, 10000.0)
    nvda_instructions = [ins for ins in instructions if ins["symbol"] == "NVDA"]
    assert len(nvda_instructions) == 1
    assert nvda_instructions[0]["action"] == "LIQUIDATE"
    assert nvda_instructions[0]["is_manual_override_required"] is True
    assert "流動性警告" in nvda_instructions[0]["reason"]


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
    new_callable=AsyncMock,
    return_value=True,
)
@patch("database.orders.get_user_active_orders", return_value=[])
async def test_check_satellite_rebalancing_liquidate_no_warning_when_bid_ask_absent(
    mock_orders: MagicMock,
    mock_cliff: AsyncMock,
    mock_get_user: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """#7: 未提供 bid/ask（近 100% 真實流量現況，預設 0.0）時應優雅降級，
    不判定流動性、不強制人工覆核。"""
    mock_get_user.return_value = MagicMock(can_trade_spreads=False)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "instrument_type": "OPTIONS_CONTRACT",
            "current_value": 5000.0,
            "quantity": 10.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.50,
            "spot_price": 200.0,
            "put_wall": 210.0,
            "gamma_flip": 215.0,
            "call_wall": 250.0,
            "ivr": 30.0,
            "is_uoa_sweep": False,
            "max_pain": 220.0,
            "sqz_mom": 0.5,
            "skew": -0.1,
            "price_15m_close": 185.0,
        },
    ]
    instructions = await engine.check_satellite_rebalancing(1, portfolio, 10000.0)
    nvda_instructions = [ins for ins in instructions if ins["symbol"] == "NVDA"]
    assert len(nvda_instructions) == 1
    assert nvda_instructions[0]["action"] == "LIQUIDATE"
    assert nvda_instructions[0]["is_manual_override_required"] is False
    assert "流動性警告" not in nvda_instructions[0]["reason"]


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="SHORT_GAMMA_CRITICAL",
)
@patch("database.orders.get_user_active_orders", return_value=[])
@patch("market_analysis.dynamic_rollover.get_full_user_context")
async def test_evaluate_margin_defense_warns_on_illiquid_option_spread(
    mock_ctx: MagicMock,
    mock_orders: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """#7: 保證金防禦強制清倉的期權部位若點差過寬，附加流動性警告文字。"""
    mock_ctx.return_value = MagicMock(cash_reserve=1000.0)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "quantity": 10.0,
            "instrument_type": "OPTIONS_CONTRACT",
            "sqz_mom": -5.0,
            "skew": -0.5,
            "bid": 1.00,
            "ask": 1.30,
        },
    ]
    result = await engine.evaluate_margin_defense(1, portfolio)
    assert len(result) == 1
    assert result[0]["action"] == "LIQUIDATE"
    assert "流動性警告" in result[0]["reason"]


# ---------------------------------------------------------------------------
# Phase 4 強化測試：#10 稅務風險資訊性提示 (純附加，不做任何攔截)
# ---------------------------------------------------------------------------


def test_maybe_append_tax_risk_note_covers_both_scenarios(
    engine: DynamicRolloverEngine,
) -> None:
    assert engine._maybe_append_tax_risk_note(False, False) == ""

    note_01dte = engine._maybe_append_tax_risk_note(True, False)
    assert "稅務提醒" in note_01dte
    assert "Assignment" in note_01dte
    assert "Wash Sale" not in note_01dte

    note_reentry = engine._maybe_append_tax_risk_note(False, True)
    assert "稅務提醒" in note_reentry
    assert "Wash Sale" in note_reentry
    assert "Assignment" not in note_reentry

    note_both = engine._maybe_append_tax_risk_note(True, True)
    assert "Assignment" in note_both
    assert "Wash Sale" in note_both


def test_maybe_append_tax_risk_note_holding_period_long_vs_short_term(
    engine: DynamicRolloverEngine,
) -> None:
    """#A2: acquired_at 粗估的長/短期資本利得稅率區間提醒 (單一日期估計，非多批次 FIFO)"""
    long_term_note = engine._maybe_append_tax_risk_note(
        False, False, holding_period_days=400
    )
    assert "長期資本利得" in long_term_note
    assert "短期資本利得" not in long_term_note

    short_term_note = engine._maybe_append_tax_risk_note(
        False, False, holding_period_days=100
    )
    assert "短期資本利得" in short_term_note
    assert "距長期門檻尚餘 265 天" in short_term_note

    # 未提供 holding_period_days 時完全不受影響 (向下相容既有兩個分支)
    assert engine._maybe_append_tax_risk_note(False, False) == ""
    assert engine._maybe_append_tax_risk_note(False, False, None) == ""


@pytest.mark.asyncio
async def test_generate_rule_based_rebalance_report_01dte_liquidate_includes_tax_risk_note(
    engine: DynamicRolloverEngine,
) -> None:
    """#10: 0/1 DTE 合約觸發 LIQUIDATE 時，報告應附加資訊性稅務提醒 (Assignment 風險)"""
    metrics = {
        "spot_price": 100.0,
        "price_15m_close": 90.0,  # 跌破停損，觸發 15m 實體破位 LIQUIDATE
        "support_wall": 100.0,
        "atr_15m": 2.0,
        "dte": 1,  # 0/1 DTE
        "ivr": 25.0,
        "sqz_mom": 1.0,
    }
    report = await engine._generate_rule_based_rebalance_report(
        symbol="XYZ",
        metrics=metrics,
        requested_action="HOLD",
        target="VOO",
        position_shares=100.0,
        current_value=10000.0,
    )
    assert report["final_action"] == "LIQUIDATE"
    assert "稅務提醒" in report["markdown_report"]
    assert "Assignment" in report["markdown_report"]


@pytest.mark.asyncio
async def test_generate_rule_based_rebalance_report_hold_omits_tax_risk_note(
    engine: DynamicRolloverEngine,
) -> None:
    """#10: 非 0/1 DTE LIQUIDATE 情境不應附加稅務提醒 (避免資訊過載)"""
    metrics = {
        "spot_price": 100.0,
        "price_15m_close": 100.0,
        "support_wall": 100.0,
        "atr_15m": 2.0,
        "dte": 30,
        "ivr": 25.0,
        "sqz_mom": 1.0,
    }
    report = await engine._generate_rule_based_rebalance_report(
        symbol="XYZ",
        metrics=metrics,
        requested_action="HOLD",
        target="VOO",
        position_shares=100.0,
        current_value=10000.0,
    )
    assert "稅務提醒" not in report["markdown_report"]


@pytest.mark.asyncio
async def test_generate_rule_based_rebalance_report_includes_holding_period_note_on_liquidate(
    engine: DynamicRolloverEngine,
) -> None:
    """#A2: metrics 帶有 acquired_at 且觸發實體破位 LIQUIDATE 時，報告應附加
    長/短期資本利得稅率區間提醒（單一 acquired_at 粗估，非多批次 FIFO）。"""
    from datetime import datetime, timedelta

    acquired_at = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
    metrics = {
        "spot_price": 90.0,
        "price_15m_close": 90.0,  # 跌破停損，觸發 15m 實體破位 LIQUIDATE
        "support_wall": 100.0,
        "atr_15m": 2.0,
        "dte": 30,
        "ivr": 25.0,
        "sqz_mom": -1.0,
        "acquired_at": acquired_at,
    }
    report = await engine._generate_rule_based_rebalance_report(
        symbol="XYZ",
        metrics=metrics,
        requested_action="HOLD",
        target="VOO",
        position_shares=100.0,
        current_value=10000.0,
    )
    assert report["final_action"] == "LIQUIDATE"
    assert "稅務提醒" in report["markdown_report"]
    assert "長期資本利得" in report["markdown_report"]


@pytest.mark.asyncio
async def test_generate_rule_based_rebalance_report_omits_holding_period_note_when_no_acquired_at(
    engine: DynamicRolloverEngine,
) -> None:
    """未設定 acquired_at (例如尚未透過 /add_holding 記錄) 時，不應假造持有天數。"""
    metrics = {
        "spot_price": 90.0,
        "price_15m_close": 90.0,
        "support_wall": 100.0,
        "atr_15m": 2.0,
        "dte": 30,
        "ivr": 25.0,
        "sqz_mom": -1.0,
    }
    report = await engine._generate_rule_based_rebalance_report(
        symbol="XYZ",
        metrics=metrics,
        requested_action="HOLD",
        target="VOO",
        position_shares=100.0,
        current_value=10000.0,
    )
    assert report["final_action"] == "LIQUIDATE"
    assert "稅務提醒" not in report["markdown_report"]


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.get_full_user_context")
@patch(
    "market_analysis.dynamic_rollover.DynamicRolloverEngine._find_best_rollover_target",
    return_value="AMD",
)
async def test_satellite_rebalancing_euphoria_exhaustion_includes_wash_sale_note(
    mock_target: MagicMock,
    mock_get_user: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """#10: Euphoria 雙軌機制留存部位開 Bear Call Spread (同標的重新建倉)
    應附加資訊性 Wash Sale 提醒"""
    mock_get_user.return_value = MagicMock(can_trade_spreads=True)
    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "current_value": 5000.0,
            "target_allocation_pct": 0.20,
            "max_allocation_pct": 0.50,
            "spot_price": 249.0,
            "put_wall": 210.0,
            "gamma_flip": 215.0,
            "call_wall": 250.0,
            "ivr": 30.0,
            "is_uoa_sweep": False,
            "max_pain": 220.0,
            "sqz_mom": -0.8,
            "skew": -0.05,
            "skew_percentile": 40.0,
        },
    ]
    instructions = await engine.check_satellite_rebalancing(1, portfolio, 10000.0)
    ins_10 = [i for i in instructions if i["sell_ratio"] == 0.1][0]
    assert "稅務提醒" in ins_10["reason"]
    assert "Wash Sale" in ins_10["reason"]


# ---------------------------------------------------------------------------
# 進場訊號六重嚴格過濾鐵律：_scan_gex_walls / _confirm_entry_signal
# ---------------------------------------------------------------------------


def test_scan_gex_walls_finds_support_and_resistance() -> None:
    gex_profile_data = {
        "gex_profile": {"90": -50.0, "95": 80.0, "100": 30.0, "105": -20.0}
    }
    support_wall, resistance_wall, support_gex, resistance_gex = _scan_gex_walls(
        "TEST", gex_profile_data
    )
    assert support_wall == 95.0
    assert support_gex == 80.0
    assert resistance_wall == 105.0
    assert resistance_gex == -20.0


def test_scan_gex_walls_missing_profile_returns_zeros() -> None:
    assert _scan_gex_walls("TEST", None) == (0.0, 0.0, 0.0, 0.0)
    assert _scan_gex_walls("TEST", {"put_wall": 100.0}) == (0.0, 0.0, 0.0, 0.0)


def test_scan_gex_walls_logs_malformed_entries(caplog: Any) -> None:
    gex_profile_data = {"gex_profile": {"bad_strike": "bad_value", "100": 50.0}}
    with caplog.at_level(logging.DEBUG, logger="market_analysis.dynamic_rollover"):
        support_wall, _, support_gex, _ = _scan_gex_walls("TEST", gex_profile_data)
    assert support_wall == 100.0
    assert support_gex == 50.0
    assert any("解析失敗" in record.message for record in caplog.records)


def _make_15m_df(bars: list[tuple[float, float]]) -> pd.DataFrame:
    """建構模擬的 15 分鐘 K 線 DataFrame，最後一筆視為待確認的收盤根。"""
    closes = [c for c, _ in bars]
    volumes = [v for _, v in bars]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 0.5 for c in closes],
            "Low": [c - 0.5 for c in closes],
            "Close": closes,
            "Volume": volumes,
        }
    )


def _green_candidate_radar() -> dict:
    """四重過濾鐵律全數通過的候選標的基準 fixture：
    - gex_profile 累積曝險在 $95 由負轉正 (Gamma Flip 估算 = 95.0)，
      且 $95 亦為全鏈最大正 GEX (支撐牆)。
    - call_wall $110，距現價 $100 有 10% 空間 (>= 5% 門檻)。
    - uoa 僅含一筆次週 CALL BTO (DTE=14)，無 STO Call 封頂。
    """
    far_expiry = (datetime.now().date() + timedelta(days=14)).strftime("%Y-%m-%d")
    return {
        "quote": {"c": 100.0},
        "gex_profile_data": {
            "call_wall": 110.0,
            "put_wall": 95.0,
            "gex_profile": {"90": -50.0, "95": 80.0, "100": 30.0, "105": -20.0},
        },
        "uoa": [
            {
                "type": "CALL",
                "action": "🟢 買入開倉 (BTO - Ask)",
                "strike": 105.0,
                "ratio": 2.5,
                "expiry": far_expiry,
            }
        ],
    }


_GREEN_15M_DF = _make_15m_df([(98.0, 1000.0)] * 20 + [(101.0, 1500.0)])


_FAR_EXPIRIES = [(datetime.now().date() + timedelta(days=14)).strftime("%Y-%m-%d")]


@pytest.mark.asyncio
@patch("database.calendar_cache.get_cached_earnings", return_value=None)
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="NORMAL",
)
@patch(
    "services.market_data_service.get_all_option_expiries",
    new_callable=AsyncMock,
    return_value=_FAR_EXPIRIES,
)
async def test_confirm_entry_signal_all_six_conditions_pass(
    mock_expiries: AsyncMock,
    mock_regime: AsyncMock,
    mock_earnings: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=_GREEN_15M_DF,
    ):
        confirmed, reason = await engine._confirm_entry_signal(
            "TEST", _green_candidate_radar(), 100.0
        )
    assert confirmed is True
    assert "條件一✅" in reason
    assert "條件二✅" in reason
    assert "條件三✅" in reason
    assert "條件四✅" in reason
    assert "條件五✅" in reason
    assert "條件六✅" in reason


@pytest.mark.asyncio
async def test_confirm_entry_signal_condition1_fails_no_volume_surge(
    engine: DynamicRolloverEngine,
) -> None:
    """條件一：15m 收盤突破門檻但未放量 -> 未通過"""
    flat_volume_df = _make_15m_df([(98.0, 1000.0)] * 20 + [(101.0, 1000.0)])
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=flat_volume_df,
    ):
        confirmed, reason = await engine._confirm_entry_signal(
            "TEST", _green_candidate_radar(), 100.0
        )
    assert confirmed is False
    assert "條件一❌" in reason
    assert "條件二✅" in reason
    assert "條件三✅" in reason
    assert "條件四✅" in reason


@pytest.mark.asyncio
async def test_confirm_entry_signal_condition1_fails_close_below_threshold(
    engine: DynamicRolloverEngine,
) -> None:
    """條件一：放量但實體收盤未站穩門檻 (仍在 Gamma Flip 之下) -> 未通過"""
    weak_close_df = _make_15m_df([(98.0, 1000.0)] * 20 + [(90.0, 1500.0)])
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=weak_close_df,
    ):
        confirmed, reason = await engine._confirm_entry_signal(
            "TEST", _green_candidate_radar(), 100.0
        )
    assert confirmed is False
    assert "條件一❌" in reason


@pytest.mark.asyncio
async def test_confirm_entry_signal_condition1_fails_gamma_flip_unavailable(
    engine: DynamicRolloverEngine,
) -> None:
    """條件一 fail-safe：GEX Profile 無零交叉點 (單一正值履約價) -> Gamma Flip
    無法估算，直接判定條件一未通過 (不發動 15m 抓取)；條件二仍可通過
    (該履約價本身即為支撐牆)。"""
    radar = _green_candidate_radar()
    radar["gex_profile_data"]["gex_profile"] = {"100": 50.0}
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
    ) as mock_history:
        confirmed, reason = await engine._confirm_entry_signal("TEST", radar, 100.0)
    assert confirmed is False
    assert "條件一❌" in reason
    assert "無法估算 Gamma Flip" in reason
    assert "條件二✅" in reason
    mock_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_entry_signal_condition1_fails_on_fetch_exception(
    engine: DynamicRolloverEngine,
) -> None:
    """條件一 fail-safe：15m K 線抓取拋例外 -> 未通過 (不預設通過)"""
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        side_effect=Exception("network error"),
    ):
        confirmed, reason = await engine._confirm_entry_signal(
            "TEST", _green_candidate_radar(), 100.0
        )
    assert confirmed is False
    assert "條件一❌" in reason


@pytest.mark.asyncio
async def test_confirm_entry_signal_condition2_fails_no_support_wall(
    engine: DynamicRolloverEngine,
) -> None:
    """條件二：GEX Profile 全數為負，無正 Gamma 支撐牆 -> 未通過"""
    radar = _green_candidate_radar()
    radar["gex_profile_data"]["gex_profile"] = {"90": -10.0, "95": -20.0}
    confirmed, reason = await engine._confirm_entry_signal("TEST", radar, 100.0)
    assert confirmed is False
    assert "條件二❌" in reason


@pytest.mark.asyncio
async def test_confirm_entry_signal_condition3_fails_physical_cap(
    engine: DynamicRolloverEngine,
) -> None:
    """條件三：上方存在單筆 ratio > 1.0x OI 的 STO Call -> 未通過"""
    radar = _green_candidate_radar()
    radar["uoa"].append(
        {
            "type": "CALL",
            "action": "🔴 賣出開倉 (STO - Bid)",
            "strike": 103.0,
            "ratio": 1.5,
            "expiry": (datetime.now().date() + timedelta(days=14)).strftime("%Y-%m-%d"),
        }
    )
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=_GREEN_15M_DF,
    ):
        confirmed, reason = await engine._confirm_entry_signal("TEST", radar, 100.0)
    assert confirmed is False
    assert "條件三❌" in reason
    assert "物理封頂" in reason
    assert "條件一✅" in reason
    assert "條件四✅" in reason


@pytest.mark.asyncio
async def test_confirm_entry_signal_condition3_fails_tight_call_wall(
    engine: DynamicRolloverEngine,
) -> None:
    """條件三：Call Wall 過近現價 (< 5% 空間) -> 未通過"""
    radar = _green_candidate_radar()
    radar["gex_profile_data"]["call_wall"] = 102.0  # (102-100)/100 = 2% < 5%
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=_GREEN_15M_DF,
    ):
        confirmed, reason = await engine._confirm_entry_signal("TEST", radar, 100.0)
    assert confirmed is False
    assert "條件三❌" in reason
    assert "空間" in reason


@pytest.mark.asyncio
async def test_confirm_entry_signal_condition4_fails_no_bullish_call(
    engine: DynamicRolloverEngine,
) -> None:
    """條件四：無驅動進場的主力 CALL BTO 買盤 -> 未通過"""
    radar = _green_candidate_radar()
    radar["uoa"] = []
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=_GREEN_15M_DF,
    ):
        confirmed, reason = await engine._confirm_entry_signal("TEST", radar, 100.0)
    assert confirmed is False
    assert "條件四❌" in reason
    assert "條件一✅" in reason
    assert "條件三✅" in reason


@pytest.mark.asyncio
async def test_confirm_entry_signal_condition4_fails_dte_too_low(
    engine: DynamicRolloverEngine,
) -> None:
    """條件四：主力 CALL BTO 買盤來自末日合約 (DTE < 7) -> 未通過"""
    radar = _green_candidate_radar()
    near_expiry = (datetime.now().date() + timedelta(days=2)).strftime("%Y-%m-%d")
    radar["uoa"] = [
        {
            "type": "CALL",
            "action": "🟢 買入開倉 (BTO - Ask)",
            "strike": 102.0,
            "ratio": 3.0,
            "expiry": near_expiry,
        }
    ]
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=_GREEN_15M_DF,
    ):
        confirmed, reason = await engine._confirm_entry_signal("TEST", radar, 100.0)
    assert confirmed is False
    assert "條件四❌" in reason


@pytest.mark.asyncio
@patch("database.calendar_cache.get_cached_earnings", return_value=None)
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="NORMAL",
)
@patch(
    "services.market_data_service.get_all_option_expiries",
    new_callable=AsyncMock,
)
async def test_confirm_entry_signal_condition6_fails_0dte(
    mock_expiries: AsyncMock,
    mock_regime: AsyncMock,
    mock_earnings: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """條件六：candidate 自身最近效期選擇權週期為 0DTE (今日到期) -> 未通過，
    即使驅動進場的主力 UOA 買盤本身是遠月合約 (條件四仍通過)。"""
    mock_expiries.return_value = [datetime.now().date().strftime("%Y-%m-%d")]
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=_GREEN_15M_DF,
    ):
        confirmed, reason = await engine._confirm_entry_signal(
            "TEST", _green_candidate_radar(), 100.0
        )
    assert confirmed is False
    assert "條件四✅" in reason
    assert "條件六❌" in reason
    assert "DTE=0" in reason


@pytest.mark.asyncio
@patch("database.calendar_cache.get_cached_earnings", return_value=None)
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="NORMAL",
)
@patch(
    "services.market_data_service.get_all_option_expiries",
    new_callable=AsyncMock,
    return_value=[],
)
async def test_confirm_entry_signal_condition6_fails_no_expiries(
    mock_expiries: AsyncMock,
    mock_regime: AsyncMock,
    mock_earnings: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """條件六 fail-safe：無法取得標的自身選擇權到期日清單 -> 未通過 (不預設通過)"""
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=_GREEN_15M_DF,
    ):
        confirmed, reason = await engine._confirm_entry_signal(
            "TEST", _green_candidate_radar(), 100.0
        )
    assert confirmed is False
    assert "條件六❌" in reason
    assert "無法取得" in reason


@pytest.mark.asyncio
@patch(
    "market_analysis.dynamic_rollover.DynamicRolloverEngine._confirm_entry_signal",
    new_callable=AsyncMock,
    return_value=(False, "mocked: entry not confirmed"),
)
@patch("database.market_cache.get_market_cache")
async def test_evaluate_opportunity_cost_for_satellites_blocked_by_entry_gate(
    mock_cache: MagicMock,
    mock_entry_gate: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """即使 EV/PSQ 判斷會觸發轉倉，進場訊號四重過濾未通過時應靜默略過，
    不產生任何指令。"""

    def cache_side_effect(symbol: str, expiry: str = None):  # type: ignore
        if symbol.upper() == "NVDA":
            return {
                "reference_spot_price": 200.0,
                "expected_move_upper": 205.0,
                "is_stale": 0,
                "is_degraded": 0,
            }
        if symbol.upper() == "SMCI":
            return {
                "reference_spot_price": 40.0,
                "expected_move_upper": 50.0,
                "is_stale": 0,
                "is_degraded": 0,
            }
        return None

    mock_cache.side_effect = cache_side_effect

    portfolio = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "spot_price": 240.0,
            "avg_cost": 200.0,
            "psq_result": {"squeeze_level": "Release", "signal_direction": "Neutral"},
        },
    ]
    candidate_radar = {
        "psq_result": {
            "squeeze_level": "High",
            "signal_direction": "Long",
            "is_breakout_long": True,
        },
        "quote": {"c": 40.0},
        "iv_metrics": {"iv_rank": 20.0},
        "gex_profile_data": {"put_wall": 0.0},
        "uoa": [],
    }

    result = await engine.evaluate_opportunity_cost_for_satellites(
        1, portfolio, set(), "SMCI", candidate_radar
    )
    assert result == []
    mock_entry_gate.assert_awaited_once()


@patch(
    "database.market_cache.get_market_cache",
    return_value={"reference_spot_price": 100.0, "expected_move_upper": 110.0},
)
def test_skew_adjusted_ev_proxy_downside_penalty(
    mock_cache: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """測試 Skew-Adjusted EV：當 Skew Percentile < 50%（市場定價極端下行尾部風險）時，EV 應依據 Skew 進行折價扣減。"""
    # 正常無 Skew 恐慌的情境 (skew_percentile = 70.0%)
    ev_normal = engine._calculate_ev_proxy(
        symbol="TEST",
        skew_percentile=70.0,
    )
    assert ev_normal == pytest.approx(0.10, rel=1e-3)

    # 存在下行尾部恐慌 (skew_percentile = 30.0%)
    # penalty = (50 - 30) / 50 * 0.5 = 0.20
    # adjusted_ev = 0.10 * (1 - 0.20) = 0.08
    ev_penalized = engine._calculate_ev_proxy(
        symbol="TEST",
        skew_percentile=30.0,
    )
    assert ev_penalized == pytest.approx(0.08, rel=1e-3)
    assert ev_penalized < ev_normal


@patch("database.watchlist.get_user_watchlist", return_value=[("NVDA", None)])
@patch("database.calendar_cache.get_cached_earnings")
def test_find_best_rollover_target_filters_earnings_pre_event(
    mock_earnings: MagicMock,
    mock_watchlist: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """測試候選標的預篩選：3 天內即將發布財報的標的應被過濾，防止跳空雙殺。"""
    from datetime import datetime, timedelta

    near_earnings_date = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    mock_earnings.return_value = {"earnings_date": near_earnings_date}

    best_sym = engine._find_best_rollover_target(user_id=1, exclude_symbols={"AMD"})
    assert best_sym == "VOO"


@pytest.mark.asyncio
@patch("database.calendar_cache.get_cached_earnings", return_value=None)
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="SHORT_GAMMA_CRITICAL",
)
async def test_confirm_entry_signal_condition5_macro_regime_fails(
    mock_regime: AsyncMock,
    mock_earn: MagicMock,
    engine: DynamicRolloverEngine,
) -> None:
    """測試進場閘門條件五：當大盤處於 SHORT_GAMMA_CRITICAL 負 Gamma 踩踏模式時，嚴禁開倉個股買方。"""
    import pandas as pd

    radar = _green_candidate_radar()
    # 確保條件一至四皆能通過 (last bar volume 30000 >= lookback mean 10000 * 1.5)
    df_15m = pd.DataFrame(
        {
            "Close": [105.0] * 25,
            "Volume": [10000.0] * 24 + [30000.0],
        }
    )
    with patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=df_15m,
    ):
        confirmed, reason = await engine._confirm_entry_signal("TEST", radar, 100.0)
    assert confirmed is False
    assert "條件五❌" in reason
    assert "SHORT_GAMMA_CRITICAL" in reason


@pytest.mark.asyncio
@patch(
    "market_analysis.index_microstructure.get_market_regime",
    new_callable=AsyncMock,
    return_value="SYSTEMIC_LIQUIDITY_CRISIS",
)
@patch("database.orders.get_user_active_orders")
@patch("database.user_settings.get_full_user_context")
async def test_margin_defense_cash_deficit_restores_cash(
    mock_user_ctx: MagicMock,
    mock_orders: MagicMock,
    mock_regime: AsyncMock,
    engine: DynamicRolloverEngine,
) -> None:
    """測試保證金防禦：當存在 GTC 買單現金赤字時，清倉資產應建議保留為 CASH 以補足現金儲備消除追繳風險。"""
    portfolio = [
        {
            "symbol": "AMD",
            "asset_class": "SATELLITE",
            "quantity": 100,
            "spot_price": 100.0,
            "current_value": 10000.0,
            "put_wall": 110.0,  # 跌破 put_wall (100 < 110) -> is_no_edge = True
            "gamma_flip": 110.0,
            "atr_14": 2.0,
            "price_15m_close": 95.0,  # 實體破位
        }
    ]
    # GTC 買單需要 5000，但用戶現金儲備為 0 -> total_deficit = 5000 > 0
    mock_orders.return_value = [
        {
            "symbol": "NVDA",
            "validity": "GTC",
            "side": "BUY",
            "limit_price": 50.0,
            "quantity": 100,
        }
    ]
    mock_ctx = MagicMock()
    mock_ctx.cash_reserve = 0.0
    mock_user_ctx.return_value = mock_ctx

    with patch(
        "market_analysis.dynamic_rollover.is_gamma_cliff_confirmed",
        new_callable=AsyncMock,
        return_value=True,
    ):
        instructions = await engine.evaluate_margin_defense(
            user_id=1,
            portfolio_assets=portfolio,
        )

    assert len(instructions) == 1
    ins = instructions[0]
    assert ins["symbol"] == "AMD"
    assert ins["target_core"] == "CASH"
    assert "保留現金" in ins["buy_action_label"]
    assert "消除保證金追繳風險" in ins["suggested_strategy"]


def test_create_dynamic_rollover_embed_hold_and_liquidate_headers() -> None:
    """測試 Embed 呈現層：HOLD 狀態與 LIQUIDATE 狀態的清晰文案與 ANSI 排版。"""
    # 測試 HOLD 狀態
    embed_hold = create_dynamic_rollover_embed(
        rollover_type="持倉防守評估",
        sell_symbol="AMD",
        sell_ratio=0.0,
        buy_symbol="AMD",
        reason="做市商支撐完好，15m 實體收盤未跌破防守線",
        suggested_strategy="HOLD (維持現狀續抱)",
        suggested_price="N/A",
        strike="N/A",
        expiry="N/A",
        direction="HOLD",
        scenario="SATELLITE_REBALANCE",
    )
    assert "安全續抱" in str(embed_hold.description)
    assert "HOLD" in str(embed_hold.fields[0].value)

    # 測試 LIQUIDATE 狀態
    embed_liq = create_dynamic_rollover_embed(
        rollover_type="核心衛星再平衡",
        sell_symbol="AMD",
        sell_ratio=1.0,
        buy_symbol="VOO",
        reason="15m 實體破位確認",
        suggested_strategy="Buy Shares",
        suggested_price="Market",
        strike="N/A",
        expiry="N/A",
        direction="BTO",
        scenario="SATELLITE_REBALANCE",
        cash_impact="$43,524",
    )
    assert "執行轉倉指令" in str(embed_liq.description)
    assert "100%" in str(embed_liq.fields[0].value)
    assert "$43,524" in str(embed_liq.fields[2].value)
