from typing import Any
import discord
import pytest
from unittest.mock import AsyncMock
from database.notification_channels import TRADING_MODULES
from database.notifications import (
    get_user_notification_settings,
    set_user_notification_setting,
    set_all_user_notification_settings,
    apply_preset_settings,
    is_notification_enabled,
    ALL_NOTIFICATION_KEYS,
    DEFAULT_NOTIFICATION_SETTINGS,
    LEGACY_KEY_ALIASES,
    set_user_notification_settings_bulk,
)


@pytest.fixture(autouse=True)
def clean_db(db_conn: Any):  # type: ignore
    """每個測試前清理 user_notification_settings"""
    cursor = db_conn.cursor()
    cursor.execute("DELETE FROM user_notification_settings")
    db_conn.commit()
    yield


def test_default_all_enabled(db_conn: Any):  # type: ignore
    """測試全新用戶 29 個通知頻道預設值（除機器人啟停通知外預設開啟）"""
    user_id = 999111
    settings = get_user_notification_settings(user_id)
    assert len(settings) == len(ALL_NOTIFICATION_KEYS)
    assert len(ALL_NOTIFICATION_KEYS) == 29

    for key in ALL_NOTIFICATION_KEYS:
        expected = key != "system_lifecycle"
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
        assert settings[key] is DEFAULT_NOTIFICATION_SETTINGS[key]

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

    # defense_portfolio_risk 已於 v081 拆解，舊 key 與其舊別名解析到拆分後的頻道
    set_user_notification_setting(user_id, "defense_portfolio_risk", False)
    assert is_notification_enabled(user_id, "defense_gamma_fragility") is False
    assert LEGACY_KEY_ALIASES["profit_lock_alert"] == "trim_profit_lock"
    assert LEGACY_KEY_ALIASES["margin_and_api_alert"] == "defense_margin_call"

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


# 預設情境 (all_on, all_off, bh_defense, focus, mute_intraday) 的完整驗證見
# test_full_preset_assertions_all_keys 與 test_presets_preserve_legacy_behaviour。


@pytest.mark.asyncio
async def test_notification_settings_view_structure(db_conn: Any):  # type: ignore
    """核心模組選單、多選 Select 與本區全關的反應"""
    from cogs.settings_ui import CORE_MODULES, NotificationSettingsView

    user_id = 999444

    view = NotificationSettingsView(user_id)
    # 1 個模組選單、1 個多選、2 個本區按鈕、4 個預設情境按鈕、1 個進階切換按鈕
    assert len(view.children) == 9

    category_select = next(
        c for c in view.children if getattr(c, "custom_id", None) == "select_category"
    )
    module_select = next(
        c for c in view.children if getattr(c, "custom_id", None) == "select_toggles"
    )

    # 預設收合：模組選單只列核心模組
    assert len(category_select.options) == len(CORE_MODULES)  # type: ignore
    # Discord 限制：每個 Select 最多 25 個選項
    for mod in TRADING_MODULES.values():
        assert len(mod["items"]) <= 25

    # 預設模組為左尾防護，多選且可全不選，預設選取＝目前開啟
    assert view.current_module == "left_tail"
    n_left_tail = len(TRADING_MODULES["left_tail"]["items"])
    assert len(module_select.options) == n_left_tail  # type: ignore
    assert module_select.min_values == 0  # type: ignore
    assert module_select.max_values == n_left_tail  # type: ignore
    assert all(o.default for o in module_select.options)  # type: ignore

    mock_interaction = AsyncMock()
    mock_interaction.user.id = user_id
    mock_interaction.response.edit_message = AsyncMock()
    await view.on_disable_module(mock_interaction)

    module_select_new = next(
        c for c in view.children if getattr(c, "custom_id", None) == "select_toggles"
    )
    assert not any(o.default for o in module_select_new.options)  # type: ignore


@pytest.mark.asyncio
async def test_notification_settings_view_preset_buttons(db_conn: Any):  # type: ignore
    """測試 NotificationSettingsView 點擊預設情境按鈕之反應"""
    from cogs.settings_ui import NotificationSettingsView

    user_id = 999555
    view = NotificationSettingsView(user_id)

    mock_interaction = AsyncMock()
    mock_interaction.user.id = user_id
    mock_interaction.response.edit_message = AsyncMock()

    await view.on_preset_focus(mock_interaction)
    assert is_notification_enabled(user_id, "heartbeat_watchlist") is False
    assert is_notification_enabled(user_id, "defense_gamma_fragility") is True

    await view.on_preset_mute_intraday(mock_interaction)
    assert is_notification_enabled(user_id, "telemetry_orders") is False

    await view.on_preset_bh_defense(mock_interaction)
    assert is_notification_enabled(user_id, "defense_option_rollover") is False
    assert is_notification_enabled(user_id, "entry_pyramid_add") is True

    await view.on_preset_all_on(mock_interaction)
    assert is_notification_enabled(user_id, "heartbeat_watchlist") is True
    assert is_notification_enabled(user_id, "alpha_market_signals") is True


@pytest.mark.asyncio
async def test_bh_defense_button_marked_recommended_for_advisory_accounts(
    db_conn: Any,
) -> None:
    import database
    from cogs.settings_ui import NotificationSettingsView

    def _label(view: Any) -> str:
        btn = next(
            c
            for c in view.children
            if getattr(c, "custom_id", None) == "btn_preset_bh_defense"
        )
        return str(btn.label)

    assert "建議" not in _label(NotificationSettingsView(999556))

    database.upsert_user_config(999557, portfolio_mode="ADVISORY")
    assert "建議" in _label(NotificationSettingsView(999557))


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


@pytest.mark.asyncio
async def test_category_navigation_and_embed_marker(db_conn: Any):  # type: ignore
    """測試 NotificationSettingsView 在 6 個模組之間切換導航與 Embed 標記反應"""
    from cogs.settings_ui import NotificationSettingsView, TRADING_MODULES

    user_id = 999777
    view = NotificationSettingsView(user_id)

    for mod_key, mod_info in TRADING_MODULES.items():
        mock_interaction = AsyncMock()
        mock_interaction.user.id = user_id
        mock_interaction.data = {"values": [mod_key]}
        mock_interaction.response.edit_message = AsyncMock()

        await view.on_category_select(mock_interaction)
        assert view.current_module == mod_key

        # 驗證 Embed 欄位中只有當前選中的維度帶有 🔹 標記
        embed = view.build_embed()
        target_field_title = f"🔹 {mod_info['title']}"
        matched_fields = [f for f in embed.fields if f.name == target_field_title]
        assert len(matched_fields) == 1

        # 驗證 Row 1 下拉選單項數與模組設定一致
        module_select = next(
            c
            for c in view.children
            if getattr(c, "custom_id", None) == "select_toggles"
        )
        assert len(module_select.options) == len(mod_info["items"])  # type: ignore


@pytest.mark.asyncio
async def test_toggle_select_callback_interaction(db_conn: Any):  # type: ignore
    """多選送出：本模組勾選者開啟、未勾選者關閉，其他模組不受影響"""
    from cogs.settings_ui import NotificationSettingsView

    user_id = 999888
    view = NotificationSettingsView(user_id)

    mock_nav = AsyncMock()
    mock_nav.user.id = user_id
    mock_nav.data = {"values": ["intel_intraday"]}
    mock_nav.response.edit_message = AsyncMock()
    await view.on_category_select(mock_nav)

    # 只勾選 telemetry_orders → 本模組其餘頻道關閉
    mock_submit = AsyncMock()
    mock_submit.user.id = user_id
    mock_submit.data = {"values": ["telemetry_orders"]}
    mock_submit.response.edit_message = AsyncMock()
    await view.on_select_callback(mock_submit)
    assert is_notification_enabled(user_id, "telemetry_orders") is True
    assert is_notification_enabled(user_id, "heartbeat_watchlist") is False
    assert is_notification_enabled(user_id, "alpha_option_scan") is False
    # 其他模組不受影響
    assert is_notification_enabled(user_id, "defense_margin_call") is True

    # 全不勾選 → 本模組全部關閉
    mock_submit.data = {"values": []}
    await view.on_select_callback(mock_submit)
    assert is_notification_enabled(user_id, "telemetry_orders") is False


def test_bulk_setter_is_single_transaction_and_resolves_aliases(db_conn: Any) -> None:
    set_user_notification_settings_bulk(
        999889,
        {"profit_lock_alert": False, "unknown_key": False, "alpha_wti_oil": False},
    )
    assert is_notification_enabled(999889, "trim_profit_lock") is False
    assert is_notification_enabled(999889, "alpha_wti_oil") is False


@pytest.mark.asyncio
async def test_module_level_batch_enable_disable_all_categories(db_conn: Any):  # type: ignore
    """測試 6 個模組各自執行本區開啟/關閉時的獨立性與精確性"""
    from cogs.settings_ui import NotificationSettingsView, TRADING_MODULES

    user_id = 999999
    view = NotificationSettingsView(user_id)

    for mod_key, mod_data in TRADING_MODULES.items():
        # 1. 導航至該模組
        mock_nav = AsyncMock()
        mock_nav.user.id = user_id
        mock_nav.data = {"values": [mod_key]}
        mock_nav.response.edit_message = AsyncMock()
        await view.on_category_select(mock_nav)

        # 2. 點擊「關閉本區所有設定」
        mock_btn = AsyncMock()
        mock_btn.user.id = user_id
        mock_btn.response.edit_message = AsyncMock()
        await view.on_disable_module(mock_btn)

        for item_key in mod_data["items"].keys():
            assert is_notification_enabled(user_id, item_key) is False

        # 3. 點擊「開啟本區所有設定」
        await view.on_enable_module(mock_btn)
        for item_key in mod_data["items"].keys():
            assert is_notification_enabled(user_id, item_key) is True


# v081 之前（PR2 時點）focus / mute_intraday 對既有頻道的值。重整後這些頻道的結果必須
# 不變；差異只允許出現在 _ALLOWED_DIFFS 列明的項目。
_LEGACY_PRESETS: dict[str, dict[str, bool]] = {
    "focus": {
        "briefing_pre_market": True,
        "briefing_post_market": True,
        "briefing_weekly_vtr": True,
        "system_lifecycle": True,
        "heartbeat_watchlist": False,
        "heartbeat_symbol_deep": False,
        "telemetry_orders": True,
        "advisory_entry_signal": True,
        "defense_option_rollover": True,
        "defense_margin_call": True,
        "defense_fundamental_thesis": True,
        "defense_macro_tail_risk": True,
        "advisory_core_levels": True,
        "alpha_market_signals": False,
        "alpha_option_scan": False,
        "alpha_polymarket": True,
        "alpha_wti_oil": True,
        "alpha_price_volume_watch": False,
    },
    "mute_intraday": {
        "briefing_pre_market": True,
        "briefing_post_market": True,
        "briefing_weekly_vtr": True,
        "system_lifecycle": True,
        "heartbeat_watchlist": False,
        "heartbeat_symbol_deep": False,
        "telemetry_orders": False,
        "advisory_entry_signal": False,
        "defense_option_rollover": False,
        "defense_margin_call": True,
        "defense_fundamental_thesis": True,
        "defense_macro_tail_risk": True,
        "advisory_core_levels": True,
        "alpha_market_signals": False,
        "alpha_option_scan": False,
        "alpha_polymarket": True,
        "alpha_wti_oil": True,
        "alpha_price_volume_watch": False,
    },
}

# 允許的差異：system_lifecycle 是 PR2 才新增的頻道，重整後歸為雜訊（機器人啟停通知
# 不屬於「精準交易」或「盤中靜音」想保留的內容）。
_ALLOWED_DIFFS: dict[tuple[str, str], bool] = {
    ("focus", "system_lifecycle"): False,
    ("mute_intraday", "system_lifecycle"): False,
}


def test_presets_preserve_legacy_behaviour() -> None:
    from database.notification_channels import PRESET_PROFILES

    for preset, legacy in _LEGACY_PRESETS.items():
        for key, old_value in legacy.items():
            expected = _ALLOWED_DIFFS.get((preset, key), old_value)
            assert PRESET_PROFILES[preset][key] is expected, (preset, key)


def test_preset_immune_channels_are_on_in_every_preset() -> None:
    from database.notification_channels import CHANNELS, PRESET_PROFILES

    immune = [c.key for c in CHANNELS if c.preset_immune]
    assert set(immune) == {
        "defense_margin_call",
        "defense_fundamental_thesis",
        "defense_macro_tail_risk",
        "defense_event_calendar",
        "defense_hedge_advice",
        "defense_structure_break",
        "defense_gamma_fragility",
        "risk_portfolio_downside",
    }
    for name, profile in PRESET_PROFILES.items():
        for key in immune:
            assert profile[key] is True, (name, key)


def test_full_preset_assertions_all_keys(db_conn: Any):  # type: ignore
    """所有頻道在 5 種預設情境下的完整狀態（新頻道逐一列明）"""
    user_id = 888111

    s_all_on = apply_preset_settings(user_id, "all_on")
    assert all(s_all_on[k] is True for k in ALL_NOTIFICATION_KEYS)

    # all_off 也不會關閉左尾防護
    s_all_off = apply_preset_settings(user_id, "all_off")
    assert s_all_off["defense_margin_call"] is True
    assert s_all_off["defense_structure_break"] is True
    assert s_all_off["alpha_wti_oil"] is False
    assert s_all_off["defense_option_rollover"] is False

    s_focus = apply_preset_settings(user_id, "focus")
    for key, expected in {
        "defense_event_calendar": True,
        "defense_hedge_advice": True,
        "defense_structure_break": True,
        "defense_gamma_fragility": True,
        "entry_pyramid_add": True,
        "alpha_short_entry": False,
        "trim_covered_call": True,
        "trim_profit_lock": True,
        "intel_market_scenario": False,
        "vtr_virtual_trades": False,
        "risk_portfolio_downside": True,
    }.items():
        assert s_focus[key] is expected, key

    s_mute = apply_preset_settings(user_id, "mute_intraday")
    for key, expected in {
        "defense_event_calendar": True,
        "defense_hedge_advice": True,
        "defense_structure_break": True,
        "defense_gamma_fragility": True,
        "entry_pyramid_add": False,
        "alpha_short_entry": False,
        "trim_covered_call": False,
        "trim_profit_lock": False,
        "intel_market_scenario": False,
        "vtr_virtual_trades": False,
        "risk_portfolio_downside": True,
    }.items():
        assert s_mute[key] is expected, key

    # 🧭 B&H 防守：上行削減全關、上行捕捉開（不含做空）、情報只留自訂門檻型
    s_bh = apply_preset_settings(user_id, "bh_defense")
    assert s_bh == {
        "defense_margin_call": True,
        "defense_fundamental_thesis": True,
        "defense_macro_tail_risk": True,
        "defense_event_calendar": True,
        "defense_hedge_advice": True,
        "defense_structure_break": True,
        "defense_gamma_fragility": True,
        "risk_portfolio_downside": True,
        "advisory_entry_signal": True,
        "entry_pyramid_add": True,
        "alpha_short_entry": False,
        "defense_option_rollover": False,
        "trim_covered_call": False,
        "trim_profit_lock": False,
        "advisory_core_levels": False,
        "heartbeat_watchlist": False,
        "heartbeat_symbol_deep": False,
        "intel_market_scenario": False,
        "telemetry_orders": False,
        "alpha_market_signals": False,
        "alpha_option_scan": False,
        "alpha_price_volume_watch": True,
        "alpha_polymarket": False,
        "alpha_wti_oil": True,
        "briefing_pre_market": True,
        "briefing_post_market": True,
        "briefing_weekly_vtr": True,
        "vtr_virtual_trades": False,
        "system_lifecycle": False,
    }


def test_v070_backfills_heartbeat_symbol_deep_from_watchlist(db_conn: Any) -> None:
    """拆分通知 key 時必須回填，否則已靜音的使用者會被自動重新訂閱。

    `heartbeat_symbol_deep` 的 DEFAULT_NOTIFICATION_SETTINGS 是 True，而
    `is_notification_enabled()` 在查無該列時會落到該預設值。凡是曾把
    `heartbeat_watchlist` 關掉（或套用 focus / mute_intraday 預設）的使用者，
    若不回填就會突然開始收到 30 分鐘個股深度心跳。
    """
    from database.migrations.v070_split_heartbeat_symbol_deep import migrate_data

    cursor = db_conn.cursor()
    # 使用者 A 明確關閉；使用者 B 明確開啟；使用者 C 從未設定
    cursor.execute(
        "INSERT OR REPLACE INTO user_notification_settings VALUES (?, ?, ?)",
        (8001, "heartbeat_watchlist", 0),
    )
    cursor.execute(
        "INSERT OR REPLACE INTO user_notification_settings VALUES (?, ?, ?)",
        (8002, "heartbeat_watchlist", 1),
    )
    db_conn.commit()

    migrate_data(db_conn)
    db_conn.commit()

    def _deep(uid: int) -> Any:
        cursor.execute(
            "SELECT enabled FROM user_notification_settings "
            "WHERE user_id = ? AND notification_key = 'heartbeat_symbol_deep'",
            (uid,),
        )
        return cursor.fetchone()

    assert _deep(8001)[0] == 0, "已靜音的使用者必須保持靜音"
    assert _deep(8002)[0] == 1
    assert _deep(8003) is None, "從未設定過的使用者維持沿用預設值，不寫入新列"

    # 重跑不得覆寫使用者在拆分之後才調整過的設定
    cursor.execute(
        "UPDATE user_notification_settings SET enabled = 1 "
        "WHERE user_id = ? AND notification_key = 'heartbeat_symbol_deep'",
        (8001,),
    )
    db_conn.commit()
    migrate_data(db_conn)
    db_conn.commit()
    assert _deep(8001)[0] == 1


def test_preset_dicts_cover_every_notification_key() -> None:
    """focus / mute_intraday 是手寫字典（all_on / all_off 是 comprehension 會自動涵蓋）。
    漏補新 key 時 apply_preset_settings 不會動到它，該 preset 對新頻道的行為即未定義，
    而逐 key 硬編碼斷言又不會失敗——這裡以結構性斷言補上這個靜默盲點。"""
    from database.notifications import PRESET_PROFILES

    for name, profile in PRESET_PROFILES.items():
        assert set(profile) == set(
            ALL_NOTIFICATION_KEYS
        ), f"preset {name} 的 key 集合不一致"


def test_notification_ui_modules_match_key_list() -> None:
    """/notif_settings 的 TRADING_MODULES 必須與 ALL_NOTIFICATION_KEYS 完全一致，
    否則新頻道使用者無法在 UI 上開關（或 UI 出現不存在的 key）。"""
    from cogs.settings_ui import TRADING_MODULES

    ui_keys = [k for m in TRADING_MODULES.values() for k in m["items"]]
    assert len(ui_keys) == len(set(ui_keys)), "同一個 key 不可出現在多個模組"
    assert set(ui_keys) == set(ALL_NOTIFICATION_KEYS)
    assert "advisory_entry_signal" in TRADING_MODULES["upside_capture"]["items"]
    assert "advisory_core_levels" in TRADING_MODULES["upside_trim"]["items"]


def test_v078_module_exports_required_attributes() -> None:
    """migration 模組缺 version / description / sql 任一者會被 get_migrations() 無聲跳過。"""
    from database.migrations import v078_backfill_advisory_entry_signal as m

    assert m.version == 78
    assert isinstance(m.description, str) and m.description
    assert hasattr(m, "sql")
    from database.core import get_migrations

    assert any(x["version"] == 78 for x in get_migrations())


def test_v078_backfills_advisory_entry_signal_from_symbol_deep(db_conn: Any) -> None:
    """已靜音個股深度心跳的使用者，不得在進場顧問上線後被自動重新訂閱。"""
    from database.migrations.v078_backfill_advisory_entry_signal import migrate_data

    cursor = db_conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO user_notification_settings VALUES (?, ?, ?)",
        (8101, "heartbeat_symbol_deep", 0),
    )
    cursor.execute(
        "INSERT OR REPLACE INTO user_notification_settings VALUES (?, ?, ?)",
        (8102, "heartbeat_symbol_deep", 1),
    )
    db_conn.commit()

    migrate_data(db_conn)
    db_conn.commit()

    def _row(uid: int, key: str) -> Any:
        cursor.execute(
            "SELECT enabled FROM user_notification_settings "
            "WHERE user_id = ? AND notification_key = ?",
            (uid, key),
        )
        return cursor.fetchone()

    assert _row(8101, "advisory_entry_signal")[0] == 0, "已靜音的使用者必須保持靜音"
    assert _row(8102, "advisory_entry_signal")[0] == 1
    assert _row(8103, "advisory_entry_signal") is None, "從未設定者維持沿用預設值"
    assert _row(8101, "advisory_core_levels") is None, "持倉位階顧問不回填"

    # 重跑不得覆寫使用者事後手動調整過的設定
    cursor.execute(
        "UPDATE user_notification_settings SET enabled = 1 "
        "WHERE user_id = ? AND notification_key = 'advisory_entry_signal'",
        (8101,),
    )
    db_conn.commit()
    migrate_data(db_conn)
    db_conn.commit()
    assert _row(8101, "advisory_entry_signal")[0] == 1


def test_v081_module_exports_required_attributes() -> None:
    from database.core import get_migrations
    from database.migrations import v081_split_notification_channels as m

    assert m.version == 81
    assert isinstance(m.description, str) and m.description
    assert hasattr(m, "sql")
    assert any(x["version"] == 81 for x in get_migrations())


def test_v081_parent_map_matches_registry() -> None:
    """遷移的凍結對照表必須與註冊表的 parent_key 一致（新增子頻道時兩邊都要更新）。"""
    from database.migrations.v081_split_notification_channels import (
        _CHILD_TO_PARENT,
    )
    from database.notification_channels import CHANNELS

    registry = {c.key: c.parent_key for c in CHANNELS if c.parent_key}
    assert dict(_CHILD_TO_PARENT) == registry


def test_v081_backfills_children_from_parents(db_conn: Any) -> None:
    from database.migrations.v081_split_notification_channels import migrate_data

    cursor = db_conn.cursor()
    rows = [
        (8201, "defense_option_rollover", 0),
        (8201, "defense_portfolio_risk", 0),
        (8201, "alpha_market_signals", 0),
        (8201, "heartbeat_watchlist", 0),
        (8201, "defense_macro_tail_risk", 0),
        (8202, "defense_option_rollover", 1),
        (8202, "defense_portfolio_risk", 1),
    ]
    cursor.executemany(
        "INSERT OR REPLACE INTO user_notification_settings VALUES (?, ?, ?)", rows
    )
    db_conn.commit()

    migrate_data(db_conn)
    db_conn.commit()

    def _row(uid: int, key: str) -> Any:
        cursor.execute(
            "SELECT enabled FROM user_notification_settings "
            "WHERE user_id = ? AND notification_key = ?",
            (uid, key),
        )
        return cursor.fetchone()

    # 已靜音母頻道的使用者，子頻道維持靜音
    for child in (
        "defense_structure_break",
        "entry_pyramid_add",
        "trim_covered_call",
        "vtr_virtual_trades",
        "defense_gamma_fragility",
        "trim_profit_lock",
        "alpha_short_entry",
        "intel_market_scenario",
        "defense_event_calendar",
        "defense_hedge_advice",
    ):
        assert _row(8201, child)[0] == 0, child
    assert _row(8202, "entry_pyramid_add")[0] == 1
    # 從未設定者不回填，沿用預設值
    assert _row(8203, "entry_pyramid_add") is None
    # MARGIN_API 併入保證金頻道但不回填
    assert _row(8201, "defense_margin_call") is None
    # 拆解後的舊頻道列被刪除
    assert _row(8201, "defense_portfolio_risk") is None
    assert _row(8202, "defense_portfolio_risk") is None

    # 重跑冪等，且不覆寫使用者事後的調整
    cursor.execute(
        "UPDATE user_notification_settings SET enabled = 1 "
        "WHERE user_id = 8201 AND notification_key = 'trim_profit_lock'"
    )
    db_conn.commit()
    migrate_data(db_conn)
    db_conn.commit()
    assert _row(8201, "trim_profit_lock")[0] == 1
    assert _row(8201, "defense_gamma_fragility")[0] == 0


# ---------------------------------------------------------------------------
# ⚙️ 進階：情報與戰報預設收合
# ---------------------------------------------------------------------------


def _child(view: Any, custom_id: str) -> Any:
    return next(c for c in view.children if getattr(c, "custom_id", None) == custom_id)


def _interaction(user_id: int, values: list[str] | None = None) -> Any:
    it = AsyncMock()
    it.user.id = user_id
    it.response.edit_message = AsyncMock()
    if values is not None:
        it.data = {"values": values}
    return it


def test_advanced_modules_are_intel_and_briefing_only() -> None:
    """分組以 risk_role 推導：進階＝只含情報 / 戰報頻道的模組，其餘為核心模組。"""
    from cogs.settings_ui import ADVANCED_MODULES, CORE_MODULES
    from database.notification_channels import CHANNELS

    assert ADVANCED_MODULES, "應至少有一個進階模組"
    assert set(ADVANCED_MODULES) | set(CORE_MODULES) == set(TRADING_MODULES)
    assert not set(ADVANCED_MODULES) & set(CORE_MODULES)
    for c in CHANNELS:
        if c.module in ADVANCED_MODULES:
            assert c.risk_role in {"INTEL", "BRIEFING"}, c.key
        else:
            assert c.risk_role in {
                "LEFT_TAIL",
                "UPSIDE_CAPTURE",
                "UPSIDE_TRIM",
            }, c.key
    assert set(CORE_MODULES) == {"left_tail", "upside_capture", "upside_trim"}


@pytest.mark.asyncio
async def test_default_view_shows_only_core_modules(db_conn: Any) -> None:
    from cogs.settings_ui import (
        ADVANCED_MODULES,
        CORE_MODULES,
        NotificationSettingsView,
    )

    view = NotificationSettingsView(990001)
    assert view.show_advanced is False
    options = [o.value for o in _child(view, "select_category").options]
    assert options == list(CORE_MODULES)
    assert not set(options) & set(ADVANCED_MODULES)
    assert _child(view, "btn_toggle_advanced").label == "⚙️ 進階：情報與戰報"

    # embed：核心模組逐項列出，進階只剩一行摘要
    embed = view.build_embed()
    names = [f.name or "" for f in embed.fields]
    for mod in ADVANCED_MODULES:
        assert not any(TRADING_MODULES[mod]["title"] in n for n in names)
    summary = next(f for f in embed.fields if f.name == "⚙️ 進階（已收合）")
    assert "情報" in (summary.value or "") and "戰報" in (summary.value or "")


@pytest.mark.asyncio
async def test_advanced_summary_counts_follow_settings(db_conn: Any) -> None:
    from cogs.settings_ui import ADVANCED_MODULES, build_advanced_summary
    from database.notification_channels import CHANNELS

    user_id = 990002
    intel = [c.key for c in CHANNELS if c.risk_role == "INTEL"]
    briefing = [c.key for c in CHANNELS if c.risk_role == "BRIEFING"]
    assert all(c.module in ADVANCED_MODULES for c in CHANNELS if c.key in intel)
    set_all_user_notification_settings(user_id, False)
    set_user_notification_setting(user_id, intel[0], True)

    text = build_advanced_summary(get_user_notification_settings(user_id))
    assert f"情報 {len(intel)} 項（1 開啟）" in text
    assert f"戰報 {len(briefing)} 項（0 開啟）" in text


@pytest.mark.asyncio
async def test_expand_advanced_and_toggle_intel_channel(db_conn: Any) -> None:
    """展開進階後可切到情報與戰報模組，並以多選正常切換開關；收合後回到核心模組。"""
    from cogs.settings_ui import (
        ADVANCED_MODULES,
        CORE_MODULES,
        NotificationSettingsView,
    )

    user_id = 990003
    view = NotificationSettingsView(user_id)

    await view.on_toggle_advanced(_interaction(user_id))
    assert view.show_advanced is True
    assert view.current_module == ADVANCED_MODULES[0]
    options = [o.value for o in _child(view, "select_category").options]
    assert options == list(CORE_MODULES + ADVANCED_MODULES)
    assert _child(view, "btn_toggle_advanced").label == "⬆️ 收合進階"
    embed = view.build_embed()
    assert not any(f.name == "⚙️ 進階（已收合）" for f in embed.fields)
    for mod in ADVANCED_MODULES:
        assert any(
            TRADING_MODULES[mod]["title"] in (f.name or "") for f in embed.fields
        )

    for mod in ADVANCED_MODULES:
        await view.on_category_select(_interaction(user_id, [mod]))
        assert view.current_module == mod
        items = list(TRADING_MODULES[mod]["items"])
        # 只勾選第一個：它開啟、其餘關閉
        await view.on_select_callback(_interaction(user_id, [items[0]]))
        assert is_notification_enabled(user_id, items[0]) is True
        for key in items[1:]:
            assert is_notification_enabled(user_id, key) is False

    await view.on_toggle_advanced(_interaction(user_id))
    assert view.show_advanced is False
    assert view.current_module == "left_tail"
    options = [o.value for o in _child(view, "select_category").options]
    assert options == list(CORE_MODULES)


@pytest.mark.asyncio
async def test_collapse_keeps_core_module_selection(db_conn: Any) -> None:
    from cogs.settings_ui import NotificationSettingsView

    user_id = 990004
    view = NotificationSettingsView(user_id)
    await view.on_toggle_advanced(_interaction(user_id))
    await view.on_category_select(_interaction(user_id, ["upside_trim"]))
    await view.on_toggle_advanced(_interaction(user_id))
    assert view.current_module == "upside_trim"


@pytest.mark.asyncio
async def test_view_respects_discord_component_limits(db_conn: Any) -> None:
    """展開與收合兩種狀態都必須在 Discord 限制內：最多 5 row、每 row 最多 5 個元件、
    每個 Select 最多 25 個選項。"""
    from collections import Counter

    from cogs.settings_ui import NotificationSettingsView

    user_id = 990005
    view = NotificationSettingsView(user_id)
    for _ in range(2):
        rows = Counter(getattr(c, "row", None) for c in view.children)
        assert set(rows) <= {0, 1, 2, 3, 4}
        assert all(n <= 5 for n in rows.values())
        for c in view.children:
            if isinstance(c, discord.ui.Select):
                assert len(c.options) <= 25
        await view.on_toggle_advanced(_interaction(user_id))
