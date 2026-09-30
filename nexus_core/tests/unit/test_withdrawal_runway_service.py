"""提領跑道服務的純邏輯測試（docs/risk_portfolio/05 階段二）。"""

from datetime import date

import numpy as np
import pytest

from database.withdrawal_runway import RunwaySnapshot
from market_analysis.macro_signals import Observation, available_date_for
from services.withdrawal_runway_service import (
    compute_user_runway,
    cpi_at,
    cpi_for_anchor,
    is_snapshot_stale,
    next_withdrawal_date,
    parse_months,
    portfolio_beta,
)


def test_parse_months_falls_back_on_bad_input() -> None:
    assert parse_months("7,1") == [1, 7]
    assert parse_months("abc") == [1, 7]
    assert parse_months("0,13") == [1, 7]


def test_next_withdrawal_date_first_trading_day() -> None:
    # 2026-09-30 之後的下一個提領日：2027-01 的首個交易日（1/1 休市 → 1/4）
    target, month, days = next_withdrawal_date(date(2026, 9, 30), [1, 7])
    assert (target, month) == (date(2027, 1, 4), 1)
    assert days > 60


def test_next_withdrawal_date_in_withdrawal_month_after_first_day() -> None:
    # 2026-07-06 已過 7 月首個交易日 → 下一次是 2027-01
    target, month, _ = next_withdrawal_date(date(2026, 7, 6), [1, 7])
    assert (target.year, month) == (2027, 1)


def test_next_withdrawal_date_day_before_counts_one_day() -> None:
    target, month, days = next_withdrawal_date(date(2026, 12, 31), [1, 7])
    assert (target, month, days) == (date(2027, 1, 4), 1, 1)


def test_cpi_lookups_respect_release_lag() -> None:
    obs = [
        Observation(
            date(2026, 7, 1), 320.0, available_date_for("monthly_cpi", date(2026, 7, 1))
        ),
        Observation(
            date(2026, 8, 1), 321.0, available_date_for("monthly_cpi", date(2026, 8, 1))
        ),
    ]
    assert available_date_for("monthly_cpi", date(2026, 8, 1)) == date(2026, 9, 15)
    assert cpi_at(obs, date(2026, 9, 14)) == 320.0
    assert cpi_at(obs, date(2026, 9, 15)) == 321.0
    assert cpi_at(obs, date(2026, 7, 1)) is None
    assert cpi_for_anchor(obs, "2026-08") == 321.0
    assert cpi_for_anchor(obs, "2025-01") is None
    assert cpi_for_anchor(obs, "bad") is None


def test_portfolio_beta() -> None:
    rng = np.random.default_rng(0)
    mkt = rng.normal(0, 0.01, 120)
    port = 1.5 * mkt
    dates = [f"d{i}" for i in range(120)]
    beta = portfolio_beta(dates, port.tolist(), dict(zip(dates, mkt.tolist())))
    assert beta == pytest.approx(1.5, rel=1e-6)
    assert (
        portfolio_beta(dates[:30], port[:30].tolist(), dict(zip(dates, mkt.tolist())))
        is None
    )
    assert portfolio_beta(dates, port.tolist(), {}) is None


def _kwargs(**over: object) -> dict:
    base: dict = dict(
        user_id=1,
        today=date(2026, 9, 30),
        nav=100000.0,
        nav_date="2026-09-30",
        boxx_value=0.0,
        beta=1.3,
        withdrawal_amount=10000.0,
        months=[1, 7],
        cpi_anchor=300.0,
        cpi_now=300.0,
    )
    base.update(over)
    return base


def test_compute_user_runway_matches_spec_example() -> None:
    snap = compute_user_runway(**_kwargs())
    assert snap is not None
    assert snap.zero_years == pytest.approx(5.0)
    assert snap.stress_years <= snap.zero_years
    assert snap.stress_years == min(snap.gfc_years, snap.dotcom_years)
    assert snap.next_date == "2027-01-04"
    assert not snap.cpi_missing and not snap.beta_is_fallback


def test_compute_user_runway_inflation_and_flags() -> None:
    snap = compute_user_runway(**_kwargs(cpi_now=330.0, beta=None))
    assert snap is not None
    assert snap.next_withdrawal == pytest.approx(11000.0)
    assert snap.beta_is_fallback and snap.beta == 1.3
    missing = compute_user_runway(**_kwargs(cpi_anchor=None))
    assert missing is not None and missing.cpi_missing
    assert missing.next_withdrawal == pytest.approx(10000.0)


def test_compute_user_runway_skips_disabled_or_empty() -> None:
    assert compute_user_runway(**_kwargs(withdrawal_amount=0.0)) is None
    assert compute_user_runway(**_kwargs(nav=0.0)) is None


def test_boxx_reduces_dotcom_exposure() -> None:
    plain = compute_user_runway(**_kwargs())
    hedged = compute_user_runway(**_kwargs(boxx_value=50000.0))
    assert plain is not None and hedged is not None
    assert hedged.boxx_payments == 5
    assert hedged.dotcom_years >= plain.dotcom_years


def _snap(nav_date: str) -> RunwaySnapshot:
    snap = compute_user_runway(**_kwargs(nav_date=nav_date))
    assert snap is not None
    return snap


def test_snapshot_stale_after_five_trading_days() -> None:
    assert not is_snapshot_stale(_snap("2026-09-30"), date(2026, 10, 7))  # 5 個交易日
    assert is_snapshot_stale(_snap("2026-09-30"), date(2026, 10, 8))  # 6 個交易日
    assert not is_snapshot_stale(_snap("2026-09-30"), date(2026, 9, 30))
