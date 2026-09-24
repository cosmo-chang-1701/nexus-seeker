"""market_analysis/downside_monitor.py：回撤階梯、CVaR 預算、快照與 NAV 快照報酬還原。"""

import numpy as np
import pytest

from market_analysis.downside_monitor import (
    CVAR_BASELINE_DAYS,
    CVAR_SHORT_WINDOW,
    DownsideSnapshot,
    NavSnapshot,
    compute_snapshot,
    cvar_budget,
    evaluate_cvar_breaches,
    evaluate_drawdown_tier,
    realized_returns_from_snapshots,
)

# ---------------------------------------------------------------------------
# 回撤階梯
# ---------------------------------------------------------------------------


def test_first_tier_triggers_and_arms() -> None:
    assert evaluate_drawdown_tier(0.11, 0.0) == (0.10, 0.10)


def test_gap_down_alerts_deepest_tier_only() -> None:
    """一天跌穿兩階：只推最深的那一階，不連發兩則。"""
    assert evaluate_drawdown_tier(0.17, 0.0) == (0.15, 0.15)


def test_same_tier_does_not_repeat_while_armed() -> None:
    assert evaluate_drawdown_tier(0.14, 0.10) == (None, 0.10)
    assert evaluate_drawdown_tier(0.19, 0.15) == (None, 0.15)


def test_rearm_requires_buffer() -> None:
    """在階梯線附近震盪（未回升超過 2.5pp）不重新武裝。"""
    assert evaluate_drawdown_tier(0.13, 0.15) == (None, 0.15)
    # 回升到 -12.4%（超過 15% − 2.5%）→ 15% 階梯重新武裝，退回仍超過的 10% 階梯
    assert evaluate_drawdown_tier(0.124, 0.15) == (None, 0.10)
    # 再跌回 -15% → 重新推播
    assert evaluate_drawdown_tier(0.15, 0.10) == (0.15, 0.15)


def test_full_recovery_rearms_everything() -> None:
    assert evaluate_drawdown_tier(0.02, 0.10) == (None, 0.0)


# ---------------------------------------------------------------------------
# CVaR 預算
# ---------------------------------------------------------------------------


def _snap(**kw: float | None) -> DownsideSnapshot:
    base: dict = dict(
        n_obs=252,
        sortino_63=1.0,
        sortino_252=1.0,
        max_drawdown=0.1,
        current_drawdown=0.0,
        var_95=0.01,
        cvar_95=0.02,
        cvar_short=0.02,
        cvar_short_baseline=0.02,
    )
    base.update(kw)
    return DownsideSnapshot(**base)


def test_cvar_budget_scales_with_risk_limit() -> None:
    assert cvar_budget(15.0) == pytest.approx(0.03)


def test_cvar_budget_breach() -> None:
    assert evaluate_cvar_breaches(_snap(cvar_95=0.031), 15.0) == ["BUDGET"]
    assert evaluate_cvar_breaches(_snap(cvar_95=0.029), 15.0) == []


def test_cvar_expansion() -> None:
    reasons = evaluate_cvar_breaches(
        _snap(cvar_short=0.03, cvar_short_baseline=0.02), 50.0
    )
    assert reasons == ["EXPANSION"]


def test_cvar_none_values_never_trigger() -> None:
    assert (
        evaluate_cvar_breaches(
            _snap(cvar_95=None, cvar_short=None, cvar_short_baseline=None), 1.0
        )
        == []
    )


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------


def test_snapshot_requires_minimum_samples() -> None:
    assert compute_snapshot([0.01] * 30, 0.04) is None


def test_snapshot_intraday_return_updates_drawdown() -> None:
    rng = np.random.default_rng(3)
    rets = rng.normal(0.0005, 0.01, 252)
    base = compute_snapshot(rets, 0.04)
    crashed = compute_snapshot(rets, 0.04, intraday_return=-0.25)
    assert base is not None and crashed is not None
    assert crashed.current_drawdown > base.current_drawdown
    assert crashed.current_drawdown >= 0.25
    # 盤中報酬不進入 CVaR（日線尾部分佈盤中不變）
    assert crashed.cvar_95 == base.cvar_95


def test_snapshot_baseline_available_with_enough_history() -> None:
    rets = np.random.default_rng(5).normal(
        0, 0.01, CVAR_SHORT_WINDOW + CVAR_BASELINE_DAYS
    )
    snap = compute_snapshot(rets, 0.04)
    assert snap is not None
    assert snap.cvar_short_baseline is not None
    assert snap.sortino_63 is not None
    assert snap.sortino_252 is None


# ---------------------------------------------------------------------------
# NAV 快照 → 已實現報酬
# ---------------------------------------------------------------------------


def test_adding_shares_is_not_counted_as_return() -> None:
    """第 2 天加碼 100 股（NAV 從 10,000 增至 20,000），但價格不變：報酬必須為 0。"""
    snaps = [
        NavSnapshot("2026-01-02", 10_000.0, {"AAA": 100.0}, {"AAA": 100.0}),
        NavSnapshot("2026-01-05", 20_000.0, {"AAA": 200.0}, {"AAA": 100.0}),
        NavSnapshot("2026-01-06", 22_000.0, {"AAA": 200.0}, {"AAA": 110.0}),
    ]
    rets = realized_returns_from_snapshots(snaps)
    assert rets[0] == pytest.approx(0.0)
    # 第 3 天：前一日 200 股 × +10 ÷ 前一日 NAV 20,000 = +10%
    assert rets[1] == pytest.approx(0.10)


def test_realized_returns_use_previous_day_holdings_and_handle_shorts() -> None:
    snaps = [
        NavSnapshot(
            "2026-01-06",
            10_000.0,
            {"LONG": 50.0, "SHORT": -20.0},
            {"LONG": 100.0, "SHORT": 50.0},
        ),
        NavSnapshot(
            "2026-01-02",
            10_000.0,
            {"LONG": 50.0, "SHORT": -20.0},
            {"LONG": 90.0, "SHORT": 60.0},
        ),
    ]
    # 依日期排序：01-02 → 01-06；LONG +10×50 = +500，SHORT −10×(−20) = +200
    assert realized_returns_from_snapshots(snaps) == [pytest.approx(0.07)]


def test_missing_prices_are_skipped() -> None:
    snaps = [
        NavSnapshot("2026-01-02", 1_000.0, {"A": 10.0, "B": 5.0}, {"A": 10.0}),
        NavSnapshot("2026-01-05", 1_000.0, {"A": 10.0}, {"A": 11.0, "B": 9.0}),
    ]
    assert realized_returns_from_snapshots(snaps) == [pytest.approx(0.01)]
