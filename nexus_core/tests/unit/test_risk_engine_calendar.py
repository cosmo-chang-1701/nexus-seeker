from market_analysis.risk_engine import optimize_position_risk


def test_optimize_position_risk_with_calendar():
    # Base case: No event
    qty_base, _ = optimize_position_risk(
        current_delta=0.0,
        unit_weighted_delta=1.0,
        user_capital=100000.0,
        spy_price=500.0,
        stock_iv=0.2,
        strategy="BTO_CALL",
        risk_limit=10.0,
        event_tte_hours=None,
    )

    # Event case: TTE < 72h
    # This should trigger Vanna weighting which reduces safe quantity
    qty_event, _ = optimize_position_risk(
        current_delta=0.0,
        unit_weighted_delta=1.0,
        user_capital=100000.0,
        spy_price=500.0,
        stock_iv=0.2,
        strategy="BTO_CALL",
        risk_limit=10.0,
        event_tte_hours=24.0,
    )

    assert qty_event < qty_base
    assert qty_event > 0


def test_optimize_position_risk_very_near_event():
    # TTE = 1h should have higher weight than TTE = 71h
    qty_71h, _ = optimize_position_risk(
        0.0, 1.0, 100000.0, 500.0, 0.2, "BTO_CALL", event_tte_hours=71.0
    )
    qty_1h, _ = optimize_position_risk(
        0.0, 1.0, 100000.0, 500.0, 0.2, "BTO_CALL", event_tte_hours=1.0
    )

    assert qty_1h < qty_71h
