"""固定比例配置 + 定期再平衡回測：再平衡觸發時點、偏離門檻、權重與成本、point-in-time
等權、BOXX 銜接與回撤恢復時間（全部使用合成資料）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from calibration.regime_momentum_backtest import CORE_SYMBOL, equal_weight_pit
from calibration.static_allocation_backtest import (
    BUCKET_CASH,
    BUCKET_CORE,
    BUCKET_TECH,
    StaticAllocationParams,
    max_drawdown_episode,
    needs_rebalance,
    rebalance_calendar,
    simulate_static,
    worst_rolling_return,
)

DAYS = pd.bdate_range("2019-12-02", "2021-03-31")
UNI = ("AAA", "BBB")


def _panel(overrides: dict[str, np.ndarray] | None = None) -> pd.DataFrame:
    t = np.arange(len(DAYS), dtype=float)
    data = {
        "AAA": 50 * 1.002**t,
        "BBB": 40 * 0.999**t,
        CORE_SYMBOL: 100 + 0.1 * t,
    }
    if overrides:
        data.update(overrides)
    return pd.DataFrame(data, index=DAYS)


def _cash(rate_per_day: float = 0.0) -> pd.Series:
    return pd.Series((1.0 + rate_per_day) ** np.arange(len(DAYS)), index=DAYS)


def _params(**kw: object) -> StaticAllocationParams:
    base: dict[str, object] = dict(
        equity_share=0.6,
        tech_share=0.5,
        rebalance="ANNUAL",
        cost_rate=0.0,
        universe=UNI,
    )
    base.update(kw)
    return StaticAllocationParams(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 再平衡時點
# ---------------------------------------------------------------------------


def test_targets_split_equity_and_cash() -> None:
    t = _params(equity_share=0.6, tech_share=0.5).targets()
    assert t == pytest.approx({BUCKET_TECH: 0.3, BUCKET_CORE: 0.3, BUCKET_CASH: 0.4})


def test_rebalance_calendar_modes() -> None:
    idx = pd.DatetimeIndex(
        [
            "2020-12-30",
            "2020-12-31",
            "2021-01-04",
            "2021-01-05",
            "2021-02-01",
            "2021-02-02",
            "2021-04-01",
        ]
    )
    annual = rebalance_calendar(idx, "ANNUAL")
    quarterly = rebalance_calendar(idx, "QUARTERLY")
    monthly = rebalance_calendar(idx, "THRESHOLD")
    assert list(annual) == [True, False, True, False, False, False, False]
    assert list(quarterly) == [False, False, True, False, False, False, True]
    assert list(monthly) == [True, False, True, False, True, False, True]


def test_annual_rebalances_only_on_first_day_of_year() -> None:
    p = _panel()
    res = simulate_static(p, p, _cash(), "2020-01-02", "2021-03-31", _params())
    # 合成日曆為 bdate_range（含 2021-01-01），「每年第一個交易日」即該日
    assert [d.strftime("%Y-%m-%d") for d in res.rebalance_days] == [
        "2020-01-02",
        "2021-01-01",
    ]


def test_quarterly_rebalances_each_quarter() -> None:
    p = _panel()
    res = simulate_static(
        p, p, _cash(), "2020-01-02", "2021-03-31", _params(rebalance="QUARTERLY")
    )
    months = [(d.year, d.month) for d in res.rebalance_days]
    assert months == [(2020, 1), (2020, 4), (2020, 7), (2020, 10), (2021, 1)]


def test_weights_hit_targets_right_after_rebalance() -> None:
    p = _panel()
    res = simulate_static(p, p, _cash(), "2020-01-02", "2021-03-31", _params())
    # 開盤＝收盤的合成資料：建倉當日收盤權重恰為目標
    w = res.bucket_weights.iloc[0]
    assert w[BUCKET_TECH] == pytest.approx(0.3)
    assert w[BUCKET_CORE] == pytest.approx(0.3)
    assert w[BUCKET_CASH] == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# 偏離門檻
# ---------------------------------------------------------------------------


def test_needs_rebalance_threshold_boundary() -> None:
    target = {BUCKET_TECH: 0.3, BUCKET_CORE: 0.3, BUCKET_CASH: 0.4}
    assert not needs_rebalance(
        {BUCKET_TECH: 0.34, BUCKET_CORE: 0.29, BUCKET_CASH: 0.37}, target, 0.05
    )
    assert needs_rebalance(
        {BUCKET_TECH: 0.35, BUCKET_CORE: 0.28, BUCKET_CASH: 0.37}, target, 0.05
    )


def test_threshold_mode_skips_quiet_months_and_fires_on_drift() -> None:
    t = np.arange(len(DAYS), dtype=float)
    # AAA 在 2020-06 之後暴漲，科技桶偏離目標
    aaa = np.where(DAYS < pd.Timestamp("2020-06-01"), 50.0, 50.0 * 1.01 ** (t - 125))
    p = _panel({"AAA": aaa, "BBB": 40 + 0 * t, CORE_SYMBOL: 100 + 0 * t})
    res = simulate_static(
        p, p, _cash(), "2020-01-02", "2021-03-31", _params(rebalance="THRESHOLD")
    )
    days = [d.strftime("%Y-%m") for d in res.rebalance_days]
    assert days[0] == "2020-01"
    assert "2020-02" not in days and "2020-05" not in days, "價格不動時不得再平衡"
    assert any(d >= "2020-07" for d in days[1:]), "偏離後必須再平衡"


# ---------------------------------------------------------------------------
# 成本、point-in-time、BOXX
# ---------------------------------------------------------------------------


def test_cost_charged_on_equity_turnover_only() -> None:
    p = _panel()
    free = simulate_static(p, p, _cash(), "2020-01-02", "2020-01-31", _params())
    paid = simulate_static(
        p, p, _cash(), "2020-01-02", "2020-01-31", _params(cost_rate=0.01)
    )
    # 建倉只買股票 60%：成本 = 淨值 × 0.6 × 1%
    assert paid.costs == pytest.approx(100_000 * 0.6 * 0.01)
    assert paid.nav.iloc[0] == pytest.approx(free.nav.iloc[0] - paid.costs)


def test_full_tech_annual_matches_equal_weight_benchmark() -> None:
    """E=100%／T=100%／年度再平衡必須與等權對照組逐位元一致（同一套規則）。"""
    p = _panel()
    res = simulate_static(
        p,
        p,
        _cash(),
        "2020-01-02",
        "2021-03-31",
        _params(equity_share=1.0, tech_share=1.0, cost_rate=0.0015),
    )
    ew = equal_weight_pit(p, p, UNI, "2020-01-02", "2021-03-31", 0.0015)
    np.testing.assert_allclose(res.nav.to_numpy(), ew.to_numpy(), rtol=1e-12)


def test_unlisted_symbol_joins_only_at_next_rebalance() -> None:
    bbb = np.where(DAYS < pd.Timestamp("2020-06-01"), np.nan, 40.0)
    p = _panel({"BBB": bbb})
    res = simulate_static(
        p,
        p,
        _cash(),
        "2020-01-02",
        "2021-03-31",
        _params(equity_share=1.0, tech_share=1.0),
    )
    w = res.bucket_weights
    assert w.loc["2020-01-02", BUCKET_TECH] == pytest.approx(1.0), "只有 AAA 可買"
    # 2021 年度再平衡後 BBB 才加入；之前科技池市值全來自 AAA（無 BBB 部位）
    assert w.loc["2020-12-31", BUCKET_TECH] == pytest.approx(1.0)


def test_cash_bucket_accrues_cash_index() -> None:
    p = _panel()
    rate = 0.0001
    res = simulate_static(
        p,
        p,
        _cash(rate),
        "2020-01-02",
        "2020-01-31",
        _params(equity_share=0.0, tech_share=0.0),
    )
    n = len(res.nav) - 1
    assert res.nav.iloc[-1] / res.nav.iloc[0] == pytest.approx((1 + rate) ** n)


# ---------------------------------------------------------------------------
# 回撤體驗指標
# ---------------------------------------------------------------------------


def test_max_drawdown_episode_recovery_days() -> None:
    idx = pd.bdate_range("2020-01-01", periods=7)
    nav = pd.Series([100, 120, 90, 100, 119, 121, 80], index=idx, dtype=float)
    ep = max_drawdown_episode(nav)
    assert ep.depth == pytest.approx(41 / 121)
    assert ep.recovered is None, "期末仍低於 121 高點"
    nav2 = pd.Series([100, 120, 60, 100, 125, 130, 131], index=idx, dtype=float)
    ep2 = max_drawdown_episode(nav2)
    assert ep2.depth == pytest.approx(0.5)
    assert ep2.recovered == idx[4]
    assert ep2.recovery_days == 3


def test_worst_rolling_return() -> None:
    idx = pd.bdate_range("2020-01-01", periods=6)
    nav = pd.Series([100, 110, 80, 90, 120, 60], index=idx, dtype=float)
    assert worst_rolling_return(nav, window=2) == pytest.approx(60 / 90 - 1)
