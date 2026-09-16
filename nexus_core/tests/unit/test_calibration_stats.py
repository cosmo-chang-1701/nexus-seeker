"""校準統計工具。"""

import pandas as pd
import pytest

from calibration.stats import (
    clustered_bootstrap_mean,
    effective_oos_split,
    shrink,
    summarize,
    wilson_interval,
)


def test_wilson_known_values() -> None:
    lo, hi = wilson_interval(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-3)
    assert hi == pytest.approx(0.5962, abs=1e-3)
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_clustered_bootstrap_is_seeded_and_wider_than_naive_when_clustered() -> None:
    values = [1.0] * 50 + [-1.0] * 50
    clusters = ["d1"] * 50 + ["d2"] * 50  # 兩個完全相關的交易日
    a = clustered_bootstrap_mean(values, clusters, 500, seed=3)
    b = clustered_bootstrap_mean(values, clusters, 500, seed=3)
    assert a == b
    _mean, lo, hi = a
    assert lo == pytest.approx(-1.0)
    assert hi == pytest.approx(1.0)


def test_shrink() -> None:
    assert shrink(0.5, 1.0, 200, 200) == pytest.approx(0.75)
    assert shrink(0.5, 1.0, 0, 200) == 0.5
    assert shrink(0.5, float("nan"), 500, 200) == 0.5


def test_effective_split_falls_back_to_quantile_for_short_history() -> None:
    long_hist = pd.Series(["2019-01-01", "2023-06-01", "2025-01-01"])
    assert effective_oos_split(long_hist, "2024-01-01") == "2024-01-01"
    hourly = pd.Series([f"2025-{m:02d}-01" for m in range(1, 11)])
    assert effective_oos_split(hourly, "2024-01-01") == "2025-07-01"
    # 固定切點只切出極小的訓練期 (1/10) → 改用分位數
    tiny_prefix = pd.Series(["2023-12-01"] + [f"2025-{m:02d}-01" for m in range(1, 10)])
    assert effective_oos_split(tiny_prefix, "2024-01-01") == "2025-06-01"


def test_summarize_oos_agreement() -> None:
    df = pd.DataFrame(
        {
            "date": ["2020-01-02", "2021-01-04", "2024-02-01", "2024-03-01"],
            "win": [1, 1, 1, 0],
            "r_multiple": [1.0, 1.0, 1.0, -0.5],
        }
    )
    st = summarize(df, 200, 1, "2024-01-01")
    assert st.n == 4 and st.n_dates == 4
    assert st.oos_agrees is True
    assert st.oos_expectancy == pytest.approx(0.25)
