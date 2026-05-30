import pytest
import discord
from unittest.mock import AsyncMock
import database
from cogs.terminal import AccountSettingsView, AccountSettingsModal


@pytest.fixture(autouse=True)
def clean_db(db_conn):
    """每個測試前清理 user_settings，並置入預設資料以防萬一"""
    cursor = db_conn.cursor()
    cursor.execute("DELETE FROM user_settings")
    db_conn.commit()
    yield


@pytest.mark.asyncio
async def test_settings_view_structure(db_conn):
    """測試 AccountSettingsView 的基礎結構與下拉選單項目"""
    user_id = 12345
    view = AccountSettingsView(user_id)

    # 驗證只有 1 個下拉選單
    assert len(view.children) == 1
    select = view.children[0]
    assert isinstance(select, discord.ui.Select)
    assert select.custom_id == "select_account_settings"

    # 驗證包含 7 個設定選項
    assert len(select.options) == 7
    labels = [opt.label for opt in select.options]
    assert "💰 總資金" in labels
    assert "🛡️ 基準風險上限 %" in labels
    assert "👻 虛擬交易室 (VTR)" in labels


@pytest.mark.asyncio
async def test_settings_toggle_boolean(db_conn):
    """測試在下拉選單選中布林值設定時，是否能直接切換狀態並儲存至資料庫"""
    user_id = 555666
    view = AccountSettingsView(user_id)

    # 1. 預設 enable_vtr 是 True
    ctx_init = database.get_full_user_context(user_id)
    assert ctx_init.enable_vtr is True

    # 2. 模擬選擇 "enable_vtr"
    mock_interaction = AsyncMock()
    mock_interaction.user.id = user_id
    mock_interaction.data = {"values": ["enable_vtr"]}

    # 模擬 edit_message 回應
    mock_interaction.response.edit_message = AsyncMock()

    await view.on_select_callback(mock_interaction)

    # 驗證資料庫已成功更新為 False
    ctx_after = database.get_full_user_context(user_id)
    assert ctx_after.enable_vtr is False

    # 驗證有呼叫 edit_message 更新介面
    mock_interaction.response.edit_message.assert_called_once()

    # 再次點擊切換回 True
    await view.on_select_callback(mock_interaction)
    ctx_back = database.get_full_user_context(user_id)
    assert ctx_back.enable_vtr is True


@pytest.mark.asyncio
async def test_settings_numeric_modal_trigger(db_conn):
    """測試選擇數值類型參數時，是否會正確彈出 Modal 視窗"""
    user_id = 777888
    view = AccountSettingsView(user_id)

    # 模擬選擇 "capital" (總資金)
    mock_interaction = AsyncMock()
    mock_interaction.user.id = user_id
    mock_interaction.data = {"values": ["capital"]}
    mock_interaction.response.send_modal = AsyncMock()

    await view.on_select_callback(mock_interaction)

    # 驗證確實呼叫了 send_modal 並且傳入的是 AccountSettingsModal 物件
    mock_interaction.response.send_modal.assert_called_once()
    modal = mock_interaction.response.send_modal.call_args[0][0]
    assert isinstance(modal, AccountSettingsModal)
    assert modal.key == "capital"
    assert modal.label == "💰 總資金"


@pytest.mark.asyncio
async def test_settings_modal_validation_capital(db_conn):
    """測試 Modal 針對 capital (總資金) 的輸入邊界驗證"""
    user_id = 999000
    view = AccountSettingsView(user_id)

    # 建立 Modal 實例
    modal = AccountSettingsModal(
        user_id=user_id,
        key="capital",
        label="💰 總資金",
        current_value=100000.0,
        placeholder="輸入大於 0 的數字",
        view=view,
    )

    # 1. 測試輸入非數字 (ValueError)
    modal.input_field._value = "abc"
    mock_interaction = AsyncMock()
    mock_interaction.response.send_message = AsyncMock()
    await modal.on_submit(mock_interaction)
    mock_interaction.response.send_message.assert_called_once()
    assert (
        "無效"
        in mock_interaction.response.send_message.call_args[1]["embed"].description
    )

    # 2. 測試輸入小於等於 0
    modal.input_field._value = "-100"
    mock_interaction = AsyncMock()
    mock_interaction.response.send_message = AsyncMock()
    await modal.on_submit(mock_interaction)
    mock_interaction.response.send_message.assert_called_once()
    assert (
        "必須大於 0"
        in mock_interaction.response.send_message.call_args[1]["embed"].description
    )


@pytest.mark.asyncio
async def test_settings_modal_validation_risk_limit(db_conn):
    """測試 Modal 針對 risk_limit (風險上限) 的 1% ~ 50% 範圍驗證"""
    user_id = 999001
    view = AccountSettingsView(user_id)
    modal = AccountSettingsModal(
        user_id=user_id,
        key="risk_limit",
        label="🛡️ 基準風險上限 %",
        current_value=15.0,
        placeholder="輸入 1.0 - 50.0 之間的數值",
        view=view,
    )

    # 測試輸入超出上限 (e.g., 60%)
    modal.input_field._value = "60.0"
    mock_interaction = AsyncMock()
    mock_interaction.response.send_message = AsyncMock()
    await modal.on_submit(mock_interaction)
    mock_interaction.response.send_message.assert_called_once()
    assert (
        "1.0% 至 50.0%"
        in mock_interaction.response.send_message.call_args[1]["embed"].description
    )


@pytest.mark.asyncio
async def test_settings_modal_validation_tax_reserve_rate(db_conn):
    """測試 Modal 針對 tax_reserve_rate (稅務比例) 的輸入百分比支援與驗證"""
    user_id = 999002
    view = AccountSettingsView(user_id)
    modal = AccountSettingsModal(
        user_id=user_id,
        key="tax_reserve_rate",
        label="🏦 稅務預留比例",
        current_value=0.20,
        placeholder="輸入 0.0 - 1.0",
        view=view,
    )

    # 1. 測試輸入直接數值超過 100% 的錯誤
    modal.input_field._value = "150.0"
    mock_interaction = AsyncMock()
    mock_interaction.response.send_message = AsyncMock()
    await modal.on_submit(mock_interaction)
    mock_interaction.response.send_message.assert_called_once()
    assert (
        "稅務比例"
        in mock_interaction.response.send_message.call_args[1]["embed"].description
    )

    # 2. 測試輸入百分比式數值 (例如 35.0 代表 35%)
    modal.input_field._value = "35.0"
    mock_interaction = AsyncMock()
    mock_interaction.response.edit_message = AsyncMock()
    await modal.on_submit(mock_interaction)

    # 驗證資料庫是否自動將 35.0 轉換成 0.35 儲存
    ctx = database.get_full_user_context(user_id)
    assert abs(ctx.tax_reserve_rate - 0.35) < 1e-6
    mock_interaction.response.edit_message.assert_called_once()


@pytest.mark.asyncio
async def test_settings_modal_successful_submission(db_conn):
    """測試 Modal 正常送出時，更新資料庫並渲染重新更新的 settings view / embed"""
    user_id = 999003
    view = AccountSettingsView(user_id)
    modal = AccountSettingsModal(
        user_id=user_id,
        key="capital",
        label="💰 總資金",
        current_value=100000.0,
        placeholder="輸入大於 0 的數字",
        view=view,
    )

    modal.input_field._value = "250000.00"
    mock_interaction = AsyncMock()
    mock_interaction.response.edit_message = AsyncMock()

    await modal.on_submit(mock_interaction)

    # 驗證資料庫已成功儲存 250000.0
    ctx = database.get_full_user_context(user_id)
    assert ctx.capital == 250000.0

    # 驗證 edit_message 中有傳入更新後的 embed，其內容包含新設定的總資金
    mock_interaction.response.edit_message.assert_called_once()
    call_kwargs = mock_interaction.response.edit_message.call_args[1]
    embed_sent = call_kwargs["embed"]
    assert "$250,000.00" in embed_sent.fields[0].value
