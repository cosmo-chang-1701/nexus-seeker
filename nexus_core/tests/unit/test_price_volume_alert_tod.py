"""價量警報：同時段量能基準與以 K 棒日期去重的單元測試。"""

from datetime import datetime
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import database
from cogs.embed_builders.alert_embeds import create_price_volume_alert_embed
from cogs.trading.price_volume_alert_monitor import PriceVolumeAlertMonitorCog
from database.price_volume_watch import (
    PriceVolumeWatch,
    WatchDirection,
    upsert_watch,
)
from market_analysis.price_volume_alert import (
    Confirmed15mBar,
    evaluate_watch_trigger,
    volume_base,
)


def _bar(
    volume: float,
    avg: float,
    tod: Optional[float] = None,
    bar_time: Optional[datetime] = None,
) -> Confirmed15mBar:
    return Confirmed15mBar(
        symbol="AAPL",
        bar_time=bar_time or datetime(2026, 11, 10, 15, 45),
        close=110.0,
        volume=volume,
        avg_volume=avg,
        tod_avg_volume=tod,
        tod_sample_count=4 if tod is not None else 0,
    )


def test_tod_base_blocks_when_same_slot_ratio_low() -> None:
    bar = _bar(volume=2500.0, avg=1000.0, tod=2000.0)  # 20 根 2.5x、同時段 1.25x
    assert not evaluate_watch_trigger(bar, 100.0, WatchDirection.ABOVE, 1.5)


def test_tod_base_triggers_when_same_slot_ratio_high() -> None:
    bar = _bar(volume=1400.0, avg=1000.0, tod=800.0)  # 20 根 1.4x、同時段 1.75x
    assert evaluate_watch_trigger(bar, 100.0, WatchDirection.ABOVE, 1.5)


def test_fallback_to_20_bar_when_tod_missing() -> None:
    assert volume_base(_bar(2000.0, 1000.0)) == (1000.0, "20 根")
    assert evaluate_watch_trigger(
        _bar(2000.0, 1000.0), 100.0, WatchDirection.ABOVE, 1.5
    )
    assert not evaluate_watch_trigger(
        _bar(1400.0, 1000.0), 100.0, WatchDirection.ABOVE, 1.5
    )


@pytest.fixture
def mock_bot() -> Any:
    bot = MagicMock()
    bot._is_leader_instance = True
    bot.queue_dm = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_monitor_dedup_uses_bar_date(mock_bot: Any, db_conn: Any) -> None:
    uid = 777555
    await upsert_watch(uid, "AAPL", 100.0, WatchDirection.ABOVE, 1.5)
    close_bar = _bar(3000.0, 1000.0, bar_time=datetime(2026, 11, 10, 15, 45))
    intraday_bar = _bar(3000.0, 1000.0, bar_time=datetime(2026, 11, 11, 10, 0))

    cog = PriceVolumeAlertMonitorCog(mock_bot)
    with (
        patch(
            "cogs.trading.price_volume_alert_monitor.get_confirmed_15m_bar",
            new_callable=AsyncMock,
        ) as mock_get_bar,
        patch("database.is_notification_enabled", return_value=True),
    ):
        # 11/11 09:32 評估到前一日 15:45 收盤棒
        mock_get_bar.return_value = close_bar
        await cog._evaluate_price_volume_alerts()
        mock_bot.queue_dm.assert_called_once()
        assert database.get_kv_cache(f"price_volume_alert_{uid}_AAPL_20261110")
        assert not database.get_kv_cache(f"price_volume_alert_{uid}_AAPL_20261111")

        # 同一根棒再評估（含隔日）不再發送
        mock_bot.queue_dm.reset_mock()
        await cog._evaluate_price_volume_alerts()
        await cog._evaluate_price_volume_alerts()
        mock_bot.queue_dm.assert_not_called()

        # 當日 10:00 真正的盤中突破必須能發送
        mock_get_bar.return_value = intraday_bar
        await cog._evaluate_price_volume_alerts()
        mock_bot.queue_dm.assert_called_once()

    await cog.cog_unload()


def test_embed_shows_same_slot_label() -> None:
    watch = PriceVolumeWatch(
        user_id=1,
        symbol="AAPL",
        target_price=100.0,
        direction=WatchDirection.ABOVE,
        volume_multiplier=1.5,
    )
    embed = create_price_volume_alert_embed(watch, _bar(3000.0, 1000.0, tod=1500.0))
    value = next(str(f.value) for f in embed.fields if f.name == "🎯 觸發事件")
    assert "同時段均量" in value
    assert "2.00x" in value
    embed2 = create_price_volume_alert_embed(watch, _bar(3000.0, 1000.0))
    value2 = next(str(f.value) for f in embed2.fields if f.name == "🎯 觸發事件")
    assert "20根均量" in value2


def test_simplenamespace_without_tod_falls_back_and_renders_dash() -> None:
    from types import SimpleNamespace

    ns: Any = SimpleNamespace(
        volume=500.0,
        avg_volume=0.0,
        close=110.0,
        bar_time=datetime(2026, 11, 10, 10, 0),
    )
    assert volume_base(ns) == (0.0, "20 根")
    watch = PriceVolumeWatch(
        user_id=1,
        symbol="AAPL",
        target_price=100.0,
        direction=WatchDirection.ABOVE,
        volume_multiplier=1.5,
    )
    embed = create_price_volume_alert_embed(watch, ns)
    value = next(str(f.value) for f in embed.fields if f.name == "🎯 觸發事件")
    assert "--x" in value
