from datetime import datetime, timezone, tzinfo
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


def _patch_now(fake_now_ny: datetime) -> type[datetime]:
    """回傳一個可作為 `market_time.datetime` 替身的類別，`now(tz)` 固定回傳
    `fake_now_ny`（轉換至呼叫端指定的時區），其餘行為委派給真實 datetime。"""

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
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


def test_get_sleep_seconds_dst_fall_back() -> None:
    """冬令時間切換（EDT -> EST，秋季日光節約結束）：
    週日有 25 小時，睡眠秒數應為 72h 59m (262,740s)，而非本地相減的 71h 59m (259,140s)。
    """
    now_ny = datetime(2026, 10, 30, 9, 1, tzinfo=market_time.ny_tz)
    target_ny = datetime(2026, 11, 2, 9, 0, tzinfo=market_time.ny_tz)

    with patch("market_time.datetime", _patch_now(now_ny)):
        sleep_secs = market_time.get_sleep_seconds(target_ny)

    assert sleep_secs == 262740.0
    assert sleep_secs != 259140.0


def test_get_sleep_seconds_dst_spring_forward() -> None:
    """夏令時間切換（EST -> EDT，春季日光節約開始）：
    週日僅 23 小時，睡眠秒數應為 70h 59m (255,540s)，而非本地相減的 71h 59m (259,140s)。
    """
    now_ny = datetime(2026, 3, 6, 9, 1, tzinfo=market_time.ny_tz)
    target_ny = datetime(2026, 3, 9, 9, 0, tzinfo=market_time.ny_tz)

    with patch("market_time.datetime", _patch_now(now_ny)):
        sleep_secs = market_time.get_sleep_seconds(target_ny)

    assert sleep_secs == 255540.0
    assert sleep_secs != 259140.0


def test_get_sleep_seconds_same_day() -> None:
    """同一日內常態計算：9:00 至 9:30 應為 1800 秒。"""
    now_ny = datetime(2026, 3, 2, 9, 0, tzinfo=market_time.ny_tz)
    target_ny = datetime(2026, 3, 2, 9, 30, tzinfo=market_time.ny_tz)

    with patch("market_time.datetime", _patch_now(now_ny)):
        sleep_secs = market_time.get_sleep_seconds(target_ny)

    assert sleep_secs == 1800.0


def test_get_sleep_seconds_none_target_returns_one_hour() -> None:
    """目標時間為 None 時應預設回傳 3600.0 秒。"""
    assert market_time.get_sleep_seconds(None) == 3600.0


def test_get_sleep_seconds_past_target_clamps_to_zero() -> None:
    """目標時間若已過期，應鉗制回傳 0.0 秒。"""
    now_ny = datetime(2026, 3, 2, 9, 30, tzinfo=market_time.ny_tz)
    target_ny = datetime(2026, 3, 2, 9, 0, tzinfo=market_time.ny_tz)

    with patch("market_time.datetime", _patch_now(now_ny)):
        sleep_secs = market_time.get_sleep_seconds(target_ny)

    assert sleep_secs == 0.0


# ─── get_options_chain_recompute_ready_at ───────────────────────────────────


def _recompute_schedule(days: list[tuple[int, int, int]]) -> pd.DataFrame:
    """以美東 09:30／16:00 建立多日行事曆（依 DST 正確轉 UTC）。"""
    rows = []
    for y, m, d in days:
        o = datetime(y, m, d, 9, 30, tzinfo=market_time.ny_tz)
        c = datetime(y, m, d, 16, 0, tzinfo=market_time.ny_tz)
        rows.append(
            {
                "market_open": pd.Timestamp(o.astimezone(timezone.utc)),
                "market_close": pd.Timestamp(c.astimezone(timezone.utc)),
            }
        )
    return pd.DataFrame(rows)


def _ready_at(now_ny: datetime, days: list[tuple[int, int, int]]) -> datetime | None:
    with patch.object(
        market_time.nyse_calendar,
        "schedule",
        return_value=_recompute_schedule(days),
    ):
        return market_time.get_options_chain_recompute_ready_at(now_ny)


def _taipei(dt: datetime) -> datetime:
    from zoneinfo import ZoneInfo

    return dt.astimezone(ZoneInfo("Asia/Taipei"))


def test_options_chain_recompute_ready_at_premarket_returns_today_open_plus_30() -> (
    None
):
    now = datetime(2026, 11, 12, 8, 0, tzinfo=market_time.ny_tz)
    result = _ready_at(now, [(2026, 11, 12), (2026, 11, 13)])
    assert result is not None
    assert (result.month, result.day, result.hour, result.minute) == (11, 12, 10, 0)
    tw = _taipei(result)
    assert (tw.day, tw.hour, tw.minute) == (12, 23, 0)


def test_options_chain_recompute_ready_at_first_30_minutes_returns_today() -> None:
    now = datetime(2026, 11, 12, 9, 40, tzinfo=market_time.ny_tz)
    result = _ready_at(now, [(2026, 11, 12), (2026, 11, 13)])
    assert result is not None
    assert (result.day, result.hour, result.minute) == (12, 10, 0)


def test_options_chain_recompute_ready_at_during_session_returns_none() -> None:
    now = datetime(2026, 11, 12, 10, 30, tzinfo=market_time.ny_tz)
    assert _ready_at(now, [(2026, 11, 12), (2026, 11, 13)]) is None


def test_options_chain_recompute_ready_at_after_close_returns_next_session() -> None:
    now = datetime(2026, 11, 12, 17, 0, tzinfo=market_time.ny_tz)
    result = _ready_at(now, [(2026, 11, 12), (2026, 11, 13)])
    assert result is not None
    assert (result.day, result.hour, result.minute) == (13, 10, 0)


def test_options_chain_recompute_ready_at_weekend_returns_monday() -> None:
    now = datetime(2026, 11, 14, 12, 0, tzinfo=market_time.ny_tz)  # 週六
    result = _ready_at(now, [(2026, 11, 16), (2026, 11, 17)])
    assert result is not None
    assert (result.day, result.hour, result.minute) == (16, 10, 0)


def test_options_chain_recompute_ready_at_summer_returns_2200_taipei() -> None:
    now = datetime(2026, 7, 15, 8, 0, tzinfo=market_time.ny_tz)
    result = _ready_at(now, [(2026, 7, 15), (2026, 7, 16)])
    assert result is not None
    assert (result.hour, result.minute) == (10, 0)
    tw = _taipei(result)
    assert (tw.hour, tw.minute) == (22, 0)


def test_options_chain_recompute_ready_at_calendar_error_returns_none() -> None:
    now = datetime(2026, 11, 12, 8, 0, tzinfo=market_time.ny_tz)
    with patch.object(
        market_time.nyse_calendar, "schedule", side_effect=RuntimeError("boom")
    ):
        assert market_time.get_options_chain_recompute_ready_at(now) is None


def _et(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=market_time.ny_tz)


def test_post_close_window_boundaries() -> None:
    """一般交易日（2026-11-11 週三，16:00 收盤）：收盤整點 False、+15 分 True、+15 分 1 秒 False。"""
    w = 15 * 60.0
    assert not market_time.is_within_post_close_window(w, _et(2026, 11, 11, 15, 59))
    assert not market_time.is_within_post_close_window(w, _et(2026, 11, 11, 16, 0))
    assert market_time.is_within_post_close_window(w, _et(2026, 11, 11, 16, 0, 1))
    assert market_time.is_within_post_close_window(w, _et(2026, 11, 11, 16, 2))
    assert market_time.is_within_post_close_window(w, _et(2026, 11, 11, 16, 3))
    assert market_time.is_within_post_close_window(w, _et(2026, 11, 11, 16, 15))
    assert not market_time.is_within_post_close_window(w, _et(2026, 11, 11, 16, 15, 1))
    assert not market_time.is_within_post_close_window(w, _et(2026, 11, 11, 16, 18))
    # naive 視為美東時間
    assert market_time.is_within_post_close_window(w, datetime(2026, 11, 11, 16, 3))


def test_post_close_window_non_trading_day() -> None:
    """週末與國定假日（感恩節 2026-11-26）一律 False。"""
    w = 15 * 60.0
    assert not market_time.is_within_post_close_window(w, _et(2026, 11, 14, 16, 3))
    assert not market_time.is_within_post_close_window(w, _et(2026, 11, 15, 16, 3))
    assert not market_time.is_within_post_close_window(w, _et(2026, 11, 26, 16, 3))
    assert not market_time.is_within_post_close_window(w, _et(2026, 11, 26, 13, 3))


def test_post_close_window_early_close() -> None:
    """半日市（感恩節翌日 2026-11-27，13:00 收盤）以 13:00 為基準。"""
    w = 15 * 60.0
    assert not market_time.is_within_post_close_window(w, _et(2026, 11, 27, 13, 0))
    assert market_time.is_within_post_close_window(w, _et(2026, 11, 27, 13, 3))
    assert market_time.is_within_post_close_window(w, _et(2026, 11, 27, 13, 15))
    assert not market_time.is_within_post_close_window(w, _et(2026, 11, 27, 13, 15, 1))
    assert not market_time.is_within_post_close_window(w, _et(2026, 11, 27, 16, 3))
    assert not market_time.is_within_post_close_window(w, _et(2026, 11, 27, 12, 50))


def test_post_close_window_defaults_to_now() -> None:
    """省略 now 時取現在時間（以 `market_time.datetime` 替身凍結）。"""
    with patch("market_time.datetime", _patch_now(_et(2026, 11, 11, 16, 2))):
        assert market_time.is_within_post_close_window()
    with patch("market_time.datetime", _patch_now(_et(2026, 11, 11, 16, 20))):
        assert not market_time.is_within_post_close_window()
