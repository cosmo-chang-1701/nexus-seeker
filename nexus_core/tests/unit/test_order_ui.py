import pytest
import sys
import os
from unittest.mock import AsyncMock, MagicMock

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from database.orders import (
    add_active_order,
    get_user_active_orders,
    delete_active_order,
    update_active_order_price,
)
from services.telemetry_pricing_engine import calculate_telemetry_price
from cogs.order_ui import (
    DynamicOrderModal,
    OrderSetupView,
    OrderUICog,
    CancelOrderModal,
    EditOrderModal,
    OrderManagementView,
    OrderItemView,
    ApplyTelemetryView,
)


@pytest.mark.asyncio
async def test_active_orders_db_operations(db_conn):
    """測試待成交委託單資料庫 CRUD 運作"""
    user_id = 999999
    symbol = "TSLA"
    quantity = 50.5
    order_type = "LIMIT"
    validity = "GTC_90"
    limit_price = 185.5
    stop_price = 0.0
    trailing_value = 0.0

    # 1. 新增訂單
    order_id = add_active_order(
        user_id=user_id,
        symbol=symbol,
        quantity=quantity,
        order_type=order_type,
        validity=validity,
        limit_price=limit_price,
        stop_price=stop_price,
        trailing_value=trailing_value,
    )
    assert order_id > 0

    # 2. 查詢該用戶訂單
    user_orders = get_user_active_orders(user_id)
    assert len(user_orders) == 1
    assert user_orders[0]["symbol"] == "TSLA"
    assert user_orders[0]["quantity"] == 50.5
    assert user_orders[0]["limit_price"] == 185.5

    # 3. 測試價格微調 (update_active_order_price)
    updated = update_active_order_price(order_id, 180.25)
    assert updated is True

    user_orders_updated = get_user_active_orders(user_id)
    assert user_orders_updated[0]["limit_price"] == 180.25

    # 4. 刪除訂單
    deleted = delete_active_order(order_id)
    assert deleted is True

    user_orders_after = get_user_active_orders(user_id)
    assert len(user_orders_after) == 0


@pytest.mark.asyncio
async def test_calculate_telemetry_pricing_engine():
    """測試「捕獸夾」遙測訂價引擎的三大維度決策樹算法"""

    # 維度一：最大痛點上移
    price_pain, qty_pain, logs_pain = await calculate_telemetry_price(
        symbol="AAPL",
        base_price=100.0,
        spot_price=100.0,
        iv=0.3,
        hist_iv=0.3,
        max_pain=120.0,  # 上移
        prev_max_pain=100.0,  # 原有
        skew_percentile=0.5,
    )
    assert price_pain == 120.0  # 100 * (120/100)
    assert qty_pain == 1.0
    assert any("最大痛點位移" in log for log in logs_pain)

    # 維度二：IV 暴噴工作流 (Pull back 3%)
    price_spike, qty_spike, logs_spike = await calculate_telemetry_price(
        symbol="AAPL",
        base_price=100.0,
        spot_price=100.0,
        iv=0.6,  # 高 IV
        hist_iv=0.3,  # 低歷史 IV
        max_pain=100.0,
        prev_max_pain=100.0,
        skew_percentile=0.5,
    )
    assert price_spike == 97.0  # 100 * 0.97
    assert qty_spike == 1.0
    assert any("IV 暴噴警報" in log for log in logs_spike)

    # 維度三：整數心理鐵壁防禦 ($100.5 -> $99.25)
    price_round, qty_round, logs_round = await calculate_telemetry_price(
        symbol="AAPL",
        base_price=100.5,  # 接近 100.0 的整數大關
        spot_price=100.5,
        iv=0.3,
        hist_iv=0.3,
        max_pain=100.0,
        prev_max_pain=100.0,
        skew_percentile=0.5,
    )
    assert price_round == 99.25  # 100 - 0.75
    assert qty_round == 1.0
    assert any("整數心理大關防禦" in log for log in logs_round)

    # 新增測試：極端 Skew 尾部風險與倉位調整聯動 (Dimension 1 Option Flow & Gravity Linkage)
    price_skew, qty_skew, logs_skew = await calculate_telemetry_price(
        symbol="AAPL",
        base_price=120.0,
        spot_price=120.0,
        iv=0.3,
        hist_iv=0.3,
        max_pain=120.0,
        prev_max_pain=120.0,
        skew_percentile=0.98,  # 觸發極端偏斜
        base_quantity=100.0,
    )
    # spot_price (120.0) 偏近 1.5% -> 因為 spot_price <= price (120.0 == 120.0), 所以 price = 120.0 * 1.015 = 121.8
    assert price_skew == 121.8
    assert qty_skew == 75.0  # 100.0 * 0.75
    assert any("偵測到期權偏斜極端尾端風險" in log for log in logs_skew)


@pytest.mark.asyncio
async def test_dynamic_order_modal_on_submit_limit_success(mock_interaction, db_conn):
    """測試限價訂單 Modal 成功送出與資料庫寫入"""
    modal = DynamicOrderModal(
        order_type="LIMIT", title="新增限價訂單", validity_db="DAY"
    )

    # 填充欄位資料
    modal.ticker._value = "NET "
    modal.quantity._value = " 100 "
    modal.limit_price._value = " 85.5 "

    await modal.on_submit(mock_interaction)

    # 驗證成功回傳 embed
    assert mock_interaction.response.send_message.called
    embed = mock_interaction.response.send_message.call_args[1]["embed"]
    assert "訂單登錄成功" in embed.title
    assert "NET" in embed.description
    assert "限價單" in embed.description
    assert "85.5" in embed.description

    # 驗證寫入資料庫
    orders = get_user_active_orders(mock_interaction.user.id)
    assert len(orders) == 1
    assert orders[0]["symbol"] == "NET"
    assert orders[0]["quantity"] == 100
    assert orders[0]["limit_price"] == 85.5
    assert orders[0]["order_type"] == "LIMIT"
    assert orders[0]["validity"] == "DAY"


@pytest.mark.asyncio
async def test_dynamic_order_modal_validation_failure_quantity(mock_interaction):
    """測試數量欄位輸入非數字的驗證失敗"""
    modal = DynamicOrderModal(
        order_type="LIMIT", title="新增限價訂單", validity_db="DAY"
    )

    modal.ticker._value = "AAPL"
    modal.quantity._value = "abc"  # 錯誤字串
    modal.limit_price._value = "150.0"

    await modal.on_submit(mock_interaction)

    # 驗證回傳錯誤訊息
    assert mock_interaction.response.send_message.called
    embed = mock_interaction.response.send_message.call_args[1]["embed"]
    assert "系統錯誤" in embed.title
    assert "請輸入有效的正數" in embed.description


@pytest.mark.asyncio
async def test_dynamic_order_modal_validation_failure_price(mock_interaction):
    """測試限價價格輸入非數字的驗證失敗"""
    modal = DynamicOrderModal(
        order_type="LIMIT", title="新增限價訂單", validity_db="DAY"
    )

    modal.ticker._value = "AAPL"
    modal.quantity._value = "10"
    modal.limit_price._value = "xyz"  # 錯誤字串

    await modal.on_submit(mock_interaction)

    # 驗證回傳錯誤限價提示
    assert mock_interaction.response.send_message.called
    embed = mock_interaction.response.send_message.call_args[1]["embed"]
    assert "系統錯誤" in embed.title
    assert "請輸入有效的限價" in embed.description


@pytest.mark.asyncio
async def test_dynamic_order_modal_on_submit_trailing_pct_success(
    mock_interaction, db_conn
):
    """測試百分比追蹤停損單的成功提交"""
    modal = DynamicOrderModal(
        order_type="TRAILING_STOP_PCT",
        title="新增百分比追蹤停損單",
        validity_db="NIGHT",
    )

    modal.ticker._value = "HOOD"
    modal.quantity._value = "50"
    modal.trailing_value._value = "8.5"

    await modal.on_submit(mock_interaction)

    # 驗證成功回傳 embed
    assert mock_interaction.response.send_message.called
    embed = mock_interaction.response.send_message.call_args[1]["embed"]
    assert "訂單登錄成功" in embed.title
    assert "HOOD" in embed.description
    assert "追蹤停損單 (%)" in embed.description
    assert "8.50%" in embed.description

    # 驗證資料庫內容
    orders = get_user_active_orders(mock_interaction.user.id)
    assert len(orders) == 1
    assert orders[0]["symbol"] == "HOOD"
    assert orders[0]["quantity"] == 50
    assert orders[0]["trailing_value"] == 8.5
    assert orders[0]["order_type"] == "TRAILING_STOP_PCT"
    assert orders[0]["validity"] == "NIGHT"


@pytest.mark.asyncio
async def test_cancel_order_modal_success(mock_interaction, db_conn):
    """測試取消委託單表單送出"""
    user_id = mock_interaction.user.id
    order_id = add_active_order(
        user_id=user_id,
        symbol="AAPL",
        quantity=10,
        order_type="LIMIT",
        validity="DAY",
        limit_price=150.0,
    )

    modal = CancelOrderModal()
    modal.order_id._value = str(order_id)

    await modal.on_submit(mock_interaction)

    assert mock_interaction.response.defer.called
    assert mock_interaction.followup.send.called
    embed = mock_interaction.followup.send.call_args[1]["embed"]
    assert "取消委託成功" in embed.title

    # 驗證資料庫已被刪除
    orders = get_user_active_orders(user_id)
    assert len(orders) == 0


@pytest.mark.asyncio
async def test_edit_order_modal_success(mock_interaction, db_conn):
    """測試編輯委託單表單送出 (更新價格 + 方向)"""
    user_id = mock_interaction.user.id
    order_id = add_active_order(
        user_id=user_id,
        symbol="AAPL",
        quantity=10,
        order_type="LIMIT",
        validity="DAY",
        limit_price=150.0,
    )

    modal = EditOrderModal()
    modal.order_id._value = str(order_id)
    modal.new_side._value = "SELL"
    modal.new_price._value = "145.50"

    await modal.on_submit(mock_interaction)

    assert mock_interaction.response.defer.called
    assert mock_interaction.followup.send.called
    embed = mock_interaction.followup.send.call_args[1]["embed"]
    assert "編輯委託單成功" in embed.title

    # 驗證資料庫價格/方向更新
    orders = get_user_active_orders(user_id)
    assert len(orders) == 1
    assert orders[0]["limit_price"] == 145.50
    assert orders[0]["side"] == "SELL"


@pytest.mark.asyncio
async def test_edit_order_modal_side_only_success(mock_interaction, db_conn):
    """測試編輯委託單表單送出 (僅更新方向，價格留空)"""
    user_id = mock_interaction.user.id
    order_id = add_active_order(
        user_id=user_id,
        symbol="AAPL",
        quantity=10,
        order_type="LIMIT",
        validity="DAY",
        limit_price=150.0,
    )

    modal = EditOrderModal()
    modal.order_id._value = str(order_id)
    modal.new_side._value = "SELL"
    modal.new_price._value = ""  # 留空：不變更價格

    await modal.on_submit(mock_interaction)

    assert mock_interaction.response.defer.called
    assert mock_interaction.followup.send.called
    embed = mock_interaction.followup.send.call_args[1]["embed"]
    assert "編輯委託單成功" in embed.title

    orders = get_user_active_orders(user_id)
    assert len(orders) == 1
    assert orders[0]["limit_price"] == 150.0
    assert orders[0]["side"] == "SELL"


@pytest.mark.asyncio
async def test_order_panel_command(mock_interaction):
    """測試喚起訂單設定面板命令"""
    bot = MagicMock()
    cog = OrderUICog(bot)

    await cog.order_panel.callback(cog, mock_interaction)

    assert mock_interaction.response.defer.called
    assert mock_interaction.followup.send.called
    kwargs = mock_interaction.followup.send.call_args[1]
    embed = kwargs["embed"]
    view = kwargs["view"]

    assert "交易委託單設定面版" in embed.title
    assert isinstance(view, OrderSetupView)


@pytest.mark.asyncio
async def test_orders_list_command(mock_interaction, db_conn):
    """測試查詢待成交委託單清單命令（預設：全部）"""
    user_id = mock_interaction.user.id
    add_active_order(
        user_id=user_id,
        symbol="TSLA",
        quantity=10,
        order_type="LIMIT",
        validity="DAY",
        limit_price=180.0,
    )

    bot = MagicMock()
    cog = OrderUICog(bot)
    # 不傳任何下拉參數時，預設視為全部
    await cog.list_orders.callback(cog, mock_interaction)

    assert mock_interaction.response.defer.called
    assert mock_interaction.followup.send.called

    # 清單式：單一訊息整合多筆（此案例只有 1 筆，因此只會送出 1 則清單訊息）
    assert mock_interaction.followup.send.call_count == 1

    kwargs = mock_interaction.followup.send.call_args_list[0].kwargs
    embed = kwargs["embed"]
    view = kwargs["view"]

    assert "待成交委託單列表" in embed.title
    assert "共" in (embed.description or "")
    assert "撤銷" in (embed.description or "")
    assert "微調" in (embed.description or "")

    assert any("TSLA" in f.name or "TSLA" in f.value for f in embed.fields)
    assert isinstance(view, OrderManagementView)


@pytest.mark.asyncio
async def test_orders_list_command_filters(mock_interaction, db_conn):
    """測試 /list_orders 下拉篩選：方向與條件"""
    user_id = mock_interaction.user.id

    add_active_order(
        user_id=user_id,
        symbol="AAPL",
        quantity=10,
        order_type="LIMIT",
        validity="DAY",
        side="BUY",
        limit_price=180.0,
    )
    add_active_order(
        user_id=user_id,
        symbol="TSLA",
        quantity=5,
        order_type="STOP",
        validity="NIGHT",
        side="SELL",
        stop_price=150.0,
    )

    bot = MagicMock()
    cog = OrderUICog(bot)

    # 篩選：只看 SELL
    await cog.list_orders.callback(cog, mock_interaction, side="SELL")

    assert mock_interaction.followup.send.called
    kwargs = mock_interaction.followup.send.call_args_list[-1].kwargs
    embed = kwargs["embed"]

    # 應只看到 TSLA (SELL)
    joined = (
        (embed.description or "")
        + "\n"
        + "\n".join([f.name + "\n" + str(f.value) for f in embed.fields])
    )
    assert "TSLA" in joined
    assert "AAPL" not in joined

    mock_interaction.followup.send.reset_mock()

    # 篩選：含停損 (HAS_STOP) + SELL
    await cog.list_orders.callback(
        cog,
        mock_interaction,
        side="SELL",
        condition="HAS_STOP",
    )

    kwargs2 = mock_interaction.followup.send.call_args_list[-1].kwargs
    embed2 = kwargs2["embed"]
    joined2 = (
        (embed2.description or "")
        + "\n"
        + "\n".join([f.name + "\n" + str(f.value) for f in embed2.fields])
    )
    assert "TSLA" in joined2
    assert "AAPL" not in joined2


@pytest.mark.asyncio
async def test_telemetry_alert_and_alignment(mock_interaction, db_conn):
    """測試半小時遙測偏離警報以及一鍵價格對齊功能"""
    user_id = mock_interaction.user.id
    add_active_order(
        user_id=user_id,
        symbol="AAPL",
        quantity=20,
        order_type="LIMIT",
        validity="DAY",
        limit_price=100.0,
    )

    bot = MagicMock()
    cog = OrderUICog(bot)

    # 1. 觸發警報面板
    await cog.telemetry_alert.callback(cog, mock_interaction)
    assert mock_interaction.response.defer.called
    assert mock_interaction.followup.send.called
    kwargs = mock_interaction.followup.send.call_args[1]
    embed = kwargs["embed"]
    view = kwargs["view"]
    assert "對齊警報" in embed.title
    assert isinstance(view, ApplyTelemetryView)

    # 2. 一鍵套用遙測建議價
    mock_btn_interaction = AsyncMock()
    mock_btn_interaction.user.id = user_id
    mock_btn_interaction.response = AsyncMock()
    mock_btn_interaction.followup = AsyncMock()

    await view.apply_telemetry_button.callback(mock_btn_interaction)
    assert mock_btn_interaction.response.defer.called
    assert mock_btn_interaction.followup.send.called

    # 驗證資料庫已被調整：價格調整為 97.46，數量調降至 75% 即 15.0 股
    orders = get_user_active_orders(user_id)
    assert len(orders) == 1
    assert orders[0]["limit_price"] == 97.46
    assert orders[0]["quantity"] == 15.0


@pytest.mark.asyncio
async def test_order_management_view_buttons(mock_interaction):
    """測試訂單管理面板的按鈕交互 (取消與編輯委託單)"""
    view = OrderManagementView()

    # 測試點擊取消委託按鈕
    await view.cancel_button.callback(mock_interaction)
    assert mock_interaction.response.send_modal.called
    modal_cancel = mock_interaction.response.send_modal.call_args[0][0]
    assert isinstance(modal_cancel, CancelOrderModal)

    # 重設 mock
    mock_interaction.response.send_modal.reset_mock()

    # 測試點擊編輯委託單按鈕
    await view.adjust_button.callback(mock_interaction)
    assert mock_interaction.response.send_modal.called
    modal_adjust = mock_interaction.response.send_modal.call_args[0][0]
    assert isinstance(modal_adjust, EditOrderModal)


@pytest.mark.asyncio
async def test_order_item_view_buttons_prefill_order_id(mock_interaction):
    """測試單筆委託單卡片按鈕會自動預填 order_id 到 Modal"""
    view = OrderItemView(order_id=99)

    await view.cancel_order_button.callback(mock_interaction)
    modal_cancel = mock_interaction.response.send_modal.call_args[0][0]
    assert isinstance(modal_cancel, CancelOrderModal)
    assert modal_cancel.order_id.default == "99"

    mock_interaction.response.send_modal.reset_mock()

    await view.edit_order_button.callback(mock_interaction)
    modal_edit = mock_interaction.response.send_modal.call_args[0][0]
    assert isinstance(modal_edit, EditOrderModal)
    assert modal_edit.order_id.default == "99"


@pytest.mark.asyncio
async def test_order_setup_view_shortcut_buttons(mock_interaction):
    """測試 OrderSetupView 中的限價、停損與市價單快捷按鈕是否正常觸發 Modal"""
    view = OrderSetupView()

    # 1. 限價單快捷
    mock_interaction.response.send_modal.reset_mock()
    await view.limit_shortcut.callback(mock_interaction)
    assert mock_interaction.response.send_modal.called
    modal = mock_interaction.response.send_modal.call_args[0][0]
    assert isinstance(modal, DynamicOrderModal)
    assert modal.order_type == "LIMIT"

    # 2. 停損單快捷
    mock_interaction.response.send_modal.reset_mock()
    await view.stop_shortcut.callback(mock_interaction)
    assert mock_interaction.response.send_modal.called
    modal = mock_interaction.response.send_modal.call_args[0][0]
    assert isinstance(modal, DynamicOrderModal)
    assert modal.order_type == "STOP"

    # 3. 市價單快捷
    mock_interaction.response.send_modal.reset_mock()
    await view.market_shortcut.callback(mock_interaction)
    assert mock_interaction.response.send_modal.called
    modal = mock_interaction.response.send_modal.call_args[0][0]
    assert isinstance(modal, DynamicOrderModal)
    assert modal.order_type == "MARKET"


@pytest.mark.asyncio
async def test_dynamic_order_modal_telemetry_fallback(
    mock_interaction, db_conn, monkeypatch
):
    """測試當限價留空/為 0 時，Modal 能否自動套用遙測定價 fallback"""
    modal = DynamicOrderModal(
        order_type="LIMIT", title="新增限價訂單", validity_db="DAY"
    )

    modal.ticker._value = "AAPL"
    modal.quantity._value = "10"
    modal.limit_price._value = ""  # 留空以觸發遙測

    # 模擬外部 API 調用，防範測試無網路
    async def mock_get_quote(symbol):
        return {"c": 150.0, "pc": 148.0}

    async def mock_get_history_df(symbol, period):
        import pandas as pd

        return pd.DataFrame({"Close": [150.0]})

    # Mock SentimentEngine & calculate_telemetry_price
    async def mock_fetch_iv(symbol):
        class MockIV:
            current_iv = 0.3
            iv_rank = 30.0

        return MockIV()

    async def mock_calculate_skew(symbol):
        return {"skew": 1.0}

    async def mock_telemetry(
        symbol,
        base_price,
        spot_price,
        iv,
        hist_iv,
        max_pain,
        prev_max_pain,
        skew_percentile,
        prev_close,
        base_quantity,
    ):
        return 145.0, base_quantity, ["Mocked Telemetry Price Resolved"]

    monkeypatch.setattr("services.market_data_service.get_quote", mock_get_quote)
    monkeypatch.setattr(
        "services.market_data_service.get_history_df", mock_get_history_df
    )
    monkeypatch.setattr(
        "market_analysis.sentiment_engine.SentimentEngine.fetch_and_calculate_iv_metrics",
        mock_fetch_iv,
    )
    monkeypatch.setattr(
        "market_analysis.sentiment_engine.SentimentEngine.calculate_skew",
        mock_calculate_skew,
    )
    monkeypatch.setattr(
        "services.telemetry_pricing_engine.calculate_telemetry_price", mock_telemetry
    )

    await modal.on_submit(mock_interaction)

    assert mock_interaction.response.defer.called
    assert mock_interaction.followup.send.called
    embed = mock_interaction.followup.send.call_args[1]["embed"]
    assert "訂單登錄成功" in embed.title
    assert "145.0" in embed.description
    assert "自動套用遙測最佳防線價" in embed.description

    # 驗證寫入資料庫
    orders = [
        o
        for o in get_user_active_orders(mock_interaction.user.id)
        if o["symbol"] == "AAPL"
    ]
    assert len(orders) == 1
    assert orders[0]["limit_price"] == 145.0


@pytest.mark.asyncio
async def test_add_order_slash_command_manual(mock_interaction, db_conn):
    """測試直接下單 Slash Command，帶手動價格"""
    bot = MagicMock()
    cog = OrderUICog(bot)

    # 測試手動帶價格的限價單
    await cog.add_order.callback(
        cog,
        mock_interaction,
        symbol="TSLA",
        quantity=5,
        order_type="LIMIT",
        validity="DAY",
        price=200.0,
    )

    assert mock_interaction.response.defer.called
    assert mock_interaction.followup.send.called
    embed = mock_interaction.followup.send.call_args[1]["embed"]
    assert "訂單登錄成功" in embed.title
    assert "TSLA" in embed.description
    assert "200.0" in embed.description

    # 驗證資料庫寫入
    orders = [
        o
        for o in get_user_active_orders(mock_interaction.user.id)
        if o["symbol"] == "TSLA"
    ]
    assert len(orders) == 1
    assert orders[0]["limit_price"] == 200.0


@pytest.mark.asyncio
async def test_add_order_slash_command_telemetry(
    mock_interaction, db_conn, monkeypatch
):
    """測試直接下單 Slash Command，留空/價格為0時自動對齊遙測"""
    bot = MagicMock()
    cog = OrderUICog(bot)

    # Mock Telemetry dependencies
    async def mock_get_quote(symbol):
        return {"c": 100.0, "pc": 99.0}

    async def mock_telemetry(
        symbol,
        base_price,
        spot_price,
        iv,
        hist_iv,
        max_pain,
        prev_max_pain,
        skew_percentile,
        prev_close,
        base_quantity,
    ):
        return 95.5, base_quantity, ["Mocked Telemetry Command Price Resolved"]

    monkeypatch.setattr("services.market_data_service.get_quote", mock_get_quote)
    monkeypatch.setattr(
        "services.telemetry_pricing_engine.calculate_telemetry_price", mock_telemetry
    )

    # 帶價格為 0 以觸發遙測
    await cog.add_order.callback(
        cog,
        mock_interaction,
        symbol="BABA",
        quantity=10,
        order_type="LIMIT",
        validity="DAY",
        price=0.0,
    )

    assert mock_interaction.response.defer.called
    assert mock_interaction.followup.send.called
    embed = mock_interaction.followup.send.call_args[1]["embed"]
    assert "訂單登錄成功" in embed.title
    assert "BABA" in embed.description
    assert "95.5" in embed.description
    assert "自動套用遙測最佳防線價" in embed.description

    # 驗證資料庫
    orders = [
        o
        for o in get_user_active_orders(mock_interaction.user.id)
        if o["symbol"] == "BABA"
    ]
    assert len(orders) == 1
    assert orders[0]["limit_price"] == 95.5
