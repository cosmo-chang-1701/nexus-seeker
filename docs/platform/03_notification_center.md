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
以 key-value 風格的 `user_notification_settings` 表管理個別開關（複合主鍵 `(user_id, notification_key)`，支援無限 schema-less 擴充）。頻道的 key、模組、標籤與屬性只在 `database/notification_channels.py` 的 `CHANNELS` 註冊表定義，`ALL_NOTIFICATION_KEYS`／`DEFAULT_NOTIFICATION_SETTINGS`／`PRESET_PROFILES`／`TRADING_MODULES` 皆由此衍生。

#### 2.2.1 分類準則：依對 B&H 投組報酬分佈的影響分組
使用者主策略為 Buy & Hold，評估以**索提諾比率為主**、MDD 與 VaR／CVaR 為輔（見 [`../risk_portfolio/07_downside_risk_sortino_var_cvar.md`](../risk_portfolio/07_downside_risk_sortino_var_cvar.md)）。Sortino 只懲罰低於 MAR 的報酬、不懲罰上行波動，因此頻道依其 `risk_role` 分為 5 種作用、6 個模組（共 28 個頻道）：

| 模組 | `risk_role` | 頻道 | 內容 |
|---|---|---|---|
| 🛡️ 左尾防護 | `LEFT_TAIL`（`preset_immune`） | `defense_margin_call` | 保證金防禦（`MARGIN_DEFENSE`）＋模擬保證金警戒（`MARGIN_API`，v081 前誤歸 `defense_portfolio_risk`） |
| | | `defense_fundamental_thesis` | SEC 財報護城河破滅 |
| | | `defense_macro_tail_risk` | VIX ≥ 30／期限結構倒掛黑天鵝 |
| | | `defense_event_calendar` | 48 小時內財報／經濟數據事件預警（`services/event_monitor.py`） |
| | | `defense_hedge_advice` | VIX 急升 SPY 對沖股數（`hedge_monitor_service.py`）＋NRO 掃描的 Re-hedge 建議 |
| | | `defense_structure_break` | 結構失效類出場分層、逃頂保護性 Put（`MACRO_TOP_ESCAPE_DEFENSE`）、顧問模式的結構失效告知 |
| | | `defense_gamma_fragility` | 持倉淨 Gamma < −20、IV 優勢掃描的高 IV 事件風險警告（`is_high_risk_vol`） |
| 🚀 上行捕捉 | `UPSIDE_CAPTURE` | `advisory_entry_signal` | 自選標的進場顧問（六重鐵律通過時推播進場價／停損／目標） |
| | | `entry_pyramid_add` | `PYRAMID_ADD`、`TRANSITION_ENGINE`（停損上推／一次性加碼）、`CORE_DEPLOYMENT` |
| | | `alpha_short_entry` | `SHORT_ENTRY` 做空進場（校準中，歸雜訊；另受 `SHORT_ENTRY_DRY_RUN` 控制） |
| ✂️ 上行削減 | `UPSIDE_TRIM` | `defense_option_rollover` | TP1–TP3 分批、衛星再平衡、機會成本換股、動態保本（`SL_TRAILING_BREAKEVEN`） |
| | | `trim_covered_call` | Covered Call 解套、CC Overlay、`COVERED_CALL_PROFIT_LOCK` |
| | | `trim_profit_lock` | DITM 深價內期權獲利鎖定 |
| | | `advisory_core_levels` | 顧問模式目標區位階告知（去重鍵 `advisory_exit_{uid}_{SYMBOL}_{EXIT_TIER}_{YYYYMMDD}`） |
| 📡 盤中情報 | `INTEL` | `heartbeat_watchlist` | 15 分鐘批次量化雷達（`cogs/trading/heartbeat.py`） |
| | | `heartbeat_symbol_deep` | 30 分鐘個股深度戰場心跳（`IntradayScanPipeline`） |
| | | `intel_market_scenario` | 自選股市場情境事件（巨鯨護航、結構破位等；與雷達共用資料但獨立開關） |
| | | `telemetry_orders` | 待成交掛單遙測對齊 |
| | | `alpha_market_signals` | DDP、廉價期權、Gamma Squeeze SPEAR |
| | | `alpha_option_scan` | NRO 期權掃描執行決策、PowerSqueeze、期權掃描卡 |
| | | `alpha_price_volume_watch` | 個股 15 分鐘價量突破（自訂門檻） |
| 🌐 全天候情報 | `INTEL` | `alpha_polymarket`、`alpha_wti_oil` | Polymarket 巨鯨與機率突變；WTI 原油（自訂門檻） |
| 📋 定時戰報與系統 | `BRIEFING` | `briefing_pre_market`、`briefing_post_market`、`briefing_weekly_vtr` | 盤前／盤後／VTR 週報 |
| | | `vtr_virtual_trades` | 虛擬交易室自動轉倉／平倉（紙上交易，不涉及真實部位） |
| | | `system_lifecycle` | 機器人啟動／關閉廣播（**預設關閉**） |

兩則心跳是完全獨立的推播路徑（詳見 [`../architecture/01_dual_watchlist_pipelines.md`](../architecture/01_dual_watchlist_pipelines.md)）。進場顧問的去重鍵為 `advisory_entry_{uid}_{SYMBOL}_{REGIME}_{YYYYMMDD}`（同日由 III-B 升級為 III 是更強的新訊號，會再發一次）。

#### 2.2.2 動態轉倉指令的頻道對照
`database/notification_channels.py::resolve_rollover_channel()` 取代 `portfolio_monitor.py` 過去的 if/elif 鏈，優先序：

1. `MARGIN_DEFENSE` → `defense_margin_call`（帳戶生存等級，任何分層都不改道）
2. 結構失效類 exit tier（`STRUCTURE_BREAK_EXIT_TIERS`：`SL_STRUCTURAL`、`SL_REGIME_FLIP`、`SL_WHALE_PUT`、`SL_WHALE_CALL`、`EXTREME_TICK_BREACH`、`IVR_FAST_EXIT`）→ `defense_structure_break`（含顧問模式的結構失效告知）
3. 顧問模式其餘告知（目標區）→ `advisory_core_levels`
4. Covered Call Overlay → `trim_covered_call`
5. `ROLLOVER_SCENARIO_CHANNEL` 情境對照表；未知情境 → `defense_option_rollover`

`SL_TRAILING_BREAKEVEN`（動態保本）刻意**不**列為結構失效：它是獲利部位回吐到成本價時的出場，性質是獲利部位管理，歸上行削減。`tests/unit/test_rollover_channel_routing.py` 窮舉保證每個 `RolloverScenario` 都有頻道。

#### 2.2.3 預設情境（由頻道屬性衍生）
每個頻道帶 `risk_role`、`cadence`（盤中／每日／每週／全天候／事件）、`noise`（高頻或尚未校準）、`user_configured`（使用者自訂門檻）與 `preset_immune` 屬性，預設情境由規則衍生，新增頻道時不可能漏填：

- `🧭 B&H 防守`（`bh_defense`）：上行捕捉開（不含做空）、上行削減全關、情報只留自訂門檻型（價量、WTI）、戰報開（不含雜訊）。帳戶 `portfolio_mode='ADVISORY'` 時按鈕標示「建議」。
- `🎯 精準交易`（`focus`）：只關閉 `noise` 頻道。
- `🔕 盤中靜音`（`mute_intraday`）：關閉 `noise` 與 `cadence == INTRADAY` 的頻道。
- `🛡️ 戰備全開`（`all_on`）；`all_off` 僅供程式呼叫。
- **左尾防護 7 個頻道皆 `preset_immune`**：任何預設情境（含 `all_off`）都不會關閉，只能逐項手動關。
- `focus`／`mute_intraday` 對既有頻道的結果與重整前逐 key 相同，唯一差異是 `system_lifecycle`（重整後歸雜訊）；`test_notification_toggles.py::test_presets_preserve_legacy_behaviour` 逐一列明。

#### 2.2.4 遷移 `v081_split_notification_channels`
新增的 10 個子頻道以母頻道（`parent_key`）的**明確設定**回填（`INSERT OR IGNORE`，冪等、不覆寫事後調整）：已靜音母頻道者子頻道維持靜音，從未設定者沿用預設值（比照 `v070`／`v078`）。`defense_portfolio_risk` 拆完後刪除其資料列，並列入 `LEGACY_KEY_ALIASES` 指向 `defense_gamma_fragility`（`profit_lock_alert` → `trim_profit_lock`、`margin_and_api_alert` → `defense_margin_call`）。`MARGIN_API` 併入 `defense_margin_call` 時刻意不回填，避免過去關閉混裝頻道的使用者連帶靜音保證金警戒。遷移內的對照表是凍結快照，`test_v081_parent_map_matches_registry` 確保與註冊表一致。

#### 2.2.5 UI（`NotificationSettingsView`，5 row 以內）
  - **核心 vs 進階**：面板預設只呈現三個會改變投組報酬分佈的**核心模組**（🛡️ 左尾防護、🚀 上行捕捉、✂️ 上行削減）；情報與戰報類模組收在「⚙️ 進階」裡。分組以註冊表的 `risk_role` 推導（模組內所有頻道皆為 `INTEL`／`BRIEFING` 即歸進階，`cogs/settings_ui.py::ADVANCED_MODULES`／`CORE_MODULES`），不寫死模組或頻道 key，註冊表新增頻道時自動跟上。收合只影響畫面呈現，**預設情境仍會一併套用到進階頻道**。
  - Row 0：模組選單（收合時只列核心模組；展開後列出全部模組；描述寫出該模組對 Sortino／回撤的作用）
  - Row 1：本模組頻道**多選** Select（`min_values=0`、預設選取＝目前開啟；送出後勾選者開啟、未勾選者關閉，以 `set_user_notification_settings_bulk()` 單一交易寫入並清快取）
  - Row 2：本區全開／全關（同一批次寫入）
  - Row 3：預設情境（🧭 B&H 防守、🎯 精準交易、🔕 盤中靜音、🛡️ 戰備全開）
  - Row 4：`⚙️ 進階：情報與戰報`／`⬆️ 收合進階` 切換按鈕。展開時直接切到第一個進階模組；收合時若正停在進階模組則回到左尾防護，停在核心模組則保持不變。
  - embed：核心模組每個頻道顯示 `🟢/🔴 標籤` 與「頻率 · 作用」標籤（`channel_status_tags()`）；進階收合時只顯示一行摘要（例如「情報 N 項（M 開啟）、戰報 N 項（M 開啟）」，`build_advanced_summary()`），展開後才逐項列出。
- **集中式推播入口（`services/notification_dispatcher.py`）**：所有主動推播一律經 `notify()`／`notify_many()`，順序固定為「頻道開關 → 去重讀取 → 入列持久化 DM 佇列 → 寫入去重旗標」：
  - 關閉的頻道不做任何 I/O、**不寫去重旗標**，使用者重新開啟後當日尚未送達的事件仍能送出；入列失敗不會燒掉旗標。
  - 昂貴工作之前先以 `is_channel_enabled()` 判斷：Polymarket 巨鯨摘要的 LLM 呼叫、對沖警報的 LLM 敘述、Covered Call 解套推薦、IV 優勢掃描。對沖警報的 VTR 紀錄與 `hedge_alerts` 仍照常寫入（餵給 Brinson 歸因）。
  - 事件預警（`services/event_monitor.py`）改為先查開關、實際入列後才標記已提醒；過去先標記後查開關，靜音期間的事件永久遺失。
  - 補上每日去重：DDP（`ddp_alert_`）、IV 優勢（`iv_alert_`）、DITM 獲利鎖定（`profit_lock_alert_`）、負 Gamma 斷層（`gamma_fragility_alert_`）、模擬保證金（`margin_api_alert_`）、掛單遙測（`telemetry_align_`，以建議價／量簽章區分，建議變動即視為新事件）、Polymarket 機率突變（`poly_prob_shift_`）。自動掃描才去重，管理員強制掃描不去重。
  - `/iv_scan` 只掃描、只回覆呼叫者自己的觀察清單（過去會私訊所有其他使用者）。
  - 頻道開關只決定「推不推」，不再決定雷達 embed「顯示什麼」：`build_radar_scan_embed` 過去依三個頻道開關以 emoji 子字串刪減洞察行，已移除。
  - `tests/unit/test_notification_dispatch_centralization.py` 以 AST 強制：`queue_dm(` 只能出現在 `bot.py`、dispatcher 與管理員專用的 `services/memory_manager.py`；傳入頻道參數的字串字面值必須是註冊表 key（拼錯的 key 會被 `is_notification_enabled` 當成未知 key 回傳 True，等於無聲繞過開關）。`test_kv_cache_dedup_whitelist.py` 亦掃描 `dedup_key=` 引數。
- **每使用者設定快取**：`get_user_notification_settings()`／`is_notification_enabled()` 讀取每使用者的整份設定快取（寫入完成後立即失效，TTL 60 秒涵蓋藍綠部署期間其他程序的寫入）。熱路徑以「使用者 × 標的」為單位查詢開關時不再每次開 SQLite 連線；`get_notification_settings_many()` 以單一 `IN (...)` 查詢預熱多位使用者。
- **Polymarket 參數分離**：非布林帳戶設定（`polymarket_threshold`、`polymarket_use_llm`、`polymarket_slippage`）乾淨地放在 `/settings`（`AccountSettingsView`），讓 `/notif_settings` 保持純粹、專注於通知頻道。
- **100% 向後相容別名引擎**：使用舊版鍵值（如 `hb_options_structure`、`ddp_alert`、`profit_lock_alert`、`wti_oil_alert`、`oil_alert`、`defense_portfolio_risk`）查詢或更新時，會透過 `LEGACY_KEY_ALIASES` 透明解析至新的鍵值。

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

- `nexus_core/database/notification_channels.py`：通知頻道註冊表（`CHANNELS`、`NotificationKey` Literal、衍生的 key 清單／預設值／預設情境／UI 分組）
- `nexus_core/services/notification_dispatcher.py`：`notify()`／`notify_many()`／`is_channel_enabled()` 集中推播入口
- `nexus_core/cogs/settings_ui.py`：`AccountSettingsView`、`AccountSettingsModal`、`TradingStrategySelectView`、`PortfolioModeSelectView`
- `nexus_core/cogs/terminal.py`：`/settings`、`/notif_settings` 指令入口
- `nexus_core/ui/watchlist_tags.py`：自選股標籤系統
- `nexus_core/database/migrations/v068_add_trading_strategy.py`：`trading_strategy` 欄位遷移
- `nexus_core/database/migrations/v061_consolidate_notification_settings.py`：通知偏好 4 模組頻道整併遷移（當時為 13 頻道）
- `nexus_core/database/migrations/v078_backfill_advisory_entry_signal.py`：以 `heartbeat_symbol_deep` 回填 `advisory_entry_signal`
- `nexus_core/database/migrations/v081_split_notification_channels.py`：依下行風險影響拆分頻道並以母頻道回填
- `nexus_core/tests/unit/test_rollover_channel_routing.py`：轉倉情境／出場分層 → 頻道對照的窮舉測試
- `nexus_core/database/migrations/v079_add_portfolio_mode.py`：`portfolio_mode` 欄位遷移
- `nexus_core/database/cache.py`：`_KV_CACHE_DEDUP_KEY_PREFIXES` 每日去重旗標清理白名單
- `nexus_core/tests/unit/test_kv_cache_dedup_whitelist.py`：AST 掃描強制新增去重旗標必須登記白名單
- `nexus_core/tests/unit/test_settings_interactive.py`：互動設定視圖與 Modal 單元測試
- `nexus_core/tests/unit/test_notification_toggles.py`：通知偏好資料庫開關與視圖單元測試
- `nexus_core/tests/unit/test_notification_dispatcher.py`：推播順序（開關 → 去重 → 入列 → 旗標）、設定快取與註冊表衍生值
- `nexus_core/tests/unit/test_notification_dispatch_centralization.py`：AST 強制推播集中化與頻道 key 合法性
