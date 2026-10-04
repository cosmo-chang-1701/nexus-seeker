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


def _squeeze_then_breakout(n_flat: int = 60, n_up: int = 6) -> pd.DataFrame:
    """前段窄幅盤整（BB 縮入 KC，擠壓中），後段連續放大陽線向上突破（擠壓解除）。"""
    rng = np.random.default_rng(7)
    flat = 100 + rng.normal(0, 0.05, n_flat)
    up = flat[-1] + np.cumsum(np.full(n_up, 2.5))
    close = np.concatenate([flat, up])
    high = close + 0.6
    low = close - 0.6
    return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close})


def _first_release_index(df: pd.DataFrame) -> int:
    """回傳第一根「前一根擠壓、本根解除」的 K 棒位置（以逐根截斷重算判定）。"""
    for end in range(41, len(df) + 1):
        prev = analyze_psq(df.iloc[: end - 1])
        cur = analyze_psq(df.iloc[:end])
        if prev is not None and cur is not None:
            if prev.is_squeezing and not cur.is_squeezing:
                return end
    raise AssertionError("fixture 未產生擠壓解除")


def test_green_dot_only_on_release_bar_by_default() -> None:
    df = _squeeze_then_breakout()
    end = _first_release_index(df)

    at_release = analyze_psq(df.iloc[:end])
    assert at_release is not None
    assert at_release.momentum_value > 0
    assert at_release.green_dot is True
    assert at_release.green_dot_bars_ago == 0

    one_bar_later = analyze_psq(df.iloc[: end + 1])
    assert one_bar_later is not None
    assert not one_bar_later.is_squeezing
    assert one_bar_later.green_dot is False


def test_green_dot_lookback_window() -> None:
    df = _squeeze_then_breakout()
    end = _first_release_index(df)

    res = analyze_psq(df.iloc[: end + 2], green_dot_lookback=3)
    assert res is not None
    assert res.green_dot is True
    assert res.green_dot_bars_ago == 2

    too_late = analyze_psq(df.iloc[: end + 3], green_dot_lookback=3)
    assert too_late is not None
    assert too_late.green_dot is False


def test_green_dot_requires_positive_momentum() -> None:
    df = _squeeze_then_breakout()
    # 鏡像成向下突破：擠壓解除但動能為負，不算 green dot
    mirrored = df.copy()
    for col in ("Open", "High", "Low", "Close"):
        mirrored[col] = 200 - df[col]
    mirrored["High"], mirrored["Low"] = 200 - df["Low"], 200 - df["High"]
    end = _first_release_index(mirrored)
    res = analyze_psq(mirrored.iloc[:end], green_dot_lookback=3)
    assert res is not None
    assert res.momentum_value < 0
    assert res.green_dot is False


def test_squeeze_range_low_tracks_latest_squeeze_run() -> None:
    df = _squeeze_then_breakout()
    end = _first_release_index(df)
    res = analyze_psq(df.iloc[:end])
    assert res is not None
    assert res.squeeze_range_low is not None
    # 擠壓區間在盤整段內，最低價應落在盤整段的 Low 範圍
    flat_low = float(df["Low"].iloc[:60].min())
    assert flat_low <= res.squeeze_range_low <= float(df["Low"].iloc[:60].max())


def test_turbo_flags_flip_back_to_light_blue() -> None:
    # 上漲 → 回落（動能轉弱為深藍）→ 再度加速（翻回淺藍）
    up1 = np.linspace(100, 120, 40)
    pause = np.linspace(120, 116, 8)
    up2 = 116 + np.cumsum(np.full(5, 2.0))
    close = np.concatenate([up1, pause, up2])
    df = pd.DataFrame(
        {"Open": close, "High": close + 0.5, "Low": close - 0.5, "Close": close}
    )
    seen_turbo = False
    for end in range(42, len(df) + 1):
        res = analyze_psq(df.iloc[:end])
        prev = analyze_psq(df.iloc[: end - 1])
        if res is None or prev is None:
            continue
        expected = (
            res.momentum_color == "LightBlue" and prev.momentum_color != "LightBlue"
        )
        assert res.turbo is expected
        seen_turbo = seen_turbo or res.turbo
    assert seen_turbo


def test_compute_psq_series_matches_analyze_psq_last_bar() -> None:
    """逐根序列的每一列，必須等於把資料截到該列再呼叫 analyze_psq 的結果。"""
    from market_analysis.psq_engine import compute_psq_series

    rng = np.random.default_rng(3)
    n = 140
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    close[60:90] = close[60] + rng.normal(0, 0.05, 30)  # 製造一段擠壓
    df = pd.DataFrame(
        {"Open": close, "High": close + 0.7, "Low": close - 0.7, "Close": close}
    )
    series = compute_psq_series(df, green_dot_lookback=3)
    assert series is not None
    for end in range(45, n + 1):
        res = analyze_psq(df.iloc[:end], green_dot_lookback=3)
        assert res is not None
        row = series.iloc[end - 1]
        assert row["squeeze_level"] == res.squeeze_level, end
        assert bool(row["is_squeezing"]) == res.is_squeezing, end
        assert row["momentum_color"] == res.momentum_color, end
        assert bool(row["green_dot"]) == res.green_dot, end
        assert bool(row["turbo"]) == res.turbo, end
