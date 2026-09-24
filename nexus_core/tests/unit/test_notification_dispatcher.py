"""services/notification_dispatcher 與 database/notifications 設定快取的行為測試。"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import database
from database.notification_channels import (
    ALL_NOTIFICATION_KEYS,
    CHANNELS,
    PRESET_PROFILES,
    TRADING_MODULES,
)
from database.notifications import (
    get_notification_settings_many,
    get_user_notification_settings,
    is_notification_enabled,
    set_user_notification_setting,
)
from services.notification_dispatcher import notify, notify_many


def _bot() -> Any:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    return bot


# ---------------------------------------------------------------------------
# notify 的順序：開關 → 去重 → 入列 → 寫旗標
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_disabled_channel_does_nothing(db_conn: Any) -> None:
    """關閉的頻道不入列，且**不寫去重旗標**——使用者之後重新開啟時仍能收到當日事件。"""
    bot = _bot()
    set_user_notification_setting(7001, "alpha_wti_oil", False)
    sent = await notify(
        bot, 7001, "alpha_wti_oil", embed=MagicMock(), dedup_key="wti_alert_7001_x"
    )
    assert sent is False
    bot.queue_dm.assert_not_awaited()
    assert database.get_kv_cache("wti_alert_7001_x") is None

    set_user_notification_setting(7001, "alpha_wti_oil", True)
    assert await notify(
        bot, 7001, "alpha_wti_oil", embed=MagicMock(), dedup_key="wti_alert_7001_x"
    )
    bot.queue_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_dedup_sends_once(db_conn: Any) -> None:
    bot = _bot()
    embed = MagicMock()
    first = await notify(
        bot, 7002, "alpha_market_signals", embed=embed, dedup_key="ddp_alert_7002_A_1"
    )
    second = await notify(
        bot, 7002, "alpha_market_signals", embed=embed, dedup_key="ddp_alert_7002_A_1"
    )
    assert (first, second) == (True, False)
    bot.queue_dm.assert_awaited_once_with(7002, embed=embed)


@pytest.mark.asyncio
async def test_notify_flag_written_after_enqueue(db_conn: Any) -> None:
    """入列失敗時不得燒掉當日旗標。"""
    bot = _bot()
    bot.queue_dm = AsyncMock(side_effect=RuntimeError("queue down"))
    with pytest.raises(RuntimeError):
        await notify(
            bot, 7003, "alpha_wti_oil", embed=MagicMock(), dedup_key="wti_alert_7003_y"
        )
    assert database.get_kv_cache("wti_alert_7003_y") is None


@pytest.mark.asyncio
async def test_notify_passes_message_only_when_given(db_conn: Any) -> None:
    bot = _bot()
    await notify(bot, 7004, "system_lifecycle", message="hi")
    bot.queue_dm.assert_awaited_once_with(7004, message="hi", embed=None)


@pytest.mark.asyncio
async def test_notify_many_checks_once_and_sends_all(db_conn: Any) -> None:
    bot = _bot()
    embeds = [MagicMock(), MagicMock(), MagicMock()]
    with patch("database.is_notification_enabled", return_value=True) as gate:
        sent = await notify_many(bot, 7005, "heartbeat_watchlist", embeds)
    assert sent is True
    assert gate.call_count == 1
    assert bot.queue_dm.await_count == 3


@pytest.mark.asyncio
async def test_notify_many_empty_is_noop(db_conn: Any) -> None:
    bot = _bot()
    assert await notify_many(bot, 7006, "heartbeat_watchlist", []) is False
    bot.queue_dm.assert_not_awaited()


# ---------------------------------------------------------------------------
# 設定快取
# ---------------------------------------------------------------------------


def test_settings_cache_avoids_repeated_connections(db_conn: Any) -> None:
    get_user_notification_settings(7101)
    with patch("database.notifications.get_read_connection") as conn:
        for key in ALL_NOTIFICATION_KEYS:
            is_notification_enabled(7101, key)
    conn.assert_not_called()


def test_settings_cache_invalidated_on_write(db_conn: Any) -> None:
    assert is_notification_enabled(7102, "telemetry_orders") is True
    set_user_notification_setting(7102, "telemetry_orders", False)
    assert is_notification_enabled(7102, "telemetry_orders") is False


def test_get_notification_settings_many_single_query(db_conn: Any) -> None:
    set_user_notification_setting(7201, "alpha_polymarket", False)
    database.clear_notification_settings_cache()
    result = get_notification_settings_many([7201, 7202, 7201])
    assert set(result) == {7201, 7202}
    assert result[7201]["alpha_polymarket"] is False
    assert result[7202]["alpha_polymarket"] is True
    # 已預熱：之後的單一查詢不再開連線
    with patch("database.notifications.get_read_connection") as conn:
        assert is_notification_enabled(7201, "alpha_polymarket") is False
    conn.assert_not_called()


def test_legacy_alias_rows_resolved_through_cache(db_conn: Any) -> None:
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO user_notification_settings (user_id, notification_key, enabled) "
        "VALUES (7301, 'oil_alert', 0)"
    )
    db_conn.commit()
    assert is_notification_enabled(7301, "alpha_wti_oil") is False


# ---------------------------------------------------------------------------
# 註冊表衍生值
# ---------------------------------------------------------------------------


def test_registry_derivations_are_consistent() -> None:
    keys = [c.key for c in CHANNELS]
    assert ALL_NOTIFICATION_KEYS == keys
    ui_keys = [k for m in TRADING_MODULES.values() for k in m["items"]]
    assert sorted(ui_keys) == sorted(keys)
    for name, profile in PRESET_PROFILES.items():
        assert list(profile) == keys, name


def test_margin_call_never_muted_by_presets() -> None:
    """帳戶生存等級警訊：任何手寫預設情境都不得關閉。"""
    assert PRESET_PROFILES["focus"]["defense_margin_call"] is True
    assert PRESET_PROFILES["mute_intraday"]["defense_margin_call"] is True
