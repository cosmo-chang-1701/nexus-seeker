from typing import Any
import pytest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from market_analysis.intraday_pipeline import (
    IntradayScanPipeline,
    build_watchlist_skew_rule_commentary,
)
from market_analysis.models import (
    TraderAccountState,
    OptionHolding,
    TickerMarketData,
    AdvancedTraderOutput,
)
from market_analysis.gamma_squeeze_engine import NexusGammaSqueezeEngine
from cogs.embed_builders.portfolio_embeds import get_scenario_guidance


@pytest.fixture
def squeeze_engine() -> Any:
    return NexusGammaSqueezeEngine(base_gate_3_threshold=1000000.0)


@pytest.fixture
def intraday_pipeline(squeeze_engine: Any) -> Any:
    return IntradayScanPipeline(MagicMock(), squeeze_engine)


@pytest.fixture
def default_account_state() -> Any:
    return TraderAccountState(
        capital=100000.0,
        cash_reserve=25000.0,
        monthly_burn_rate=6000.0,
        current_vix=14.0,
    )


@pytest.fixture
def default_market_data() -> Any:
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
def default_holdings() -> Any:
    return [
        OptionHolding(symbol="AAPL", quantity=2.0, theta=-0.12),
        OptionHolding(symbol="MSFT", quantity=-1.0, theta=0.08),
    ]


@pytest.fixture
def default_greeks() -> Any:
    return {"vanna": 1.5, "beta": 1.2}


def test_validate_gates_all_pass(squeeze_engine: Any, default_market_data: Any) -> Any:
    passed, failed = squeeze_engine.validate_gates(default_market_data, "Phase B")
    assert passed is True
    assert len(failed) == 0


def test_validate_gates_failures(squeeze_engine: Any):  # type: ignore
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


def test_validate_gates_phase_a_reduction(squeeze_engine: Any):  # type: ignore
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


def test_analyze_ticker_spear_route(  # type: ignore
    squeeze_engine: Any,
    default_market_data: Any,
    default_account_state: Any,
    default_holdings: Any,
    default_greeks: Any,
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


def test_analyze_ticker_shield_vix_gated(  # type: ignore
    squeeze_engine: Any,
    default_market_data: Any,
    default_account_state: Any,
    default_holdings: Any,
    default_greeks: Any,
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


def test_financial_runway_calculation(  # type: ignore
    squeeze_engine: Any,
    default_market_data: Any,
    default_account_state: Any,
    default_holdings: Any,
    default_greeks: Any,
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


def test_vanna_hedging_instruction(  # type: ignore
    squeeze_engine: Any,
    default_market_data: Any,
    default_account_state: Any,
    default_holdings: Any,
    default_greeks: Any,
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


def test_post_market_attribution_evolution(squeeze_engine: Any):  # type: ignore
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


@pytest.mark.asyncio
async def test_build_watchlist_heartbeat_embed_includes_option_plan(
    intraday_pipeline: Any,
) -> None:
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
        symbol_gex=None,
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
        "market_analysis.intraday_pipeline.build_watchlist_skew_rule_commentary",
        return_value="rule-skew-commentary",
    ) as mock_skew_commentary, patch(
        "cogs.embed_builder.create_watchlist_signal_embed",
        return_value=MagicMock(),
    ) as mock_create_embed:
        embed = await intraday_pipeline._build_watchlist_heartbeat_embed(
            evaluation, user_context
        )

    assert embed == mock_create_embed.return_value
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

    mock_skew_commentary.assert_called_once()
    mock_create_embed.assert_called_once()
    create_embed_kwargs = mock_create_embed.call_args[1]
    assert create_embed_kwargs["symbol"] == "MU"
    assert create_embed_kwargs["report_body"] == "heartbeat snapshot"
    assert create_embed_kwargs["option_guidance"] == "option guidance"
    assert create_embed_kwargs["event_risk_summary"] == "財報前風控"
    assert create_embed_kwargs["skew_state"] == "+6.25% ｜ 左偏保護"
    assert create_embed_kwargs["alert_level"] == "yellow"
    assert create_embed_kwargs["option_plan"] == "option-plan"
    assert create_embed_kwargs["skew_commentary"] == "rule-skew-commentary"
    assert create_embed_kwargs["has_position"] is False
    assert create_embed_kwargs["holding_quantity"] is None
    assert create_embed_kwargs["holding_avg_cost"] is None
    assert create_embed_kwargs["suitable_buy_price"] == 377.78
    assert create_embed_kwargs["suitable_buy_shares"] == 10
    assert create_embed_kwargs["suitable_sell_price"] == 0.0
    assert create_embed_kwargs["suitable_sell_shares"] == 0
    assert "Skew 避險情緒折價" in create_embed_kwargs["buy_rationale"]


@pytest.mark.asyncio
async def test_build_watchlist_heartbeat_embed_writes_back_uoa_cache(
    intraday_pipeline: Any,
) -> None:
    """
    測試: 心跳算出的 UOA 結果應寫回 /x 終端共用的 `uoa_{symbol}` kv_cache，
    避免 /x 對同一標的重複觸發昂貴的 UOA 自癒偵測 (併發抓多個到期日期權鏈)。
    """
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
        symbol_gex=None,
    )
    user_context = SimpleNamespace(user_id=42, capital=120000.0, risk_limit=12.0)
    uoa_result = [{"trade_type": "SWEEP", "strike": 420.0}]

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
    ), patch(
        "market_analysis.intraday_pipeline.build_watchlist_option_plan",
        new_callable=AsyncMock,
        return_value="option-plan",
    ), patch(
        "market_analysis.intraday_pipeline.build_watchlist_skew_rule_commentary",
        return_value="rule-skew-commentary",
    ), patch(
        "cogs.embed_builder.create_watchlist_signal_embed",
        return_value=MagicMock(),
    ), patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": 410.5},
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_pcr",
        new_callable=AsyncMock,
        return_value={"pcr": 1.0},
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.detect_uoa",
        new_callable=AsyncMock,
        return_value=uoa_result,
    ), patch(
        "market_analysis.sentiment_engine.SentimentEngine.get_unified_max_pain",
        new_callable=AsyncMock,
        return_value={"max_pain": 400.0},
    ), patch(
        "database.cache.save_kv_cache",
        new_callable=AsyncMock,
    ) as mock_save_kv_cache:
        await intraday_pipeline._build_watchlist_heartbeat_embed(
            evaluation, user_context
        )

    mock_save_kv_cache.assert_awaited_once_with("uoa_MU", uoa_result)


@pytest.mark.asyncio
async def test_build_watchlist_heartbeat_embed_skips_uoa_writeback_on_fetch_failure(
    intraday_pipeline: Any,
) -> None:
    """
    測試: 若心跳補充數據抓取失敗 (例如報價 API 出錯)，不應該用空列表覆寫既有的
    `uoa_{symbol}` kv_cache -- 避免把 /x 終端原本有效的快取資料誤清空。
    """
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
        symbol_gex=None,
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
    ), patch(
        "market_analysis.intraday_pipeline.build_watchlist_option_plan",
        new_callable=AsyncMock,
        return_value="option-plan",
    ), patch(
        "market_analysis.intraday_pipeline.build_watchlist_skew_rule_commentary",
        return_value="rule-skew-commentary",
    ), patch(
        "cogs.embed_builder.create_watchlist_signal_embed",
        return_value=MagicMock(),
    ), patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        side_effect=RuntimeError("quote api down"),
    ), patch(
        "database.cache.save_kv_cache",
        new_callable=AsyncMock,
    ) as mock_save_kv_cache:
        await intraday_pipeline._build_watchlist_heartbeat_embed(
            evaluation, user_context
        )

    mock_save_kv_cache.assert_not_awaited()


def _build_skew_test_metrics(**overrides: Any) -> Any:
    from models.schemas import EnhancedWatchlistMetrics

    payload: dict[str, Any] = dict(
        symbol="TEST",
        exchange="NASDAQ",
        current_price=100.0,
        buy_zone_status="Wait",
        buy_price_phase1=90.0,
        buy_price_phase2=80.0,
        buy_price_phase3=70.0,
        sell_zone_status="Wait",
        sell_price_phase1=110.0,
        sell_price_phase2=120.0,
        sell_price_phase3=130.0,
        rsi_14=50.0,
        atr_14=2.0,
        beta=1.0,
        ma20=100.0,
        ma50=100.0,
        ma200=100.0,
        iv_rank=40.0,
        iv_percentile=40.0,
        option_skew=0.0,
        skew_percentile=50.0,
        option_skew_state="正常",
        pcr=1.0,
        volume_poc=100.0,
        gex_max_put_wall=100.0,
        vanna_sensitivity=0.1,
        relative_strength_spy=1.0,
        squeeze_momentum=None,
    )
    payload.update(overrides)
    return EnhancedWatchlistMetrics(**payload)


def _build_skew_test_tactical(**overrides: Any) -> Any:
    from models.schemas import WatchlistTacticalPlan

    payload: dict[str, Any] = dict(
        scenario="wait",
        sddm_route="WAIT (正常)",
        action_guideline="指引",
        dynamic_grid_step=1.0,
        hidden_delta_risk=0.0,
        hedge_instruction=None,
        hedge_allocation_shares=0,
        alert_level="yellow",
    )
    payload.update(overrides)
    return WatchlistTacticalPlan(**payload)


def test_skew_commentary_badge_syncs_with_tactical_route() -> None:
    metrics = _build_skew_test_metrics(
        option_skew=6.25, skew_percentile=85.0, option_skew_state="左偏保護"
    )
    tactical = _build_skew_test_tactical(
        scenario="premium-harvest", sddm_route="SHIELD"
    )

    result = build_watchlist_skew_rule_commentary(metrics, tactical)

    assert "🔴" in result
    assert "✅ 與操盤路由同向 (SDDM: SHIELD)" in result
    assert "（Skew 型態：左偏保護）" in result


def test_skew_commentary_badge_diverges_from_tactical_route() -> None:
    metrics = _build_skew_test_metrics(
        option_skew=6.25, skew_percentile=85.0, option_skew_state="左偏保護"
    )
    tactical = _build_skew_test_tactical(scenario="wait", sddm_route="STANDBY")

    result = build_watchlist_skew_rule_commentary(metrics, tactical)

    assert "⚠️ 訊號不同步，建議以操盤路由為準 (SDDM: STANDBY)" in result


def test_skew_commentary_flags_momentum_divergence() -> None:
    metrics = _build_skew_test_metrics(
        option_skew=-5.0,
        skew_percentile=15.0,
        option_skew_state="右偏亢奮",
        squeeze_momentum=-3.0,
    )

    result = build_watchlist_skew_rule_commentary(metrics, tactical=None)

    assert "🟢" in result
    assert "🔻 動能背離：SQZ MOM 轉負，建議降低倉位確認" in result


def test_skew_commentary_without_tactical_stays_backward_compatible() -> None:
    metrics = _build_skew_test_metrics(option_skew=0.5, skew_percentile=50.0)

    result = build_watchlist_skew_rule_commentary(metrics)

    assert "SDDM" not in result
    assert "🟡" in result


@pytest.mark.asyncio
async def test_run_loop_exception_isolation(intraday_pipeline: Any):  # type: ignore
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # Setup mocks
    intraday_pipeline.is_running = True
    mock_now = datetime(2026, 6, 5, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    called_tickers = []

    async def mock_evaluate(ticker: Any):  # type: ignore
        called_tickers.append(ticker)
        if ticker == "AAPL":
            raise ValueError("Mock AAPL Exception")
        return None

    intraday_pipeline.evaluate_watchlist_symbol = mock_evaluate

    with patch(
        "market_analysis.intraday_pipeline.is_market_open", return_value=True
    ), patch(
        "market_analysis.intraday_pipeline.datetime"
    ) as mock_datetime_class, patch(
        "database.get_all_user_ids", return_value=[42]
    ), patch("database.get_full_user_context") as mock_ctx, patch(
        "database.get_user_watchlist", return_value=[("AAPL", 1), ("MSFT", 1)]
    ):
        mock_datetime_class.now.return_value = mock_now

        user_ctx = SimpleNamespace(
            user_id=42,
            enable_analyst_agent=True,
            total_capital=100000.0,
            risk_limit=15.0,
            monthly_burn_rate=5000.0,
            cash_reserve=20000.0,
        )
        mock_ctx.return_value = user_ctx

        async def mock_sleep(secs: Any):  # type: ignore
            intraday_pipeline.is_running = False

        with patch("asyncio.sleep", side_effect=mock_sleep):
            await intraday_pipeline._run_loop()

    assert "AAPL" in called_tickers
    assert "MSFT" in called_tickers
    assert intraday_pipeline.is_running is False


@pytest.mark.asyncio
async def test_evaluate_watchlist_symbol_iv_suppression() -> None:
    from market_analysis.intraday_pipeline import evaluate_watchlist_symbol
    from unittest.mock import AsyncMock, patch
    from models.schemas import (
        EnhancedWatchlistMetrics,
        WatchlistTacticalPlan,
        WatchlistEventContext,
    )

    mock_metrics = EnhancedWatchlistMetrics(
        symbol="AAPL",
        exchange="NASDAQ",
        current_price=100.0,
        buy_zone_status="Wait",
        buy_price_phase1=90.0,
        buy_price_phase2=80.0,
        buy_price_phase3=70.0,
        sell_zone_status="Wait",
        sell_price_phase1=110.0,
        sell_price_phase2=120.0,
        sell_price_phase3=130.0,
        rsi_14=50.0,
        atr_14=2.0,
        beta=1.0,
        ma20=100.0,
        ma50=100.0,
        ma200=100.0,
        iv_rank=10.0,  # < 15%
        iv_percentile=10.0,
        option_skew=0.0,
        skew_percentile=50.0,
        option_skew_state="Normal",
        pcr=1.0,
        volume_poc=100.0,
        gex_max_put_wall=100.0,
        vanna_sensitivity=0.1,
        relative_strength_spy=1.0,
    )

    mock_event = WatchlistEventContext(
        earnings_date=None,
        earnings_tte_hours=None,
        macro_event=None,
        macro_event_time=None,
        macro_tte_hours=None,
        risk_mode="normal",
        summary="Normal",
    )

    dummy_tactical = WatchlistTacticalPlan(
        scenario="wait",
        sddm_route="WAIT (正常)",
        action_guideline="指引",
        dynamic_grid_step=1.0,
        hidden_delta_risk=0.0,
        hedge_instruction=None,
        hedge_allocation_shares=0,
        alert_level="yellow",
    )

    with patch(
        "market_analysis.intraday_pipeline.build_enhanced_watchlist_metrics",
        new_callable=AsyncMock,
        return_value=mock_metrics,
    ), patch(
        "market_analysis.intraday_pipeline.build_watchlist_event_context",
        new_callable=AsyncMock,
        return_value=mock_event,
    ), patch(
        "market_analysis.intraday_pipeline.WatchlistRiskController.process_metrics",
        return_value=dummy_tactical,
    ), patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"dp": -5.0},  # < -3%
    ):
        res = await evaluate_watchlist_symbol("AAPL")
        assert res is not None
        assert res.tactical.alert_level == "red"
        assert "IV 壓抑背離" in res.tactical.sddm_route


def test_avgo_positive_gamma_support_avoids_forbidden_zone() -> None:
    from market_analysis.insights_engine import RiskInsightsContext, InsightsEngine

    # Mocking AVGO data (現價 $370.78, PutWall $372.50, 有大額正 Gamma, 預期區間 $363.75 ~ $377.81)
    context = RiskInsightsContext(
        symbol="AVGO",
        current_price=370.78,
        put_wall=372.50,
        net_gex_status="POSITIVE_GAMMA",
        term_structure=1.0,
        uoa_institutional_short_call=False,
        iv_rank=0.5,
        max_pain_deviation_pct=-0.04,  # -4% means it's within +-5%
        can_trade_spreads=True,
        cash_reserve_protection=True,
        expected_move_lower=363.75,
        has_positive_gamma_support=True,
        cb_triggered=False,
    )

    dmp_label, status_label, suggestion = InsightsEngine.generate_cro_insight(context)

    # 斷言 該標的在批次雷達中不會觸發 🛑 觸發鐵律一：左側禁區 0%
    if status_label:
        assert "🛑 觸發鐵律一：左側禁區 0%" not in status_label

    # 斷言 該標的的風控指引顯示為 價格接近最大痛點，維持震盪
    assert status_label == "價格接近最大痛點，維持震盪"


def test_dark_pool_dirty_data_filter() -> None:
    """測試暗池價格嚴重偏離 (大於20%) 時的過濾機制"""
    from market_analysis.dark_pool_engine import sanitize_darkpool_prints

    current_price = 394.06
    prints = [
        {
            "price": 100.53,
            "volume": 383236,
            "premium": 38000000,
        },  # Dirty (SATS/RCAT mis-mapped)
        {"price": 393.10, "volume": 50000, "premium": 19655000},  # Valid
    ]

    valid_prints = sanitize_darkpool_prints("AVGO", prints, current_price, 0.20)

    assert len(valid_prints) == 1
    assert valid_prints[0]["price"] == 393.10


def test_fixed_income_hedging_whitelist() -> None:
    """測試 BOXX 等避險資產的白名單豁免邏輯"""
    from market_analysis.insights_engine import RiskInsightsContext, InsightsEngine

    context = RiskInsightsContext(
        symbol="BOXX",
        current_price=117.30,
        put_wall=117.30,
        net_gex_status="NEGATIVE_GAMMA_ZONE",
        term_structure=1.0,
        uoa_institutional_short_call=False,
        iv_rank=0.0,
        max_pain_deviation_pct=0.0,
        can_trade_spreads=False,
        cash_reserve_protection=True,
        expected_move_lower=None,
        expected_move_upper=None,
        sqz_mom=0.0,
    )

    dmp_label, status_label, suggestion = InsightsEngine.generate_cro_insight(context)
    assert dmp_label == "(避險資產)"
    assert status_label == "現金避險部位，風控豁免 🛡️"
    assert "底牆保衛" not in (status_label or "")


def test_bullish_momentum_tag_priority() -> None:
    """測試強勢突破標的正確短路，不觸發跌破底牆"""
    from market_analysis.insights_engine import RiskInsightsContext, InsightsEngine

    context = RiskInsightsContext(
        symbol="AVGO",
        current_price=388.69,
        put_wall=360.0,  # fallback putwall
        net_gex_status="NEGATIVE_GAMMA_ZONE",
        term_structure=1.0,
        uoa_institutional_short_call=False,
        iv_rank=0.5,
        max_pain_deviation_pct=0.029,
        can_trade_spreads=True,
        cash_reserve_protection=True,
        expected_move_lower=377.29,
        expected_move_upper=400.09,
        sqz_mom=9.2,  # 🟢 多頭
    )

    dmp_label, status_label, suggestion = InsightsEngine.generate_cro_insight(context)
    assert status_label == "🟢 多頭推進 / 蓄力突破"
    assert "底牆保衛" not in (status_label or "")


def test_gex_empty_heatmap_degradation() -> Any:
    """測試 GEX Profile 全為 0 時，是否正確觸發 [GEX 鏈盤前未刷新] 降級"""
    display_strikes = [385.0, 390.0, 395.0, 400.0]
    gex_prof = {str(k): 0.0 for k in display_strikes}
    gex_putwall = 394.06

    def _safe_gex(k_val: float) -> float:
        val = gex_prof.get(str(k_val), gex_prof.get(k_val))  # type: ignore
        try:
            return float(val) if val is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    is_gex_empty = all(abs(_safe_gex(k)) == 0.0 for k in display_strikes)
    has_putwall = gex_putwall and float(gex_putwall) > 0

    assert is_gex_empty is True
    assert has_putwall is True


def test_max_pain_calendar_label() -> Any:
    """測試非週五到期的 DTE <= 7 合約是否正確標記為 [期中特約/末日週線]"""
    # 假設今天是 2026-07-09 (週四)
    today = date(2026, 7, 9)

    def get_calendar_label(today_date: Any, exp_date: Any) -> Any:
        dte = (exp_date - today_date).days
        is_friday = exp_date.weekday() == 4
        if dte <= 7:
            if is_friday:
                return "週五即期"
            else:
                return "期中特約/末日週線"
        elif dte <= 14:
            return "次週主力"
        else:
            return "月線主力"

    # 測試週五到期 (2026-07-10, DTE 1)
    friday_exp = date(2026, 7, 10)
    assert get_calendar_label(today, friday_exp) == "週五即期"

    # 測試下週三到期 (2026-07-15, DTE 6)
    wed_exp = date(2026, 7, 15)
    assert get_calendar_label(today, wed_exp) == "期中特約/末日週線"


def test_scenario_guidance_above_max_pain() -> None:
    guidance = get_scenario_guidance(394.39, 375.00)
    assert "價格高於最大痛點，結算日前需防範向痛點震盪拉回" in guidance


def test_scenario_guidance_below_max_pain() -> None:
    guidance = get_scenario_guidance(350.00, 375.00)
    assert "價格遠低於最大痛點，具備磁吸效應回升動能" in guidance


@pytest.mark.asyncio
@patch("database.market_cache.get_fundamental_cache")
@patch("market_analysis.intraday_pipeline.build_enhanced_watchlist_metrics")
@patch("market_analysis.index_microstructure.get_market_regime")
@patch("market_analysis.index_microstructure.fetch_symbol_gex_metrics")
async def test_global_defense_gate_blocks_bullish_signals(
    mock_fetch_gex: AsyncMock,
    mock_get_regime: AsyncMock,
    mock_build_metrics: AsyncMock,
    mock_get_fc: MagicMock,
) -> None:
    mock_fetch_gex.return_value = {"net_gex": 0.0, "call_wall": 0.0, "put_wall": 0.0}
    from market_analysis.intraday_pipeline import evaluate_watchlist_symbol

    from models.schemas import EnhancedWatchlistMetrics

    # Set up mock metrics that would normally trigger a bullish "spear" mode
    mock_metrics = EnhancedWatchlistMetrics(
        symbol="TSLA",
        exchange="NASDAQ",
        current_price=200.0,
        buy_zone_status="buy",
        buy_price_phase1=195.0,
        buy_price_phase2=190.0,
        buy_price_phase3=185.0,
        sell_zone_status="wait",
        sell_price_phase1=210.0,
        sell_price_phase2=220.0,
        sell_price_phase3=230.0,
        atr_14=5.0,
        skew_percentile=50.0,
        pcr=1.0,
        beta=1.2,
        option_skew_state="normal",
        volume_poc=195.0,
        relative_strength_spy=1.1,
        gex_max_put_wall=180.0,
        iv_rank=30.0,
        is_premarket=False,
    )

    mock_build_metrics.return_value = mock_metrics
    mock_get_regime.return_value = "NORMAL"

    # CASE 1: Thesis is NOT broken
    mock_get_fc.return_value = {"is_broken": 0, "reasoning": "Still good"}

    res_healthy = await evaluate_watchlist_symbol("TSLA")

    assert res_healthy is not None
    # Should normally not be "wait" if it passes conditions (it might be Spear or Shield, but definitely not LIQUIDATE)
    assert "LIQUIDATE (基本面破滅強制清算)" not in res_healthy.tactical.sddm_route

    # CASE 2: Thesis IS broken
    mock_get_fc.return_value = {
        "is_broken": 1,
        "reasoning": "Deteriorating margins and lost market share.",
    }

    res_broken = await evaluate_watchlist_symbol("TSLA")

    assert res_broken is not None
    # Global Defense Gate should override
    assert res_broken.tactical.scenario == "wait"
    assert res_broken.tactical.sddm_route == "LIQUIDATE (基本面破滅強制清算)"
    assert "LLM 護城河破滅警告" in res_broken.tactical.action_guideline
    assert "Deteriorating margins" in res_broken.tactical.action_guideline
    assert res_broken.tactical.alert_level == "red"


def _build_squeeze_test_metrics(**overrides: Any) -> Any:
    from models.schemas import EnhancedWatchlistMetrics

    payload: dict[str, Any] = dict(
        symbol="TSLA",
        exchange="NASDAQ",
        current_price=185.0,
        buy_zone_status="wait",
        buy_price_phase1=170.0,
        buy_price_phase2=160.0,
        buy_price_phase3=150.0,
        sell_zone_status="wait",
        sell_price_phase1=200.0,
        sell_price_phase2=210.0,
        sell_price_phase3=220.0,
        atr_14=5.0,
        skew_percentile=50.0,
        pcr=1.0,
        beta=1.2,
        option_skew_state="normal",
        volume_poc=195.0,
        relative_strength_spy=1.1,
        gex_max_put_wall=150.0,
        iv_rank=60.0,
        oi_pcr=1.2,
        is_premarket=False,
    )
    payload.update(overrides)
    return EnhancedWatchlistMetrics(**payload)


@pytest.mark.asyncio
@patch("database.cache.save_kv_cache", new_callable=AsyncMock)
@patch("database.cache.get_kv_cache")
@patch("database.market_cache.get_fundamental_cache")
@patch("market_analysis.intraday_pipeline.build_enhanced_watchlist_metrics")
@patch("market_analysis.index_microstructure.get_market_regime")
@patch("market_analysis.index_microstructure.fetch_symbol_gex_metrics")
async def test_squeeze_warning_requires_gamma_flip_pcr_and_iv_confirmation(
    mock_fetch_gex: AsyncMock,
    mock_get_regime: AsyncMock,
    mock_build_metrics: AsyncMock,
    mock_get_fc: MagicMock,
    mock_get_kv: MagicMock,
    mock_save_kv: AsyncMock,
) -> None:
    """Item 4：軋空預警校正，須同時滿足 GEX Flip 翻轉 + OI PCR>=1.0 + IV 隨價同步走揚。"""
    from market_analysis.intraday_pipeline import evaluate_watchlist_symbol

    mock_get_regime.return_value = "NORMAL"
    mock_get_fc.return_value = {"is_broken": 0, "reasoning": "ok"}

    # gex_profile 由 90(-1M) 累加至 200(+2M)，累積值於 100~200 之間由負轉正
    # -> estimate_symbol_gamma_flip 估算 gamma_flip = 200.0
    gex_profile = {"90": -1_000_000.0, "150": 0.0, "200": 2_000_000.0}

    # CASE 1：舊條件為真 (spot > call_wall 且 net_gex < 0)，新條件應不觸發
    mock_fetch_gex.return_value = {
        "net_gex": -500_000.0,
        "call_wall": 150.0,
        "put_wall": 100.0,
        "gex_profile": {},
    }
    mock_build_metrics.return_value = _build_squeeze_test_metrics(
        current_price=155.0, gex_max_put_wall=100.0
    )
    mock_get_kv.return_value = 40.0

    res_old_condition = await evaluate_watchlist_symbol("TSLA")
    assert res_old_condition is not None
    assert "軋空預警" not in res_old_condition.tactical.action_guideline

    # CASE 2：gamma_flip 缺值 (profile 為空 -> 估算為 0.0)，fail-safe 不觸發
    mock_fetch_gex.return_value = {
        "net_gex": 2_000_000.0,
        "call_wall": 150.0,
        "put_wall": 100.0,
        "gex_profile": {},
    }
    mock_build_metrics.return_value = _build_squeeze_test_metrics(
        current_price=160.0, gex_max_put_wall=100.0, oi_pcr=1.5
    )
    mock_get_kv.return_value = 30.0

    res_no_flip = await evaluate_watchlist_symbol("TSLA")
    assert res_no_flip is not None
    assert "軋空預警" not in res_no_flip.tactical.action_guideline

    # CASE 3：三條件皆滿足 -> 觸發軋空預警
    mock_fetch_gex.return_value = {
        "net_gex": 2_000_000.0,
        "call_wall": 190.0,
        "put_wall": 150.0,
        "gex_profile": gex_profile,
    }
    mock_build_metrics.return_value = _build_squeeze_test_metrics(
        current_price=205.0, gex_max_put_wall=150.0, iv_rank=60.0, oi_pcr=1.2
    )
    mock_get_kv.return_value = 40.0  # prev iv_rank (40.0) < 目前 60.0 -> IV 走揚

    res_confirmed = await evaluate_watchlist_symbol("TSLA")
    assert res_confirmed is not None
    assert "軋空預警" in res_confirmed.tactical.action_guideline
    assert "Gamma Flip" in res_confirmed.tactical.action_guideline


def test_evaluate_advanced_filters_excludes_whale_hedge_and_low_dte_uoa() -> None:
    """Item 3：net_uoa_delta 加總須排除 Whale_Hedge 標記與 DTE<7 的雜訊項目。"""
    from market_analysis.intraday_pipeline import evaluate_advanced_filters
    from models.schemas import ScanParams

    metrics = SimpleNamespace(
        squeeze_status=False,
        squeeze_momentum=0.0,
        current_price=100.0,
        dark_pool_skew=None,
    )
    params = ScanParams(min_net_uoa_delta=0.5)

    uoa_data = [
        # 應排除：Whale_Hedge (深價內避險 Put)，即使 DTE 合格
        {
            "trade_type": "SWEEP",
            "delta": 0.90,
            "intent": "Whale_Hedge (巨鯨避險)",
            "dte": 10,
        },
        # 應排除：DTE < 7 雜訊
        {"trade_type": "SWEEP", "delta": 0.80, "intent": "一般買盤", "dte": 2},
        # 應納入：跨週期且非避險標記
        {"trade_type": "SWEEP", "delta": 0.70, "intent": "一般買盤", "dte": 10},
    ]

    passed, _tags = evaluate_advanced_filters(metrics, {}, uoa_data, params)
    # net_uoa_delta 僅計入第三筆 (0.70) >= min_net_uoa_delta(0.5) -> 通過
    assert passed is True

    # 若移除唯一合格項目，net_uoa_delta 應歸零而無法達標
    uoa_data_without_valid = uoa_data[:2]
    passed_without_valid, _tags2 = evaluate_advanced_filters(
        metrics, {}, uoa_data_without_valid, params
    )
    assert passed_without_valid is False
