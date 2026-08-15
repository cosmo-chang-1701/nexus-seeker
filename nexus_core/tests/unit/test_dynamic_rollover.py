import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from market_analysis.dynamic_rollover import (
    DynamicRolloverEngine,
    FundamentalThesisResult,
)
from cogs.embed_builders.rollover_embeds import (
    create_dynamic_rollover_embed,
    create_thesis_passed_embed,
)


@pytest.fixture
def engine() -> DynamicRolloverEngine:
    return DynamicRolloverEngine()


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


@pytest.mark.asyncio
@patch("market_analysis.dynamic_rollover.is_memory_safe", return_value=False)
async def test_evaluate_fundamental_thesis_memory_unsafe(
    mock_mem: MagicMock, engine: DynamicRolloverEngine
) -> None:
    res = await engine.evaluate_fundamental_thesis("AMD", "Bad news")
    assert res is None


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
    """rollover embed 的 reason field value ≤ 1024 字元上限。"""
    long_reason = "A" * 2000
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
    )
    reason_field = embed.fields[0]
    assert reason_field.value is not None
    assert len(reason_field.value) <= 1024


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


def test_generate_rule_based_rebalance_report_grayscale_hold(
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

    report = engine._generate_rule_based_rebalance_report(
        symbol="AMD",
        metrics=metrics,
        system_action="HOLD",
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


def test_generate_rule_based_rebalance_report_hard_breakdown(
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

    report = engine._generate_rule_based_rebalance_report(
        symbol="AMD",
        metrics=metrics,
        system_action="HOLD",
        target="VOO",
        position_shares=195.0,
        current_value=43524.0,
    )

    assert report["final_action"] == "LIQUIDATE"
    assert report["final_target"] == "VOO"
    assert "15m 實體破位確認" in report["markdown_report"]
    assert "100% LIQUIDATE (轉入 VOO)" in report["options_strategy"]
    assert "$43,524" in report["markdown_report"]


def test_generate_rule_based_rebalance_report_dynamic_generic_ticker(
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

    report = engine._generate_rule_based_rebalance_report(
        symbol="NVDA",
        metrics=metrics,
        system_action="HOLD",
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


def test_01dte_risk_parity_position_sizing(engine: DynamicRolloverEngine) -> None:
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
    report = engine._generate_rule_based_rebalance_report(
        symbol="XYZ",
        metrics=metrics,
        system_action="HOLD",
        target="VOO",
        position_shares=100.0,
        current_value=10000.0,
    )
    # VOO est price = 560. 10000 / 560 = 17 shares. With 50% risk parity scale = 8 shares.
    assert "0/1 DTE 風險平價" in report["markdown_report"]
    assert "削減 50%" in report["markdown_report"]
    assert "3.0 × ATR_15m" in report["markdown_report"]
    assert "$94.00" in report["markdown_report"]


def test_lvn_secondary_hvn_snapping(engine: DynamicRolloverEngine) -> None:
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
    report = engine._generate_rule_based_rebalance_report(
        symbol="XYZ",
        metrics=metrics,
        system_action="HOLD",
        target="VOO",
        position_shares=100.0,
        current_value=10000.0,
    )
    assert "$94.40" in report["markdown_report"]


def test_dual_track_exit_options_vs_spot(engine: DynamicRolloverEngine) -> None:
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
    report_spot = engine._generate_rule_based_rebalance_report(
        symbol="XYZ",
        metrics=metrics_a,
        system_action="HOLD",
        asset_class="SPOT",
    )
    assert report_spot["final_action"] == "HOLD"
    assert "15m 實體 K 線過濾" in report_spot["markdown_report"]

    # 期權 OPTIONS: 現價貫穿 Stop Loss (95.0 < 97.0) -> 3-5m 快速通道即時清倉 LIQUIDATE (拒絕等待 15m)
    report_options = engine._generate_rule_based_rebalance_report(
        symbol="XYZ",
        metrics=metrics_a,
        system_action="HOLD",
        asset_class="OPTIONS",
    )
    assert report_options["final_action"] == "LIQUIDATE"
    assert "期權雙軌快速通道觸發" in report_options["markdown_report"]
    assert "3-5m 快速通道" in report_options["markdown_report"]
