"""開盤窗口的 15m K 棒新鮮度：前一交易日的最後一根不得被誤報為資料源停擺。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from market_analysis.intraday_consistency import BarAssessment, assess_15m_bar

PRIOR_BAR = datetime(2026, 10, 14, 15, 45)


def _assess(**overrides: object) -> BarAssessment:
    kwargs: dict[str, Any] = dict(
        bar_time=PRIOR_BAR,
        high=100.0,
        low=99.0,
        volume=100_000.0,
        now_ny=datetime(2026, 10, 15, 9, 37),
        market_open=True,
        day_high=101.0,
        day_low=98.0,
        session_date=date(2026, 10, 15),
        session_volume=1_000_000.0,
        session_bar_count=0,
    )
    kwargs.update(overrides)
    return assess_15m_bar(**kwargs)


def test_before_first_bar_close_is_not_stale() -> None:
    res = _assess()
    assert res.is_prior_session and not res.is_stale and not res.is_anomalous
    assert res.notes == ["開盤首根 15m K 棒尚未收盤，量比待更新"]
    assert not any("資料源未更新" in n for n in res.notes)


def test_shortly_after_first_bar_close_not_stale() -> None:
    res = _assess(now_ny=datetime(2026, 10, 15, 10, 5))
    assert res.is_prior_session and not res.is_stale
    assert any("尚未更新" in n for n in res.notes)


def test_prior_bar_long_after_open_is_stale() -> None:
    res = _assess(now_ny=datetime(2026, 10, 15, 10, 20))
    assert res.is_prior_session and res.is_stale
    assert res.notes == ["K 棒資料延遲 35 分鐘（今日 K 棒未更新）"]


def test_today_bar_fresh_has_no_notes() -> None:
    res = _assess(
        bar_time=datetime(2026, 10, 15, 10, 0),
        now_ny=datetime(2026, 10, 15, 10, 20),
        session_bar_count=2,
    )
    assert not res.is_prior_session and not res.is_stale and res.notes == []


def test_today_bar_stale_keeps_existing_wording() -> None:
    res = _assess(
        bar_time=datetime(2026, 10, 15, 10, 0),
        now_ny=datetime(2026, 10, 15, 11, 0),
        session_bar_count=2,
    )
    assert res.is_stale and not res.is_prior_session
    assert res.notes == ["K 棒資料延遲 45 分鐘（資料源未更新）"]


def test_market_closed_has_no_freshness_notes() -> None:
    res = _assess(now_ny=datetime(2026, 10, 15, 9, 37), market_open=False)
    assert not res.is_prior_session and not res.is_stale and res.notes == []


def test_monday_open_with_friday_bar() -> None:
    res = _assess(
        bar_time=datetime(2026, 10, 9, 15, 45),
        now_ny=datetime(2026, 10, 12, 9, 37),
        session_date=date(2026, 10, 12),
    )
    assert res.is_prior_session and not res.is_stale
    assert res.notes == ["開盤首根 15m K 棒尚未收盤，量比待更新"]
