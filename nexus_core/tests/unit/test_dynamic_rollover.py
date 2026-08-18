import discord
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from market_analysis.dynamic_rollover import (
    DynamicRolloverEngine,
    FundamentalThesisResult,
    CORE_DEFENSE_ETF_SYMBOLS,
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
        requested_action="HOLD",
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
        requested_action="HOLD",
        asset_class="SPOT",
    )
    assert report_spot["final_action"] == "HOLD"
    assert "15m 實體 K 線過濾" in report_spot["markdown_report"]

    # 期權 OPTIONS: 現價貫穿 Stop Loss (95.0 < 97.0) -> 3-5m 快速通道即時清倉 LIQUIDATE (拒絕等待 15m)
    report_options = engine._generate_rule_based_rebalance_report(
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
@patch("database.market_cache.get_market_cache")
async def test_evaluate_opportunity_cost_for_satellites_triggers(
    mock_cache: MagicMock, engine: DynamicRolloverEngine
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


def test_resolve_target_reference_price_uses_market_cache_over_stale_constant(
    engine: DynamicRolloverEngine,
) -> None:
    """
    目標為 VOO/SPY 時應優先讀取 market_cache 的 reference_spot_price，
    而非沿用過期的硬編碼估計值 (曾為 560.0)。
    """
    with patch("database.market_cache.get_market_cache") as mock_cache:
        mock_cache.return_value = {"reference_spot_price": 612.34}
        price = engine._resolve_target_reference_price("VOO", fallback_spot=500.0)
        assert price == 612.34
        assert price != 560.0


def test_resolve_target_reference_price_falls_back_when_cache_missing(
    engine: DynamicRolloverEngine,
) -> None:
    """
    VOO/SPY 目標快取缺失時，比照原始程式碼行為 (原硬編碼 560.0 的分支從不採用
    該資產自身現價)，退回具名備援常數，而非誤用不相關的 fallback_spot。
    """
    with patch("database.market_cache.get_market_cache", return_value=None):
        assert engine._resolve_target_reference_price("VOO", fallback_spot=555.0) == (
            500.0
        )
    # 非 VOO/SPY 目標一律使用自身現價 (與快取無關，維持原始 else 分支行為)
    assert engine._resolve_target_reference_price("SMCI", fallback_spot=40.0) == 40.0
    # 非 VOO/SPY 且現價也缺失時，使用同一具名備援常數
    assert engine._resolve_target_reference_price("SMCI", fallback_spot=0.0) == 500.0


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
