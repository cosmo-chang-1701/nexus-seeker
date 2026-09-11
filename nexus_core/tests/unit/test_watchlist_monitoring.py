from typing import Any
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


def _sample_metrics(**overrides):  # type: ignore
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
        "iv_percentile": 64.0,
        "option_skew": -6.4,
        "skew_percentile": 10.0,
        "option_skew_state": "右偏 (Call 昂貴)",
        "pcr": 0.95,
        "volume_poc": 126.5,
        "gex_max_put_wall": 120.0,
        "vanna_sensitivity": 0.35,
        "relative_strength_spy": 0.08,
    }
    payload.update(overrides)
    return EnhancedWatchlistMetrics(**payload)  # type: ignore


def _sample_event_context(**overrides):  # type: ignore
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
    return WatchlistEventContext(**payload)  # type: ignore


@pytest.fixture(autouse=True)
def clear_watchlist_metrics_cache() -> Any:
    _WATCHLIST_METRICS_CACHE.clear()
    yield
    _WATCHLIST_METRICS_CACHE.clear()


def test_enhanced_watchlist_metrics_computes_bias_and_support_distance() -> None:
    metrics = _sample_metrics()
    assert metrics.bias_ma20 == 0.0
    assert metrics.distance_to_absolute_support == pytest.approx(
        (132.0 - 118.0) / 132.0
    )


def test_enhanced_watchlist_metrics_rejects_invalid_phase_order() -> None:
    with pytest.raises(ValidationError):
        _sample_metrics(buy_price_phase1=120.0, buy_price_phase2=124.0)


# premium-harvest 情境的基準案例驗證見
# tests/unit/test_watchlist_risk_controller.py::test_process_metrics_premium_harvest_without_backwardation。


def test_watchlist_risk_controller_routes_hard_hedge() -> None:
    tactical = WatchlistRiskController.process_metrics(
        _sample_metrics(current_price=123.0, beta=1.5, vanna_sensitivity=0.4)
    )
    assert tactical.scenario == "hard-hedge"
    assert tactical.alert_level == "red"
    assert tactical.hidden_delta_risk == 0.00
    assert tactical.hedge_allocation_shares == 0
    assert tactical.hedge_instruction is None


def test_watchlist_risk_controller_routes_wait() -> None:
    tactical = WatchlistRiskController.process_metrics(
        _sample_metrics(current_price=136.0, iv_rank=40.0)
    )
    assert tactical.scenario == "wait"
    assert tactical.alert_level == "green"
    assert tactical.hidden_delta_risk == 0.0
    assert tactical.hedge_instruction is None


def test_generate_ansi_watchlist_report_contains_sections() -> None:
    metrics = _sample_metrics(current_price=123.0, beta=1.5, vanna_sensitivity=0.4)
    tactical = WatchlistRiskController.process_metrics(metrics)
    report = generate_ansi_watchlist_report(metrics, tactical)
    assert report.startswith("```ansi")
    assert "Skew" in report
    assert "技術 / 防禦牆" in report
    assert "SDDM / 對沖" in report
    assert "NVDA | NASDAQ" in report


def test_derive_watchlist_option_guidance_mentions_skew_and_strategy() -> None:
    metrics = _sample_metrics(current_price=129.0, iv_rank=78.0, option_skew=-7.2)
    tactical = WatchlistRiskController.process_metrics(metrics)

    guidance = derive_watchlist_option_guidance(metrics, tactical)

    assert "Cash-Secured Put" in guidance


def test_derive_watchlist_option_guidance_switches_to_position_management_copy() -> (
    None
):
    metrics = _sample_metrics(current_price=129.0, iv_rank=78.0, option_skew=-7.2)
    tactical = WatchlistRiskController.process_metrics(metrics)

    guidance = derive_watchlist_option_guidance(metrics, tactical, has_position=True)

    assert "已持有現貨部位" in guidance
    assert "Covered Call" in guidance


def test_derive_watchlist_option_guidance_prioritizes_event_guard() -> None:
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

    assert "Cash-Secured Put" in guidance


def test_derive_watchlist_option_guidance_uses_position_copy_during_event_guard() -> (
    None
):
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

    assert "已持有現貨部位" in guidance
    assert "Covered Call" in guidance


@pytest.mark.asyncio
async def test_build_watchlist_option_plan_builds_credit_spread() -> None:
    metrics = _sample_metrics(current_price=129.0, iv_rank=78.0, option_skew=7.2)
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
    assert plan.strategy_name == "Cash-Secured Put"
    assert plan.suggested_contracts >= 1
    assert len(plan.legs) == 1
    assert plan.legs[0].action == "SELL"


@pytest.mark.asyncio
async def test_build_watchlist_option_plan_blocks_credit_before_earnings() -> None:
    """財報 72 小時內 (event-lock) 必須擋掉所有信用（賣方）結構。

    此測試原本斷言此情境下仍會回傳 Cash-Secured Put，鎖住的其實是一個 bug：
    `build_watchlist_option_plan` 的 event-lock 判定寫成
    `"Credit" in strategy_name`，但 strategy_name 只會是 "Covered Call (...)"
    或 "Cash-Secured Put"，永遠不含 "Credit"，這道風控從未生效。判定改為
    `premium_type == "credit"` 後，此情境應如 event_context 文案所述「禁做賣方」，
    並回傳一個 0 口的 WAIT 計畫（而非 None），讓使用者看得到封鎖原因而不是期權
    區塊無聲消失。
    """
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
    assert plan.strategy_name == "WAIT (財報事件鎖定，禁做賣方)"
    assert plan.suggested_contracts == 0
    assert plan.legs == []
    assert "禁做賣方" in plan.rationale


@pytest.mark.asyncio
async def test_build_watchlist_option_plan_reduces_size_before_macro_event() -> None:
    metrics = _sample_metrics(current_price=130.0, iv_rank=72.0, option_skew=1.2)
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
async def test_build_enhanced_watchlist_metrics_assembles_quant_fields() -> None:
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
        return_value={
            "symbol": "MSFT",
            "skew": 4.8,
            "skew_percentile": 55.0,
            "state": "左偏 (Put 昂貴)",
        },
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
        new_callable=AsyncMock,
        return_value={"symbol": "MSFT", "pcr": 0.92, "state": "平衡"},
    ), patch(
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
        new_callable=AsyncMock,
        return_value={"put_wall": 165.0, "call_wall": 180.0, "net_gex": 1000000.0},
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
    assert metrics.iv_percentile == 64.0
    assert metrics.option_skew == 4.8
    assert metrics.skew_percentile == 55.0
    assert metrics.option_skew_state == "左偏 (Put 昂貴)"
    assert metrics.pcr == 0.92
    assert metrics.gex_max_put_wall == 165.0
    assert metrics.vanna_sensitivity == 0.0
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
async def test_evaluate_watchlist_symbol_returns_wait_snapshot() -> None:
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
    ), patch(
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
        new_callable=AsyncMock,
        return_value={"net_gex": 0.0, "call_wall": 0.0, "put_wall": 0.0},
    ):
        evaluation = await evaluate_watchlist_symbol("NVDA")

    assert evaluation is not None
    assert evaluation.metrics.symbol == "NVDA"
    assert evaluation.tactical.scenario == "wait"
    assert evaluation.tactical.sddm_route == "WAIT (觀望 / 待機)"
    assert evaluation.event_context.risk_mode == "normal"


@pytest.mark.asyncio
async def test_build_watchlist_event_context_marks_earnings_lock() -> None:
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


def test_watchlist_risk_controller_hard_hedge_suppresses_spy_hedging() -> None:
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
async def test_rule2_premium_selling_option_strategy_routing() -> None:
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

    # 2. Without position: routes to Cash-Secured Put
    with patch(
        "market_analysis.strategy.find_best_contract", new_callable=AsyncMock
    ) as mock_find:
        mock_find.return_value = {"strike": 105.0, "expiry": "2026-06-26", "mid": 4.15}

        plan_unheld = await build_watchlist_option_plan(
            metrics, tactical, capital=100000.0, risk_limit=15.0, has_position=False
        )
        assert plan_unheld is not None
        assert plan_unheld.strategy_name == "Cash-Secured Put"
        assert plan_unheld.premium_type == "credit"
        assert len(plan_unheld.legs) == 1


@pytest.mark.asyncio
async def test_rule3_macro_timer_cache_invalidation() -> None:
    """Rule 3: Macro event release time in the past invalidates countdown and switches to published state."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    # Set event release time 1 hour in the past (within 2-hour post-release cooldown defense)
    past_time_1h = (
        datetime.now(ZoneInfo("Asia/Taipei")) - timedelta(hours=1)
    ).isoformat()

    macro_event_cooldown = type(
        "EconomicEvent",
        (),
        {"event": "ISM Manufacturing PMI", "time": past_time_1h, "tte_hours": -1.0},
    )()

    context_cooldown = await build_watchlist_event_context(
        "INTC", earnings_event=None, macro_event=macro_event_cooldown
    )

    # Within 2 hours, should maintain macro-guard during market digestion
    assert context_cooldown.risk_mode == "macro-guard"
    assert "消化冷卻期中" in context_cooldown.summary

    # Set event release time 3 hours in the past (past 2-hour cooldown window)
    past_time_3h = (
        datetime.now(ZoneInfo("Asia/Taipei")) - timedelta(hours=3)
    ).isoformat()

    macro_event_past = type(
        "EconomicEvent",
        (),
        {"event": "ISM Manufacturing PMI", "time": past_time_3h, "tte_hours": -3.0},
    )()

    context = await build_watchlist_event_context(
        "INTC", earnings_event=None, macro_event=macro_event_past
    )

    assert context.risk_mode == "normal"
    assert context.macro_tte_hours is None
    assert "ISM Manufacturing PMI" in context.summary
    assert "正式公布" in context.summary
    assert "宏觀不確定性逐步落地" in context.summary


def test_rule5_support_distance_formula_correctness() -> None:
    """Rule 5 & Bug 4: Support distance is defined as (current - support) / current."""
    metrics = _sample_metrics(current_price=108.99, iv_rank=74.7)
    metrics.buy_price_phase3 = 49.60
    metrics.gex_max_put_wall = 50.00

    # support_price = min(buy_price_phase3, gex_max_put_wall) = 49.60
    # distance = (108.99 - 49.60) / 108.99 = 0.544912...
    dist = metrics.distance_to_absolute_support
    assert abs(dist - 0.5449) < 0.001


# ─────────────────────────────────────────────────────────────────────────────
# calculate_dynamic_trading_signals 欄位驗算測試
# ─────────────────────────────────────────────────────────────────────────────


def test_buy_shares_zero_div_protection() -> None:
    """suitable_buy_price 極小值或邊界不應觸發 ZeroDivisionError，shares 應為 0。"""
    from market_analysis.signal_calculator import calculate_dynamic_trading_signals
    from models.schemas import WatchlistTacticalPlan

    # 建構一組會讓 buy_price_phase3 接近 0 的 metrics
    metrics = _sample_metrics(
        current_price=0.01,
        buy_price_phase1=0.001,
        buy_price_phase2=0.0005,
        buy_price_phase3=0.00001,
        rsi_14=50.0,
        option_skew=0.0,
    )
    tactical = WatchlistTacticalPlan(
        scenario="premium-harvest",
        sddm_route="SPEAR",
        action_guideline="Normal",
        dynamic_grid_step=3.0,
    )
    result = calculate_dynamic_trading_signals(
        metrics,
        tactical,
        has_position=False,
        capital=10000.0,
        risk_limit=15.0,
    )
    # 不應拋出異常，shares 應為非負整數
    assert isinstance(result["suitable_buy_shares"], int)
    assert result["suitable_buy_shares"] >= 0


def test_buy_shares_normal_case() -> None:
    """正常情況下 buy_shares 應大於 0 且符合 budget 計算邏輯。"""
    from market_analysis.signal_calculator import calculate_dynamic_trading_signals
    from models.schemas import WatchlistTacticalPlan

    metrics = _sample_metrics(
        current_price=132.0,
        rsi_14=50.0,
        option_skew=0.0,
    )
    tactical = WatchlistTacticalPlan(
        scenario="premium-harvest",
        sddm_route="SPEAR",
        action_guideline="Normal",
        dynamic_grid_step=3.0,
    )
    result = calculate_dynamic_trading_signals(
        metrics,
        tactical,
        has_position=False,
        capital=100000.0,
        risk_limit=15.0,
    )
    assert result["suitable_buy_shares"] > 0
    assert isinstance(result["suitable_buy_price"], float)
    assert result["suitable_buy_price"] > 0.0


def test_buy_shares_is_zero_in_crisis_mode() -> None:
    """風控鎖定 (SHIELD 底牆破位) 時 shares 應強制為 0，且 buy_price 為字串說明。"""
    from market_analysis.signal_calculator import calculate_dynamic_trading_signals
    from models.schemas import WatchlistTacticalPlan

    metrics = _sample_metrics(current_price=132.0, rsi_14=50.0)
    tactical = WatchlistTacticalPlan(
        scenario="wait",
        sddm_route="SHIELD 網格防禦",
        action_guideline="Crisis",
        dynamic_grid_step=3.0,
    )
    result = calculate_dynamic_trading_signals(
        metrics,
        tactical,
        has_position=False,
        capital=100000.0,
        risk_limit=15.0,
    )
    assert result["suitable_buy_shares"] == 0
    assert isinstance(result["suitable_buy_price"], str)
    assert "風控鎖定" in result["suitable_buy_price"]


def test_sell_shares_25pct_normal_rsi() -> None:
    """RSI 常態時 (40 < rsi < 60)，sell_shares 應為持倉的 25%。"""
    from market_analysis.signal_calculator import calculate_dynamic_trading_signals
    from models.schemas import WatchlistTacticalPlan

    metrics = _sample_metrics(
        current_price=140.0,
        rsi_14=55.0,
        option_skew=0.0,
        sell_price_phase1=136.0,
        sell_price_phase2=142.0,
        sell_price_phase3=148.0,
    )
    tactical = WatchlistTacticalPlan(
        scenario="premium-harvest",
        sddm_route="SPEAR",
        action_guideline="Normal",
        dynamic_grid_step=3.0,
    )
    result = calculate_dynamic_trading_signals(
        metrics,
        tactical,
        has_position=True,
        holding_quantity=100.0,
        holding_avg_cost=130.0,
        capital=100000.0,
        risk_limit=15.0,
    )
    # 25% of 100 = 25
    assert result["suitable_sell_shares"] == 25


def test_sell_shares_hard_hedge_full_exit() -> None:
    """硬避險模式下 sell_shares 應為全部持倉。"""
    from market_analysis.signal_calculator import calculate_dynamic_trading_signals
    from models.schemas import WatchlistTacticalPlan

    metrics = _sample_metrics(current_price=100.0, rsi_14=50.0)
    tactical = WatchlistTacticalPlan(
        scenario="hard-hedge",
        sddm_route="SHIELD",
        action_guideline="Hard hedge",
        dynamic_grid_step=3.0,
    )
    result = calculate_dynamic_trading_signals(
        metrics,
        tactical,
        has_position=True,
        holding_quantity=200.0,
        holding_avg_cost=110.0,
        capital=100000.0,
        risk_limit=15.0,
    )
    assert result["suitable_sell_shares"] == 200
    assert "硬避險" in result["sell_rationale"] or "Hard" in result["sell_rationale"]


@pytest.mark.asyncio
async def test_symbol_earnings_countdown_is_not_frozen_by_memory_cache() -> None:
    """財報倒數必須每次重算，不能被記憶體快取凍結。

    `_earnings_cache` 是純 LRU（`services/bounded_cache.py`），沒有任何 TTL，而
    `get_symbol_earnings()` 過去在命中時直接回傳「算好的 EarningsEvent」且不做
    新鮮度檢查——`tte_hours` 因此在進程生命週期內永遠不變，心跳的「🗓️ 事件風控」
    會無限顯示同一個倒數，`_resolve_watchlist_event_mode` 也會鎖死在
    event-lock / earnings-guard。
    """
    from datetime import datetime, timedelta
    from services.calendar_service import CalendarService, ny_tz

    service = CalendarService()
    earnings_day = (datetime.now(ny_tz) + timedelta(days=3)).date()

    with patch(
        "services.calendar_service.get_cached_earnings",
        return_value={
            "earnings_date": earnings_day.strftime("%Y-%m-%d"),
            "checked_at": datetime.now().isoformat(),
        },
    ), patch("services.calendar_service.save_earnings_cache", return_value=None):
        first = await service.get_symbol_earnings("NVDA")
        assert first is not None

        # 第二次呼叫走記憶體快取路徑；把「現在」往後推 2 小時，倒數必須跟著縮短。
        real_datetime = datetime

        class _ShiftedDatetime(real_datetime):  # type: ignore[misc,valid-type]
            @classmethod
            def now(cls, tz=None):  # type: ignore[no-untyped-def]
                return real_datetime.now(tz) + timedelta(hours=2)

        with patch("services.calendar_service.datetime", _ShiftedDatetime):
            second = await service.get_symbol_earnings("NVDA")

    assert second is not None
    assert second.date == first.date
    assert second.tte_hours == pytest.approx(first.tte_hours - 2.0, abs=0.2)


def test_fundamental_liquidation_route_survives_later_skew_gate() -> None:
    """基本面破滅的強制清算指令，不得被後續 Skew 閘門整包覆寫。

    `evaluate_watchlist_symbol()` 的四道 Skew / 動能 / IV 背離閘門過去都是
    `tactical = WatchlistTacticalPlan(...)` 重建，會把「立即清算並轉倉至 CORE
    資產」降級成一般的「機構避險背離」觀望文案。
    """
    from market_analysis.intraday_pipeline.evaluation import _apply_tactical_gate
    from models.schemas import WatchlistTacticalPlan

    locked = WatchlistTacticalPlan(
        scenario="wait",
        sddm_route="LIQUIDATE (基本面破滅強制清算)",
        action_guideline="⛔ 【LLM 護城河破滅警告】建議立即清算。",
        dynamic_grid_step=1.5,
        alert_level="red",
        capital_retreat_required=True,
    )

    result = _apply_tactical_gate(
        locked,
        locked=True,
        sddm_route="WAIT (機構避險背離/尾部風險警戒)",
        action_guideline="⚠️ 機構避險背離/尾部風險警戒",
        capital_retreat_required=True,
    )

    assert result.sddm_route == "LIQUIDATE (基本面破滅強制清算)"
    assert "立即清算" in result.action_guideline
    assert "機構避險背離" in result.action_guideline  # 警語仍以追加方式保留
    assert result.alert_level == "red"

    # 對照組：沒有更高優先級鎖定時，閘門仍照常改寫路由。
    unlocked = WatchlistTacticalPlan(
        scenario="premium-harvest",
        sddm_route="SHIELD (防禦網格)",
        action_guideline="收租",
        dynamic_grid_step=1.5,
        alert_level="yellow",
    )
    overridden = _apply_tactical_gate(
        unlocked,
        locked=False,
        sddm_route="WAIT (機構避險背離/尾部風險警戒)",
        action_guideline="⚠️ 機構避險背離/尾部風險警戒",
        capital_retreat_required=True,
    )
    assert overridden.sddm_route == "WAIT (機構避險背離/尾部風險警戒)"
    assert overridden.capital_retreat_required is True


def test_capital_retreat_flag_drives_allocation_cap() -> None:
    """資金藍圖 70%~85% 退守閘門改由顯式旗標驅動，不再依賴中文字串比對。"""
    from market_analysis.signal_calculator import calculate_dynamic_trading_signals
    from models.schemas import WatchlistTacticalPlan

    metrics = _sample_metrics(current_price=100.0, iv_rank=50.0, option_skew=0.0)

    def _plan(retreat: bool) -> WatchlistTacticalPlan:
        return WatchlistTacticalPlan(
            scenario="premium-harvest",
            # 刻意使用不含「負 Gamma」字樣的路由名稱——evaluation.py 實際設定的
            # 就是這種名稱，舊的子字串比對在此永遠不會匹配。
            sddm_route="SHIELD 網格防禦 (負 Gamma 踩踏)".replace(
                "負 Gamma 踩踏", "壓力測試"
            ),
            action_guideline="test",
            dynamic_grid_step=1.0,
            alert_level="yellow",
            capital_retreat_required=retreat,
        )

    baseline = calculate_dynamic_trading_signals(
        metrics, _plan(False), has_position=False, capital=100000.0, risk_limit=15.0
    )
    retreated = calculate_dynamic_trading_signals(
        metrics, _plan(True), has_position=False, capital=100000.0, risk_limit=15.0
    )

    # 旗標為 True 時才會掛上退守警語；為 False 時不得誤觸。
    assert "退守大盤流動性資產" in retreated["buy_rationale"]
    assert "退守大盤流動性資產" not in baseline["buy_rationale"]
    # 未提供已部署曝險時（預設 0.0），15% 額度全數可用，部位規模不受影響。
    assert retreated["suitable_buy_shares"] == baseline["suitable_buy_shares"]


def test_capital_retreat_cap_is_portfolio_level() -> None:
    """資金退守是**組合層**限額：可用預算 = 總資金 15% − 已部署戰術曝險。

    過去這裡是單一部位的 `min(allocated_budget, capital * 0.15)`，而單一部位預算
    的理論上限只有 capital * 0.1265，min() 永遠取前者，閘門實際從未縮減過任何部位。
    """
    from market_analysis.signal_calculator import calculate_dynamic_trading_signals
    from models.schemas import WatchlistTacticalPlan

    metrics = _sample_metrics(current_price=100.0, iv_rank=50.0, option_skew=0.0)
    plan = WatchlistTacticalPlan(
        scenario="premium-harvest",
        sddm_route="SHIELD 網格防禦",
        action_guideline="test",
        dynamic_grid_step=1.0,
        alert_level="yellow",
        capital_retreat_required=True,
    )

    def _shares(deployed: float) -> int:
        return int(
            calculate_dynamic_trading_signals(
                metrics,
                plan,
                has_position=False,
                capital=100000.0,
                risk_limit=15.0,
                deployed_tactical_value=deployed,
            )["suitable_buy_shares"]
        )

    # 額度充裕（15,000 上限、已用 8,000 → 剩 7,000 > 本輪預算 5,500）：不受限
    assert _shares(8_000.0) == _shares(0.0)
    # 額度快滿（剩 1,000）：部位被壓縮
    assert 0 < _shares(14_000.0) < _shares(0.0)
    # 額度用罄與超額：一律不得新增部位
    assert _shares(15_000.0) == 0
    assert _shares(22_000.0) == 0

    exhausted = calculate_dynamic_trading_signals(
        metrics,
        plan,
        has_position=False,
        capital=100000.0,
        risk_limit=15.0,
        deployed_tactical_value=22_000.0,
    )
    assert "已達上限" in exhausted["buy_rationale"]
    assert exhausted["capital_retreat_remaining"] == 0.0


def test_compute_deployed_tactical_value_scope() -> None:
    """戰術曝險只計衛星部位；CORE 與現金等價物是退守目的地，不得計入。"""
    from market_analysis.signal_calculator import compute_deployed_tactical_value

    spot = [
        {
            "symbol": "NVDA",
            "asset_class": "SATELLITE",
            "quantity": 10.0,
            "avg_cost": 120.0,
        },
        # CORE 是退守目的地
        {"symbol": "VOO", "asset_class": "CORE", "quantity": 50.0, "avg_cost": 500.0},
        # 現金等價物同理
        {
            "symbol": "BOXX",
            "asset_class": "SATELLITE",
            "quantity": 100.0,
            "avg_cost": 110.0,
        },
        # 未分類視為衛星
        {"symbol": "AMD", "asset_class": None, "quantity": 5.0, "avg_cost": 100.0},
        # 髒資料應被跳過而非中斷整體計算
        {
            "symbol": "BAD",
            "asset_class": "SATELLITE",
            "quantity": "N/A",
            "avg_cost": None,
        },
    ]
    options = [
        # 長倉：已付權利金
        {
            "symbol": "TSLA",
            "opt_type": "call",
            "strike": 250.0,
            "entry_price": 3.0,
            "quantity": 2.0,
        },
        # 賣出 PUT：佔用擔保現金
        {
            "symbol": "MU",
            "opt_type": "put",
            "strike": 90.0,
            "entry_price": 1.5,
            "quantity": -1.0,
        },
        # 賣出 CALL：擔保品是股票，已由現貨計入，不得重複計算
        {
            "symbol": "NVDA",
            "opt_type": "call",
            "strike": 150.0,
            "entry_price": 2.0,
            "quantity": -1.0,
        },
    ]

    total = compute_deployed_tactical_value(
        spot_holdings=spot, option_positions=options
    )
    # 10*120 (NVDA) + 5*100 (AMD) + 2*3*100 (TSLA long) + 1*90*100 (MU short put)
    assert total == pytest.approx(1200.0 + 500.0 + 600.0 + 9000.0)

    assert compute_deployed_tactical_value() == 0.0


def test_capital_retreat_flag_is_sticky_across_gates() -> None:
    """後續未設此旗標的閘門不得清掉前面已設好的資金退守要求。

    實務情境：skew_percentile=95 先由 Skew 閘門設 True，同一根 K 棒又滿足
    dp<-3% 且 IVR<15，IV 壓抑閘門把 plan 整個重建。若旗標沒有 sticky，重建後
    旗標歸 False、路由也不再含「機構避險背離」/「負 Gamma」字樣，資金退守與
    is_crisis 兩道保護會同時失效，反而在尾部風險當下輸出全額買點與股數。
    """
    from market_analysis.intraday_pipeline.evaluation import _apply_tactical_gate
    from models.schemas import WatchlistTacticalPlan

    after_skew_gate = WatchlistTacticalPlan(
        scenario="wait",
        sddm_route="WAIT (機構避險背離/尾部風險警戒)",
        action_guideline="⚠️ 機構避險背離",
        dynamic_grid_step=1.0,
        alert_level="red",
        capital_retreat_required=True,
    )

    # IV 壓抑閘門本身不帶 capital_retreat_required
    after_iv_gate = _apply_tactical_gate(
        after_skew_gate,
        locked=False,
        sddm_route="WAIT (IV 壓抑背離)",
        action_guideline="⚠️ IV Suppression Divergence",
    )

    assert after_iv_gate.sddm_route == "WAIT (IV 壓抑背離)"
    assert after_iv_gate.capital_retreat_required is True


@pytest.mark.asyncio
async def test_symbol_earnings_tte_stays_positive_on_earnings_day() -> None:
    """財報當日盤中 (13:30 ET) tte_hours 不得為負，否則 event-lock 會在最需要時降級為 normal。

    盤中 13:30 ET 時 AMC (或預設) 財報距離 16:30 發布約 3.0 小時；
    `_resolve_watchlist_event_mode()` 要求 `0 < earnings_tte_hours <= 72` 保持 event-lock。
    而盤前 BMO 財報在 13:30 ET 時已公布 (is_released=True)，不進入 event-lock。
    """
    from datetime import datetime, time
    from market_analysis.intraday_pipeline.events import _resolve_watchlist_event_mode
    from services.calendar_service import CalendarService, ny_tz

    service = CalendarService()
    today = datetime.now(ny_tz).date()
    midday_ny = datetime.combine(today, time(13, 30)).replace(tzinfo=ny_tz)

    # 1. 測試 AMC / 預設未知時段：盤中 13:30 應保持正數倒數 (3.0h) 並鎖定 event-lock
    with patch("services.calendar_service.datetime") as mock_dt, patch(
        "services.calendar_service.get_cached_earnings",
        return_value={
            "earnings_date": today.strftime("%Y-%m-%d"),
            "checked_at": datetime.now().isoformat(),
        },
    ), patch("services.calendar_service.save_earnings_cache", return_value=None):
        mock_dt.now.side_effect = (
            lambda tz=None: midday_ny if tz is None else midday_ny.astimezone(tz)
        )
        mock_dt.combine = datetime.combine
        mock_dt.min = datetime.min
        mock_dt.strptime = datetime.strptime
        mock_dt.fromisoformat = datetime.fromisoformat

        event = await service.get_symbol_earnings("NVDA")

    assert event is not None
    assert event.tte_hours == 3.0
    assert event.is_released is False
    assert _resolve_watchlist_event_mode(event.tte_hours, None) == "event-lock"

    # 2. 測試 BMO 盤前發布：盤中 13:30 已過 08:30，is_released=True，解除 event-lock
    service_bmo = CalendarService()
    with patch("services.calendar_service.datetime") as mock_dt, patch(
        "services.calendar_service.get_cached_earnings",
        return_value={
            "earnings_date": today.strftime("%Y-%m-%d"),
            "hour": "bmo",
            "checked_at": datetime.now().isoformat(),
        },
    ), patch("services.calendar_service.save_earnings_cache", return_value=None):
        mock_dt.now.side_effect = (
            lambda tz=None: midday_ny if tz is None else midday_ny.astimezone(tz)
        )
        mock_dt.combine = datetime.combine
        mock_dt.min = datetime.min
        mock_dt.strptime = datetime.strptime
        mock_dt.fromisoformat = datetime.fromisoformat

        event_bmo = await service_bmo.get_symbol_earnings("NVDA")

    assert event_bmo is not None
    assert event_bmo.is_released is True
    assert event_bmo.tte_hours == -5.0
    assert (
        _resolve_watchlist_event_mode(
            event_bmo.tte_hours, None, is_earnings_released=event_bmo.is_released
        )
        == "normal"
    )


@pytest.mark.asyncio
async def test_option_plan_illiquid_path_builds_wait_plan() -> None:
    """期權鏈流動性不足時應產出 0 口 WAIT 計畫，而不是拋 ValidationError。

    WatchlistOptionPlan 原本限制 suggested_contracts>=1 與 legs 至少 1 筆，
    但這條分支就是以 0 口 / 空 legs 建構，等於每次都拋例外並被上游的
    per-ticker except 吞掉，連帶讓該標的整則心跳消失。
    """
    import pandas as pd

    metrics = _sample_metrics(current_price=129.0, iv_rank=78.0, option_skew=7.2)
    tactical = WatchlistRiskController.process_metrics(metrics)

    # bid/ask 點差極大 → is_spread_illiquid 判定為不流動
    with patch(
        "market_analysis.strategy.find_best_contract",
        new_callable=AsyncMock,
        return_value={
            "strike": 132.0,
            "expiry": "2026-06-19",
            "mid": 2.3,
            "bid": 0.5,
            "ask": 4.5,
        },
    ), patch(
        "services.market_data_service.get_option_chain",
        new_callable=AsyncMock,
        return_value=type(
            "Chain", (), {"calls": pd.DataFrame([]), "puts": pd.DataFrame([])}
        )(),
    ):
        plan = await build_watchlist_option_plan(
            metrics, tactical, capital=100000.0, risk_limit=15.0, has_position=True
        )

    assert plan is not None
    assert "流動性不足" in plan.strategy_name
    assert plan.suggested_contracts == 0
    assert plan.legs == []


# ---------------------------------------------------------------------------
# Top 3 / ISSUE-01 & ISSUE-04: 戰術閘門順序副作用（Sequential Clobbering）修復驗證
# ---------------------------------------------------------------------------


def test_tactical_gate_sequential_clobbering_preserves_prior_warnings() -> None:
    """[Top 3 / ISS-01 修復驗證]
    當前置已觸發【軋空預警】或【負 Gamma 踩踏】時，後續閘門（如動能發散、IV壓抑）
    不得整包重建覆蓋並抹除前置高危警報，必須以追加方式（Guideline Append）保留。
    """
    from market_analysis.intraday_pipeline.evaluation import _apply_tactical_gate
    from models.schemas import WatchlistTacticalPlan

    # 模擬前置已累積軋空預警與負 Gamma 踩踏警告的 plan
    prior_plan = WatchlistTacticalPlan(
        scenario="wait",
        sddm_route="SHIELD 網格防禦 (負 Gamma 踩踏)",
        action_guideline=(
            "⚠️ 負 Gamma 踩踏/波動放大區 (做市商 Delta 剛性拋壓風險全面壓倒遠期痛點磁吸)\n"
            "🚨 【軋空預警】現價站上 Gamma Flip 進入正 Gamma 區間"
        ),
        dynamic_grid_step=2.0,
        alert_level="red",
        capital_retreat_required=True,
    )

    # 後續閘門 1：Skew Divergence
    after_skew = _apply_tactical_gate(
        prior_plan,
        locked=False,
        sddm_route="WAIT (機構避險背離/尾部風險警戒)",
        action_guideline="⚠️ 機構避險背離/尾部風險警戒｜Skew 分位極端高位",
        capital_retreat_required=True,
    )

    # 後續閘門 2：IV 壓抑背離
    after_iv = _apply_tactical_gate(
        after_skew,
        locked=False,
        sddm_route="WAIT (IV 壓抑背離)",
        action_guideline="⚠️ WARNING: IV Suppression Divergence｜現價暴跌但波動率低壓",
    )

    # 驗證：前置的負 Gamma 踩踏、軋空預警與 Skew 背離全部被保留，未被 IV 壓抑覆蓋抹除
    assert "負 Gamma 踩踏" in after_iv.action_guideline
    assert "【軋空預警】" in after_iv.action_guideline
    assert "機構避險背離" in after_iv.action_guideline
    assert "IV Suppression Divergence" in after_iv.action_guideline
    assert after_iv.alert_level == "red"
    assert after_iv.capital_retreat_required is True


def test_is_crisis_blocks_buy_on_capital_retreat_flag() -> None:
    """[Top 3 / ISS-04 修復驗證]
    當 tactical_model.capital_retreat_required 為 True 時，即使 sddm_route 為
    "WAIT (機構避險背離...)" 而非 "SHIELD"，calculate_dynamic_trading_signals
    亦必須視為 is_crisis，將 suitable_buy_price 標為風控鎖定，股數為 0，
    杜絕系統一邊宣告嚴密避險一邊產出買入點位的分裂行為。
    """
    from market_analysis.signal_calculator import calculate_dynamic_trading_signals
    from models.schemas import WatchlistTacticalPlan

    metrics = _sample_metrics(
        current_price=100.0, rsi_14=25.0
    )  # 超賣，若無風控通常會買
    tactical = WatchlistTacticalPlan(
        scenario="wait",
        sddm_route="WAIT (機構避險背離/尾部風險警戒)",  # 不含 "SHIELD"
        action_guideline="⚠️ 機構避險背離",
        dynamic_grid_step=2.0,
        alert_level="red",
        capital_retreat_required=True,  # 顯式資本退守旗標
    )

    signals = calculate_dynamic_trading_signals(
        metrics,
        tactical,
        has_position=False,
        capital=100000.0,
        risk_limit=15.0,
    )

    assert signals["suitable_buy_shares"] == 0
    assert isinstance(signals["suitable_buy_price"], str)
    assert "風控鎖定" in signals["suitable_buy_price"]


# ---------------------------------------------------------------------------
# Top 4 / ISSUE-02: 防洗盤緩衝時間週期量綱校正（日線 ATR vs 15 分鐘收盤）驗證
# ---------------------------------------------------------------------------


def test_atr_time_scale_alignment_with_scale_flag() -> None:
    """[Top 4 / ISS-02 修復驗證]
    當指定 scale_atr_to_15m=True 時，日線 ATR (atr_14=5.10) 依據隨機遊走時間平方根法則
    (美股 26 根 15m K棒，sqrt(26) ≈ 5.099) 折算為 ATR₁₅ₘ ≈ 1.00，
    1.5×ATR 防洗盤緩衝應為 $1.50（而非未折算的 $7.65 荒謬巨幅偏差），
    且文案明確標註 1.5×ATR₁₅ₘ。
    """
    from market_analysis.signal_calculator import calculate_dynamic_trading_signals
    from models.schemas import WatchlistTacticalPlan

    daily_atr = 5.0990195  # 剛好為 sqrt(26)，折算後 ATR_15m 應為 1.00
    metrics = _sample_metrics(
        current_price=100.0,
        rsi_14=25.0,  # 極度超賣 -> 基準買點為 buy_price_phase1 (95.0)
        buy_price_phase1=95.0,
        buy_price_phase2=90.0,
        buy_price_phase3=85.0,
        atr_14=daily_atr,
        option_skew=0.0,
    )
    tactical = WatchlistTacticalPlan(
        scenario="premium-harvest",
        sddm_route="SPEAR",
        action_guideline="Normal",
        dynamic_grid_step=2.0,
    )

    signals = calculate_dynamic_trading_signals(
        metrics,
        tactical,
        has_position=False,
        capital=100000.0,
        risk_limit=15.0,
        scale_atr_to_15m=True,
    )

    # 基準買點 95.0 - (1.0 * 1.5 = 1.5) = 93.50，避開整數/關卡 .50 -> 93.47
    assert signals["suitable_buy_price"] == 93.47
    assert "1.5×ATR₁₅ₘ = $1.50" in signals["buy_rationale"]
    assert "15 分鐘 K 線實體跌破" in signals["buy_rationale"]


def test_atr_time_scale_alignment_with_explicit_atr_15m() -> None:
    """[Top 4 / ISS-02 修復驗證]
    當呼叫端傳入專屬的 15m ATR (atr_15m=0.80) 時，優先使用真實 15m ATR，
    緩衝為 0.80 * 1.5 = $1.20，文案標註 1.5×ATR₁₅ₘ = $1.20。
    """
    from market_analysis.signal_calculator import calculate_dynamic_trading_signals
    from models.schemas import WatchlistTacticalPlan

    metrics = _sample_metrics(
        current_price=100.0,
        rsi_14=25.0,
        buy_price_phase1=95.0,
        buy_price_phase2=90.0,
        buy_price_phase3=85.0,
        atr_14=10.0,  # 即使日線 ATR 極大 (10.0)
        option_skew=0.0,
    )
    tactical = WatchlistTacticalPlan(
        scenario="premium-harvest",
        sddm_route="SPEAR",
        action_guideline="Normal",
        dynamic_grid_step=2.0,
    )

    signals = calculate_dynamic_trading_signals(
        metrics,
        tactical,
        has_position=False,
        capital=100000.0,
        risk_limit=15.0,
        atr_15m=0.80,  # 明確傳入 15m ATR
    )

    # 基準買點 95.0 - (0.80 * 1.5 = 1.20) = 93.80
    assert signals["suitable_buy_price"] == 93.80
    assert "1.5×ATR₁₅ₘ = $1.20" in signals["buy_rationale"]
