from models.schemas import EnhancedWatchlistMetrics, WatchlistTacticalPlan
from market_analysis.intraday_pipeline import (
    calculate_dynamic_trading_signals,
    derive_watchlist_option_guidance,
)


def _sample_metrics(**overrides):
    payload = {
        "symbol": "AAPL",
        "exchange": "NASDAQ",
        "current_price": 175.0,
        "buy_zone_status": "🟢 買點：趨勢支撐 (VIX 修正)",
        "buy_price_phase1": 170.0,
        "buy_price_phase2": 165.0,
        "buy_price_phase3": 160.0,
        "sell_zone_status": "🟢 賣點：第一壓力帶",
        "sell_price_phase1": 180.0,
        "sell_price_phase2": 185.0,
        "sell_price_phase3": 190.0,
        "pe_ratio": 30.0,
        "rsi_14": 50.0,
        "atr_14": 5.0,
        "beta": 1.2,
        "ma20": 172.0,
        "ma50": 168.0,
        "ma200": 155.0,
        "bias_ma20": 0.0,
        "iv_rank": 45.0,
        "option_skew": 2.0,  # 2% skew
        "option_skew_state": "Normal",
        "volume_poc": 167.0,
        "gex_max_put_wall": 160.0,
        "vanna_sensitivity": 0.15,
        "relative_strength_spy": 0.02,
    }
    payload.update(overrides)
    return EnhancedWatchlistMetrics(**payload)


def _sample_tactical(**overrides):
    payload = {
        "scenario": "premium-harvest",
        "sddm_route": "SPEAR",
        "action_guideline": "Cash-Secured Put",
        "dynamic_grid_step": 3.0,
        "hidden_delta_risk": 0.0,
        "hedge_instruction": None,
        "hedge_allocation_shares": 0,
        "alert_level": "green",
    }
    payload.update(overrides)
    return WatchlistTacticalPlan(**payload)


def test_unheld_oversold_signals():
    # RSI < 30 (oversold) -> Base buy at buy_price_phase1 (170.0)
    metrics = _sample_metrics(rsi_14=25.0, option_skew=0.0)
    tactical = _sample_tactical()

    signals = calculate_dynamic_trading_signals(
        metrics,
        tactical,
        has_position=False,
        capital=100000.0,
        risk_limit=15.0,
    )

    assert signals["suitable_buy_price"] == 170.0
    assert signals["suitable_buy_shares"] > 0
    assert "RSI 極度超賣" in signals["buy_rationale"]


def test_unheld_overbought_signals():
    # RSI > 70 (overbought) -> Base buy at buy_price_phase3 (160.0)
    metrics = _sample_metrics(rsi_14=75.0, option_skew=0.0)
    tactical = _sample_tactical()

    signals = calculate_dynamic_trading_signals(
        metrics,
        tactical,
        has_position=False,
        capital=100000.0,
        risk_limit=15.0,
    )

    assert signals["suitable_buy_price"] == 160.0
    assert "RSI 超買" in signals["buy_rationale"]


def test_unheld_skew_discount():
    # High positive skew (e.g. 10.0%) -> Expect discount on suitable buy price
    metrics = _sample_metrics(rsi_14=50.0, option_skew=10.0)
    tactical = _sample_tactical()

    signals = calculate_dynamic_trading_signals(
        metrics,
        tactical,
        has_position=False,
        capital=100000.0,
        risk_limit=15.0,
    )

    # Base buy is buy_price_phase2 (165.0)
    # Skew discount = max(-0.05, min(0.15, (10 / 100) * 0.5)) = 5%
    # Expected buy = 165.0 * 0.95 = 156.75 -> capped at buy_price_phase3 * 0.9 = 144.0
    # So suitable_buy_price should be 156.75
    assert signals["suitable_buy_price"] == 156.75
    assert "Skew 避險情緒折價" in signals["buy_rationale"]


def test_held_overbought_signals():
    # RSI > 70 -> Base sell is sell_price_phase1 (180.0)
    # RSI > 75 -> 50% scale out
    metrics = _sample_metrics(rsi_14=78.0, option_skew=0.0)
    tactical = _sample_tactical()

    signals = calculate_dynamic_trading_signals(
        metrics,
        tactical,
        has_position=True,
        holding_quantity=100.0,
        holding_avg_cost=150.0,
        capital=100000.0,
        risk_limit=15.0,
    )

    assert signals["suitable_sell_price"] == 180.0
    assert signals["suitable_sell_shares"] == 50  # 50% scale out since RSI > 75
    assert "RSI 超買過熱" in signals["sell_rationale"]


def test_held_hard_hedge_signals():
    metrics = _sample_metrics(rsi_14=50.0)
    tactical = _sample_tactical(scenario="hard-hedge")

    signals = calculate_dynamic_trading_signals(
        metrics,
        tactical,
        has_position=True,
        holding_quantity=100.0,
        holding_avg_cost=150.0,
        capital=100000.0,
        risk_limit=15.0,
    )

    # Scenario hard-hedge -> exit completely (100 shares)
    assert signals["suitable_sell_shares"] == 100
    assert "硬避險" in signals["sell_rationale"]


def test_option_guidance_includes_strikes():
    metrics = _sample_metrics(rsi_14=50.0, option_skew=6.0)
    tactical = _sample_tactical(scenario="premium-harvest")

    guidance_unheld = derive_watchlist_option_guidance(
        metrics,
        tactical,
        has_position=False,
        suitable_buy_price=162.50,
    )

    assert "162.50" in guidance_unheld
    assert "Cash-Secured Put" in guidance_unheld

    guidance_held = derive_watchlist_option_guidance(
        metrics,
        tactical,
        has_position=True,
        suitable_sell_price=182.00,
    )

    assert "182.00" in guidance_held
    assert "Covered Call" in guidance_held
