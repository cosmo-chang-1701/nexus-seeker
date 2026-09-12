# 🌌 Nexus Seeker - AGENTS.md

## Project Overview

Nexus Seeker is a multi-tenant **Discord-first options risk-control and trading operations platform**. It combines technical structure, Black-Scholes-Merton pricing, Greeks-based portfolio risk, event-aware calendar defenses, and LLM-assisted structured commentary.

Current released core version: **`1.13.29`**


The codebase is optimized for:

- **low-RAM VPS deployment**
- **persistent Discord DM delivery**
- **field-based, centralized embed output**
- **SQLite-first caching for recurring event data**

---

## Current Runtime Architecture

### Services

1. **`nexus_core`**
   - Main Discord bot
   - Owns all slash commands, background schedulers, embeds, portfolio/risk logic, watchlist heartbeat, and DM queueing

2. **`nexus_edge_scraper`**
   - Optional FastAPI + Playwright edge service
   - Used for Reddit RSS fetching, macro data fetching, and SEC structured section extraction (`section_extractor.py`) without exposing the bot runtime directly.
   - **Graceful Degradation Proxy**: Acts as a local proxy tunnel for `yfinance` requests (history K-lines, options expiries, options chains) to seamlessly bypass datacenter IP blocks (e.g., DigitalOcean) when Yahoo Finance triggers HTTP 403/429.

### Important Runtime Distinction

- **Watchlist 15 分鐘心跳** is currently emitted by `cogs/trading.py` via `SchedulerCog.dynamic_market_scanner()`
- **Analyst Agent** is a separate report family in `cogs/analyst_agent.py`
- `market_analysis/intraday_pipeline.py` currently serves as the **shared watchlist evaluation / option-plan / engine helper module**, and also contains the reusable `IntradayScanPipeline` class and gamma squeeze engine logic

Do **not** assume that enabling Analyst Agent is required for the watchlist heartbeat; in current code, those are separate paths. Note there are in fact **two** watchlist push loops (15-minute radar via `cogs/trading/heartbeat.py`, and the 30-minute `標的分析中心 2.0` heartbeat via `IntradayScanPipeline`) that share no data path — see the full comparison table (data source, cadence, embed, `/notif_settings` channel) in [`docs/architecture/01_dual_watchlist_pipelines.md`](docs/architecture/01_dual_watchlist_pipelines.md) before touching either.

---

## Key Technologies

- **Language:** Python 3.12
- **Discord framework:** `discord.py`
- **Edge API:** `FastAPI`
- **Validation:** `Pydantic v2`
- **Type checking:** `mypy`
- **Market data:** `finnhub-python`, `yfinance`, `pandas-ta`, `py_vollib`
- **Quant stack:** `numpy`, `pandas`, `scipy`
- **AI / LLM:** OpenAI-compatible API with structured `pydantic` outputs
- **Persistence:** SQLite + migration engine + event caches
- **Infra:** Docker / Docker Compose / optional Cloudflare Tunnel
- **Quality:** `ruff`, `pre-commit`, `semgrep`, containerized `pytest`

---

## Active Background Jobs

### In `cogs/trading/`

- `kv_cache_dedup_purge` — **03:00 ET** (off-peak; deletes stale one-shot daily anti-spam dedup flags in `kv_cache` — see `database.cache.purge_stale_kv_cache_dedup_keys` — past a 3-day retention window; scoped to a whitelist of known dedup-only key prefixes so permanent caches/config are never touched)
- `fundamental_filing_scan` — **08:00 ET** (holdings-only, skips non-trading days)
- `daily_reddit_update` — **08:30 ET**
- `pre_market_risk_monitor` — **08:45 ET** (staggered pre-warming of quant metrics, IV, Max Pain & Squeeze before 09:00 Analyst Agent)
- `dynamic_market_scanner` — **every 15 minutes (:00, :15, :30 & :45) during market hours**
- `wti_oil_monitor` — **every 30 minutes (24/7, 00:00–06:00 ET quiet hours)**
- `price_volume_alert_monitor` — **every 15 minutes during market hours** (with `Semaphore(3)` concurrent K-line bar retrieval)
- `monitor_real_portfolio_task` — **every 15 minutes (:05, :20, :35 & :50) during market hours** (staggered 5 minutes after dynamic scanner to consume shared in-memory radar cache)
- `dynamic_after_market_report` — **16:15 ET**
- `weekly_vtr_report_task` — **Friday 17:05 ET**

### In `cogs/calendar.py`

- `event_checker` — **every 4 hours** (major events check & periodic FedWatch probability auto-update)

### In `cogs/analyst_agent.py`

- `pre_market_loop` — **30 minutes before market open** (09:00 ET)
- `post_market_loop` — **post-market report flow**

### In `bot.py`

- persistent DM queue worker
- health worker
- memory manager start/stop
- hedge monitor start/stop
- polymarket service start/stop

---

## Business Logic & Feature Reference

以下所有量化模型、交易策略、風控引擎與平台功能的詳細規格，SSOT 已整併至 `docs/`（見 [`docs/README.md`](docs/README.md) 的完整索引與跨模組導讀路徑）。本節僅列出「主題 → 文件」對照，供貢獻者快速定位；**新增或修改功能時，請更新對應的 docs/ 頁面，而不是把敘事堆進本檔案。**

### 交易策略與進出場 (`docs/strategies/`)
- 4-Regime 市場環境動態路由矩陣、右側動能六重鐵律、左側均值回歸六重鐵律、Trading Strategy Modes（`RIGHT_SIDE`/`LEFT_SIDE`/`DYNAMIC`，`user_settings.trading_strategy`）→ [`01_regime_routing_matrix.md`](docs/strategies/01_regime_routing_matrix.md)、[`02_right_side_momentum_ironclad.md`](docs/strategies/02_right_side_momentum_ironclad.md)、[`03_left_side_mean_reversion_ironclad.md`](docs/strategies/03_left_side_mean_reversion_ironclad.md)
- Dynamic Rollover Engine 八大情境（Fundamental Thesis／Opportunity Cost／Core-Satellite Rebalance／Margin Defense／Core Deployment／Macro Top-Escape／Covered Call Profit-Lock／Transition Engine）、DTE 三態機、`/stress_test` 現金赤字精算 → [`04_dynamic_rollover_state_machine.md`](docs/strategies/04_dynamic_rollover_state_machine.md)
- 雙軌防洗盤動態停損與微觀結構出場決策矩陣（SL-結構失效／SL-狀態翻轉／SL-主力對沖／SL-動態保本／TP1-TP3）→ [`05_dual_track_anti_washout_stop_loss.md`](docs/strategies/05_dual_track_anti_washout_stop_loss.md)
- Watchlist 心跳所依賴的 Relative Strength 公式、ExecutionRouter、Skew Divergence Gate、Momentum Vector Gate，以及 Event-Driven Market Scenario Alerts（巨鯨護航共振等六大情境）亦記載於本系列文件。

### 做市商微觀結構與訂單流 (`docs/microstructure/`)
- Net GEX 拓撲、三階牆體、底牆物理約束（$K < \text{Spot}$）→ [`01_gex_topology_and_walls.md`](docs/microstructure/01_gex_topology_and_walls.md)、[`02_wall_physical_constraints.md`](docs/microstructure/02_wall_physical_constraints.md)
- Gamma Flip 翻轉線估算、Index Microstructure 大盤 Regime（`SHORT_GAMMA_CRITICAL`）與其快取降級策略 → [`03_gamma_flip_estimation.md`](docs/microstructure/03_gamma_flip_estimation.md)
- UOA 權利金排序、`paced_ratio` 時段正規化、SWEEP/BLOCK/CROSS 分類 → [`04_uoa_notional_and_paced_ratio.md`](docs/microstructure/04_uoa_notional_and_paced_ratio.md)
- Volume Profile／暗池 DP-POC 代理、Gamma Squeeze Engine 與 SPEAR 進攻訊號 → [`05_volume_profile_and_dp_poc.md`](docs/microstructure/05_volume_profile_and_dp_poc.md)、[`06_gamma_squeeze_engine_and_spear.md`](docs/microstructure/06_gamma_squeeze_engine_and_spear.md)

### 定價模型與波動率策略 (`docs/valuation_pricing/`)
- TDP 估值三擊／DDP 雙重折價定價模型 → [`01_tdp_valuation_model.md`](docs/valuation_pricing/01_tdp_valuation_model.md)
- 預期波幅（EM）與多 DTE 最大痛點重力過濾體系 → [`02_expected_move_and_max_pain.md`](docs/valuation_pricing/02_expected_move_and_max_pain.md)
- Skew 25-Delta 計算、百分位統計視窗與樣本邊界防護（`SKEW_D25`、`_MIN_PERCENTILE_SAMPLES`、midrank）、Volume PCR 瀑布流背離、三重結構性風險合流、Deterministic Skew Interpretation 分支順序表 → [`03_skew_pcr_divergence_confluence.md`](docs/valuation_pricing/03_skew_pcr_divergence_confluence.md)
- IVR 波動率位階、做市商負 Gamma 賣方禁售閘門、Pre-Market IV Sentiment Scan 降級與 1.4x 事件加載係數揭露 → [`04_ivr_regime_and_seller_lockout.md`](docs/valuation_pricing/04_ivr_regime_and_seller_lockout.md)

### 投資組合風控與數學模型 (`docs/risk_portfolio/`)
- Beta 加權 Delta 與二階 Gamma 曝險推導 → [`01_beta_weighted_greeks.md`](docs/risk_portfolio/01_beta_weighted_greeks.md)
- VIX 戰情階梯與動態分數凱利公式 → [`02_vix_battle_ladder_and_kelly.md`](docs/risk_portfolio/02_vix_battle_ladder_and_kelly.md)
- AROC 資本效率硬鎖閘門 → [`03_aroc_capital_efficiency.md`](docs/risk_portfolio/03_aroc_capital_efficiency.md)
- DITM 凸性防護與獲利鎖定、Covered Call Unlock Recovery Rules → [`04_ditm_convexity_profit_lock.md`](docs/risk_portfolio/04_ditm_convexity_profit_lock.md)
- 財務生存跑道與 Theta 現金流防禦緩衝模型 → [`05_financial_runway_and_liquidity.md`](docs/risk_portfolio/05_financial_runway_and_liquidity.md)
- 對沖績效 Brinson 歸因與動態 Tau 自我進化閉環 → [`06_brinson_performance_attribution.md`](docs/risk_portfolio/06_brinson_performance_attribution.md)

### 總體經濟、事件日曆與輿情預測 (`docs/macro_sentiment/`)
- 宏觀逃頂推演矩陣、CME FedWatch、流動性退潮防禦 → [`01_macro_escape_top_matrix.md`](docs/macro_sentiment/01_macro_escape_top_matrix.md)
- SEC 財報護城河自動掃描器（Dynamic Rollover Scenario 1 的每日排程路徑）→ [`02_sec_filing_moat_scanner.md`](docs/macro_sentiment/02_sec_filing_moat_scanner.md)
- WTI 原油期貨 24/7 監控與板塊衝擊矩陣 → [`03_wti_crude_oil_monitor.md`](docs/macro_sentiment/03_wti_crude_oil_monitor.md)
- StockAliasMatrix（4 層文本比對別名 + 4 層快取解析架構）、Polymarket VWBP 加權勝率、雙頁籤輿情共振雷達 → [`04_polymarket_vwbp_sentiment_radar.md`](docs/macro_sentiment/04_polymarket_vwbp_sentiment_radar.md)

### 系統運行管線與工程規範 (`docs/architecture/`)
- 雙自選標的心跳管線架構與排程隔離（15 分鐘雷達 vs 30 分鐘「標的分析中心 2.0」，兩條完全獨立的推播路徑與 `/notif_settings` 頻道）→ [`01_dual_watchlist_pipelines.md`](docs/architecture/01_dual_watchlist_pipelines.md)
- 盤前 08:45 預熱與 SQLite Cache-Aside、Quantitative Radar Terminal 零延遲規則引擎 → [`02_pre_market_cache_aside.md`](docs/architecture/02_pre_market_cache_aside.md)
- 雙服務架構（`nexus_core` / `nexus_edge_scraper`）與三階式降級代理 → [`03_dual_service_and_proxy.md`](docs/architecture/03_dual_service_and_proxy.md)
- Discord 防爆分頁、`Semaphore(3)` 併發控制、跨模組共享雷達快取（`bot._latest_radar_data_cache`）等高效能背景排程規範 → [`04_engineering_standards.md`](docs/architecture/04_engineering_standards.md)

### 平台工程與使用者體驗 (`docs/platform/`)
- Analyst Agent 報告排程（盤前財報、盤後綜合結算）→ [`01_analyst_agent_reporting.md`](docs/platform/01_analyst_agent_reporting.md)
- 委託單管理與遙測定價對齊引擎 → [`02_order_management_and_telemetry.md`](docs/platform/02_order_management_and_telemetry.md)
- 互動設定與通知偏好中心（4 模組 13 頻道）→ [`03_notification_center.md`](docs/platform/03_notification_center.md)
- 事件日曆架構與宏觀事件翻譯引擎 → [`04_calendar_translation_engine.md`](docs/platform/04_calendar_translation_engine.md)
- Embed 渲染架構（`NexusEmbed`、輸出集中化）與 DM 佇列投遞層 → [`05_embed_architecture_and_dm_queue.md`](docs/platform/05_embed_architecture_and_dm_queue.md)
- 個股 15 分鐘價量突破警報系統 → [`06_price_volume_alert_system.md`](docs/platform/06_price_volume_alert_system.md)

---

## Core Modules to Know

- `nexus_core/bot.py` — bot bootstrap, DM queue, service lifecycle
- `nexus_core/cogs/trading.py` — active runtime scheduler and watchlist heartbeat sender
- `nexus_core/cogs/trading/wti_monitor.py` — 24/7 background WTI crude oil price monitor loop
- `nexus_core/cogs/trading/price_volume_alert_monitor.py` — 15-minute, market-hours-only background price-volume breakout monitor loop
- `nexus_core/cogs/trading/fundamental_filing_monitor.py` — daily (08:00 ET) automated SEC filing scanner for holding-only symbols, routing new 10-K/10-Q/8-K filings through the form-type-aware Dynamic Rollover Scenario 1 pipeline
- `nexus_core/cogs/analyst_agent.py` — analyst report scheduler and dispatcher
- `nexus_core/cogs/order_ui.py` — active orders entrypoints
- `nexus_core/cogs/order_views.py` — interactive list views and telemetry alignment buttons
- `nexus_core/cogs/order_modals.py` — cancellation/adjustment modals
- `nexus_core/cogs/settings_ui.py` — interactive account, notification settings views, `TradingStrategySelectView` (交易策略 3 選 1 選單), and WtiConfigModal
- `nexus_core/cogs/terminal.py` — terminal command entrypoints (including settings, runway analysis, and `/wti_config`)
- `nexus_core/cogs/unified_terminal/` — modular trader terminal and radar hubs (`cog.py`, `symbol_view.py`, `portfolio_view.py`, `batch_scan_view.py`, `pulse_view.py`, `utils.py`)
- `nexus_core/cogs/calendar.py` — upgraded macro and earnings calendar command with event caching
- `nexus_core/cogs/cc_recovery.py` — filter and display optimal OTM Covered Call contracts
- `nexus_core/cogs/embed_builders/` — single source of truth for embeds (`embed_builder.py` is shim)
- `nexus_core/cogs/intelligence.py` — Market Intelligence & Edge Detection Terminal (news, reddit, polymarket)
- `nexus_core/cogs/hedging.py` — automated hedging tracking and settlement interface
- `nexus_core/database/orders.py` — active orders SQLite database state CRUD operations
- `nexus_core/database/wti_config.py` — WTI alert user configuration model and kv_cache CRUD
- `nexus_core/database/migrations/v038_add_active_orders.py` — migration registering the active_orders table in SQLite
- `nexus_core/database/migrations/v047_remediate_missing_structures.py` — migration remediating/adding economic calendar columns consensus_value and fedwatch_probability
- `nexus_core/database/migrations/v048_add_escape_window_settings.py` — migration adding escape window configuration columns to user settings
- `nexus_core/database/migrations/v062_add_fundamental_scan_state.py` — migration registering the fundamental_scan_state table, the dedup cursor (per-symbol last analyzed accession_number) used by the automated daily SEC filing scanner
- `nexus_core/database/migrations/v068_add_trading_strategy.py` — migration adding `user_settings.trading_strategy` (交易策略模式，預設 `RIGHT_SIDE` 以維持既有行為不變)
- `nexus_core/database/migrations/v070_split_heartbeat_symbol_deep.py` — migration backfilling `heartbeat_symbol_deep` from each user's existing `heartbeat_watchlist` value when the two heartbeat channels were split, so anyone who had muted the shared toggle is not silently re-subscribed by the new key's `True` default
- `nexus_core/database/migrations/v072_remove_margin_buying_power.py` — migration dropping deprecated `option_buying_power` and `margin_used` manual reference columns from `user_settings`
- `nexus_core/market_analysis/macro_calendar_translator.py` — Macro calendar 150+ translation dictionary & dynamic Fed speech parsing engine
- `nexus_core/market_analysis/wti_analysis.py` — WTI crude oil technicals, energy correlation, and event analysis engine
- `nexus_core/market_analysis/intraday_pipeline.py` — watchlist evaluation, option-plan logic, intraday engine helpers
- `nexus_core/market_analysis/index_microstructure.py` — market regime determination (SHORT_GAMMA_CRITICAL) using VIX, VIX3M, and zero-gamma line GEX
- `nexus_core/market_analysis/sentiment_engine.py` — Facade entrypoint for skew / UOA / IV stack
- `nexus_core/market_analysis/sentiment/` — Dedicated submodules (`iv_metrics`, `max_pain`, `options_flow`, `uoa_detector`, `history_storage`, `cache`, `skew_taxonomy`)
- `nexus_core/market_analysis/sentiment/skew_taxonomy.py` — leaf module (stdlib-only, so it can be imported from both `sentiment` and `intraday_pipeline` without a cycle) holding the single source of truth for the Skew history-indicator key (`SKEW_D25`), the 80/20 classification thresholds, and the two extreme state strings
- `nexus_core/market_analysis/telemetry_pricing_engine.py` — central alignment alert pipeline and decision gating logic (stale-lock, deep sea gap limits, pure stock gate, UOA squeeze classification)
- `nexus_core/risk_engine/nro.py` — WatchlistRiskController translating technical status to SDDM tactical routes (SHIELD, SPEAR, STANDBY)
- `nexus_core/formatters/execution_embeds.py` — embeds formatter separating execution decision view logic
- `nexus_core/market_analysis/ghost_trader.py` — GhostTrader Virtual Trading Room execution and monitoring logic
- `nexus_core/services/calendar_service.py` — shared event cache entrypoint
- `nexus_core/services/llm_service.py` — structured LLM outputs and memory-safe degradation
- `nexus_core/services/trading_service.py` — scan / report / validation data orchestration
- `nexus_core/services/telemetry_pricing_engine.py` — dynamic telemetry pricing calculation covering Max Pain, EM, Skew, IV Spikes, and psychological round numbers
- `nexus_core/services/polymarket_service.py` — Polymarket whale tracking, VWBP aggregation, and AI summary service
- `nexus_core/services/order_telemetry_service.py` — Order telemetry scanning service
- `nexus_core/database/notifications.py` — custom user notification preferences database operations
- `nexus_core/database/virtual_trading.py` — Database interface for virtual trades (VTR)
- `nexus_core/market_analysis/dynamic_rollover/` — Dynamic rollover engine package (facade `__init__.py` + `fundamental_thesis.py` / `opportunity_cost.py` / `anti_washout.py` / `margin_defense.py` / `structural_signals.py` (also houses the DTE three-tier state machine, `evaluate_option_dte_tier`) / `covered_call_profit_lock.py` / `inverse_hedge.py` (Scenario 4's third `target_core` destination: symbol→inverse-ETF resolution + pure-spot momentum confirmation) / `core_deployment.py` (Scenario 5 + Covered Call Overlay) / `macro_top_escape_defense.py` (Scenario 6: probabilistic leading-indicator defense, dispatched last in the evaluation order) / `left_side_entry.py` (左側交易六重鐵律) / `regime_classifier.py` (動態調整 4 態盤勢分類器) / `transition_engine.py` (動態調整狀態切換引擎) / `models.py` / `constants.py`), anti-washout stop engine, and asset class bifurcation logic. Public import path stays `market_analysis.dynamic_rollover`.
- `nexus_core/market_analysis/signal_calculator.py` — Dynamic trading signal calculator (1.5x ATR buffers, capital allocation models)
- `nexus_core/market_analysis/scenario_classifier.py` — Event-driven quantitative scenario classifier (6 market scenarios including Whale Escort Resonance)
- `nexus_core/database/watchlist.py` — Database CRUD operations for user watchlist symbols (100% deterministic rule-based zero-LLM architecture)
- `nexus_core/database/migrations/v039_add_notification_toggles.py` — migration registering the user_notification_settings table in SQLite
- `nexus_core/tests/unit/test_db_write_centralization.py` — AST 掃描強制「單一寫入者」不變式：除 `database/connection.py` 與 `database/core.py` 外，不得出現 `sqlite3.connect`、`conn.commit()`，或把連線當成 context manager 使用
- `nexus_core/tests/unit/test_left_side_entry.py` — unit tests for the left-side six-rule entry gate (per-condition pass/fail/fail-safe, candle-pattern primitive, confirmed-bar guard)
- `nexus_core/tests/unit/test_regime_classifier.py` — unit tests for the 4-regime classifier boundaries and its fail-safe default to Regime II
- `nexus_core/tests/unit/test_transition_engine.py` — unit tests for the four transition paths, entry-bar-low capture, and the anti_washout coordination/Track-2 universality guards
- `nexus_core/tests/unit/test_wti_alert.py` — unit tests for WTI crude oil price alert system, technicals, and embed rendering
- `nexus_core/tests/unit/test_fundamental_filing_monitor.py` — unit tests for the automated daily SEC filing scanner (dedup cursor, is_broken dispatch gating, per-user notification toggle, multi-holder symbol dedup)
- `nexus_core/tests/unit/test_edge_detection_sentiment.py` — unit tests for Edge Detection, Reddit sentiment classification, VWBP, and dual-tab layout
- `nexus_core/tests/unit/test_intraday_pipeline.py` — heartbeat and phase-B gating tests
- `nexus_core/tests/unit/test_embed_builder.py` — embed contract tests
- `nexus_core/tests/unit/test_output_centralization.py` — embed-centralization enforcement
- `nexus_core/tests/unit/test_order_ui.py` — unit tests for order UI, active order database, and telemetry pricing alignment
- `nexus_core/tests/unit/test_settings_interactive.py` — unit tests for interactive settings view and modals
- `nexus_core/tests/unit/test_notification_toggles.py` — unit tests for notification preferences database toggles and views
- `nexus_core/tests/unit/test_macro_risk_upgrade.py` — unit tests for macro risk upgrade, index microstructure, and covered call unlocking
- `nexus_core/tests/unit/test_telemetry_pricing_engine.py` — unit tests for telemetry pricing alignment pipeline and gating
- `nexus_edge_scraper/section_extractor.py` — SEC filings structured section extraction module

---

## Development Conventions

### User-facing output

- All user-facing strings should be **Traditional Chinese**
- Private settings / sensitive account operations should use `ephemeral=True`

### Database changes

- Never edit schema manually
- Add a migration file in `nexus_core/database/migrations/`
- 遷移模組必須匯出 **`version` / `description` / `sql` 三個模組層級屬性**。
  `database/core.py::get_migrations()` 只收錄同時具備這三者的模組，其餘一律**無聲跳過**
  （不會 log、不會報錯）。`upgrade(cursor)` / `run(conn)` 這類函式介面不會被執行——
  `v054_add_cro_risk_settings.py` 與 `v057_fundamental_cache.py` 就是這樣從未套用過。

### SQLite 連線與寫入架構（單一寫入者）

**沒有任何模組可以自己開連線寫入。** 這條不變式由
`tests/unit/test_db_write_centralization.py` 以 AST 掃描強制（比照
`test_output_centralization.py` 的作法），白名單只有 `database/connection.py`
與 `database/core.py`（migration 執行器）。

過去這條不變式被 ~35 個函式破壞：它們各自 `sqlite3.connect(config.DB_NAME)`
（預設 5 秒 busy timeout、無任何 PRAGMA）然後 `conn.commit()`，與寫入佇列的
連線互搶 WAL 寫入鎖，正是 production 上 `database is locked` 的來源。

- **連線一律經 `database/connection.py::connect_db()`**（`get_read_connection()`
  是它的別名）。單一 `_BUSY_TIMEOUT_MS` 旋鈕取代先前散落的 5s / 15s / 30s 三種值。
  `journal_mode=WAL` **只在 `run_migrations()` 設定一次**（它寫在資料庫檔頭、是
  持久設定），不要在每條連線重設——熱路徑每秒會開關數十條連線。
- **寫入入口**（全部在 `database/connection.py`）：
  | 入口 | 用途 |
  |---|---|
  | `execute_write_async` / `execute_write` | 單一語句 |
  | `execute_write_rowcount(_async)` | 需要「影響筆數」時。`execute_write` 回傳 `lastrowid or rowcount or True`，DELETE 命中 0 筆會回傳 `True`，無法與成功區分 |
  | `execute_write_many(_async)` | 多語句共用一個交易、只 commit 一次；statement 為 `(query, params)` 或 `(query, seq, True)`（走 `executemany`）。回傳逐語句 rowcount |
  | `run_maintenance()` | WAL checkpoint + `PRAGMA optimize`，由 03:00 ET 離峰排程呼叫 |
- **寫入 worker 跑在專屬執行緒 `nexus-db-writer` 上，不是 event loop 的 task。**
  早期版本用 `loop.create_task(_worker_loop())`，而 `_process_task` 的 `"sql"`
  分支裡沒有任何 `await`——`cursor.execute()` / `conn.commit()` 是同步 C 呼叫，
  一旦 SQLite 回 SQLITE_BUSY，busy handler 就在 Discord gateway 的執行緒上睡滿
  整個 busy timeout，直接觸發「heartbeat blocked for more than N seconds」，
  逾時後再拋 `database is locked`。**不要把任何 SQLite 呼叫搬回 event loop。**
- **worker 內不得有網路 I/O。** 它是全程序唯一且序列化的寫入者，任何網路等待都會
  head-of-line 卡住全程序所有 `execute_write_async`。`save_historical_iv` 的 HV
  fallback 就是因此移到呼叫端（`history_storage.py`）先解析完才入列。
- **不要在持有寫入交易的狀態下 `await`。** `refresh_portfolio_greeks` 曾在一個開啟
  的連線內逐筆 `await get_option_chain_mid_iv(...)` 再於迴圈結束後 commit，等於在
  N 次網路往返期間持續持有寫入鎖。作法：先把結果蒐集到記憶體，最後一次性批次寫入。
- **同步寫入函式只能從非 event loop 執行緒呼叫。** `put_task_sync()` 的守衛會對
  event loop 執行緒直接拋 `RuntimeError`；呼叫端請包 `asyncio.to_thread(...)`。
  ⚠️ 這道守衛在 pytest 下**不會觸發**（測試環境佇列未啟用，走
  `_execute_direct_write` 直寫捷徑），因此這類缺陷只在 production 顯形——寫入函式的
  `except` 務必留 `logger.error`，否則會全靜默失效。AST 強制測試就是為了補上這個
  測試環境看不見的破口。
- **讀取也不該阻塞 event loop。** 熱路徑請把多次讀取**合併**成一次
  `asyncio.to_thread` 呼叫，而不是每個讀取各包一次（那只是把數百次 connect 搬到
  別的執行緒）。既有範例：`database/cache.py::get_kv_cache_many()`（單一連線、單一
  `key IN (...)` 查詢）、`cogs/unified_terminal/radar_data.py::_load_symbol_caches()`
  （一次取齊 kv_cache / market_cache / squeeze_cache）、
  `database/portfolio.py::get_all_portfolio_symbol_pairs()`（取代 O(使用者 × 標的)
  的 `is_symbol_in_portfolio()` 巢狀查詢）。
- **讀取函式不得有寫入副作用。** `get_user_portfolio()` / `get_all_portfolio()` 曾在
  開頭呼叫 `archive_expired_portfolio_records()`（全表掃描的歸檔寫入），等於每次讀取
  持倉都取得一次寫入鎖，而 15 分鐘心跳每輪都會踩到。該歸檔已移至 03:00 ET 離峰排程。
- **`with sqlite3.connect(...) as conn:` 是錯的。** sqlite3 的 context manager 只
  commit/rollback，**不會關閉連線**。一律用 `try/finally: conn.close()`。
- **跨程序競爭無法用佇列消除。** 藍綠部署期間會有兩個容器並存掛載同一個 DB volume，
  因此 worker 內建 SQLITE_BUSY 的 jittered 指數退避重試；`busy_timeout` 與退避重試
  是一等公民設計，不是備援。

### Memory / VPS safety

- prefer `BoundedCache` for recurring hot data
- strictly gate background tasks and LLM workflows with `is_memory_safe()` (85% RAM memory gate)
- keep all features safe for 1GB RAM deployment

### Type safety

- prefer explicit Pydantic models / aliases over loose dicts
- keep literal types consistent with model fields
- avoid `Any` unless truly unavoidable at integration boundaries
- **Strict Annotations for Empty Collections**: Always provide explicit type annotations when initializing empty collections (e.g. `_my_set: set[str] = set()`, `_my_list: list[str] = []`, `_my_dict: dict[str, Any] = {}`).
- **Union & Nullability Safety**: Always perform explicit check-guards (e.g. `if obj is not None:`) before accessing properties on optional/nullable objects (like `interaction.message` or `self.view` on Discord items) to avoid Mypy `union-attr` check failures.
- **Dynamic Property Reflection**: Use safe dynamic helpers `getattr(obj, "attr", default)` or `setattr(obj, "attr", val)` when passing or querying dynamic custom states across UI components (e.g. tracking pre-selected states in views before triggering modals).
- **Mypy Exclusion Configuration**: Stale build directories (`build/`, `dist/`) must be kept clean and explicitly ignored in `[tool.mypy]` `exclude` configuration under `pyproject.toml` to prevent build-pipeline duplicate scans.
- **型別自我檢測 (Pre-commit Type Check)**：Mypy 已開啟嚴格模式（Strict Mode），並遞迴檢查所有單元與整合測試模組。在提交程式碼前，開發人員應在包含完整依賴的 Docker 容器中手動跑一次全域型別檢查（在 `nexus_core` 目錄下執行 `docker compose run --rm nexus-seeker python -m mypy --config-file pyproject.toml .`），以確保所有第三方套件（如 `discord.py`）的型別解析正確無誤，避免型別錯誤進入遠端倉庫。

### Security

- use parameterized SQL
- avoid raw string interpolation in SQL execution

---

## Testing

Tests must be run from `nexus_core` inside Docker:

```bash
cd nexus_core
docker compose run --rm nexus-seeker python -m pytest tests
```

Useful focused runs:

```bash
cd nexus_core
docker compose run --rm nexus-seeker python -m pytest tests/unit/test_intraday_pipeline.py
docker compose run --rm nexus-seeker python -m pytest tests/unit/test_embed_builder.py
docker compose run --rm nexus-seeker python -m pytest tests/unit/test_output_centralization.py
docker compose run --rm nexus-seeker python -m pytest tests/unit/test_order_ui.py
docker compose run --rm nexus-seeker python -m pytest tests/unit/test_settings_interactive.py
docker compose run --rm nexus-seeker python -m pytest tests/unit/test_notification_toggles.py
docker compose run --rm nexus-seeker python -m pytest tests/unit/test_macro_risk_upgrade.py
```

---

## Deployment Notes

- `nexus_core/docker-compose.yml` currently defines the core bot service
- `nexus_edge_scraper/docker-compose.yml` defines the optional edge scraper + cloudflared sidecar
- production release flow is tag-driven (`v*`)
- pre-commit hooks run ruff lint/format, strict mypy, and general quality checks
- pre-push hooks run semgrep and dockerized tests (core-test and scraper-test)

---

## Documentation Guidance

- `docs/README.md` 是所有量化模型／交易策略／風控引擎／平台功能敘述的 SSOT；本檔案（`AGENTS.md`）只負責貢獻者導覽（服務架構、模組地圖）與工程慣例（DB／測試／型別／部署），**不應**再累積功能敘事或修復歷史。
- 新增或修改一項功能時：
  - 若屬於既有 29 篇規格書（`docs/{strategies,microstructure,valuation_pricing,risk_portfolio,macro_sentiment,architecture}/`）的既有主題，更新該檔案對應段落即可，並維持其強制的 6 段式結構（核心哲學／數學模型 LaTeX／Mermaid 決策圖／具名常數表／邊界條件／程式碼路徑）；修改後執行 `python3 scripts/verify_docs_integrity.py` 驗證。
  - 若屬於全新量化主題且值得獨立成篇，新增前請評估是否應納入 `scripts/verify_docs_integrity.py` 的 `EXPECTED_SPECIFICATIONS` 強制清單（需符合 6 段式模板）。
  - 若屬於 UX／排程／通知／委託單等非量化平台功能，寫入或更新 `docs/platform/` 下對應文件（格式較自由，但仍需維持繁體中文、不含 Docker/`.env`/quickstart 內容、內部連結有效），並在 [`docs/README.md`](docs/README.md) 的平台文件索引中加入條目。
- 撰寫任何文件時：
  1. 區分**實際執行流程**與輔助模組
  2. 明確區分 **watchlist 心跳**與 **Analyst Agent**（兩者是完全獨立的報告族群）
  3. 反映現行的欄位式（field-based）embed 格式
  4. 討論通知行為時提及持久化 DM 佇列
  5. `docs/` 保持功能／業務邏輯導向，`AGENTS.md` 保持貢獻者工作流程導向
  6. **純文件更新**（僅修改 `AGENTS.md`、`README.md`、或 `.md` 檔案）**不需要跑測試套件**，但若改動的是 `docs/` 下的 29 篇規格書或新增／修改 `docs/platform/` 文件，仍應手動執行一次 `python3 scripts/verify_docs_integrity.py`
