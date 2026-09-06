from datetime import datetime, timezone, tzinfo
from typing import Optional, Type
from unittest.mock import patch

import pandas as pd

import market_time


def _mock_schedule(
    market_open_utc: datetime, market_close_utc: datetime
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "market_open": pd.Timestamp(market_open_utc),
                "market_close": pd.Timestamp(market_close_utc),
            }
        ]
    )


def _patch_now(fake_now_ny: datetime) -> Type[datetime]:
    """回傳一個可作為 `market_time.datetime` 替身的類別，`now(tz)` 固定回傳
    `fake_now_ny`（轉換至呼叫端指定的時區），其餘行為委派給真實 datetime。"""

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: Optional[tzinfo] = None) -> datetime:  # type: ignore[override]
            if tz is not None:
                return fake_now_ny.astimezone(tz)
            return fake_now_ny

    return _FixedDatetime


def test_get_trading_day_elapsed_fraction_midday() -> None:
    """盤中時段進度：09:30 開盤、16:00 收盤，13:00 應為 3.5/6.5 小時 ≈ 53.85%。"""
    open_utc = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)  # 09:30 ET
    close_utc = datetime(2026, 3, 2, 21, 0, tzinfo=timezone.utc)  # 16:00 ET
    now_ny = datetime(2026, 3, 2, 13, 0, tzinfo=market_time.ny_tz)

    with patch.object(
        market_time.nyse_calendar,
        "schedule",
        return_value=_mock_schedule(open_utc, close_utc),
    ), patch("market_time.datetime", _patch_now(now_ny)):
        fraction = market_time.get_trading_day_elapsed_fraction()

    assert 0.53 < fraction < 0.545


def test_get_trading_day_elapsed_fraction_before_open_returns_one() -> None:
    """盤前（尚未開盤）應回傳 1.0，等同不做任何正規化。"""
    open_utc = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)
    close_utc = datetime(2026, 3, 2, 21, 0, tzinfo=timezone.utc)
    now_ny = datetime(2026, 3, 2, 8, 0, tzinfo=market_time.ny_tz)

    with patch.object(
        market_time.nyse_calendar,
        "schedule",
        return_value=_mock_schedule(open_utc, close_utc),
    ), patch("market_time.datetime", _patch_now(now_ny)):
        fraction = market_time.get_trading_day_elapsed_fraction()

    assert fraction == 1.0


def test_get_trading_day_elapsed_fraction_after_close_returns_one() -> None:
    """盤後應回傳 1.0，等同不做任何正規化。"""
    open_utc = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)
    close_utc = datetime(2026, 3, 2, 21, 0, tzinfo=timezone.utc)
    now_ny = datetime(2026, 3, 2, 18, 0, tzinfo=market_time.ny_tz)

    with patch.object(
        market_time.nyse_calendar,
        "schedule",
        return_value=_mock_schedule(open_utc, close_utc),
    ), patch("market_time.datetime", _patch_now(now_ny)):
        fraction = market_time.get_trading_day_elapsed_fraction()

    assert fraction == 1.0


def test_get_trading_day_elapsed_fraction_weekend_returns_one() -> None:
    """假日/週末（行事曆查無資料）應回傳 1.0，等同不做任何正規化。"""
    now_ny = datetime(2026, 3, 1, 13, 0, tzinfo=market_time.ny_tz)  # 週日

    with patch.object(
        market_time.nyse_calendar, "schedule", return_value=pd.DataFrame([])
    ), patch("market_time.datetime", _patch_now(now_ny)):
        fraction = market_time.get_trading_day_elapsed_fraction()

    assert fraction == 1.0


def test_get_trading_day_elapsed_fraction_clamps_near_open() -> None:
    """開盤後極短時間內應鉗制至 min_fraction，避免分母趨近零造成失真倍數。"""
    open_utc = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)
    close_utc = datetime(2026, 3, 2, 21, 0, tzinfo=timezone.utc)
    now_ny = datetime(2026, 3, 2, 9, 31, tzinfo=market_time.ny_tz)  # 開盤僅 1 分鐘

    with patch.object(
        market_time.nyse_calendar,
        "schedule",
        return_value=_mock_schedule(open_utc, close_utc),
    ), patch("market_time.datetime", _patch_now(now_ny)):
        fraction = market_time.get_trading_day_elapsed_fraction(min_fraction=0.05)

    assert fraction == 0.05


def test_get_trading_day_elapsed_fraction_calendar_error_returns_one() -> None:
    """行事曆查詢例外時應 fail-safe 回傳 1.0，不得向上拋出例外。"""
    with patch.object(
        market_time.nyse_calendar,
        "schedule",
        side_effect=RuntimeError("boom"),
    ):
        fraction = market_time.get_trading_day_elapsed_fraction()

    assert fraction == 1.0
