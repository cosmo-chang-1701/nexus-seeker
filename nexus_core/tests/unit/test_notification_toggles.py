from typing import Any
import pytest
from unittest.mock import AsyncMock
from database.notifications import (
    get_user_notification_settings,
    set_user_notification_setting,
    set_all_user_notification_settings,
    is_notification_enabled,
    ALL_NOTIFICATION_KEYS,
)


@pytest.fixture(autouse=True)
def clean_db(db_conn: Any):  # type: ignore
    """每個測試前清理 user_notification_settings"""
    cursor = db_conn.cursor()
    cursor.execute("DELETE FROM user_notification_settings")
    db_conn.commit()
    yield


def test_default_all_enabled(db_conn: Any):  # type: ignore
    """測試全新用戶通知預設值（預設全部開啟）"""
    user_id = 999111
    settings = get_user_notification_settings(user_id)
    assert len(settings) == len(ALL_NOTIFICATION_KEYS)

    for key in ALL_NOTIFICATION_KEYS:
        expected = True
        assert settings[key] is expected
        assert is_notification_enabled(user_id, key) is expected


def test_toggle_single_setting(db_conn: Any):  # type: ignore
    """測試單一通知項目的切換 (ON/OFF)"""
    user_id = 999111
    target_key = "hb_options_structure"

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


@pytest.mark.asyncio
async def test_notification_settings_view_structure(db_conn: Any):  # type: ignore
    """測試 NotificationSettingsView 結構與一鍵本區全部開啟/關閉的反應"""
    from cogs.terminal import NotificationSettingsView

    user_id = 999333

    view = NotificationSettingsView(user_id)
    # 預期包含 1 個 Select (Category), 1 個 Select (Toggles), 2 個 Button
    assert len(view.children) == 4

    category_select = next(
        c for c in view.children if getattr(c, "custom_id", None) == "select_category"
    )  # type: ignore
    module_select = next(
        c for c in view.children if getattr(c, "custom_id", None) == "select_toggles"
    )  # type: ignore

    # 分類大於 0 個
    assert len(category_select.options) > 0  # type: ignore

    # 預設模組有大於 0 個設定
    assert len(module_select.options) > 0  # type: ignore

    # 預期預設選項前綴為 🟢
    assert module_select.options[0].label.startswith("🟢")  # type: ignore

    # 模擬點擊「關閉本區所有設定」按鈕
    mock_interaction = AsyncMock()
    mock_interaction.user.id = user_id
    await view.on_disable_module(mock_interaction)

    # 驗證狀態皆關閉且 View 重新載入，下拉選單前綴變為 🔴
    module_select_new = next(
        c for c in view.children if getattr(c, "custom_id", None) == "select_toggles"
    )  # type: ignore
    assert module_select_new.options[0].label.startswith("🔴")  # type: ignore


@pytest.mark.asyncio
async def test_notification_settings_polymarket_toggle(db_conn: Any):  # type: ignore
    """測試在通知中心點選 🐳 巨鯨交易異動警報，是否能成功切換其通知狀態"""
    from cogs.terminal import NotificationSettingsView

    user_id = 999444
    view = NotificationSettingsView(user_id)

    # 1. 預設是開啟 (True)
    assert is_notification_enabled(user_id, "polymarket_whale_alert") is True

    # 先模擬選擇 polymarket 類別
    mock_interaction_cat = AsyncMock()
    mock_interaction_cat.data = {"values": ["polymarket"]}
    mock_interaction_cat.response.edit_message = AsyncMock()
    await view.on_category_select(mock_interaction_cat)

    # 2. 模擬選擇 "polymarket_whale_alert"
    mock_interaction = AsyncMock()
    mock_interaction.user.id = user_id
    mock_interaction.data = {"values": ["polymarket_whale_alert"]}
    mock_interaction.response.edit_message = AsyncMock()

    await view.on_select_callback(mock_interaction)

    # 驗證狀態已成功更新為 False
    assert is_notification_enabled(user_id, "polymarket_whale_alert") is False
    mock_interaction.response.edit_message.assert_called_once()


@pytest.mark.asyncio
async def test_notification_settings_polymarket_prob_shift_toggle(db_conn: Any):  # type: ignore
    """測試在通知中心點選 ⚡ Polymarket 預測機率閃崩 / 暴拉，是否能成功切換其通知狀態"""
    from cogs.terminal import NotificationSettingsView

    user_id = 999445
    view = NotificationSettingsView(user_id)

    # 1. 預設是開啟 (True)
    assert is_notification_enabled(user_id, "polymarket_prob_shift_alert") is True

    # 先模擬選擇 polymarket 類別
    mock_interaction_cat = AsyncMock()
    mock_interaction_cat.data = {"values": ["polymarket"]}
    mock_interaction_cat.response.edit_message = AsyncMock()
    await view.on_category_select(mock_interaction_cat)

    # 2. 模擬選擇 "polymarket_prob_shift_alert"
    mock_interaction = AsyncMock()
    mock_interaction.user.id = user_id
    mock_interaction.data = {"values": ["polymarket_prob_shift_alert"]}
    mock_interaction.response.edit_message = AsyncMock()

    await view.on_select_callback(mock_interaction)

    # 驗證狀態已成功更新為 False
    assert is_notification_enabled(user_id, "polymarket_prob_shift_alert") is False
    mock_interaction.response.edit_message.assert_called_once()


@pytest.mark.asyncio
async def test_notification_settings_polymarket_use_llm_toggle(db_conn: Any):  # type: ignore
    """測試在通知中心切換 Polymarket AI 分析 (Polymarket Settings)，是否能成功更新資料庫"""
    from cogs.terminal import NotificationSettingsView
    import database

    user_id = 999555
    view = NotificationSettingsView(user_id)

    # 1. 預設 enable 是 True
    ctx_init = database.get_full_user_context(user_id)
    assert ctx_init.polymarket_use_llm is True

    # 2. 模擬選擇 "polymarket_use_llm"
    mock_interaction = AsyncMock()
    mock_interaction.user.id = user_id
    mock_interaction.data = {"values": ["polymarket_use_llm"]}
    mock_interaction.response.edit_message = AsyncMock()

    await view.on_select_callback(mock_interaction)

    # 驗證資料庫已成功更新為 False
    ctx_after = database.get_full_user_context(user_id)
    assert ctx_after.polymarket_use_llm is False


@pytest.mark.asyncio
async def test_notification_settings_polymarket_modal_trigger(db_conn: Any):  # type: ignore
    """測試在通知中心選擇 Polymarket 監控門檻，是否會正確彈出專屬 Modal"""
    from cogs.terminal import NotificationSettingsView, NotificationSettingsModal

    user_id = 999666
    view = NotificationSettingsView(user_id)

    # 先模擬選擇 polymarket 類別
    mock_interaction_cat = AsyncMock()
    mock_interaction_cat.data = {"values": ["polymarket"]}
    mock_interaction_cat.response.edit_message = AsyncMock()
    await view.on_category_select(mock_interaction_cat)

    # 1. 模擬選擇 "polymarket_use_llm"
    mock_interaction = AsyncMock()
    mock_interaction.user.id = user_id
    mock_interaction.data = {"values": ["polymarket_threshold"]}
    mock_interaction.response.send_modal = AsyncMock()

    await view.on_select_callback(mock_interaction)

    # 驗證 send_modal 確實被呼叫，且傳入 NotificationSettingsModal
    mock_interaction.response.send_modal.assert_called_once()
    modal = mock_interaction.response.send_modal.call_args[0][0]
    assert isinstance(modal, NotificationSettingsModal)
    assert modal.key == "polymarket_threshold"


@pytest.mark.asyncio
async def test_notification_settings_modal_successful_submission(db_conn: Any):  # type: ignore
    """測試通知中心 Modal 正常提交更新時，資料庫更新與畫面渲染"""
    from cogs.terminal import NotificationSettingsView, NotificationSettingsModal
    import database

    user_id = 999777
    view = NotificationSettingsView(user_id)
    modal = NotificationSettingsModal(
        user_id=user_id,
        key="polymarket_threshold",
        label="🐋 巨鯨監控門檻",
        current_value=10000.0,
        placeholder="輸入大於等於 0 的數字",
        view=view,
    )

    # 模擬輸入新的門檻金額為 50000
    modal.input_field._value = "50000.0"
    mock_interaction = AsyncMock()
    mock_interaction.response.edit_message = AsyncMock()

    await modal.on_submit(mock_interaction)

    # 驗證資料庫已經寫入最新的數值
    ctx = database.get_full_user_context(user_id)
    assert ctx.polymarket_threshold == 50000.0

    # 驗證 edit_message 中有傳入包含新設定的 Embed
    mock_interaction.response.edit_message.assert_called_once()
    call_kwargs = mock_interaction.response.edit_message.call_args[1]
    embed_sent = call_kwargs["embed"]
    assert "$50,000" in embed_sent.fields[-1].value
