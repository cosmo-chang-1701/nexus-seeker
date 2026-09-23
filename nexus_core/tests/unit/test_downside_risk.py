"""market_analysis/downside_risk.py：Sortino / MDD / VaR / CVaR 單一權威定義的手算驗證。"""

import math

import numpy as np
import pytest

from market_analysis.downside_risk import (
    MIN_VAR_SAMPLES,
    annualized_downside_deviation,
    annualized_return,
    current_drawdown,
    downside_deviation,
    historical_var_cvar,
    max_drawdown,
    nav_from_returns,
    sharpe_ratio,
    sortino_ratio,
)


def test_downside_deviation_uses_full_sample_denominator() -> None:
    """分母是全部樣本數：[+2%, -1%, +3%, -3%] → sqrt((0.01² + 0.03²) / 4)。"""
    returns = [0.02, -0.01, 0.03, -0.03]
    expected = math.sqrt((0.01**2 + 0.03**2) / 4)
    assert downside_deviation(returns) == pytest.approx(expected)


def test_downside_deviation_penalises_loss_frequency() -> None:
    """只對負報酬取均值時，兩組序列的下行差會相同；全樣本分母必須區分虧損頻率。"""
    rare = [0.01] * 9 + [-0.02]
    frequent = [0.01] * 5 + [-0.02] * 5
    assert downside_deviation(frequent) > downside_deviation(rare)


def test_downside_deviation_measured_against_mar() -> None:
    """低於 MAR 但仍為正的報酬也算下行。"""
    assert downside_deviation([0.001, 0.001], mar_per_period=0.002) == pytest.approx(
        0.001
    )
    assert downside_deviation([0.001, 0.001]) == 0.0


def test_sortino_ignores_upside_volatility() -> None:
    """放大上行波動不改變 Sortino 分母；Sharpe 則會把它當成風險而下降。"""
    base = [0.01, -0.01] * 50
    more_upside = [0.03, -0.01] * 50
    assert annualized_downside_deviation(base) == pytest.approx(
        annualized_downside_deviation(more_upside)
    )
    # 同樣的年化報酬下，Sortino 不受上行波動影響、Sharpe 被懲罰
    r = 0.10
    assert sortino_ratio(base, annual_return=r) == pytest.approx(
        sortino_ratio(more_upside, annual_return=r)
    )
    assert sharpe_ratio(more_upside, annual_return=r) < sharpe_ratio(
        base, annual_return=r
    )


def test_sortino_matches_manual_formula() -> None:
    returns = np.array([0.01, -0.02, 0.015, -0.005, 0.0] * 20)
    mar = 0.045
    dd = math.sqrt(np.mean(np.minimum(0.0, returns - mar / 252) ** 2)) * math.sqrt(252)
    ann = float(np.prod(1 + returns)) ** (252 / returns.size) - 1
    assert sortino_ratio(returns, mar) == pytest.approx((ann - mar) / dd)


def test_sortino_zero_when_no_downside() -> None:
    assert sortino_ratio([0.01] * 30) == 0.0


def test_annualized_return_geometric() -> None:
    assert annualized_return([0.01] * 252) == pytest.approx(1.01**252 - 1)
    assert annualized_return([]) == 0.0


def test_max_drawdown_and_indices() -> None:
    nav = [100, 120, 90, 110, 60, 130]
    res = max_drawdown(nav)
    assert res.max_drawdown == pytest.approx(0.5)
    assert res.peak_index == 1
    assert res.trough_index == 4


def test_max_drawdown_monotonic_is_zero() -> None:
    assert max_drawdown([1, 2, 3]).max_drawdown == 0.0
    assert max_drawdown([]).max_drawdown == 0.0


def test_current_drawdown() -> None:
    assert current_drawdown([100, 120, 90]) == pytest.approx(0.25)
    assert current_drawdown([100, 120]) == 0.0


def test_nav_from_returns_round_trip() -> None:
    nav = nav_from_returns([0.1, -0.5], start=100.0)
    assert list(nav) == pytest.approx([100.0, 110.0, 55.0])


def test_var_cvar_historical() -> None:
    """100 筆：-10%..-1% 各一筆 + 90 筆 +1%。5% 分位落在 -6% 與 -5% 之間。"""
    returns = [-(i / 100) for i in range(1, 11)] + [0.01] * 90
    res = historical_var_cvar(returns, confidence=0.95)
    assert res is not None
    q = float(np.quantile(returns, 0.05))
    assert res.var == pytest.approx(-q)
    tail = [r for r in returns if r <= q]
    assert res.cvar == pytest.approx(-sum(tail) / len(tail))
    assert res.cvar >= res.var


def test_var_cvar_insufficient_samples_returns_none() -> None:
    assert historical_var_cvar([-0.01] * (MIN_VAR_SAMPLES - 1)) is None


def test_var_floor_at_zero_when_no_losses() -> None:
    res = historical_var_cvar([0.01] * MIN_VAR_SAMPLES)
    assert res is not None
    assert res.var == 0.0
    assert res.cvar == 0.0


def test_non_finite_values_are_dropped() -> None:
    assert downside_deviation([float("nan"), -0.02, 0.02]) == pytest.approx(
        math.sqrt(0.02**2 / 2)
    )


def test_scaled_mix_downside_deviation_is_linear_in_weight() -> None:
    """減碼對照組的理論基礎：w×B&H + (1−w)×rf 的下行差（MAR=rf）精確等於 w×B&H 下行差。"""
    rng = np.random.default_rng(7)
    bench = rng.normal(0.0005, 0.012, 252)
    rf = 0.045
    w = 0.54
    mix = w * bench + (1 - w) * rf / 252
    assert annualized_downside_deviation(mix, rf) == pytest.approx(
        w * annualized_downside_deviation(bench, rf)
    )
