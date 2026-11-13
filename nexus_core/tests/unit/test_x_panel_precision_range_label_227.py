"""GitHub #227：/x embed 1σ 精度統一為 1 位小數；盤前／週末報價區間標籤改為「前日區間」。"""

from datetime import datetime
from typing import Any
from unittest.mock import patch

from cogs.embed_builders.portfolio_embeds import (
    _quote_range_label,
    create_tactical_symbol_embed,
    get_scenario_guidance,
)
from market_time import ny_tz


def _ts(y: int, mo: int, d: int, h: int, mi: int) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=ny_tz).timestamp()


def _now(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=ny_tz)


def test_sigma_precision_one_decimal() -> None:
    text = get_scenario_guidance(284.68, 270.0, sigma_to_expiry=9.8123)
    assert "±$9.8)" in text
    assert "9.81" not in text


def test_helper_prev_day_t_before_open_is_previous_day() -> None:
    label = _quote_range_label(_ts(2026, 11, 12, 16, 0), _now(2026, 11, 13, 3, 34))
    assert label == " 前日區間"


def test_helper_same_day_after_hours_stays_today() -> None:
    label = _quote_range_label(_ts(2026, 11, 13, 16, 0), _now(2026, 11, 13, 18, 0))
    assert label == " 今日區間"


def test_helper_same_day_intraday_stays_today() -> None:
    label = _quote_range_label(_ts(2026, 11, 13, 10, 0), _now(2026, 11, 13, 10, 5))
    assert label == " 今日區間"


def test_helper_extended_hours_print_before_open_is_previous_day() -> None:
    label = _quote_range_label(_ts(2026, 11, 13, 7, 55), _now(2026, 11, 13, 8, 0))
    assert label == " 前日區間"


def test_helper_friday_close_then_saturday_is_previous_day() -> None:
    label = _quote_range_label(_ts(2026, 11, 13, 16, 0), _now(2026, 11, 14, 12, 0))
    assert label == " 前日區間"


def test_helper_missing_or_invalid_t_after_open_is_today() -> None:
    now = _now(2026, 11, 13, 11, 0)
    assert _quote_range_label(None, now) == " 今日區間"
    assert _quote_range_label("x", now) == " 今日區間"
    assert _quote_range_label(0, now) == " 今日區間"


def test_helper_missing_t_before_open_is_previous_day() -> None:
    assert _quote_range_label(None, _now(2026, 11, 13, 8, 0)) == " 前日區間"


def test_embed_premarket_shows_previous_day_range_label() -> None:
    data: dict[str, Any] = {
        "symbol": "MRVL",
        "price": 284.68,
        "quote": {
            "c": 284.68,
            "d": -2.33,
            "dp": -0.8118,
            "pc": 287.01,
            "o": 280.0,
            "h": 286.5,
            "l": 279.1,
            "t": _ts(2026, 11, 12, 16, 0),
        },  # fmt: skip
    }

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "_FakeDT":
            return _now(2026, 11, 13, 3, 34)  # type: ignore[return-value]

    with patch("cogs.embed_builders.portfolio_embeds.datetime", _FakeDT):
        embed = create_tactical_symbol_embed(data)

    text = "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)
    assert "前日區間" in text
    assert "今日區間" not in text
