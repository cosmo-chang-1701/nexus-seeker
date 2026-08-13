from typing import Any
import pytest
from unittest.mock import AsyncMock
from database.notifications import (
    get_user_notification_settings,
    set_user_notification_setting,
    set_all_user_notification_settings,
    apply_preset_settings,
    is_notification_enabled,
    ALL_NOTIFICATION_KEYS,
    LEGACY_KEY_ALIASES,
)


@pytest.fixture(autouse=True)
def clean_db(db_conn: Any):  # type: ignore
    """每個測試前清理 user_notification_settings"""
    cursor = db_conn.cursor()
    cursor.execute("DELETE FROM user_notification_settings")
    db_conn.commit()
    yield


def test_default_all_enabled(db_conn: Any):  # type: ignore
    """測試全新用戶 10 大通知頻道預設值（預設全部開啟）"""
    user_id = 999111
    settings = get_user_notification_settings(user_id)
    assert len(settings) == len(ALL_NOTIFICATION_KEYS)
    assert len(ALL_NOTIFICATION_KEYS) == 10

    for key in ALL_NOTIFICATION_KEYS:
        expected = True
        assert settings[key] is expected
        assert is_notification_enabled(user_id, key) is expected


def test_toggle_single_setting(db_conn: Any):  # type: ignore
    """測試單一通知項目的切換 (ON/OFF)"""
    user_id = 999111
    target_key = "heartbeat_watchlist"

    # 1. 切換為關閉 (False)
    set_user_notification_setting(user_id, target_key, False)
    assert is_notification_enabled(user_id, target_key) is False

    settings = get_user_notification_settings(user_id)
    assert settings[target_key] is False
    # 其他未設定項目仍應維持預設值
    for key in ALL_NOTIFICATION_KEYS:
        if key == target_key:
            continue
        expected = True
        assert settings[key] is expected

    # 2. 切換回開啟 (True)
    set_user_notification_setting(user_id, target_key, True)
    assert is_notification_enabled(user_id, target_key) is True
    assert get_user_notification_settings(user_id)[target_key] is True


def test_legacy_key_aliases(db_conn: Any):  # type: ignore
    """測試舊版 Key 別名自動解析與相容性"""
    user_id = 999112

    # 透過舊 key 設定關閉
    set_user_notification_setting(user_id, "hb_options_structure", False)
    # 驗證新 key 與舊 key 查詢結果皆為 False
    assert is_notification_enabled(user_id, "heartbeat_watchlist") is False
    assert is_notification_enabled(user_id, "hb_options_structure") is False
    assert is_notification_enabled(user_id, "hb_execution_risk") is False

    # 透過舊 key 設定開啟
    set_user_notification_setting(user_id, "ddp_alert", True)
    assert is_notification_enabled(user_id, "alpha_market_signals") is True
    assert is_notification_enabled(user_id, "volatility_alert") is True

    # 驗證所有別名都有映射到新 key
    for old_k, new_k in LEGACY_KEY_ALIASES.items():
        assert new_k in ALL_NOTIFICATION_KEYS


def test_toggle_all_settings(db_conn: Any):  # type: ignore
    """測試一鍵全部開啟與一鍵全部關閉"""
    user_id = 999222

    # 1. 一鍵全部關閉
    set_all_user_notification_settings(user_id, False)
    settings = get_user_notification_settings(user_id)
    for key in ALL_NOTIFICATION_KEYS:
        assert settings[key] is False
        assert is_notification_enabled(user_id, key) is False

    # 2. 一鍵全部開啟
    set_all_user_notification_settings(user_id, True)
    settings = get_user_notification_settings(user_id)
    for key in ALL_NOTIFICATION_KEYS:
        assert settings[key] is True
        assert is_notification_enabled(user_id, key) is True


def test_apply_preset_settings(db_conn: Any):  # type: ignore
    """測試戰術預設情境模式 (all_on, all_off, focus, mute_intraday)"""
    user_id = 999333

    # 1. 精準交易 (focus)
    settings = apply_preset_settings(user_id, "focus")
    assert settings["briefing_pre_market"] is True
    assert settings["briefing_post_market"] is True
    assert settings["defense_portfolio_risk"] is True
    assert settings["heartbeat_watchlist"] is False
    assert settings["alpha_market_signals"] is False

    # 2. 盤中靜音 (mute_intraday)
    settings_mute = apply_preset_settings(user_id, "mute_intraday")
    assert settings_mute["briefing_pre_market"] is True
    assert settings_mute["heartbeat_watchlist"] is False
    assert settings_mute["telemetry_orders"] is False
    assert settings_mute["defense_portfolio_risk"] is True
    assert settings_mute["defense_option_rollover"] is False

    # 3. 戰備全開 (all_on)
    settings_all = apply_preset_settings(user_id, "all_on")
    for key in ALL_NOTIFICATION_KEYS:
        assert settings_all[key] is True


@pytest.mark.asyncio
async def test_notification_settings_view_structure(db_conn: Any):  # type: ignore
    """測試 NotificationSettingsView 4 大模組結構與一鍵本區全部開啟/關閉的反應"""
    from cogs.settings_ui import NotificationSettingsView

    user_id = 999444

    view = NotificationSettingsView(user_id)
    # 預期包含 1 個 Select (Category), 1 個 Select (Toggles), 2 個本區按鈕, 3 個 Preset 按鈕
    assert len(view.children) == 7

    category_select = next(
        c for c in view.children if getattr(c, "custom_id", None) == "select_category"
    )  # type: ignore
    module_select = next(
        c for c in view.children if getattr(c, "custom_id", None) == "select_toggles"
    )  # type: ignore

    # 4 大分類
    assert len(category_select.options) == 4  # type: ignore

    # 預設模組為 briefings (3 項)
    assert len(module_select.options) == 3  # type: ignore
    assert module_select.options[0].label.startswith("🟢")  # type: ignore

    # 模擬點擊「關閉本區所有設定」按鈕
    mock_interaction = AsyncMock()
    mock_interaction.user.id = user_id
    mock_interaction.response.edit_message = AsyncMock()
    await view.on_disable_module(mock_interaction)

    # 驗證狀態皆關閉且 View 重新載入，下拉選單前綴變為 🔴
    module_select_new = next(
        c for c in view.children if getattr(c, "custom_id", None) == "select_toggles"
    )  # type: ignore
    assert module_select_new.options[0].label.startswith("🔴")  # type: ignore


@pytest.mark.asyncio
async def test_notification_settings_view_preset_buttons(db_conn: Any):  # type: ignore
    """測試 NotificationSettingsView 點擊 Preset 按鈕之反應"""
    from cogs.settings_ui import NotificationSettingsView

    user_id = 999555
    view = NotificationSettingsView(user_id)

    mock_interaction = AsyncMock()
    mock_interaction.user.id = user_id
    mock_interaction.response.edit_message = AsyncMock()

    # 點擊「🎯 精準交易」
    await view.on_preset_focus(mock_interaction)
    assert is_notification_enabled(user_id, "heartbeat_watchlist") is False
    assert is_notification_enabled(user_id, "defense_portfolio_risk") is True

    # 點擊「🔕 盤中靜音」
    await view.on_preset_mute_intraday(mock_interaction)
    assert is_notification_enabled(user_id, "telemetry_orders") is False

    # 點擊「🛡️ 戰備全開」
    await view.on_preset_all_on(mock_interaction)
    assert is_notification_enabled(user_id, "heartbeat_watchlist") is True
    assert is_notification_enabled(user_id, "alpha_market_signals") is True


@pytest.mark.asyncio
async def test_account_settings_polymarket_configuration(db_conn: Any):  # type: ignore
    """測試在帳戶全域設定 (/settings) 中修改 Polymarket 門檻與 AI 分析開關"""
    from cogs.settings_ui import AccountSettingsView, AccountSettingsModal
    import database

    user_id = 999666
    view = AccountSettingsView(user_id)

    # 1. 測試切換 polymarket_use_llm
    mock_interaction_toggle = AsyncMock()
    mock_interaction_toggle.user.id = user_id
    mock_interaction_toggle.data = {"values": ["polymarket_use_llm"]}
    mock_interaction_toggle.response.edit_message = AsyncMock()

    await view.on_select_callback(mock_interaction_toggle)
    ctx = database.get_full_user_context(user_id)
    assert ctx.polymarket_use_llm is False

    # 2. 測試透過 Modal 修改 polymarket_threshold
    modal = AccountSettingsModal(
        user_id=user_id,
        key="polymarket_threshold",
        label="🐋 Polymarket 巨鯨門檻",
        current_value=10000.0,
        placeholder="輸入大於等於 0 的數字",
        view=view,
    )
    modal.input_field._value = "25000.0"

    mock_interaction_modal = AsyncMock()
    mock_interaction_modal.response.edit_message = AsyncMock()
    await modal.on_submit(mock_interaction_modal)

    ctx_after = database.get_full_user_context(user_id)
    assert ctx_after.polymarket_threshold == 25000.0
