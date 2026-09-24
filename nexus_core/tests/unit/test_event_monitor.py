from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.event_monitor import EventMonitor


@pytest.mark.asyncio
async def test_send_event_alert_uses_embed_builder() -> None:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    monitor = EventMonitor(bot)
    embed = object()
    events = [SimpleNamespace(type="ECONOMIC", event="CPI", tte_hours=8)]

    with (
        patch(
            "services.event_monitor.get_full_user_context",
            return_value=SimpleNamespace(
                capital=100000.0,
                risk_limit=15.0,
                total_weighted_delta=20.0,
                total_theta=0.2,
                total_gamma=-5.0,
                total_vanna=6.0,
            ),
        ),
        patch(
            "services.event_monitor.get_quote",
            new=AsyncMock(return_value={"c": 500.0}),
        ),
        patch(
            "services.event_monitor.create_proactive_event_alert_embed",
            return_value=[embed],
        ) as mock_create,
    ):
        await monitor._send_event_alert(123, events)

    payloads = mock_create.call_args.args[0]
    assert len(payloads) == 1
    assert payloads[0]["name"] == "🔴 經濟數據: CPI"
    assert "持倉風險狀態" not in payloads[0]["risk_status"]
    assert "Vanna" in payloads[0]["instruction"] or "賣方" in payloads[0]["instruction"]
    bot.queue_dm.assert_awaited_once_with(123, embed=embed)


@pytest.mark.asyncio
async def test_check_upcoming_events_deduplicates_alerts() -> None:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    monitor = EventMonitor(bot)
    event = SimpleNamespace(
        type="ECONOMIC",
        event="CPI",
        time="2026-05-21T20:30:00",
        tte_hours=6,
    )

    with (
        patch("services.event_monitor.get_all_user_ids", return_value=[123]),
        patch(
            "services.event_monitor.calendar_service.get_portfolio_events",
            new=AsyncMock(return_value=[event]),
        ),
        patch.object(monitor, "_send_event_alert", new_callable=AsyncMock) as mock_send,
    ):
        await monitor.check_upcoming_events()
        await monitor.check_upcoming_events()

    mock_send.assert_awaited_once_with(123, [event])


@pytest.mark.asyncio
async def test_muted_events_are_not_marked_and_arrive_after_unmute() -> None:
    """過去先把事件標記為已提醒、到 _send_event_alert 才查開關，靜音期間的事件永久遺失。"""
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    monitor = EventMonitor(bot)
    event = SimpleNamespace(
        type="EARNINGS", symbol="NVDA", date="2026-05-21", tte_hours=20
    )

    enabled = {"on": False}
    with (
        patch("services.event_monitor.get_all_user_ids", return_value=[123]),
        patch(
            "services.event_monitor.calendar_service.get_portfolio_events",
            new=AsyncMock(return_value=[event]),
        ),
        patch(
            "database.is_notification_enabled",
            side_effect=lambda uid, key: enabled["on"],
        ),
        patch.object(
            monitor, "_send_event_alert", new=AsyncMock(return_value=True)
        ) as mock_send,
    ):
        await monitor.check_upcoming_events()
        mock_send.assert_not_awaited()

        enabled["on"] = True
        await monitor.check_upcoming_events()
        mock_send.assert_awaited_once_with(123, [event])


@pytest.mark.asyncio
async def test_failed_send_is_not_marked() -> None:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    monitor = EventMonitor(bot)
    event = SimpleNamespace(type="ECONOMIC", event="CPI", time="t", tte_hours=6)

    with (
        patch("services.event_monitor.get_all_user_ids", return_value=[123]),
        patch(
            "services.event_monitor.calendar_service.get_portfolio_events",
            new=AsyncMock(return_value=[event]),
        ),
        patch.object(
            monitor, "_send_event_alert", new=AsyncMock(return_value=False)
        ) as mock_send,
    ):
        await monitor.check_upcoming_events()
        await monitor.check_upcoming_events()

    assert mock_send.await_count == 2
