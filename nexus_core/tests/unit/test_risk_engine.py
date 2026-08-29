import pytest
from unittest.mock import patch
from market_analysis.risk_engine import (
    evaluate_ditm_defense,
    DITMDefenseAction,
    calculate_vega_adjusted_delta,
    get_macro_risk_metrics,
    MacroContext,
    get_macro_modifiers,
    optimize_position_risk,
    kelly_position_fraction,
)
from models.quant import OptimizationResult, MacroRiskMetrics


def test_kelly_position_fraction_matches_raw_formula() -> None:
    """
    共用凱利公式函式應與手動展開之公式 f* = (p*b - (1-p))/b 數值一致，
    這是 services/execution_router.py 與 market_analysis/strategy.py
    先前各自內嵌、現已整併為單一來源的核心公式。
    """
    p, b = 0.55, 1.8
    raw_kelly_f = (p * b - (1.0 - p)) / b
    result = kelly_position_fraction(win_prob=p, odds=b, kelly_scale=1.0, cap=1.0)
    assert result == pytest.approx(raw_kelly_f)


def test_kelly_position_fraction_applies_scale_and_cap() -> None:
    # Half-Kelly，理論上限 0.15，且 p=0.55/b=1.8 之原始 kelly_f 遠大於此
    result = kelly_position_fraction(win_prob=0.55, odds=1.8, kelly_scale=0.5, cap=0.15)
    assert result == pytest.approx(0.15)


def test_kelly_position_fraction_never_negative() -> None:
    # 勝率過低時，凱利公式本身可能為負，應鉗制為 0 而非允許放空倉位
    result = kelly_position_fraction(win_prob=0.1, odds=1.0, kelly_scale=0.5, cap=0.15)
    assert result == 0.0


def test_kelly_position_fraction_zero_odds_is_safe() -> None:
    assert (
        kelly_position_fraction(win_prob=0.5, odds=0.0, kelly_scale=0.5, cap=0.15)
        == 0.0
    )


def test_evaluate_ditm_defense() -> None:
    # Case 1: Not DITM
    assert evaluate_ditm_defense(1, 0.5, 30, 0.5) == DITMDefenseAction.HOLD

    # Case 2: DITM but plenty of time
    assert evaluate_ditm_defense(1, 0.9, 30, 2.0) == DITMDefenseAction.HOLD

    # Case 3: DITM and short time (between 7 and 21 days)
    assert evaluate_ditm_defense(1, 0.9, 15, 2.0) == DITMDefenseAction.ROLL_UP_OUT

    # Case 4: DITM and very short time (<= 7 days)
    assert evaluate_ditm_defense(1, 0.9, 5, 2.0) == DITMDefenseAction.DEFENSIVE_CLOSE

    # Case 5: Short position (quantity <= 0)
    assert evaluate_ditm_defense(-1, 0.9, 5, 2.0) == DITMDefenseAction.HOLD


def test_calculate_vega_adjusted_delta() -> None:
    # Delta_adj = Delta + Vanna * Delta_Vol
    total_delta = 100.0
    total_vanna = 50.0
    vol_change = 0.10  # 10% increase in IV

    adj_delta = calculate_vega_adjusted_delta(total_delta, total_vanna, vol_change)
    assert adj_delta == 100.0 + (50.0 * 0.10)
    assert adj_delta == 105.0


def test_get_macro_risk_metrics() -> None:
    metrics = get_macro_risk_metrics(
        total_beta_delta=10.0,
        total_theta=50.0,
        total_margin_used=5000.0,
        total_gamma=2.0,
        user_capital=100000.0,
        spy_price=500.0,
        vix_spot=20.0,
        total_vega=100.0,
        total_vanna=20.0,
    )

    assert isinstance(metrics, MacroRiskMetrics)
    assert metrics.net_exposure_dollars == 10.0 * 500.0
    assert metrics.exposure_pct == (5000.0 / 100000.0) * 100
    assert metrics.vix_tier_name == "摩拳擦掌 (Ready)"
    assert metrics.portfolio_heat == (5000.0 / 100000.0) * 100
    assert metrics.total_vanna == 20.0


def test_get_macro_modifiers_all_cases() -> None:
    # VIX tiers
    assert (
        get_macro_modifiers(MacroContext(vix=35.0, oil_price=70.0, vix_change=0.0))[0]
        == 2.0
    )
    assert (
        get_macro_modifiers(MacroContext(vix=30.0, oil_price=70.0, vix_change=0.0))[0]
        == 1.5
    )
    assert (
        get_macro_modifiers(MacroContext(vix=24.0, oil_price=70.0, vix_change=0.0))[0]
        == 1.2
    )
    assert (
        get_macro_modifiers(MacroContext(vix=18.0, oil_price=70.0, vix_change=0.0))[0]
        == 1.0
    )
    assert (
        get_macro_modifiers(MacroContext(vix=15.0, oil_price=70.0, vix_change=0.0))[0]
        == 0.5
    )

    # Oil prices
    assert (
        get_macro_modifiers(MacroContext(vix=20.0, oil_price=70.0, vix_change=0.0))[1]
        == 1.0
    )
    assert (
        get_macro_modifiers(MacroContext(vix=20.0, oil_price=80.0, vix_change=0.0))[1]
        == 0.9
    )
    assert (
        get_macro_modifiers(MacroContext(vix=20.0, oil_price=90.0, vix_change=0.0))[1]
        == 0.7
    )
    assert (
        get_macro_modifiers(MacroContext(vix=20.0, oil_price=100.0, vix_change=0.0))[1]
        == 0.5
    )

    # Regime / VTS / Trend
    _, _, w_regime = get_macro_modifiers(
        MacroContext(
            vix=20.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9, vix_trend_up=False
        )
    )
    assert w_regime == 1.0
    _, _, w_regime = get_macro_modifiers(
        MacroContext(
            vix=20.0, oil_price=70.0, vix_change=0.0, vts_ratio=1.1, vix_trend_up=False
        )
    )
    assert w_regime == 0.6
    _, _, w_regime = get_macro_modifiers(
        MacroContext(
            vix=20.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9, vix_trend_up=True
        )
    )
    assert w_regime == 0.6


def test_optimize_position_risk_all_branches() -> None:
    # BTO strategy with market heat (low PCR)
    macro = MacroContext(vix=20.0, oil_price=70.0, vix_change=0.0, vts_ratio=0.9)
    res = optimize_position_risk(
        current_delta=0.0,
        unit_weighted_delta=1.0,
        user_capital=100000.0,
        spy_price=500.0,
        stock_iv=0.2,
        strategy="BTO_CALL",
        macro_data=macro,
        pcr=0.5,  # Low PCR
    )
    assert isinstance(res, OptimizationResult)
    assert res.suggested_contracts > 0

    # High tail risk
    res_normal = optimize_position_risk(
        0.0, 1.0, 100000.0, 500.0, 0.2, "BTO_CALL", macro
    )
    res_tail = optimize_position_risk(
        0.0, 1.0, 100000.0, 500.0, 0.2, "BTO_CALL", macro, is_high_tail_risk=True
    )
    assert res_tail.suggested_contracts < res_normal.suggested_contracts

    # All-in mode (VIX > 35)
    macro_extreme = MacroContext(vix=36.0, oil_price=70.0, vix_change=0.0)
    res_extreme = optimize_position_risk(
        0.0, 1.0, 100000.0, 500.0, 0.2, "BTO_CALL", macro_extreme, vix_spot=36.0
    )
    assert res_extreme.suggested_contracts > 0


def test_evaluate_defense_status() -> None:
    from market_analysis.risk_engine import evaluate_defense_status

    # Short position (quantity < 0)
    assert "建議停利" in evaluate_defense_status(-1, "put", 0.6, -0.2, 30)
    assert "強制停損" in evaluate_defense_status(-1, "put", -1.6, -0.2, 30)
    assert "動態轉倉" in evaluate_defense_status(-1, "put", 0.0, -0.45, 30)
    assert "Gamma 陷阱" in evaluate_defense_status(-1, "put", 0.0, -0.2, 15)

    # Long position (quantity > 0)
    assert "建議停利" in evaluate_defense_status(1, "call", 1.1, 0.5, 30)
    assert "停損警戒" in evaluate_defense_status(1, "call", -0.6, 0.5, 30)
    assert "動能衰竭" in evaluate_defense_status(1, "call", 0.0, 0.5, 15)

    # Hold
    assert "繼續持有" in evaluate_defense_status(1, "call", 0.1, 0.5, 30)


def test_calculate_beta() -> None:
    import pandas as pd
    import numpy as np

    # Create synthetic data with beta = 1.5
    dates = pd.date_range(start="2024-01-01", periods=100)
    spy_returns = np.random.normal(0.001, 0.01, 100)
    stock_returns = spy_returns * 1.5 + np.random.normal(0, 0.002, 100)

    spy_close = 100 * np.exp(np.cumsum(spy_returns))
    stock_close = 100 * np.exp(np.cumsum(stock_returns))

    df_spy = pd.DataFrame({"Close": spy_close}, index=dates)
    df_stock = pd.DataFrame({"Close": stock_close}, index=dates)

    from market_analysis.risk_engine import calculate_beta

    beta = calculate_beta(df_stock, df_spy)
    assert 1.4 <= beta <= 1.6


def test_simulate_exposure_impact() -> None:
    from market_analysis.risk_engine import simulate_exposure_impact

    new_trade = {"strategy": "BTO_CALL", "weighted_delta": 0.5}
    proj_delta, proj_exp = simulate_exposure_impact(
        current_total_delta=10.0,
        new_trade_data=new_trade,
        user_capital=100000.0,
        spy_price=500.0,
        suggested_contracts=2,
    )
    # 10.0 + (0.5 * 1 * 2) = 11.0
    assert proj_delta == 11.0
    assert proj_exp == (11.0 * 500.0 / 100000.0) * 100


@pytest.mark.asyncio
async def test_analyze_sector_correlation() -> None:
    from market_analysis.risk_engine import analyze_sector_correlation
    import pandas as pd
    import numpy as np

    symbols = ["AAPL", "MSFT"]
    dates = pd.date_range(start="2024-01-01", periods=60)
    returns = np.random.normal(0.001, 0.01, 60)

    df1 = pd.DataFrame({"Close": 100 * np.exp(np.cumsum(returns))}, index=dates)
    # High correlation with some noise
    df2 = pd.DataFrame(
        {
            "Close": 100
            * np.exp(np.cumsum(returns * 0.9 + np.random.normal(0, 0.001, 60)))
        },
        index=dates,
    )

    with patch(
        "services.market_data_service.get_history_df", autospec=True
    ) as mock_hist:
        mock_hist.side_effect = [df1, df2]

        pairs = await analyze_sector_correlation(symbols)
        assert len(pairs) == 1
        assert pairs[0][0] == "AAPL"
        assert pairs[0][1] == "MSFT"
        assert pairs[0][2] > 0.75


def test_sector_benchmark_mapping() -> None:
    from market_analysis.risk_engine import get_sector_benchmark

    assert get_sector_benchmark("MU") == "SMH"
    assert get_sector_benchmark("NVDA") == "SMH"
    assert get_sector_benchmark("AAPL") == "XLK"
    assert get_sector_benchmark("UNKNOWN_TICKER") == "SPY"


def test_calculate_relative_strength_index() -> None:
    import pandas as pd
    from market_analysis.risk_engine import calculate_relative_strength_index

    # Stock goes up by 20%, Benchmark goes up by 10%
    # RS = (120 / 100) / (110 / 100) = 1.2 / 1.1 = 1.0909
    df_stock = pd.DataFrame({"Close": [100.0] * 20 + [120.0]})
    df_bench = pd.DataFrame({"Close": [100.0] * 20 + [110.0]})

    rs = calculate_relative_strength_index(df_stock, df_bench, n=20)
    assert rs == pytest.approx(1.0909, abs=0.001)

    # Test edge cases where index length is insufficient
    assert calculate_relative_strength_index(pd.DataFrame(), pd.DataFrame()) == 1.0


@pytest.mark.asyncio
async def test_boxx_capital_adjustment_and_beta() -> None:
    """測試持倉中有 BOXX 時，可用資本折算（套用 90% 折價）的計算是否正確"""
    from unittest.mock import patch, AsyncMock
    from services.trading_service import get_adjusted_user_capital

    mock_holdings = [
        {"symbol": "AAPL", "quantity": 10},
        {"symbol": "BOXX", "quantity": 100, "avg_cost": 210.0},
    ]
    mock_quote = {"c": 210.0}

    with patch(
        "database.holdings.get_user_holdings", return_value=mock_holdings
    ), patch(
        "services.market_data_service.get_quote", AsyncMock(return_value=mock_quote)
    ):
        # Base capital = 50000.0
        # BOXX value = 100 * 210.0 = 21000.0
        # Collateral value = 21000.0 * 0.90 = 18900.0
        # Adjusted capital = 50000.0 + 18900.0 = 68900.0
        adj_capital = await get_adjusted_user_capital(12345, 50000.0)
        assert adj_capital == 68900.0


def test_optimize_position_risk_with_calendar() -> None:
    # Base case: No event
    res_base = optimize_position_risk(
        current_delta=0.0,
        unit_weighted_delta=1.0,
        user_capital=100000.0,
        spy_price=500.0,
        stock_iv=0.2,
        strategy="BTO_CALL",
        risk_limit=10.0,
        event_tte_hours=None,
    )
    qty_base = res_base.suggested_contracts

    # Event case: TTE < 72h
    # This should trigger Vanna weighting which reduces safe quantity
    res_event = optimize_position_risk(
        current_delta=0.0,
        unit_weighted_delta=1.0,
        user_capital=100000.0,
        spy_price=500.0,
        stock_iv=0.2,
        strategy="BTO_CALL",
        risk_limit=10.0,
        event_tte_hours=24.0,
    )
    qty_event = res_event.suggested_contracts

    assert qty_event < qty_base
    assert qty_event > 0


def test_optimize_position_risk_very_near_event() -> None:
    # TTE = 1h should have higher weight than TTE = 71h
    res_71h = optimize_position_risk(
        0.0, 1.0, 100000.0, 500.0, 0.2, "BTO_CALL", event_tte_hours=71.0
    )
    res_1h = optimize_position_risk(
        0.0, 1.0, 100000.0, 500.0, 0.2, "BTO_CALL", event_tte_hours=1.0
    )

    qty_71h = res_71h.suggested_contracts
    qty_1h = res_1h.suggested_contracts

    assert qty_1h < qty_71h
