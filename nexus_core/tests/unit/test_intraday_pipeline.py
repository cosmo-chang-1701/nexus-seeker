import pytest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from market_analysis.intraday_pipeline import (
    IntradayScanPipeline,
    TraderAccountState,
    OptionHolding,
    TickerMarketData,
    NexusGammaSqueezeEngine,
    AdvancedTraderOutput,
)


@pytest.fixture
def squeeze_engine():
    return NexusGammaSqueezeEngine(base_gate_3_threshold=1000000.0)


@pytest.fixture
def intraday_pipeline(squeeze_engine):
    return IntradayScanPipeline(MagicMock(), squeeze_engine)


@pytest.fixture
def default_account_state():
    return TraderAccountState(
        capital=100000.0,
        cash_reserve=25000.0,
        monthly_burn_rate=6000.0,
        current_vix=14.0,
    )


@pytest.fixture
def default_market_data():
    return TickerMarketData(
        ticker="AAPL",
        spot_price=173.5,
        market_cap_billion=250.0,
        avg_option_volume=80000,
        days_until_earnings=10,
        tomorrow_expiring_otm_calls_premium=1200000.0,
        iv_rank=60.0,
        option_skew=0.08,
    )


@pytest.fixture
def default_holdings():
    return [
        OptionHolding(symbol="AAPL", quantity=2.0, theta=-0.12),
        OptionHolding(symbol="MSFT", quantity=-1.0, theta=0.08),
    ]


@pytest.fixture
def default_greeks():
    return {"vanna": 1.5, "beta": 1.2}


def test_validate_gates_all_pass(squeeze_engine, default_market_data):
    passed, failed = squeeze_engine.validate_gates(default_market_data, "Phase B")
    assert passed is True
    assert len(failed) == 0


def test_validate_gates_failures(squeeze_engine):
    bad_data = TickerMarketData(
        ticker="LOW_LIQ",
        spot_price=50.0,
        market_cap_billion=10.0,  # Fail (< 20B)
        avg_option_volume=10000,  # Fail (< 50,000)
        days_until_earnings=2,  # Fail (<= 3)
        tomorrow_expiring_otm_calls_premium=500000.0,  # Fail (< 1M)
        iv_rank=30.0,  # Fail (< 50)
        option_skew=0.02,  # Fail (< 0.05 skew absolute)
    )
    passed, failed = squeeze_engine.validate_gates(bad_data, "Phase B")
    assert passed is False
    assert len(failed) == 4  # Liquidity, Event, Efficiency, Cross-Market


def test_validate_gates_phase_a_reduction(squeeze_engine):
    # In Phase A, threshold is reduced to 70% ($700,000)
    # If premium is $800,000, it should pass in Phase A but fail in Phase B
    borderline_data = TickerMarketData(
        ticker="BORDER",
        spot_price=100.0,
        market_cap_billion=50.0,
        avg_option_volume=60000,
        days_until_earnings=15,
        tomorrow_expiring_otm_calls_premium=800000.0,
        iv_rank=70.0,
        option_skew=0.06,
    )

    passed_a, failed_a = squeeze_engine.validate_gates(borderline_data, "Phase A")
    assert passed_a is True
    assert len(failed_a) == 0

    passed_b, failed_b = squeeze_engine.validate_gates(borderline_data, "Phase B")
    assert passed_b is False
    assert len(failed_b) == 1
    assert "資金效率不足" in failed_b[0]


def test_analyze_ticker_spear_route(
    squeeze_engine,
    default_market_data,
    default_account_state,
    default_holdings,
    default_greeks,
):
    output = squeeze_engine.analyze_ticker(
        data=default_market_data,
        account_state=default_account_state,
        options_holdings=default_holdings,
        portfolio_greeks=default_greeks,
        market_phase="Phase B",
    )

    assert isinstance(output, AdvancedTraderOutput)
    assert output.sddm_route == "SPEAR"
    assert output.is_applicable is True
    assert len(output.failed_gates) == 0
    assert (
        output.kelly_position_scaling == 0.25
    )  # Base Kelly multiplier = 1.0 (VIX < 15)
    assert "SPEAR" in output.recommended_actions[0]
    assert output.magnet_target == 175.0


def test_analyze_ticker_shield_vix_gated(
    squeeze_engine,
    default_market_data,
    default_account_state,
    default_holdings,
    default_greeks,
):
    # If VIX is high, forced to SHIELD and Kelly scaled to 0.1
    default_account_state.current_vix = 28.0
    output = squeeze_engine.analyze_ticker(
        data=default_market_data,
        account_state=default_account_state,
        options_holdings=default_holdings,
        portfolio_greeks=default_greeks,
        market_phase="Phase B",
    )

    assert output.sddm_route == "SHIELD"
    assert output.kelly_position_scaling == 0.025  # base_kelly * 0.1
    assert "SHIELD" in output.recommended_actions[0]
    assert "高波動警戒區" in output.recommended_actions[1]


def test_financial_runway_calculation(
    squeeze_engine,
    default_market_data,
    default_account_state,
    default_holdings,
    default_greeks,
):
    # Monthly burn rate = 6000 -> Daily burn rate = 200
    # Holdings theta yield = 2 * (-0.12) * 100 + (-1) * 0.08 * 100 = -24 - 8 = -32
    # Cash reserve = 25000
    # Runway = (25000 - 32) / 200 = 24968 / 200 = 124 days
    output = squeeze_engine.analyze_ticker(
        data=default_market_data,
        account_state=default_account_state,
        options_holdings=default_holdings,
        portfolio_greeks=default_greeks,
        market_phase="Phase B",
    )

    assert output.financial_runway_days == 124
    assert output.theta_coverage_pct == -16.0  # -32 / 200 * 100
    assert "🟢" not in output.runway_status_msg  # 124 is yellow (🟡)
    assert "🟡" in output.runway_status_msg


def test_vanna_hedging_instruction(
    squeeze_engine,
    default_market_data,
    default_account_state,
    default_holdings,
    default_greeks,
):
    # Vanna = 1.5, Beta = 1.2
    # d_vol = 0.10
    # hidden_delta_shares = 1.5 * 0.10 * 100 = 15
    # shares_needed = -round(15 * 1.2) = -18
    # Direction: SELL 18 SPY
    output = squeeze_engine.analyze_ticker(
        data=default_market_data,
        account_state=default_account_state,
        options_holdings=default_holdings,
        portfolio_greeks=default_greeks,
        market_phase="Phase B",
    )

    assert "SELL 賣出 18 單位 SPY" in output.vanna_hedging_instruction


def test_post_market_attribution_evolution(squeeze_engine):
    # Case 1: High protection score (portfolio suffered loss, hedge avoided it)
    # Portfolio PnL = -1000, Hedge PnL = +800 -> Score = 80% (>= 70)
    # Gate 3 threshold should be reduced by 10%
    res = squeeze_engine.run_post_market_attribution(
        portfolio_pnl=-1000.0, hedge_pnl=800.0
    )
    assert res["protection_score"] == 80.0
    assert res["old_threshold"] == 1000000.0
    assert res["new_threshold"] == 900000.0
    assert "調降明日 Gate 3" in res["evolution_msg"]

    # Case 2: Low protection score
    # Portfolio PnL = -1000, Hedge PnL = +200 -> Score = 20% (< 40)
    # Threshold should increase by 15% from 900000 to 1035000
    res2 = squeeze_engine.run_post_market_attribution(
        portfolio_pnl=-1000.0, hedge_pnl=200.0
    )
    assert res2["protection_score"] == 20.0
    assert res2["old_threshold"] == 900000.0
    assert res2["new_threshold"] == 1035000.0
    assert "調升明日 Gate 3" in res2["evolution_msg"]


def test_intraday_scan_report_only_sends_once_in_phase_b(intraday_pipeline):
    trading_date = date(2026, 5, 22)

    assert intraday_pipeline._should_send_intraday_scan_report(
        42, "MU", "Phase B", trading_date
    )

    intraday_pipeline._mark_intraday_scan_report_sent(42, "MU", trading_date)

    assert not intraday_pipeline._should_send_intraday_scan_report(
        42, "MU", "Phase B", trading_date
    )


def test_intraday_scan_report_skips_non_mid_session(intraday_pipeline):
    trading_date = date(2026, 5, 22)

    assert not intraday_pipeline._should_send_intraday_scan_report(
        42, "MU", "Phase A", trading_date
    )
    assert not intraday_pipeline._should_send_intraday_scan_report(
        42, "MU", "Phase C", trading_date
    )


@pytest.mark.asyncio
async def test_build_watchlist_heartbeat_embed_includes_option_plan(intraday_pipeline):
    evaluation = SimpleNamespace(
        metrics=SimpleNamespace(
            symbol="MU",
            current_price=410.5,
            iv_rank=68.0,
            option_skew=6.25,
            option_skew_state="左偏保護",
            buy_zone_status="🟡 測試買區",
            sell_zone_status="⚪ 測試賣區",
        ),
        tactical=SimpleNamespace(
            alert_level="yellow",
            scenario="premium-harvest",
            sddm_route="SHIELD",
        ),
        event_context=SimpleNamespace(summary="財報前風控"),
    )
    user_context = SimpleNamespace(user_id=42, capital=120000.0, risk_limit=12.0)

    with patch(
        "ui.formatter.generate_ansi_watchlist_report",
        return_value="heartbeat snapshot",
    ), patch(
        "database.is_symbol_in_portfolio",
        return_value=False,
    ), patch(
        "database.get_user_holdings",
        return_value=[],
    ), patch(
        "market_analysis.intraday_pipeline.derive_watchlist_option_guidance",
        return_value="option guidance",
    ) as mock_guidance, patch(
        "market_analysis.intraday_pipeline.build_watchlist_option_plan",
        new_callable=AsyncMock,
        return_value="option-plan",
    ) as mock_build_plan, patch(
        "services.llm_service.generate_watchlist_skew_commentary",
        new_callable=AsyncMock,
        return_value="llm-skew-commentary",
    ) as mock_skew_commentary, patch(
        "cogs.embed_builder.create_watchlist_signal_embed",
        return_value="watchlist-embed",
    ) as mock_create_embed:
        embed = await intraday_pipeline._build_watchlist_heartbeat_embed(
            evaluation, user_context
        )

    assert embed == "watchlist-embed"
    mock_build_plan.assert_awaited_once_with(
        evaluation.metrics,
        evaluation.tactical,
        capital=120000.0,
        risk_limit=12.0,
        event_context=evaluation.event_context,
        has_position=False,
    )
    mock_guidance.assert_called_once()
    assert mock_guidance.call_args[1]["suitable_buy_price"] == 377.78
    assert mock_guidance.call_args[1]["suitable_sell_price"] == 0.0

    mock_skew_commentary.assert_awaited_once()
    mock_create_embed.assert_called_once()
    create_embed_kwargs = mock_create_embed.call_args[1]
    assert create_embed_kwargs["symbol"] == "MU"
    assert create_embed_kwargs["report_body"] == "heartbeat snapshot"
    assert create_embed_kwargs["option_guidance"] == "option guidance"
    assert create_embed_kwargs["event_risk_summary"] == "財報前風控"
    assert create_embed_kwargs["skew_state"] == "+6.25% ｜ 左偏保護"
    assert create_embed_kwargs["alert_level"] == "yellow"
    assert create_embed_kwargs["option_plan"] == "option-plan"
    assert create_embed_kwargs["skew_commentary"] == "llm-skew-commentary"
    assert create_embed_kwargs["has_position"] is False
    assert create_embed_kwargs["holding_quantity"] is None
    assert create_embed_kwargs["holding_avg_cost"] is None
    assert create_embed_kwargs["suitable_buy_price"] == 377.78
    assert create_embed_kwargs["suitable_buy_shares"] == 10
    assert create_embed_kwargs["suitable_sell_price"] == 0.0
    assert create_embed_kwargs["suitable_sell_shares"] == 0
    assert "Skew 避險情緒折價" in create_embed_kwargs["buy_rationale"]
