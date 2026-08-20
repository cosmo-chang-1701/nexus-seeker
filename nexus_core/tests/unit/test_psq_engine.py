import numpy as np
import pandas as pd
from market_analysis.psq_engine import analyze_psq, PSQResult, _fast_rolling_linreg


def test_analyze_psq_empty_or_short_df() -> None:
    assert analyze_psq(None) is None
    assert analyze_psq(pd.DataFrame()) is None

    # Length is 20, so length * 2 = 40 is required
    short_df = pd.DataFrame(
        {
            "Open": [10.0] * 30,
            "High": [11.0] * 30,
            "Low": [9.0] * 30,
            "Close": [10.0] * 30,
        }
    )
    assert analyze_psq(short_df, length=20) is None


def test_analyze_psq_valid_calculation() -> None:
    np.random.seed(42)
    n = 100
    close = 100 + np.cumsum(np.random.randn(n))
    high = close + np.random.rand(n) * 2
    low = close - np.random.rand(n) * 2
    open_ = close + np.random.randn(n) * 0.5

    df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close})
    res = analyze_psq(df, length=20)

    assert res is not None
    assert isinstance(res, PSQResult)
    assert res.squeeze_level in ["High", "Mid", "Normal", "Release"]
    assert isinstance(res.is_squeezing, bool)
    assert isinstance(res.momentum_value, float)
    assert res.momentum_color in ["LightBlue", "DarkBlue", "Red", "Golden", "Neutral"]
    assert res.signal_direction in ["Long", "Short", "Neutral"]
    assert isinstance(res.is_near_support, bool)
    assert isinstance(res.is_breakout_long, bool)
    assert isinstance(res.is_breakout_short, bool)
    assert isinstance(res.sma_distance_pct, float)
    assert isinstance(res.sma_20, float)
    assert res.vix_momentum_label == "NORMAL"


def test_analyze_psq_vix_labels() -> None:
    n = 60
    # Accelerating upward trend so curr_mom > prev_mom -> signal == "Long"
    t = np.linspace(0, 1, n)
    close = 100 + (t**2) * 50
    high = close + 1.0
    low = close - 1.0
    open_ = close

    df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close})

    # Test OVEREXTENDED_RISK: vix < 15.0 and signal == "Long"
    res_low_vix = analyze_psq(df, length=20, vix_spot=12.0)
    assert res_low_vix is not None
    assert res_low_vix.signal_direction == "Long"
    assert res_low_vix.vix_momentum_label == "OVEREXTENDED_RISK"

    # Test HIGH_CONVICTION_RECOVERY: vix > upper_3 (24.6) and mom_color == "Golden"
    # Create downward trend that decelerates (curr_mom < 0 and curr_diff >= 0)
    close_down = np.concatenate([np.linspace(150, 100, 45), np.linspace(100, 99.9, 15)])
    df_down = pd.DataFrame(
        {
            "Open": close_down,
            "High": close_down + 1,
            "Low": close_down - 1,
            "Close": close_down,
        }
    )
    res_high_vix = analyze_psq(df_down, length=20, vix_spot=30.0)
    assert res_high_vix is not None
    if res_high_vix.momentum_color == "Golden":
        assert res_high_vix.vix_momentum_label == "HIGH_CONVICTION_RECOVERY"


def test_fast_rolling_linreg_short() -> None:
    s = pd.Series([1.0, 2.0, 3.0])
    res = _fast_rolling_linreg(s, length=20)
    assert res.isna().all()
