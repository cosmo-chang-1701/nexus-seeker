# 互動設定與通知偏好中心

## 1. 功能總覽

為了提供無痛設定體驗、避免參數過多的 slash command 介面，平台採用完全互動式的設定架構：將核心帳戶指標與警示設定分離，使用 Discord View／Modal 進行動態輸入，並對自動化測試保持向後相容。

## 2. 參數分離與資料庫結構

設定被嚴格劃分為兩個功能區域，最大化關注點分離：

### 2.1 核心帳戶設定（`/settings`）
追蹤高階財務參數，存於 `user_settings` 表：

- `capital`（總資本，必須 `> 0`）
- `risk_limit`（基礎風險百分比限制，範圍 `1.0` 至 `50.0`）
- `enable_vtr`、`enable_psq_watchlist`、`monthly_expense`、`tax_reserve_rate`、`cash_reserve`
- `trading_strategy`（`RIGHT_SIDE` / `LEFT_SIDE` / `SHORT_SIDE` / `DYNAMIC`，遷移 `v068`，預設 `RIGHT_SIDE`）——詳見 [`../strategies/01_regime_routing_matrix.md`](../strategies/01_regime_routing_matrix.md)。這是第一個既非布林開關、也非自由文字數值欄位的設定，因此 `AccountSettingsView.on_select_callback()` 新增了**第三分支**：一個固定 4 選項的 `TradingStrategySelectView`，直接寫入資料庫並導回父視圖，不經過 Modal（仿照 `ui/watchlist_tags.py` 的 Select 子類作法）。
  - `SHORT_SIDE`（做空交易）是後續新增的第四個值。因該欄位是 `TEXT DEFAULT 'RIGHT_SIDE'` 且無 `CHECK` 約束，新增 enum 值**不需要**新的 migration。選單描述文字刻意標明方向（「做多・逆勢均值回歸」／「做空・結構破位追空」），避免使用者把「左側」誤讀為「做空」——左側本質仍是做多。
- `portfolio_mode`（`COMMAND` / `ADVISORY`，遷移 `v079`，預設 `COMMAND`）——持倉停利停損輸出語意的帳戶層開關，詳見 [`../strategies/05_dual_track_anti_washout_stop_loss.md`](../strategies/05_dual_track_anti_washout_stop_loss.md) §3.1。比照 `risk_appetite` 的既有 Select 範式（`PortfolioModeSelectView`，固定 2 選項、直接寫入 DB、不經 Modal）。單檔可用 `/edit_holding advisory_mode` 三態覆寫（`FOLLOW`/`ADVISORY`/`COMMAND`，存於 `assets.metadata.advisory_only`）。
- 也整合了**自選股標籤系統**：允許使用者透過互動下拉選單與 Modal，為自選股資產附加自訂分類標籤（如 `TECH`、`CORE`，`ui/watchlist_tags.py`）。此標籤引擎也完整暴露於 `/list_watch` 指令輸出中，透過在地化的「🏷️ 原地編輯標籤」捷徑按鈕，實現自動重建並替換原始 Discord 視圖的無縫、類 SPA 編輯體驗。

### 2.2 通知偏好（`/notif_settings`）
以 key-value 風格的 `user_notification_settings` 表管理個別開關（複合主鍵 `(user_id, notification_key)`，支援無限 schema-less 擴充）。完整整併為 **4 大戰術維度、17 個核心頻道**（遷移 `v061` + WTI 警示 + `defense_fundamental_thesis` + `alpha_price_volume_watch` + 自選標的進場顧問 `advisory_entry_signal` + B&H 持倉位階顧問 `advisory_core_levels`）：

- **4 大戰術模組**：
  1. `briefings`（📋 定時戰報與覆盤）：`briefing_pre_market`、`briefing_post_market`、`briefing_weekly_vtr`
  2. `telemetry`（📡 盤中自選與掛單遙測）：`heartbeat_watchlist`、`heartbeat_symbol_deep`、`telemetry_orders`、`advisory_entry_signal`
     - `heartbeat_watchlist` → **15 分鐘批次量化雷達**（`cogs/trading/heartbeat.py` → `build_radar_scan_embed`）
     - `heartbeat_symbol_deep` → **30 分鐘個股深度戰場心跳**（`IntradayScanPipeline` → `create_watchlist_signal_embed`）
     - `advisory_entry_signal` → **自選標的進場顧問**：獨立於上述兩則心跳的第三條推播路徑，僅在進場六重鐵律通過時推播進場價／停損／目標。去重旗標鍵為 `advisory_entry_{uid}_{SYMBOL}_{REGIME}_{YYYYMMDD}`（含 Regime：同日由 III-B 升級為 III 是更強的新訊號，值得再發一次）。
     - 兩則心跳是完全獨立的推播路徑（詳見 [`../architecture/01_dual_watchlist_pipelines.md`](../architecture/01_dual_watchlist_pipelines.md)）。過去共用同一個 `heartbeat_watchlist` key，無法分別靜音，而標籤還誤寫成「30 分鐘」卻同時管著 15 分鐘那條。
  3. `defense`（🛡️ 持倉風控與極端防禦）：`defense_portfolio_risk`、`defense_option_rollover`、`defense_margin_call`、`defense_fundamental_thesis`、`defense_macro_tail_risk`、`advisory_core_levels`
  4. `alpha`（🎯 Alpha 策略與情報）：`alpha_market_signals`、`alpha_polymarket`、`alpha_wti_oil`、`alpha_price_volume_watch`
     - 動態轉倉引擎的 **`SHORT_ENTRY` 做空進場訊號**走 `alpha_market_signals`，而非 `defense_option_rollover`：它是進場訊號、不是持倉防禦。`focus` 與 `mute_intraday` 預設關閉此頻道，做空系統校準前較不易打擾使用者；另受 `SHORT_ENTRY_DRY_RUN`（預設開啟，只寫稽核紀錄）控制。其餘動態轉倉情境維持 `defense_option_rollover`，保證金強制平倉維持 `defense_margin_call`。
  - `advisory_core_levels`（`defense` 模組）→ **B&H 持倉位階顧問**：僅告知目標區與結構失效位階、不建議減碼／換股；去重旗標鍵為 `advisory_exit_{uid}_{SYMBOL}_{EXIT_TIER}_{YYYYMMDD}`。
  - 上述兩個頻道的推播路徑分別由後續階段實作；在此之前開關存在但不會產生推播。兩個去重旗標前綴已登記於 `database/cache.py::_KV_CACHE_DEDUP_KEY_PREFIXES`（03:00 ET 清理白名單）。
- **動態雙層架構與預設模式**：為提供簡潔不雜亂的使用者體驗：
  - Row 0：類別選擇器（`briefings`、`telemetry`、`defense`、`alpha`）
  - Row 1：開關選項，即時 `🟢` / `🔴` 指示器
  - Row 2：模組批次控制（`⚡ 開啟本區`、`💤 關閉本區`）
  - Row 3：一鍵預設快捷按鈕：
    - `🛡️ 戰備全開`（`all_on`）：啟用全部 17 個頻道。
    - `🎯 精準交易`（`focus`）：保留定時戰報、即時持倉防禦，以及常駐情報（`alpha_wti_oil`、`alpha_polymarket`），靜音盤中掃描器／Alpha 雜訊（`heartbeat_watchlist`、`alpha_market_signals`、`alpha_price_volume_watch`）；`advisory_entry_signal`／`advisory_core_levels` 皆維持開啟（高信號、僅在條件通過時才推播）。
    - `🔕 盤中靜音`（`mute_intraday`）：保留盤前盤後戰報、保證金與尾部風險警示、基本面論點警示，以及常駐 WTI／Polymarket 情報；只靜音盤中節奏的雜訊（`heartbeat_watchlist`、`telemetry_orders`、`defense_option_rollover`、`advisory_entry_signal`、`alpha_market_signals`、`alpha_price_volume_watch`）；`advisory_core_levels` 屬持倉防禦等級，維持開啟。
  - `focus` 與 `mute_intraday` 是手寫字典（`all_on`／`all_off` 為 comprehension 自動涵蓋），`test_notification_toggles.py::test_preset_dicts_cover_every_notification_key` 以結構性斷言強制其 key 集合與 `ALL_NOTIFICATION_KEYS` 一致。
  - 既有使用者的新 key 無資料列時會落到預設 `True`；遷移 `v078` 以 `heartbeat_symbol_deep` 為代理訊號回填 `advisory_entry_signal`（比照 `v070`），避免已靜音盤中推播者被自動訂閱。
- **Polymarket 參數分離**：非布林帳戶設定（`polymarket_threshold`、`polymarket_use_llm`、`polymarket_slippage`）乾淨地放在 `/settings`（`AccountSettingsView`），讓 `/notif_settings` 保持純粹、專注於通知頻道。
- **100% 向後相容別名引擎**：使用舊版鍵值（如 `hb_options_structure`、`ddp_alert`、`profit_lock_alert`、`wti_oil_alert`、`oil_alert`）查詢或更新時，會透過 `LEGACY_KEY_ALIASES` 透明解析至新的整併鍵值。

## 3. UI 元件管線（`cogs/settings_ui.py` & `cogs/terminal.py`）

`/settings` 與 `/notif_settings`（定義於 `cogs/terminal.py`）皆使用 `cogs/settings_ui.py` 中定義的 ephemeral Discord Views：

- **布林開關與切換**：選擇布林設定（如 `enable_vtr` 或通知開關）會立即在 SQLite 中翻轉狀態，觸發 `.refresh_items()` 重新生成選項（含狀態表情符號：`🟢` 為開、`🔴` 為關），並編輯目前訊息以更新 embed。
- **動態文字輸入 Modal**：選擇數值欄位會觸發 Discord Modal 彈窗（`AccountSettingsModal`）。
  - **前端驗證與清理**：Modal 的 `on_submit()` 執行嚴格驗證，例如數值邊界檢查、`capital > 0` 驗證、使用者輸入清理。
  - **視圖刷新**：驗證與持久化成功後，Modal 動態觸發父視圖重繪，即時刷新儀表板，不額外發送訊息區塊。

## 4. 整合測試相容性設計

`discord.app_commands.Command` 的 slash command callback 是唯讀的。為了讓 slash command 對 Discord UI 使用者呈現無參數介面，同時保留完整參數化的程式化呼叫供整合測試使用，`TerminalCog` 初始化時動態包裝指令的私有 `_callback` 參照：

```python
async def compat_callback(cog, interaction, **kwargs):
    return await cog._update_settings_impl(interaction, **kwargs)
self.update_settings._callback = compat_callback
```

此手法優雅地讓帶關鍵字參數呼叫的測試驅動流程，直接路由至資料庫寫入函式，而標準使用者呼叫則乾淨地觸發互動式的 `AccountSettingsView`。

## 5. 輸出集中化

為遵守輸出集中化規則、避免 `test_output_centralization.py` 失敗：

- cogs、views、modals 皆不直接建構 `discord.Embed` 物件。
- 展示層完全集中於 `cogs/embed_builders/`（`embed_builder.py` 僅作為向後相容 shim）：
  - `create_account_settings_embed(details_list: list[str]) -> discord.Embed`
  - `create_notification_settings_embed(module_fields: list[tuple[str, str]]) -> discord.Embed`

## 6. 核心程式碼檔案路徑關聯

- `nexus_core/cogs/settings_ui.py`：`AccountSettingsView`、`AccountSettingsModal`、`TradingStrategySelectView`、`PortfolioModeSelectView`
- `nexus_core/cogs/terminal.py`：`/settings`、`/notif_settings` 指令入口
- `nexus_core/ui/watchlist_tags.py`：自選股標籤系統
- `nexus_core/database/migrations/v068_add_trading_strategy.py`：`trading_strategy` 欄位遷移
- `nexus_core/database/migrations/v061_consolidate_notification_settings.py`：通知偏好 4 模組頻道整併遷移（當時為 13 頻道）
- `nexus_core/database/migrations/v078_backfill_advisory_entry_signal.py`：以 `heartbeat_symbol_deep` 回填 `advisory_entry_signal`
- `nexus_core/database/migrations/v079_add_portfolio_mode.py`：`portfolio_mode` 欄位遷移
- `nexus_core/database/cache.py`：`_KV_CACHE_DEDUP_KEY_PREFIXES` 每日去重旗標清理白名單
- `nexus_core/tests/unit/test_kv_cache_dedup_whitelist.py`：AST 掃描強制新增去重旗標必須登記白名單
- `nexus_core/tests/unit/test_settings_interactive.py`：互動設定視圖與 Modal 單元測試
- `nexus_core/tests/unit/test_notification_toggles.py`：通知偏好資料庫開關與視圖單元測試
