"""history_cache_expiry：依 interval × 交易時段決定歷史 K 線快取到期時間（純函式）。"""

from datetime import datetime

import pytest

from market_time import ny_tz
from services.market_data_service.caches import history_cache_expiry


def _ts(y: int, mo: int, d: int, h: int, mi: int) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=ny_tz).timestamp()


# 2026-10-07 為週三（一般交易日），10:07 ET 盤中
_NOW = _ts(2026, 10, 7, 10, 7)


@pytest.mark.parametrize(
    "interval,period,expected",
    [
        ("15m", "5d", _ts(2026, 10, 7, 10, 15)),
        ("1m", "1d", _ts(2026, 10, 7, 10, 8)),
        ("1h", "5d", _ts(2026, 10, 7, 10, 30)),
        ("1d", "5d", _NOW + 900),
        ("1d", "1y", _NOW + 21600),
    ],
)
def test_intraday_session_expiry(interval: str, period: str, expected: float) -> None:
    assert history_cache_expiry(interval, period, _NOW) == pytest.approx(expected)


def test_last_bar_never_exceeds_close() -> None:
    now = _ts(2026, 10, 7, 15, 55)
    assert history_cache_expiry("15m", "5d", now) == pytest.approx(
        _ts(2026, 10, 7, 16, 0)
    )
    # 60m bar 的邊界 15:30 之後下一根是 16:30，需被收盤截斷
    now2 = _ts(2026, 10, 7, 15, 45)
    assert history_cache_expiry("1h", "5d", now2) == pytest.approx(
        _ts(2026, 10, 7, 16, 0)
    )


def test_expiry_is_exactly_bar_close_not_after() -> None:
    """到期須正好在 bar 收盤時刻：之後命中的舊快取最後一根是部分 K 棒。"""
    now = _ts(2026, 10, 7, 10, 7)
    assert history_cache_expiry("15m", "5d", now) == _ts(2026, 10, 7, 10, 15)


def test_fetch_inside_settle_grace_caches_only_until_grace_end() -> None:
    """抓取落在 bar 收盤後 60 秒寬限內：只快取到寬限結束（Yahoo 尚未定案）。"""
    now = _ts(2026, 10, 7, 10, 15) + 20
    assert history_cache_expiry("15m", "5d", now) == pytest.approx(
        _ts(2026, 10, 7, 10, 15) + 60
    )
    # 寬限剛結束：回到「下一根 bar 收盤」
    after = _ts(2026, 10, 7, 10, 15) + 60
    assert history_cache_expiry("15m", "5d", after) == pytest.approx(
        _ts(2026, 10, 7, 10, 30)
    )


def test_open_boundary_has_no_grace() -> None:
    """開盤時刻不是 bar 收盤，不套寬限；9:30:20 仍到期於 9:45。"""
    now = _ts(2026, 10, 7, 9, 30) + 20
    assert history_cache_expiry("15m", "5d", now) == pytest.approx(
        _ts(2026, 10, 7, 9, 45)
    )


def test_unknown_interval_is_conservative_in_session() -> None:
    assert history_cache_expiry("weird", "1y", _NOW) == pytest.approx(_NOW + 900)


def test_post_close_short_ttl_then_next_open_refresh() -> None:
    now = _ts(2026, 10, 7, 16, 10)
    assert history_cache_expiry("15m", "5d", now) == pytest.approx(now + 300)
    assert history_cache_expiry("1d", "1y", now) == pytest.approx(now + 300)
    late = _ts(2026, 10, 7, 17, 0)
    assert history_cache_expiry("1d", "1y", late) == pytest.approx(
        _ts(2026, 10, 8, 8, 30)
    )


def test_weekend_expires_next_monday_pre_open() -> None:
    sat = _ts(2026, 10, 10, 12, 0)  # 週六
    assert history_cache_expiry("1d", "1y", sat) == pytest.approx(
        _ts(2026, 10, 12, 8, 30)
    )
    # 週一 10/12 哥倫布日 NYSE 照常開市；改驗證感恩節週末 → 週一
    fri_after = _ts(2026, 11, 27, 20, 0)  # 提前收盤日 13:00 之後很久
    sat_thanks = _ts(2026, 11, 28, 9, 0)
    assert history_cache_expiry("1d", "1y", sat_thanks) == pytest.approx(
        _ts(2026, 11, 30, 8, 30)
    )
    assert history_cache_expiry("1d", "1y", fri_after) == pytest.approx(
        _ts(2026, 11, 30, 8, 30)
    )


def test_holiday_monday_skips_to_tuesday() -> None:
    # 2026-09-07 週一勞動節休市 → 週六的快取到週二 08:30
    sat = _ts(2026, 9, 5, 12, 0)
    assert history_cache_expiry("1d", "1y", sat) == pytest.approx(
        _ts(2026, 9, 8, 8, 30)
    )


def test_pre_open_windows() -> None:
    assert history_cache_expiry("1d", "1y", _ts(2026, 10, 7, 7, 0)) == pytest.approx(
        _ts(2026, 10, 7, 8, 30)
    )
    assert history_cache_expiry("1d", "1y", _ts(2026, 10, 7, 9, 0)) == pytest.approx(
        _ts(2026, 10, 7, 9, 30) + 60
    )


def test_half_day_post_close_window() -> None:
    # 2026-11-27 提前 13:00 收盤；13:05 落在「收盤後 30 分內」
    now = _ts(2026, 11, 27, 13, 5)
    assert history_cache_expiry("15m", "5d", now) == pytest.approx(now + 300)


def test_never_returns_past_time() -> None:
    now = _ts(2026, 10, 7, 8, 29) + 50  # 距 08:30 僅 10 秒
    assert history_cache_expiry("1d", "1y", now) >= now + 60
