"""價量警報監控器：收盤棒於收盤後 16:02 評估、不對前一場次的棒告警。"""

from datetime import datetime, tzinfo
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import market_time
from cogs.trading.price_volume_alert_monitor import PriceVolumeAlertMonitorCog
from database.price_volume_watch import WatchDirection, upsert_watch
from market_analysis.price_volume_alert import Confirmed15mBar


def _et(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=market_time.ny_tz)


def _frozen(fake_now: datetime) -> type[datetime]:
    class _Fixed(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
            return fake_now.astimezone(tz) if tz is not None else fake_now

    return _Fixed


def _bar(bar_time: datetime) -> Confirmed15mBar:
    return Confirmed15mBar(
        symbol="AAPL",
        bar_time=bar_time,
        close=110.0,
        volume=3000.0,
        avg_volume=1000.0,
    )


@pytest.fixture
def mock_bot() -> Any:
    bot = MagicMock()
    bot._is_leader_instance = True
    bot.queue_dm = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    return bot


async def _run_loop_body(cog: Any, now: datetime) -> AsyncMock:
    """以凍結時間跑一次排程主體，回傳被 patch 的 `_evaluate_price_volume_alerts`。"""
    evaluate = AsyncMock()
    with (
        patch("market_time.datetime", _frozen(now)),
        patch.object(cog, "_evaluate_price_volume_alerts", new=evaluate),
        patch(
            "cogs.trading.price_volume_alert_monitor.flush_dispatch_records",
            new=AsyncMock(),
        ),
    ):
        await cog.price_volume_alert_monitor.coro(cog)
    return evaluate


async def test_post_close_1602_evaluates_closing_bar(
    mock_bot: Any, db_conn: Any
) -> None:
    uid = 777901
    await upsert_watch(uid, "AAPL", 100.0, WatchDirection.ABOVE, 1.5)
    now = _et(2026, 11, 11, 16, 2)
    cog = PriceVolumeAlertMonitorCog(mock_bot)

    # 排程主體在收盤後 2 分鐘放行
    assert (await _run_loop_body(cog, now)).await_count == 1

    # 且 15:45 收盤棒（今日）會被評估並告警
    with (
        patch(
            "cogs.trading.price_volume_alert_monitor.get_confirmed_15m_bar",
            new=AsyncMock(return_value=_bar(datetime(2026, 11, 11, 15, 45))),
        ),
        patch("database.is_notification_enabled", return_value=True),
        patch("cogs.trading.price_volume_alert_monitor._now_et", return_value=now),
    ):
        await cog._evaluate_price_volume_alerts()
    mock_bot.queue_dm.assert_called_once()
    await cog.cog_unload()


async def test_post_close_1617_loop_body_skipped(mock_bot: Any) -> None:
    cog = PriceVolumeAlertMonitorCog(mock_bot)
    assert (await _run_loop_body(cog, _et(2026, 11, 11, 16, 17))).await_count == 0
    # 週末 16:02 也不跑
    assert (await _run_loop_body(cog, _et(2026, 11, 14, 16, 2))).await_count == 0
    # 盤中照常
    assert (await _run_loop_body(cog, _et(2026, 11, 11, 10, 32))).await_count == 1
    await cog.cog_unload()


async def test_previous_session_bar_no_alert_at_0932(
    mock_bot: Any, db_conn: Any
) -> None:
    uid = 777902
    await upsert_watch(uid, "AAPL", 100.0, WatchDirection.ABOVE, 1.5)
    now = _et(2026, 11, 11, 9, 32)
    cog = PriceVolumeAlertMonitorCog(mock_bot)
    with (
        patch(
            "cogs.trading.price_volume_alert_monitor.get_confirmed_15m_bar",
            new=AsyncMock(return_value=_bar(datetime(2026, 11, 10, 15, 45))),
        ),
        patch("database.is_notification_enabled", return_value=True),
        patch("cogs.trading.price_volume_alert_monitor._now_et", return_value=now),
    ):
        await cog._evaluate_price_volume_alerts()
    mock_bot.queue_dm.assert_not_called()
    await cog.cog_unload()


async def test_after_close_row_not_treated_as_closing_bar(
    mock_bot: Any, db_conn: Any
) -> None:
    uid = 777903
    await upsert_watch(uid, "AAPL", 100.0, WatchDirection.ABOVE, 1.5)
    now = _et(2026, 11, 11, 16, 2)
    cog = PriceVolumeAlertMonitorCog(mock_bot)
    with (
        patch(
            "cogs.trading.price_volume_alert_monitor.get_confirmed_15m_bar",
            new=AsyncMock(return_value=_bar(datetime(2026, 11, 11, 16, 0))),
        ),
        patch("database.is_notification_enabled", return_value=True),
        patch("cogs.trading.price_volume_alert_monitor._now_et", return_value=now),
    ):
        await cog._evaluate_price_volume_alerts()
    mock_bot.queue_dm.assert_not_called()

    # 半日市：13:00 之後起始的列同樣不算收盤棒，12:45 才是
    now2 = _et(2026, 11, 27, 13, 2)
    with (
        patch(
            "cogs.trading.price_volume_alert_monitor.get_confirmed_15m_bar",
            new=AsyncMock(return_value=_bar(datetime(2026, 11, 27, 13, 0))),
        ),
        patch("database.is_notification_enabled", return_value=True),
        patch("cogs.trading.price_volume_alert_monitor._now_et", return_value=now2),
    ):
        await cog._evaluate_price_volume_alerts()
    mock_bot.queue_dm.assert_not_called()
    with (
        patch(
            "cogs.trading.price_volume_alert_monitor.get_confirmed_15m_bar",
            new=AsyncMock(return_value=_bar(datetime(2026, 11, 27, 12, 45))),
        ),
        patch("database.is_notification_enabled", return_value=True),
        patch("cogs.trading.price_volume_alert_monitor._now_et", return_value=now2),
    ):
        await cog._evaluate_price_volume_alerts()
    mock_bot.queue_dm.assert_called_once()
    await cog.cog_unload()
