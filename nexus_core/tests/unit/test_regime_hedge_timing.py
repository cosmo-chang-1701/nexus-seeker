"""regime_hedge_timing 單元測試。"""

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from market_analysis.dynamic_rollover.regime_hedge_timing import (
    MacroFreezeStatus,
    find_macro_freeze_event,
    is_preliminary_window,
    load_earnings_dates,
    load_macro_freeze_status,
)

NY = ZoneInfo("America/New_York")


def _ny(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def _evt(name: str, when_ny: datetime, impact: str = "HIGH") -> dict[str, str]:
    return {
        "event": name,
        "event_time": when_ny.astimezone(timezone.utc).isoformat(),
        "impact": impact,
    }


def test_preliminary_window_before_1000_et() -> None:
    # 2026-11-12 週四：開盤前、開盤後 10 分、週末皆為預警窗口
    assert is_preliminary_window(_ny(2026, 11, 12, 8, 0))
    assert is_preliminary_window(_ny(2026, 11, 12, 9, 40))
    assert is_preliminary_window(_ny(2026, 11, 14, 11, 0))  # 週六


def test_not_preliminary_after_1000_et() -> None:
    assert not is_preliminary_window(_ny(2026, 11, 12, 10, 5))


def test_fomc_within_8h_freezes() -> None:
    now = _ny(2026, 11, 12, 9, 30).astimezone(timezone.utc)
    events = [_evt("FOMC Rate Decision", _ny(2026, 11, 12, 14, 0))]
    hit = find_macro_freeze_event(events, now)
    assert hit is not None and hit[0] == "FOMC RATE DECISION"
    status = MacroFreezeStatus(True, True, hit[0], hit[1])
    assert status.reason_text() == (
        "FOMC RATE DECISION 將於 14:00 ET 公布（8 小時內），凍結新建 −3x 反向 ETF"
    )


def test_event_beyond_8h_not_frozen() -> None:
    now = _ny(2026, 11, 12, 5, 0).astimezone(timezone.utc)
    events = [_evt("FOMC Rate Decision", _ny(2026, 11, 12, 14, 0))]
    assert find_macro_freeze_event(events, now) is None
    assert MacroFreezeStatus(False, True).reason_text() is None
    # 已過事件與低影響事件不命中
    now2 = _ny(2026, 11, 12, 15, 0).astimezone(timezone.utc)
    assert find_macro_freeze_event(events, now2) is None
    low = [_evt("Redbook Index", _ny(2026, 11, 12, 10, 0), "LOW")]
    assert find_macro_freeze_event(low, _ny(2026, 11, 12, 9, 0)) is None


def test_core_pce_matches() -> None:
    now = _ny(2026, 11, 12, 6, 0).astimezone(timezone.utc)
    events = [
        _evt("Core PCE Price Index MoM", _ny(2026, 11, 12, 8, 30)),
        {"event": "CPI YoY", "event_time": "garbage"},
    ]
    hit = find_macro_freeze_event(events, now)
    assert hit is not None and "PCE" in hit[0]


def test_incomplete_calendar_fails_closed() -> None:
    now = _ny(2026, 11, 12, 9, 30).astimezone(timezone.utc)
    with patch(
        "database.calendar_cache.get_macro_month_status", return_value=None
    ), patch("database.calendar_cache.get_macro_events_between", return_value=[]):
        status = asyncio.run(load_macro_freeze_status(now))
    assert status.frozen and not status.calendar_known
    assert status.reason_text() == "總經日曆快取不完整，保守凍結新建 −3x 反向 ETF"

    with patch(
        "database.calendar_cache.get_macro_month_status",
        side_effect=RuntimeError("boom"),
    ):
        status = asyncio.run(load_macro_freeze_status(now))
    assert status.frozen and not status.calendar_known

    events = [_evt("NFP", _ny(2026, 11, 12, 12, 0))]
    with patch(
        "database.calendar_cache.get_macro_month_status",
        return_value={"event_count": 1},
    ), patch("database.calendar_cache.get_macro_events_between", return_value=events):
        status = asyncio.run(load_macro_freeze_status(now))
    assert status.frozen and status.calendar_known


def test_load_earnings_dates_parsing() -> None:
    rows = {
        "AAPL": {"earnings_date": "2026-11-14"},
        "MSFT": {"earnings_date": "2026-11-14 00:00:00"},
        "SPY": {"earnings_date": None},
        "BAD": {"earnings_date": "亂碼"},
    }
    with patch("database.calendar_cache.get_cached_earnings_many", return_value=rows):
        out = asyncio.run(load_earnings_dates(["aapl", "msft", "spy", "bad"]))
    assert out == {
        "AAPL": date(2026, 11, 14),
        "MSFT": date(2026, 11, 14),
        "SPY": None,
    }
    with patch(
        "database.calendar_cache.get_cached_earnings_many",
        side_effect=RuntimeError("x"),
    ):
        assert asyncio.run(load_earnings_dates(["AAPL"])) == {}


def test_chinese_event_names_match() -> None:
    now = _ny(2026, 11, 12, 9, 0).astimezone(timezone.utc)
    for name in ("聯準會利率決策", "非農就業人數"):
        events = [_evt(name, _ny(2026, 11, 12, 12, 0))]
        assert find_macro_freeze_event(events, now) is not None
    adp = [_evt("ADP 就業人數 (小非農)", _ny(2026, 11, 12, 10, 0))]
    assert find_macro_freeze_event(adp, now) is None


def test_cross_month_missing_fails_closed() -> None:
    now = _ny(2026, 10, 31, 20, 0).astimezone(timezone.utc)

    def status(key: str) -> dict[str, int] | None:
        return {"event_count": 1} if key == "2026-10" else None

    with patch(
        "database.calendar_cache.get_macro_month_status", side_effect=status
    ), patch("database.calendar_cache.get_macro_events_between", return_value=[]):
        result = asyncio.run(load_macro_freeze_status(now))
    assert result.frozen and not result.calendar_known
