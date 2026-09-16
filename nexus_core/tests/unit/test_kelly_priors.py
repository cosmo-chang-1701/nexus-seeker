"""凱利勝率先驗 (market_analysis/kelly_priors.py) 單元測試。"""

import math

import pytest

from market_analysis import kelly_priors
from market_analysis.kelly_priors import (
    KELLY_PRIOR_CAP,
    KELLY_WIN_RATE_PRIORS,
    WinRateBucket,
    get_win_rate_prior,
)
from market_analysis.risk_engine import kelly_position_fraction


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_buckets_cover_zero_to_hundred_without_gaps(side: str) -> None:
    buckets = KELLY_WIN_RATE_PRIORS[side]  # type: ignore[index]
    assert buckets[0].rsi_min == 0.0
    assert buckets[-1].rsi_max == 100.0
    for prev, nxt in zip(buckets, buckets[1:]):
        assert prev.rsi_max == nxt.rsi_min


def test_long_prior_matches_legacy_values() -> None:
    """多頭先驗必須與改動前 ExecutionRouter 寫死的值位元一致。"""
    assert get_win_rate_prior("LONG", 40.0) == 0.55
    assert get_win_rate_prior("LONG", 50.0) == 0.45
    assert get_win_rate_prior("LONG", 100.0) == 0.45


@pytest.mark.parametrize("rsi", [0.0, 10.0, 30.0, 49.9, 50.0, 70.0, 100.0])
def test_short_prior_never_exceeds_long(rsi: float) -> None:
    assert get_win_rate_prior("SHORT", rsi) <= get_win_rate_prior("LONG", rsi)


def test_short_clamp_holds_even_if_table_is_misedited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使日後誤把做空表改得比做多激進，結構性夾制仍保證做空不超過做多。"""
    monkeypatch.setattr(
        kelly_priors,
        "KELLY_WIN_RATE_PRIORS",
        {
            "LONG": (WinRateBucket(0.0, 100.0, 0.50),),
            "SHORT": (WinRateBucket(0.0, 100.0, 0.70),),
        },
    )
    assert kelly_priors.get_win_rate_prior("SHORT", 60.0) == 0.50
    assert kelly_priors.get_win_rate_prior("SHORT", None) == 0.50


@pytest.mark.parametrize("rsi", [None, math.nan, -1.0, 101.0])
def test_unknown_rsi_uses_side_minimum(rsi: object) -> None:
    assert get_win_rate_prior("LONG", rsi) == 0.45  # type: ignore[arg-type]
    assert get_win_rate_prior("SHORT", rsi) == 0.40  # type: ignore[arg-type]


def test_short_cap_is_tighter_than_long() -> None:
    assert KELLY_PRIOR_CAP["SHORT"] < KELLY_PRIOR_CAP["LONG"]
    short_f = kelly_position_fraction(
        get_win_rate_prior("SHORT", 40.0), 1.8, 0.5, KELLY_PRIOR_CAP["SHORT"]
    )
    long_f = kelly_position_fraction(
        get_win_rate_prior("LONG", 40.0), 1.8, 0.5, KELLY_PRIOR_CAP["LONG"]
    )
    assert short_f <= long_f
