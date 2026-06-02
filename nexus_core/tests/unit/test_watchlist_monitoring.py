import pytest
import pandas as pd
from pydantic import ValidationError
from unittest.mock import AsyncMock, patch

from market_analysis.intraday_pipeline import (
    _WATCHLIST_METRICS_CACHE,
    build_enhanced_watchlist_metrics,
    build_watchlist_event_context,
    build_watchlist_option_plan,
    derive_watchlist_option_guidance,
    evaluate_watchlist_symbol,
)
from models.quant import IVMetrics
from models.schemas import EnhancedWatchlistMetrics, WatchlistEventContext
from risk_engine.nro import WatchlistRiskController
from ui.formatter import generate_ansi_watchlist_report


def _sample_metrics(**overrides):
    payload = {
        "symbol": "NVDA",
        "exchange": "NASDAQ",
        "current_price": 132.0,
        "buy_zone_status": "🟢 買點：趨勢支撐 (VIX 修正)",
        "buy_price_phase1": 130.0,
        "buy_price_phase2": 124.0,
        "buy_price_phase3": 118.0,
        "sell_zone_status": "🟢 賣點：第一壓力帶",
        "sell_price_phase1": 136.0,
        "sell_price_phase2": 142.0,
        "sell_price_phase3": 148.0,
        "pe_ratio": 42.5,
        "rsi_14": 56.4,
        "atr_14": 6.0,
        "beta": 1.4,
        "ma20": 128.0,
        "ma50": 122.0,
        "ma200": 110.0,
        "bias_ma20": 999.0,
        "iv_rank": 72.0,
        "option_skew": -6.4,
        "option_skew_state": "⚠️ 預警性對沖 (Put 昂貴)",
        "volume_poc": 126.5,
        "gex_max_put_wall": 120.0,
        "vanna_sensitivity": 0.35,
        "relative_strength_spy": 0.08,
    }
    payload.update(overrides)
    return EnhancedWatchlistMetrics(**payload)


def _sample_event_context(**overrides):
    payload = {
        "earnings_date": None,
        "earnings_tte_hours": None,
        "macro_event": None,
        "macro_event_time": None,
        "macro_tte_hours": None,
        "risk_mode": "normal",
        "summary": "未偵測到近期需調整參數的重大事件。",
    }
    payload.update(overrides)
    return WatchlistEventContext(**payload)


@pytest.fixture(autouse=True)
def clear_watchlist_metrics_cache():
    _WATCHLIST_METRICS_CACHE.clear()
    yield
    _WATCHLIST_METRICS_CACHE.clear()


def test_enhanced_watchlist_metrics_computes_bias_and_support_distance():
    metrics = _sample_metrics()
    assert metrics.bias_ma20 == pytest.approx((132.0 / 128.0) - 1.0)
    assert metrics.distance_to_absolute_support == pytest.approx(
        (132.0 - 118.0) / 132.0
    )


def test_enhanced_watchlist_metrics_rejects_invalid_phase_order():
    with pytest.raises(ValidationError):
        _sample_metrics(buy_price_phase1=120.0, buy_price_phase2=124.0)


def test_watchlist_risk_controller_routes_premium_harvest():
    tactical = WatchlistRiskController.process_metrics(
        _sample_metrics(current_price=129.0, iv_rank=78.0)
    )
    assert tactical.scenario == "premium-harvest"
    assert tactical.alert_level == "yellow"
    assert "Cash-Secured Put" in tactical.action_guideline
    assert tactical.dynamic_grid_step == 3.0


def test_watchlist_risk_controller_routes_hard_hedge():
    tactical = WatchlistRiskController.process_metrics(
        _sample_metrics(current_price=123.0, beta=1.5, vanna_sensitivity=0.4)
    )
    assert tactical.scenario == "hard-hedge"
    assert tactical.alert_level == "red"
    assert tactical.hidden_delta_risk == 0.00
    assert tactical.hedge_allocation_shares == 0
    assert tactical.hedge_instruction is None


def test_watchlist_risk_controller_routes_wait():
    tactical = WatchlistRiskController.process_metrics(
        _sample_metrics(current_price=136.0, iv_rank=40.0)
    )
    assert tactical.scenario == "wait"
    assert tactical.alert_level == "green"
    assert tactical.hidden_delta_risk == 0.0
    assert tactical.hedge_instruction is None


def test_generate_ansi_watchlist_report_contains_sections():
    metrics = _sample_metrics(current_price=123.0, beta=1.5, vanna_sensitivity=0.4)
    tactical = WatchlistRiskController.process_metrics(metrics)
    report = generate_ansi_watchlist_report(metrics, tactical)
    assert report.startswith("```ansi")
    assert "Skew" in report
    assert "技術 / 防禦牆" in report
    assert "SDDM / 對沖" in report
    assert "NVDA | NASDAQ" in report


def test_derive_watchlist_option_guidance_mentions_skew_and_strategy():
    metrics = _sample_metrics(current_price=129.0, iv_rank=78.0, option_skew=-7.2)
    tactical = WatchlistRiskController.process_metrics(metrics)

    guidance = derive_watchlist_option_guidance(metrics, tactical)

    assert "Skew" in guidance
    assert "Cash-Secured Put" in guidance


def test_derive_watchlist_option_guidance_switches_to_position_management_copy():
    metrics = _sample_metrics(current_price=129.0, iv_rank=78.0, option_skew=-7.2)
    tactical = WatchlistRiskController.process_metrics(metrics)

    guidance = derive_watchlist_option_guidance(metrics, tactical, has_position=True)

    assert "已有部位" in guidance
    assert "Covered Call" in guidance


def test_derive_watchlist_option_guidance_prioritizes_event_guard():
    metrics = _sample_metrics(current_price=129.0, iv_rank=78.0, option_skew=-7.2)
    tactical = WatchlistRiskController.process_metrics(metrics)
    event_context = _sample_event_context(
        earnings_date="2026-05-24",
        earnings_tte_hours=36.0,
        risk_mode="event-lock",
        summary="NVDA 財報倒數 36.0 小時 ｜ 禁做賣方、僅保留保護性 / Debit Spread 類型。",
    )

    guidance = derive_watchlist_option_guidance(
        metrics, tactical, event_context=event_context
    )

    assert "禁做賣方" in guidance
    assert "Debit Spread" in guidance


def test_derive_watchlist_option_guidance_uses_position_copy_during_event_guard():
    metrics = _sample_metrics(current_price=129.0, iv_rank=78.0, option_skew=-7.2)
    tactical = WatchlistRiskController.process_metrics(metrics)
    event_context = _sample_event_context(
        earnings_date="2026-05-24",
        earnings_tte_hours=36.0,
        risk_mode="event-lock",
        summary="NVDA 財報倒數 36.0 小時 ｜ 禁做賣方、僅保留保護性 / Debit Spread 類型。",
    )

    guidance = derive_watchlist_option_guidance(
        metrics,
        tactical,
        event_context=event_context,
        has_position=True,
    )

    assert "已有部位" in guidance
    assert "保護性 Put" in guidance


@pytest.mark.asyncio
async def test_build_watchlist_option_plan_builds_credit_spread():
    metrics = _sample_metrics(current_price=129.0, iv_rank=78.0, option_skew=-7.2)
    tactical = WatchlistRiskController.process_metrics(metrics)
    chain = type(
        "Chain",
        (),
        {
            "calls": pd.DataFrame(),
            "puts": pd.DataFrame(
                [
                    {"strike": 120.0, "bid": 1.0, "ask": 1.2, "lastPrice": 1.1},
                    {"strike": 118.0, "bid": 0.7, "ask": 0.9, "lastPrice": 0.8},
                ]
            ),
        },
    )()

    with patch(
        "market_analysis.strategy.find_best_contract",
        new_callable=AsyncMock,
        return_value={"strike": 120.0, "expiry": "2026-06-19", "mid": 1.1},
    ), patch(
        "services.market_data_service.get_option_chain",
        new_callable=AsyncMock,
        return_value=chain,
    ):
        plan = await build_watchlist_option_plan(
            metrics,
            tactical,
            capital=100000.0,
            risk_limit=15.0,
        )

    assert plan is not None
    assert plan.strategy_name == "Bull Put Spread"
    assert plan.suggested_contracts >= 1
    assert len(plan.legs) == 2
    assert plan.legs[0].action == "SELL"
    assert plan.legs[1].action == "BUY"


@pytest.mark.asyncio
async def test_build_watchlist_option_plan_switches_to_debit_before_earnings():
    metrics = _sample_metrics(current_price=129.0, iv_rank=78.0, option_skew=7.2)
    tactical = WatchlistRiskController.process_metrics(metrics)
    event_context = _sample_event_context(
        earnings_date="2026-05-24",
        earnings_tte_hours=36.0,
        risk_mode="event-lock",
        summary="NVDA 財報倒數 36.0 小時 ｜ 禁做賣方、僅保留保護性 / Debit Spread 類型。",
    )
    chain = type(
        "Chain",
        (),
        {
            "calls": pd.DataFrame(
                [
                    {"strike": 132.0, "bid": 2.2, "ask": 2.4, "lastPrice": 2.3},
                    {"strike": 138.0, "bid": 0.9, "ask": 1.1, "lastPrice": 1.0},
                ]
            ),
            "puts": pd.DataFrame(
                [
                    {"strike": 120.0, "bid": 1.0, "ask": 1.2, "lastPrice": 1.1},
                    {"strike": 118.0, "bid": 0.7, "ask": 0.9, "lastPrice": 0.8},
                ]
            ),
        },
    )()

    with patch(
        "market_analysis.strategy.find_best_contract",
        new_callable=AsyncMock,
        return_value={"strike": 132.0, "expiry": "2026-06-19", "mid": 2.3},
    ), patch(
        "services.market_data_service.get_option_chain",
        new_callable=AsyncMock,
        return_value=chain,
    ):
        plan = await build_watchlist_option_plan(
            metrics,
            tactical,
            capital=100000.0,
            risk_limit=15.0,
            event_context=event_context,
        )

    assert plan is not None
    assert plan.strategy_name == "Bull Call Spread"
    assert plan.premium_type == "debit"
    assert "財報倒數" in plan.rationale
    assert plan.legs[0].action == "BUY"


@pytest.mark.asyncio
async def test_build_watchlist_option_plan_reduces_size_before_macro_event():
    metrics = _sample_metrics(current_price=130.0, iv_rank=55.0, option_skew=1.2)
    tactical = WatchlistRiskController.process_metrics(metrics)
    normal_context = _sample_event_context()
    macro_context = _sample_event_context(
        macro_event="CPI",
        macro_event_time="2026-05-22T12:30:00Z",
        macro_tte_hours=12.0,
        risk_mode="macro-guard",
        summary="CPI 倒數 12.0 小時 ｜ 先縮口數，優先定義風險的 Debit Spread / 保護性部位。",
    )
    chain = type(
        "Chain",
        (),
        {
            "calls": pd.DataFrame(
                [
                    {"strike": 124.0, "bid": 3.0, "ask": 3.2, "lastPrice": 3.1},
                    {"strike": 130.0, "bid": 1.2, "ask": 1.4, "lastPrice": 1.3},
                ]
            ),
            "puts": pd.DataFrame(),
        },
    )()

    with patch(
        "market_analysis.strategy.find_best_contract",
        new_callable=AsyncMock,
        return_value={"strike": 124.0, "expiry": "2026-06-19", "mid": 3.1},
    ), patch(
        "services.market_data_service.get_option_chain",
        new_callable=AsyncMock,
        return_value=chain,
    ):
        normal_plan = await build_watchlist_option_plan(
            metrics,
            tactical,
            capital=100000.0,
            risk_limit=15.0,
            event_context=normal_context,
        )
        macro_plan = await build_watchlist_option_plan(
            metrics,
            tactical,
            capital=100000.0,
            risk_limit=15.0,
            event_context=macro_context,
        )

    assert normal_plan is not None
    assert macro_plan is not None
    assert macro_plan.suggested_contracts <= normal_plan.suggested_contracts
    assert "CPI" in macro_plan.rationale


@pytest.mark.asyncio
async def test_build_enhanced_watchlist_metrics_assembles_quant_fields():
    dates = pd.date_range("2025-01-01", periods=90, freq="D")
    stock_df = pd.DataFrame(
        {
            "Open": [100.0 + i * 0.8 for i in range(90)],
            "High": [101.5 + i * 0.8 for i in range(90)],
            "Low": [99.0 + i * 0.8 for i in range(90)],
            "Close": [100.5 + i * 0.8 for i in range(90)],
            "Volume": [1_000_000 + i * 1000 for i in range(90)],
        },
        index=dates,
    )
    spy_df = pd.DataFrame(
        {
            "Open": [400.0 + i * 0.4 for i in range(90)],
            "High": [401.0 + i * 0.4 for i in range(90)],
            "Low": [399.0 + i * 0.4 for i in range(90)],
            "Close": [400.5 + i * 0.4 for i in range(90)],
            "Volume": [2_000_000 + i * 2000 for i in range(90)],
        },
        index=dates,
    )

    with patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 172.5},
    ), patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=stock_df,
    ), patch(
        "services.market_data_service.get_spy_history_df",
        new_callable=AsyncMock,
        return_value=spy_df,
    ), patch(
        "services.market_data_service.get_basic_financials",
        new_callable=AsyncMock,
        return_value={"peTTM": 31.2},
    ), patch(
        "services.market_data_service.get_company_profile",
        new_callable=AsyncMock,
        return_value={"exchange": "NASDAQ"},
    ), patch(
        "services.market_data_service.get_dividend_yield",
        new_callable=AsyncMock,
        return_value=0.01,
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
        new_callable=AsyncMock,
        return_value=IVMetrics(
            symbol="MSFT",
            current_iv=0.32,
            iv_rank=68.0,
            iv_percentile=64.0,
            expected_move_weekly=8.4,
            iv_status="High",
        ),
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
        new_callable=AsyncMock,
        return_value={"symbol": "MSFT", "skew": 4.8, "state": "正常"},
    ), patch(
        "market_analysis.intraday_pipeline._estimate_options_wall_metrics",
        new_callable=AsyncMock,
        return_value=(165.0, 0.44),
    ), patch(
        "market_analysis.risk_engine.calculate_beta",
        return_value=1.23,
    ):
        metrics = await build_enhanced_watchlist_metrics("msft")

    assert metrics is not None
    assert metrics.symbol == "MSFT"
    assert metrics.exchange == "NASDAQ"
    assert metrics.current_price == 172.5
    assert metrics.pe_ratio == 31.2
    assert metrics.iv_rank == 68.0
    assert metrics.option_skew == 4.8
    assert metrics.option_skew_state == "正常"
    assert metrics.gex_max_put_wall == 165.0
    assert metrics.vanna_sensitivity == 0.44
    assert metrics.beta == 1.23
    assert (
        metrics.buy_price_phase1 >= metrics.buy_price_phase2 >= metrics.buy_price_phase3
    )
    assert (
        metrics.sell_price_phase1
        <= metrics.sell_price_phase2
        <= metrics.sell_price_phase3
    )


@pytest.mark.asyncio
async def test_evaluate_watchlist_symbol_returns_wait_snapshot():
    metrics = _sample_metrics(current_price=136.0, iv_rank=40.0)
    event_context = _sample_event_context()

    with patch(
        "market_analysis.intraday_pipeline.build_enhanced_watchlist_metrics",
        new_callable=AsyncMock,
        return_value=metrics,
    ), patch(
        "market_analysis.intraday_pipeline.build_watchlist_event_context",
        new_callable=AsyncMock,
        return_value=event_context,
    ):
        evaluation = await evaluate_watchlist_symbol("NVDA")

    assert evaluation is not None
    assert evaluation.metrics.symbol == "NVDA"
    assert evaluation.tactical.scenario == "wait"
    assert evaluation.tactical.sddm_route == "WAIT (觀望 / 待機)"
    assert evaluation.event_context.risk_mode == "normal"


@pytest.mark.asyncio
async def test_build_watchlist_event_context_marks_earnings_lock():
    earnings_event = type(
        "EarningsEvent", (), {"date": "2026-05-24", "tte_hours": 36.0}
    )()
    from datetime import datetime, timedelta

    future_time = (datetime.now() + timedelta(days=5)).isoformat()
    macro_event = type(
        "EconomicEvent",
        (),
        {"event": "CPI", "time": future_time, "tte_hours": 60.0},
    )()

    context = await build_watchlist_event_context(
        "NVDA", earnings_event=earnings_event, macro_event=macro_event
    )

    assert context.risk_mode == "event-lock"
    assert "禁做賣方" in context.summary


def test_watchlist_risk_controller_hard_hedge_suppresses_spy_hedging():
    """Rule 1: Hard-Hedge triggers suppression of index short hedging."""
    metrics = _sample_metrics(current_price=95.0, iv_rank=74.7)
    metrics.buy_price_phase2 = 100.0  # spot < phase2 triggers hard-hedge

    tactical = WatchlistRiskController.process_metrics(metrics)

    assert tactical.scenario == "hard-hedge"
    assert tactical.sddm_route == "SHIELD (全面防禦中)"
    assert tactical.hidden_delta_risk == 0.00
    assert tactical.hedge_allocation_shares == 0
    assert tactical.hedge_instruction is None
    assert "無需執行 SPY 指數對沖" in tactical.action_guideline


@pytest.mark.asyncio
async def test_rule2_premium_selling_option_strategy_routing():
    """Rule 2: IV_Rank > 50% and Option_Skew < 0% routes to Premium Selling Strategies and bans Debit Spreads."""
    from models.schemas import WatchlistTacticalPlan

    metrics = _sample_metrics(current_price=108.99, iv_rank=74.7)
    metrics.option_skew = -5.10  # right-skewed / calls overvalued

    tactical = WatchlistTacticalPlan(
        scenario="premium-harvest",
        sddm_route="SHIELD (全面防禦中)",
        dynamic_grid_step=4.65,
        action_guideline="test",
        alert_level="yellow",
    )

    # 1. With position: routes to Covered Call
    with patch(
        "market_analysis.strategy.find_best_contract", new_callable=AsyncMock
    ) as mock_find:
        mock_find.return_value = {"strike": 115.0, "expiry": "2026-06-26", "mid": 4.15}
        plan_held = await build_watchlist_option_plan(
            metrics, tactical, capital=100000.0, risk_limit=15.0, has_position=True
        )
        assert plan_held is not None
        assert "Covered Call" in plan_held.strategy_name
        assert plan_held.premium_type == "credit"
        assert len(plan_held.legs) == 1
        assert plan_held.legs[0].action == "SELL"
        assert plan_held.legs[0].opt_type == "CALL"

    # 2. Without position: routes to Bull Put Spread
    with patch(
        "market_analysis.strategy.find_best_contract", new_callable=AsyncMock
    ) as mock_find, patch(
        "market_analysis.intraday_pipeline._pick_watchlist_cover_leg",
        new_callable=AsyncMock,
    ) as mock_cover:
        mock_find.return_value = {"strike": 105.0, "expiry": "2026-06-26", "mid": 4.15}
        mock_cover.return_value = {"strike": 100.0, "expiry": "2026-06-26", "mid": 1.15}

        plan_unheld = await build_watchlist_option_plan(
            metrics, tactical, capital=100000.0, risk_limit=15.0, has_position=False
        )
        assert plan_unheld is not None
        assert plan_unheld.strategy_name == "Bull Put Spread"
        assert plan_unheld.premium_type == "credit"
        assert len(plan_unheld.legs) == 2


@pytest.mark.asyncio
async def test_rule3_macro_timer_cache_invalidation():
    """Rule 3: Macro event release time in the past invalidates countdown and switches to published state."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    # Set event release time 1 hour in the past
    past_time = (datetime.now(ZoneInfo("Asia/Taipei")) - timedelta(hours=1)).isoformat()

    macro_event = type(
        "EconomicEvent",
        (),
        {"event": "ISM Manufacturing PMI", "time": past_time, "tte_hours": -1.0},
    )()

    context = await build_watchlist_event_context(
        "INTC", earnings_event=None, macro_event=macro_event
    )

    assert context.risk_mode == "normal"
    assert context.macro_tte_hours is None
    assert "ISM Manufacturing PMI" in context.summary
    assert "正式公布" in context.summary
    assert "宏觀不確定性逐步落地" in context.summary


def test_rule5_support_distance_formula_correctness():
    """Rule 5 & Bug 4: Support distance is defined as (current - support) / current."""
    metrics = _sample_metrics(current_price=108.99, iv_rank=74.7)
    metrics.buy_price_phase3 = 49.60
    metrics.gex_max_put_wall = 50.00

    # support_price = min(buy_price_phase3, gex_max_put_wall) = 49.60
    # distance = (108.99 - 49.60) / 108.99 = 0.544912...
    dist = metrics.distance_to_absolute_support
    assert abs(dist - 0.5449) < 0.001
