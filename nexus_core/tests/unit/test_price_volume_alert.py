"""Unit tests for the price-volume breakout alert feature."""

from typing import Any
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from pydantic import ValidationError

import market_time
from database.notifications import ALL_NOTIFICATION_KEYS, PRESET_PROFILES
from database.price_volume_watch import (
    PriceVolumeWatch,
    WatchDirection,
    WatchLimitExceededError,
    delete_watch,
    get_all_watches,
    get_user_watches,
    upsert_watch,
)
from market_analysis.price_volume_alert import (
    Confirmed15mBar,
    evaluate_watch_trigger,
    get_confirmed_15m_bar,
)
from cogs.embed_builders.alert_embeds import create_price_volume_alert_embed
from cogs.trading.price_volume_alert_monitor import PriceVolumeAlertMonitorCog


def _make_15m_df(
    last_bar_age_minutes: float,
    num_bars: int = 22,
    last_close: float = 100.0,
    last_volume: float = 1000.0,
    lookback_volume: float = 100.0,
) -> pd.DataFrame:
    """建立一份 15 分鐘 K 線 DataFrame，最後一根的「起始時間」在
    `last_bar_age_minutes` 分鐘前，其餘全部往前推 15 分鐘一根。
    """
    now_naive = datetime.now(market_time.ny_tz).replace(tzinfo=None)
    last_bar_start = now_naive - timedelta(minutes=last_bar_age_minutes)
    idx = [last_bar_start - timedelta(minutes=15 * i) for i in range(num_bars)][::-1]

    closes = [50.0] * (num_bars - 1) + [last_close]
    volumes = [lookback_volume] * (num_bars - 1) + [last_volume]

    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": volumes,
        },
        index=pd.DatetimeIndex(idx),
    )


# ============================================================================
# 1. get_confirmed_15m_bar — bar-completeness tests
# ============================================================================


@pytest.mark.asyncio
async def test_get_confirmed_15m_bar_excludes_forming_bar() -> None:
    """盤中查詢時，最後一根尚未收盤 (起始時間 + 15分 > 現在)，應改用倒數第二根。"""
    df = _make_15m_df(
        last_bar_age_minutes=5,  # 5 分鐘前開始，尚未收盤
        last_close=999.0,
        last_volume=999999.0,
    )

    with patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as mock_hist:
        mock_hist.return_value = df
        bar = await get_confirmed_15m_bar("AAPL")

    assert bar is not None
    # 應使用倒數第二根 (bar_time == df.index[-2])，不可用尚在成型的最後一根
    assert bar.bar_time == df.index[-2].to_pydatetime()
    assert bar.close == 50.0
    assert bar.close != 999.0


@pytest.mark.asyncio
async def test_get_confirmed_15m_bar_uses_last_bar_when_closed() -> None:
    """最後一根起始時間 + 15分 已過去，代表已收盤，應直接使用最後一根。"""
    df = _make_15m_df(
        last_bar_age_minutes=20,  # 20 分鐘前開始，已收盤
        last_close=123.45,
        last_volume=5000.0,
        lookback_volume=1000.0,
    )

    with patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as mock_hist:
        mock_hist.return_value = df
        bar = await get_confirmed_15m_bar("AAPL")

    assert bar is not None
    assert bar.bar_time == df.index[-1].to_pydatetime()
    assert bar.close == 123.45
    assert bar.volume == 5000.0
    assert bar.avg_volume == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_get_confirmed_15m_bar_force_refresh_used() -> None:
    """必須繞過快取，確保排程不會拿到過期資料。"""
    df = _make_15m_df(last_bar_age_minutes=20)

    with patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as mock_hist:
        mock_hist.return_value = df
        await get_confirmed_15m_bar("AAPL")

    mock_hist.assert_called_once()
    _, kwargs = mock_hist.call_args
    assert kwargs["force_refresh"] is True
    assert kwargs["interval"] == "15m"


@pytest.mark.asyncio
async def test_get_confirmed_15m_bar_insufficient_data_returns_none() -> None:
    """資料不足 20+2 根時應回傳 None，不誤判。"""
    df = _make_15m_df(last_bar_age_minutes=20, num_bars=10)

    with patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as mock_hist:
        mock_hist.return_value = df
        bar = await get_confirmed_15m_bar("AAPL")

    assert bar is None


@pytest.mark.asyncio
async def test_get_confirmed_15m_bar_empty_df_returns_none() -> None:
    """空 DataFrame (抓取失敗/無資料) 應回傳 None。"""
    with patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as mock_hist:
        mock_hist.return_value = pd.DataFrame()
        bar = await get_confirmed_15m_bar("AAPL")

    assert bar is None


@pytest.mark.asyncio
async def test_get_confirmed_15m_bar_fetch_exception_returns_none() -> None:
    """抓取拋出例外應被攔截並回傳 None，而非讓例外向外傳遞。"""
    with patch(
        "services.market_data_service.get_history_df", new_callable=AsyncMock
    ) as mock_hist:
        mock_hist.side_effect = Exception("network error")
        bar = await get_confirmed_15m_bar("AAPL")

    assert bar is None


# ============================================================================
# 2. evaluate_watch_trigger — threshold comparison tests
# ============================================================================


def _bar(close: float, volume: float, avg_volume: float) -> Confirmed15mBar:
    return Confirmed15mBar(
        symbol="AAPL",
        bar_time=datetime.now(),
        close=close,
        volume=volume,
        avg_volume=avg_volume,
    )


def test_evaluate_watch_trigger_above_both_pass() -> None:
    bar = _bar(close=110.0, volume=3000.0, avg_volume=1000.0)
    assert (
        evaluate_watch_trigger(
            bar,
            target_price=100.0,
            direction=WatchDirection.ABOVE,
            volume_multiplier=1.5,
        )
        is True
    )


def test_evaluate_watch_trigger_above_price_fails() -> None:
    bar = _bar(close=90.0, volume=3000.0, avg_volume=1000.0)
    assert (
        evaluate_watch_trigger(
            bar,
            target_price=100.0,
            direction=WatchDirection.ABOVE,
            volume_multiplier=1.5,
        )
        is False
    )


def test_evaluate_watch_trigger_above_volume_fails() -> None:
    bar = _bar(close=110.0, volume=1200.0, avg_volume=1000.0)
    assert (
        evaluate_watch_trigger(
            bar,
            target_price=100.0,
            direction=WatchDirection.ABOVE,
            volume_multiplier=1.5,
        )
        is False
    )


def test_evaluate_watch_trigger_below_both_pass() -> None:
    bar = _bar(close=90.0, volume=3000.0, avg_volume=1000.0)
    assert (
        evaluate_watch_trigger(
            bar,
            target_price=100.0,
            direction=WatchDirection.BELOW,
            volume_multiplier=1.5,
        )
        is True
    )


def test_evaluate_watch_trigger_below_price_fails() -> None:
    bar = _bar(close=110.0, volume=3000.0, avg_volume=1000.0)
    assert (
        evaluate_watch_trigger(
            bar,
            target_price=100.0,
            direction=WatchDirection.BELOW,
            volume_multiplier=1.5,
        )
        is False
    )


def test_evaluate_watch_trigger_zero_avg_volume_fails() -> None:
    bar = _bar(close=110.0, volume=3000.0, avg_volume=0.0)
    assert (
        evaluate_watch_trigger(
            bar,
            target_price=100.0,
            direction=WatchDirection.ABOVE,
            volume_multiplier=1.5,
        )
        is False
    )


def test_evaluate_watch_trigger_zero_volume_multiplier_passes() -> None:
    # 設為 0 時為純價格警報，即使 volume=0 或 avg_volume=0 只要價格達標即觸發
    bar = _bar(close=110.0, volume=0.0, avg_volume=0.0)
    assert (
        evaluate_watch_trigger(
            bar,
            target_price=100.0,
            direction=WatchDirection.ABOVE,
            volume_multiplier=0.0,
        )
        is True
    )

    # 設為 0 但價格不符時仍不觸發
    bar_fail = _bar(close=90.0, volume=5000.0, avg_volume=1000.0)
    assert (
        evaluate_watch_trigger(
            bar_fail,
            target_price=100.0,
            direction=WatchDirection.ABOVE,
            volume_multiplier=0.0,
        )
        is False
    )


def test_evaluate_watch_trigger_one_volume_multiplier() -> None:
    # 設為 1.0 時成交量需達到均量
    bar_pass = _bar(close=110.0, volume=1000.0, avg_volume=1000.0)
    assert (
        evaluate_watch_trigger(
            bar_pass,
            target_price=100.0,
            direction=WatchDirection.ABOVE,
            volume_multiplier=1.0,
        )
        is True
    )

    bar_fail = _bar(close=110.0, volume=999.0, avg_volume=1000.0)
    assert (
        evaluate_watch_trigger(
            bar_fail,
            target_price=100.0,
            direction=WatchDirection.ABOVE,
            volume_multiplier=1.0,
        )
        is False
    )


# ============================================================================
# 3. PriceVolumeWatch model & CRUD tests
# ============================================================================


def test_price_volume_watch_defaults_and_rounding() -> None:
    watch = PriceVolumeWatch(user_id=1, symbol="aapl", target_price=100.456)
    assert watch.symbol == "AAPL"
    assert watch.target_price == 100.46
    assert watch.direction == WatchDirection.ABOVE
    assert watch.volume_multiplier == 1.5

    # 測試允許 0 與 1.0
    watch_zero = PriceVolumeWatch(
        user_id=1, symbol="NVDA", target_price=120.0, volume_multiplier=0
    )
    assert watch_zero.volume_multiplier == 0.0

    watch_one = PriceVolumeWatch(
        user_id=1, symbol="NVDA", target_price=120.0, volume_multiplier=1.0
    )
    assert watch_one.volume_multiplier == 1.0


def test_price_volume_watch_validation_error() -> None:
    with pytest.raises(ValidationError):
        PriceVolumeWatch(user_id=1, symbol="AAPL", target_price=-5.0)
    with pytest.raises(ValidationError):
        PriceVolumeWatch(
            user_id=1, symbol="AAPL", target_price=100.0, volume_multiplier=10.0
        )
    with pytest.raises(ValidationError):
        PriceVolumeWatch(
            user_id=1, symbol="AAPL", target_price=100.0, volume_multiplier=-0.1
        )


@pytest.mark.asyncio
async def test_upsert_get_delete_watch(db_conn: Any) -> None:
    uid = 555111
    watch = await upsert_watch(uid, "TSLA", 250.0, WatchDirection.ABOVE, 1.5)
    assert watch.symbol == "TSLA"

    watches = get_user_watches(uid)
    assert len(watches) == 1
    assert watches[0].target_price == 250.0

    # upsert 同一標的應更新而非新增第二筆
    await upsert_watch(uid, "tsla", 260.0, WatchDirection.BELOW, 2.0)
    watches = get_user_watches(uid)
    assert len(watches) == 1
    assert watches[0].target_price == 260.0
    assert watches[0].direction == WatchDirection.BELOW

    removed = await delete_watch(uid, "TSLA")
    assert removed is True
    assert get_user_watches(uid) == []

    removed_again = await delete_watch(uid, "TSLA")
    assert removed_again is False


@pytest.mark.asyncio
async def test_upsert_watch_limit_exceeded(db_conn: Any) -> None:
    uid = 555222
    for i in range(15):
        await upsert_watch(uid, f"SYM{i}", 100.0)

    with pytest.raises(WatchLimitExceededError):
        await upsert_watch(uid, "OVERFLOW", 100.0)

    # 更新既有標的不應受上限阻擋
    updated = await upsert_watch(uid, "SYM0", 150.0)
    assert updated.target_price == 150.0


@pytest.mark.asyncio
async def test_get_all_watches_spans_users(db_conn: Any) -> None:
    await upsert_watch(1, "AAPL", 200.0)
    await upsert_watch(2, "AAPL", 210.0)
    await upsert_watch(2, "MSFT", 400.0)

    all_watches = get_all_watches()
    assert len(all_watches) == 3
    symbols = {(w.user_id, w.symbol) for w in all_watches}
    assert (1, "AAPL") in symbols
    assert (2, "AAPL") in symbols
    assert (2, "MSFT") in symbols


# ============================================================================
# 4. Embed builder tests
# ============================================================================


def test_create_price_volume_alert_embed_above() -> None:
    watch = PriceVolumeWatch(
        user_id=1,
        symbol="AAPL",
        target_price=230.0,
        direction=WatchDirection.ABOVE,
        volume_multiplier=1.5,
    )
    bar = Confirmed15mBar(
        symbol="AAPL",
        bar_time=datetime(2026, 8, 19, 10, 30),
        close=231.5,
        volume=250000.0,
        avg_volume=100000.0,
    )

    embed = create_price_volume_alert_embed(watch, bar)
    assert embed.title is not None and "突破" in embed.title

    fields = {
        str(f.name): str(f.value)
        for f in embed.fields
        if f.name is not None and f.value is not None
    }
    for fname, fval in fields.items():
        assert fval.startswith("```ansi\n"), f"Field {fname} must start with ```ansi"
        assert fval.endswith("\n```"), f"Field {fname} must end with ```"

    assert "🎯 觸發事件" in fields
    assert "$231.50" in fields["🎯 觸發事件"]
    assert "$230.00" in fields["🎯 觸發事件"]


def test_create_price_volume_alert_embed_below() -> None:
    watch = PriceVolumeWatch(
        user_id=1,
        symbol="TSLA",
        target_price=200.0,
        direction=WatchDirection.BELOW,
        volume_multiplier=2.0,
    )
    bar = Confirmed15mBar(
        symbol="TSLA",
        bar_time=datetime(2026, 8, 19, 10, 30),
        close=195.0,
        volume=300000.0,
        avg_volume=100000.0,
    )

    embed = create_price_volume_alert_embed(watch, bar)
    assert embed.title is not None and "跌破" in embed.title


def test_create_price_volume_alert_embed_zero_multiplier() -> None:
    watch = PriceVolumeWatch(
        user_id=1,
        symbol="NVDA",
        target_price=125.0,
        direction=WatchDirection.ABOVE,
        volume_multiplier=0.0,
    )
    bar = Confirmed15mBar(
        symbol="NVDA",
        bar_time=datetime(2026, 8, 19, 10, 30),
        close=126.0,
        volume=50000.0,
        avg_volume=100000.0,
    )

    embed = create_price_volume_alert_embed(watch, bar)
    fields = {
        str(f.name): str(f.value)
        for f in embed.fields
        if f.name is not None and f.value is not None
    }
    assert "🎯 觸發事件" in fields
    assert "無放量門檻限制" in fields["🎯 觸發事件"]


# ============================================================================
# 5. Scheduler dedup & dispatch tests
# ============================================================================


@pytest.fixture
def mock_bot() -> Any:
    bot = MagicMock()
    bot._is_leader_instance = True
    bot.queue_dm = AsyncMock()
    bot.wait_until_ready = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_monitor_dispatches_and_dedups(mock_bot: Any, db_conn: Any) -> None:
    """同一 user+symbol 當日第二次評估不應重複發送 DM。"""
    uid = 777333
    await upsert_watch(uid, "AAPL", 100.0, WatchDirection.ABOVE, 1.5)

    triggering_bar = Confirmed15mBar(
        symbol="AAPL",
        bar_time=datetime.now(),
        close=110.0,
        volume=3000.0,
        avg_volume=1000.0,
    )

    cog = PriceVolumeAlertMonitorCog(mock_bot)

    with patch(
        "cogs.trading.price_volume_alert_monitor.get_confirmed_15m_bar",
        new_callable=AsyncMock,
    ) as mock_get_bar, patch("database.is_notification_enabled", return_value=True):
        mock_get_bar.return_value = triggering_bar

        await cog._evaluate_price_volume_alerts()
        mock_bot.queue_dm.assert_called_once()

        # 第二次評估應被 KV cache 去重擋下，不再重複發送
        mock_bot.queue_dm.reset_mock()
        await cog._evaluate_price_volume_alerts()
        mock_bot.queue_dm.assert_not_called()

    await cog.cog_unload()


@pytest.mark.asyncio
async def test_monitor_skips_when_notification_disabled(
    mock_bot: Any, db_conn: Any
) -> None:
    uid = 777444
    await upsert_watch(uid, "MSFT", 100.0, WatchDirection.ABOVE, 1.5)

    triggering_bar = Confirmed15mBar(
        symbol="MSFT",
        bar_time=datetime.now(),
        close=110.0,
        volume=3000.0,
        avg_volume=1000.0,
    )

    cog = PriceVolumeAlertMonitorCog(mock_bot)

    with patch(
        "cogs.trading.price_volume_alert_monitor.get_confirmed_15m_bar",
        new_callable=AsyncMock,
    ) as mock_get_bar, patch("database.is_notification_enabled", return_value=False):
        mock_get_bar.return_value = triggering_bar
        await cog._evaluate_price_volume_alerts()
        mock_bot.queue_dm.assert_not_called()

    await cog.cog_unload()


# ============================================================================
# 6. Notification toggle registration tests
# ============================================================================


def test_price_volume_watch_notification_key_registered() -> None:
    assert "alpha_price_volume_watch" in ALL_NOTIFICATION_KEYS
    assert PRESET_PROFILES["all_on"]["alpha_price_volume_watch"] is True
    assert PRESET_PROFILES["all_off"]["alpha_price_volume_watch"] is False
    assert PRESET_PROFILES["focus"]["alpha_price_volume_watch"] is False
    assert PRESET_PROFILES["mute_intraday"]["alpha_price_volume_watch"] is False
