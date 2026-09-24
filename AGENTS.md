# 🌌 Nexus Seeker - AGENTS.md

## Project Overview

Nexus Seeker is a multi-tenant **Discord-first options risk-control and trading operations platform**. It combines technical structure, Black-Scholes-Merton pricing, Greeks-based portfolio risk, event-aware calendar defenses, and LLM-assisted structured commentary.

Current released core version: **`1.13.38`**


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

- `regime_outcome_labeler` — **03:30 ET** (leader-only, `is_memory_safe()` gated; back-fills forward price paths for `regime_evaluation_log` rows once 5 sessions have elapsed, then applies retention — see `services/regime_outcome_labeler.py`. Kept separate from the 03:00 job because it does per-symbol network fetches)
- `kv_cache_dedup_purge` — **03:00 ET** (off-peak; deletes stale one-shot daily anti-spam dedup flags in `kv_cache` — see `database.cache.purge_stale_kv_cache_dedup_keys` — past a 3-day retention window; scoped to a whitelist of known dedup-only key prefixes so permanent caches/config are never touched)。同一個任務另負責 `uoa_history` 的 10 個交易日保留期清理（`database.uoa_history.purge_stale_uoa_history`）、過期合約歸檔，以及 `sentiment_history`（60 交易日）／`sentiment_daily_canonical`（約 260 交易日）的保留期清理
- `fundamental_filing_scan` — **08:00 ET** (holdings-only, skips non-trading days)
- `daily_reddit_update` — **08:30 ET**
- `pre_market_risk_monitor` — **08:45 ET** (staggered pre-warming of quant metrics, IV, Max Pain & Squeeze before 09:00 Analyst Agent; first back-fills the previous trading day's `sentiment_daily_canonical` snapshot if the 16:15 run was missed; also warms the per-user portfolio downside-risk return series so the intraday drawdown check never fetches history — see `services/downside_risk_service.py`)
- `dynamic_market_scanner` — **every 15 minutes (:00, :15, :30 & :45) during market hours**
- `wti_oil_monitor` — **every 30 minutes (24/7, 00:00–06:00 ET quiet hours)**
- `price_volume_alert_monitor` — **every 15 minutes during market hours** (with `Semaphore(3)` concurrent K-line bar retrieval)
- `monitor_real_portfolio_task` — **every 15 minutes (:05, :20, :35 & :50) during market hours** (staggered 5 minutes after dynamic scanner to consume shared in-memory radar cache)
- `dynamic_after_market_report` — **16:15 ET** (maintenance, plus the daily `sentiment_daily_canonical` snapshot — see `market_analysis/sentiment/canonical_history.py` — and the portfolio downside-risk close job: rebuilds return series, writes the `portfolio_nav_daily` snapshot, evaluates drawdown tiers and CVaR budget → `risk_portfolio_downside`)
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
- Alpaca 即時 1 分 K 串流 start/stop（leader-only，預設關閉）

---

## Business Logic & Feature Reference

以下所有量化模型、交易策略、風控引擎與平台功能的詳細規格，SSOT 已整併至 `docs/`（見 [`docs/README.md`](docs/README.md) 的完整索引與跨模組導讀路徑）。本節僅列出「主題 → 文件」對照，供貢獻者快速定位；**新增或修改功能時，請更新對應的 docs/ 頁面，而不是把敘事堆進本檔案。**

### 交易策略與進出場 (`docs/strategies/`)
- 6-Regime 市場環境動態路由矩陣（含 Regime III-B 右側趨勢延續態）、右側動能六重鐵律、左側均值回歸六重鐵律、做空破位追空六重鐵律、Trading Strategy Modes（`RIGHT_SIDE`/`LEFT_SIDE`/`SHORT_SIDE`/`DYNAMIC`，`user_settings.trading_strategy`）→ [`01_regime_routing_matrix.md`](docs/strategies/01_regime_routing_matrix.md)、[`02_right_side_momentum_ironclad.md`](docs/strategies/02_right_side_momentum_ironclad.md)、[`03_left_side_mean_reversion_ironclad.md`](docs/strategies/03_left_side_mean_reversion_ironclad.md)、[`07_short_side_breakdown_ironclad.md`](docs/strategies/07_short_side_breakdown_ironclad.md)
- 動態自適應波動率空間門檻（單一權威演算法，取代先前散落 7 處的固定百分比；含緩衝雙邊界與破位追空次級節點空間）→ [`06_dynamic_adaptive_room_threshold.md`](docs/strategies/06_dynamic_adaptive_room_threshold.md)
  - ⚠️ **左側（`LEFT_SIDE`）本質是做多**——逆勢均值回歸、Put Wall 底牆接刀，條件三算的是「向上」回歸空間。`SHORT_SIDE` 才是唯一的空頭方向進場路徑。
- Dynamic Rollover Engine 十大情境（Fundamental Thesis／Opportunity Cost／Core-Satellite Rebalance／Margin Defense／Core Deployment／Macro Top-Escape／Covered Call Profit-Lock／Transition Engine／**Short Entry 做空進場訊號**／**Pyramid Add 順勢金字塔加碼**）、DTE 三態機、`/stress_test` 現金赤字精算 → [`04_dynamic_rollover_state_machine.md`](docs/strategies/04_dynamic_rollover_state_machine.md)
- 雙軌防洗盤動態停損與微觀結構出場決策矩陣（SL-結構失效／SL-狀態翻轉／SL-主力對沖／SL-動態保本／TP1-TP3）→ [`05_dual_track_anti_washout_stop_loss.md`](docs/strategies/05_dual_track_anti_washout_stop_loss.md)
- Watchlist 心跳所依賴的 Relative Strength 公式、ExecutionRouter、Skew Divergence Gate、Momentum Vector Gate，以及 Event-Driven Market Scenario Alerts（巨鯨護航共振等六大情境）亦記載於本系列文件。

### 做市商微觀結構與訂單流 (`docs/microstructure/`)
- Net GEX 拓撲、三階牆體、底牆物理約束（$K < \text{Spot}$）→ [`01_gex_topology_and_walls.md`](docs/microstructure/01_gex_topology_and_walls.md)、[`02_wall_physical_constraints.md`](docs/microstructure/02_wall_physical_constraints.md)
- Gamma Flip 翻轉線估算、Index Microstructure 大盤 Regime（`SHORT_GAMMA_CRITICAL`）與其快取降級策略 → [`03_gamma_flip_estimation.md`](docs/microstructure/03_gamma_flip_estimation.md)
- UOA 權利金排序、`paced_ratio` 時段正規化、SWEEP/BLOCK/CROSS 分類 → [`04_uoa_notional_and_paced_ratio.md`](docs/microstructure/04_uoa_notional_and_paced_ratio.md)
- Volume Profile (成交量分佈 / Volume-POC)、Gamma Squeeze Engine 與 SPEAR 進攻訊號 → [`05_volume_profile_and_dp_poc.md`](docs/microstructure/05_volume_profile_and_dp_poc.md)、[`06_gamma_squeeze_engine_and_spear.md`](docs/microstructure/06_gamma_squeeze_engine_and_spear.md)

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
- 下行風險評估體系（**Sortino 為主判讀指標**、MDD 與 VaR / CVaR 為輔；Sharpe／Calmar 只作回測描述，不得作為判讀或優化目標；減碼 B&H 對照組以下行差對齊）→ [`07_downside_risk_sortino_var_cvar.md`](docs/risk_portfolio/07_downside_risk_sortino_var_cvar.md)

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
- 回測校準工具（離線事件研究，只產報告不改參數）與前向蒐集管線（`regime_evaluation_log`、edge `gex_snapshot_history`）→ [`05_calibration_harness_and_forward_collection.md`](docs/architecture/05_calibration_harness_and_forward_collection.md)

### 平台工程與使用者體驗 (`docs/platform/`)
- Analyst Agent 報告排程（盤前財報、盤後綜合結算）→ [`01_analyst_agent_reporting.md`](docs/platform/01_analyst_agent_reporting.md)
- 委託單管理與遙測定價對齊引擎 → [`02_order_management_and_telemetry.md`](docs/platform/02_order_management_and_telemetry.md)
- 互動設定與通知偏好中心（依對 B&H 投組下行風險的影響分 6 模組 29 頻道；頻道註冊表 `database/notification_channels.py`、集中推播入口 `services/notification_dispatcher.py`）→ [`03_notification_center.md`](docs/platform/03_notification_center.md)
- 事件日曆架構與宏觀事件翻譯引擎 → [`04_calendar_translation_engine.md`](docs/platform/04_calendar_translation_engine.md)
- Embed 渲染架構（`NexusEmbed`、輸出集中化）與 DM 佇列投遞層 → [`05_embed_architecture_and_dm_queue.md`](docs/platform/05_embed_architecture_and_dm_queue.md)
- 個股 15 分鐘價量突破警報系統 → [`06_price_volume_alert_system.md`](docs/platform/06_price_volume_alert_system.md)
- Alpaca 即時 1 分 K 串流（動態訂閱、Forward Fill、資料完整性不變式、`get_quote` Tier 0、價量警報影子模式）→ [`07_alpaca_realtime_stream.md`](docs/platform/07_alpaca_realtime_stream.md)

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
- `nexus_core/cogs/settings_ui.py` — interactive account, notification settings views, `TradingStrategySelectView` (交易策略 4 選 1 選單), and WtiConfigModal
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
- `nexus_core/database/migrations/v068_add_trading_strategy.py` — migration adding `user_settings.trading_strategy` (交易策略模式，預設 `RIGHT_SIDE` 以維持既有行為不變；該欄位為 `TEXT DEFAULT` 且無 `CHECK` 約束，後續新增 `SHORT_SIDE` enum 值**不需要**新的 migration)
- `nexus_core/database/migrations/v070_split_heartbeat_symbol_deep.py` — migration backfilling `heartbeat_symbol_deep` from each user's existing `heartbeat_watchlist` value when the two heartbeat channels were split, so anyone who had muted the shared toggle is not silently re-subscribed by the new key's `True` default
- `nexus_core/database/migrations/v072_remove_margin_buying_power.py` — migration dropping deprecated `option_buying_power` and `margin_used` manual reference columns from `user_settings`
- `nexus_core/database/migrations/v074_add_previous_put_wall.py` — migration adding `market_cache.put_wall` / `previous_put_wall`, the mirror of `v069`'s call-wall pair. Without it the short-side TP2 "支撐牆向下遷移 >= 3%" branch is permanently dormant and silently falls back to the 1.5% break test
- `nexus_core/database/uoa_history.py` — UOA 歷史存取層。`kv_cache` 的 `uoa_{SYMBOL}` 是 upsert、只留最新快照，結構上無法回看；右側條件四的 Regime III-B 時間窗需要歷史，故另立 `uoa_history`。寫入掛在 15 分鐘心跳既有的 UOA 計算之後（零額外期權鏈抓取），保留期 10 個交易日由 03:00 ET 排程清理
- `nexus_core/database/migrations/v077_add_uoa_history.py` — migration 建立 `uoa_history`；去重鍵為 (symbol, 15m bar, expiry, strike, type, action)，避免多使用者共用標的時同一事實被重複記錄
- `nexus_core/database/migrations/v075_add_regime_evaluation_log.py` — migration adding `regime_evaluation_log` (every regime / entry-gate evaluation with the GEX numbers it actually used; no `user_id`, deduped per symbol/evaluator/source/15m bar) and `regime_evaluation_outcome` (direction-neutral forward path labels). GEX-dependent thresholds cannot be backtested because every GEX cache is an upsert — this is the only calibration data source for them
- `nexus_core/database/regime_evaluation_log.py` — access layer for the two tables above (single batched writes through `connection.py`)
- `nexus_core/market_analysis/evaluation_recorder.py` — hot-path forward-collection recorder: O(1) append into a bounded deque, records **only inside an `evaluation_source()` context** (so unit tests and ad-hoc scripts never pollute data), flushed once per cycle. Hooked into `classify_dynamic_regime`, the right/left gates and `evaluate_short_entry`
- `nexus_core/market_analysis/outcome_labeling.py` — single source of truth for forward-path labels (±k×ATR₁D first touch, same-bar double touch counts as adverse), shared by the production labeler and `calibration/`. ⚠️ `get_history_df` returns **tz-naive US/Eastern** indexes; this module localizes them as Eastern — treating them as UTC shifts intraday times 4–5 h and daily dates by one day
- `nexus_core/services/regime_outcome_labeler.py` — `run_outcome_labeling()`, invoked by the 03:30 ET scheduler task
- `nexus_core/market_analysis/downside_risk.py` — 下行風險指標的**單一權威**（numpy 葉模組）：全樣本分母下行差、Sortino（MAR = $R_f$）、MDD、歷史模擬 VaR／CVaR（樣本 < 60 回 `None`）。`calibration/backtest_engine_2025.py` 與日後任何績效／風險評估都必須呼叫它；`sharpe_ratio()` 僅供回測描述
- `nexus_core/market_analysis/downside_monitor.py` — 投組下行風險即時監控的純邏輯葉模組（快照、回撤階梯 10/15/20% 與 2.5pp 重新武裝、CVaR 預算 = risk_limit × 0.20 與尾部體制轉換、NAV 快照報酬還原），閾值皆 PRE_CALIBRATION；指標一律呼叫 `downside_risk.py`
- `nexus_core/services/downside_risk_service.py` — 投組下行風險 I/O：「現權重 × 一年歷史」模擬報酬序列（期權以原始 Delta 等值股數、缺值 ±0.5 近似）、08:45 預熱、盤中只走快取的回撤檢查、16:15 NAV 快照與 CVaR 判定；推播未送達時武裝狀態（`downside_state_` 前綴，刻意不在去重清理白名單）不前進
- `nexus_core/database/migrations/v082_add_portfolio_nav_daily.py` — migration 建立 `portfolio_nav_daily (user_id, date, nav, positions_json)`；日報酬以前一日持股計算，加減碼不被算成報酬。⚠️ 遷移執行器只套用大於目前最大版本者，必須在 `v081` 之後部署
- `nexus_core/market_analysis/kelly_priors.py` — stdlib leaf holding the direction-aware Kelly win-rate prior table (`LONG` / `SHORT`, structurally clamped so SHORT never exceeds LONG). Consumed by `ExecutionRouter` and SHORT_ENTRY sizing. Values are `PRE_CALIBRATION`
- `nexus_core/calibration/` — offline backtest calibration harness (`python -m calibration fetch|run|forward-report|all|micro-snapshot|micro-report|skew-proxy`; the last three are the D-03/D-04/Skew-threshold studies in `microstructure.py` / `edge_history.py` / `skew_proxy.py`, which only write through `data_store.py` / `report.py` and open the edge DB only via `database.connection.connect_external_readonly()`) 以及 2025 多資產動態轉倉回測引擎 (`backtest_engine_2025.py` / `scripts/run_rollover_backtest_2025.py`)：event study on price/VIX proxies + 2025 年 NVDA/SPY/GLD 全量轉倉回測與 forward-collection report。**Never edits code or writes the DB**; outputs `report.md` / `results.json` for human review. Run on a dev machine, not the VPS
- `nexus_core/market_analysis/macro_calendar_translator.py` — Macro calendar 150+ translation dictionary & dynamic Fed speech parsing engine
- `nexus_core/market_analysis/wti_analysis.py` — WTI crude oil technicals, energy correlation, and event analysis engine
- `nexus_core/market_analysis/margin.py` — 全資產類別保證金模型（`calculate_option_margin` 名稱沿用歷史）。空頭選擇權走既有公式，空頭**現貨**走 Reg-T 初始保證金（市值 × 50%）。⚠️ 其輸出經 `total_margin_used` 匯總成 `portfolio_heat`，是「是否允許開新倉」的主要煞車——任何一種空頭部位若在此回傳 0.0，該煞車對它就完全失效
- `nexus_core/market_analysis/gex_wall_depth.py` — 薄牆門檻（D-04）的 stdlib 葉模組：`thin_wall_threshold(adv) = max(500k, GEX_WALL_MIN_DEPTH_RATIO × ADV₂₀ × 100)`。GEX 原始值是每 100% 價格變動的尺度，固定 500k 對大型股形同虛設；正規化項只會讓門檻變嚴（小型股仍以 500k 為下限）。成交額經雷達資料的 `gex_profile_data["adv_dollar_20d"]` 傳入，缺值時行為與改版前相同。`index_microstructure.py` 重新匯出這些名稱
- `nexus_core/market_analysis/room_threshold.py` — **單一權威**的動態自適應波動率空間門檻實作（公式 A 方向性空間門檻／公式 B 牆體緩衝雙邊界／公式 C 破位追空次級節點空間／公式 D `resolve_effective_target()` 晴空萬里有效目標天花板，供 Regime IV 封頂判定、右側條件三、`PYRAMID_ADD` 條件四三處共用同一天花板定義）。刻意只依賴 stdlib 的葉模組（比照 `sentiment/skew_taxonomy.py`），故可同時被 `dynamic_rollover/`、`gamma_squeeze_engine.py` 與 `cogs/embed_builders/` 匯入而不產生循環相依。共用的是**演算法**而非常數值——各站點仍各自獨立呼叫，`constants.py` 的「路由層與進場確認層門檻不合併」政策不被破壞
- `nexus_core/market_time.py` — NYSE 行事曆 helper。`get_trading_days_ago_utc(n)` 回傳「往回第 n 個**已開盤**交易日」的 UTC 時戳，供 UOA 回看窗等時間窗過濾使用；以日曆日回看會讓同一個「N 日窗」在週末／連假前後代表的樣本量相差近一倍
- `nexus_core/market_analysis/intraday_consistency.py` — `/x` 日內資料一致性閘門（純函式、無 I/O）：以全市場 15m K 線放寬報價日高低點（Tier 0 IEX 高低點偏窄）、VWAP 必在當日區間內、15m K 棒凍結／多根合併偵測、期權成交價無套利下界。VWAP 與區間極值須出自同一份 K 線（`vwap_utils.fetch_session_stats()`）
- `nexus_core/market_analysis/atr_utils.py` — 共用 ATR helper：`fetch_atr_15m()`／`compute_atr_15m_from_df()`／`compute_atr_14_from_daily_df()`／`fetch_atr_1d()`（後者刻意不 `force_refresh`，日線 ATR 盤中幾乎不動）
- `nexus_core/services/alpaca_stream_service.py` — Alpaca 即時 1 分 K 串流（leader-only、`ENABLE_ALPACA_STREAM` 預設關閉）。訂閱前 30 檔（持倉 → 白名單內價量監測 → 其餘依前一交易日 IEX 成交筆數，`rank_symbols()`），每 15 分鐘比對差異；下游經模組層級 registry `get_stream_service()` 取得（**不要**在 `services/` 內 `import bot`）。⚠️ 串流資料只在 `complete_since` 涵蓋的區間內可用——斷線即清空，重連後以 REST 回補；`get_quote_snapshot()` 的 `pc` 是官方日線昨收，絕不能是上一分鐘收盤
- `nexus_core/market_analysis/stream_bars.py` — 串流的純 stdlib 計算葉模組：Forward Fill（不跨越開盤、缺口 > 30 分鐘不補）、`SessionStats`（開盤錨定 VWAP、只計真實 K 棒）、15 分 K 聚合、分鐘級技術指標
- `nexus_core/tests/unit/test_stream_bars.py` / `test_alpaca_stream_service.py` — Forward Fill／VWAP／聚合；認證才算連線、錯誤碼 402/405/406、盤前盤後丟棄、訂閱優先序與差異、Tier 0 的 `pc` 為昨收與新鮮度／完整性閘門、15 分 K 寬限期與覆蓋不足時回 None、REST 回補合併
- `nexus_core/market_analysis/intraday_pipeline.py` — watchlist evaluation, option-plan logic, intraday engine helpers
- `nexus_core/market_analysis/intraday_pipeline/entry_advisor.py` — 自選標的進場顧問核心：`evaluate_entry_advice()` 以與 `/x` 進場鐵律頁籤相同的策略／Regime 分派表做六重鐵律確認並附進場／停損／目標／盈虧比，結果以 `(strategy:symbol, 15m bar)` 記憶（鍵必含 strategy）。由 `pipeline.py::_dispatch_entry_advisor_alert` 以獨立頻道 `advisory_entry_signal` 推播（**不**新增 `scenario`、不覆寫 `tactical`、不受 `enable_analyst_agent` 約束）。⚠️ 呼叫點必須在 `engine_enabled` 的 `continue` 之前；三個乾跑旗標（`WATCHLIST_ADVISOR_DRY_RUN`／`REGIME_III_B_DRY_RUN`／`SHORT_ENTRY_DRY_RUN`）由派發函式自行檢查，乾跑**不寫**去重旗標
- `nexus_core/market_analysis/index_microstructure.py` — market regime determination (SHORT_GAMMA_CRITICAL) using VIX, VIX3M, and zero-gamma line GEX
- `nexus_core/market_analysis/sentiment_engine.py` — Facade entrypoint for skew / UOA / IV stack
- `nexus_core/market_analysis/sentiment/` — Dedicated submodules (`iv_metrics`, `max_pain`, `options_flow`, `uoa_detector`, `history_storage`, `cache`, `skew_taxonomy`)
- `nexus_core/market_analysis/sentiment/canonical_history.py` — Skew / PCR 百分位的日級規範母體（`sentiment_daily_canonical`，≤252 交易日）。`resample_daily_close()` 是唯一的重採樣定義（交易日盤中最後一筆觀測，半日市以 13:00 ET 為界），v080 回填、16:15 ET 收盤快照與 08:45 ET 補寫三處共用；純 DB、不抓網路、`INSERT OR IGNORE` 冪等。未滿 20 個交易日時 `history_storage.get_indicator_percentile_detail()` 退回舊的 500 列高頻池。⚠️ 母體切換沒有改變任何門檻數值，新門檻需走 `calibration/`
- `nexus_core/database/migrations/v080_add_sentiment_daily_canonical.py` — migration 建立 `sentiment_daily_canonical`（及 `sentiment_history.timestamp` 索引），以 Python 從 `sentiment_history` 回填（SQLite `date()` 不支援 IANA 時區）。舊 `SKEW` 序列刻意**不**映射進 `SKEW_D25`；回填失敗只記錄、不中斷 migration
- `nexus_core/market_analysis/sentiment/skew_taxonomy.py` — leaf module (stdlib-only, so it can be imported from both `sentiment` and `intraday_pipeline` without a cycle) holding the single source of truth for the Skew history-indicator key (`SKEW_D25`), the 80/20 classification thresholds, the downstream gate thresholds (`SKEW_DIVERGENCE_*` 85/15, `SKEW_HIGH_DEFENSE_PERCENTILE` 90, `SKEW_TRIPLE_CONFLUENCE_PERCENTILE` 98), `ensure_percentile_pct()` (0~100 contract guard — deliberately no 0~1 auto-rescaling, which would flip the bullish tail into a bearish extreme), and the two extreme state strings
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
- `nexus_core/database/notifications.py` — custom user notification preferences database operations（每使用者設定快取，寫入後即失效；`get_notification_settings_many()` 批次預熱）
- `nexus_core/database/notification_channels.py` — 通知頻道**單一註冊表**：key／模組／標籤／`risk_role`／`cadence`／`preset_immune`／`parent_key` 只在 `CHANNELS` 定義，`ALL_NOTIFICATION_KEYS`／`PRESET_PROFILES`（由屬性規則衍生，左尾防護頻道任何情境都不關）／`TRADING_MODULES` 由此衍生；`resolve_rollover_channel()` 是轉倉指令 → 頻道的唯一對照（新增 `RolloverScenario` 或結構失效類 exit tier 時要更新 `ROLLOVER_SCENARIO_CHANNEL`／`STRUCTURE_BREAK_EXIT_TIERS`，`test_rollover_channel_routing.py` 窮舉把關）；`NotificationKey` Literal 供 mypy 攔下拼錯的頻道名
- `nexus_core/services/notification_dispatcher.py` — 主動推播**唯一入口** `notify()`／`notify_many()`：頻道開關 → 去重 → 入列 → 寫旗標；昂貴工作前以 `is_channel_enabled()` 先判斷。⚠️ 不要直接呼叫 `bot.queue_dm()`（`test_notification_dispatch_centralization.py` 以 AST 強制，白名單只有 `bot.py` 與管理員專用的 `memory_manager.py`）；新的 `dedup_key` 前綴須登記於 `_KV_CACHE_DEDUP_KEY_PREFIXES`
- `nexus_core/database/virtual_trading.py` — Database interface for virtual trades (VTR)
- `nexus_core/market_analysis/dynamic_rollover/` — Dynamic rollover engine package (facade `__init__.py` + `fundamental_thesis.py` / `opportunity_cost.py` / `anti_washout.py` / `margin_defense.py` / `structural_signals.py` (also houses the DTE three-tier state machine, `evaluate_option_dte_tier`) / `covered_call_profit_lock.py` / `inverse_hedge.py` (Scenario 4's third `target_core` destination: symbol→inverse-ETF resolution + pure-spot momentum confirmation) / `core_deployment.py` (Scenario 5 + Covered Call Overlay) / `macro_top_escape_defense.py` (Scenario 6: probabilistic leading-indicator defense, dispatched last in the evaluation order) / `left_side_entry.py` (左側交易六重鐵律，**做多**) / `short_side_entry.py` (做空交易六重鐵律，本系統唯一的空頭方向進場路徑，含「區間內做空」與「破位追空」兩子模式；`evaluate_short_entry()` 回傳含價位中間值的 `ShortEntryEvaluation`) / `short_entry_deployment.py` (SHORT_ENTRY 情境：做空確認後產生獨立的做空進場訊號，與多頭的機會成本轉倉完全脫鉤) / `short_entry_sizing.py` (做空價位與「風險預算 ÷ 停損距離」倉位) / `pyramid_add.py` (PYRAMID_ADD 情境：右側獲利倉順勢金字塔加碼，八項條件 + 同源倉位模型方向反轉，可重複觸發至 `_PYRAMID_MAX_ADDS` 次，與 `transition_engine.py` 的一次性 `OPEN_PYRAMID` 觸發源不同) / `regime_classifier.py` (動態調整 6 態盤勢分類器，含 Regime V 破位追空態與 Regime III-B 右側趨勢延續態) / `transition_engine.py` (動態調整狀態切換引擎) / `advisory_mode.py` (B&H 持倉顧問模式：`is_advisory_asset()` + `build_advisory_instruction()`，把 SATELLITE_REBALANCE 的 TP/SL 分層減碼換股指令轉為純位階告知或丟棄，是葉模組、亦被 `opportunity_cost.py`／`macro_top_escape_defense.py` 匯入以跳過顧問持倉；`MARGIN_DEFENSE` 不受影響) / `models.py` / `constants.py`), anti-washout stop engine, and asset class bifurcation logic. Public import path stays `market_analysis.dynamic_rollover`.
- `nexus_core/market_analysis/signal_calculator.py` — Dynamic trading signal calculator (1.5x ATR buffers, capital allocation models)
- `nexus_core/market_analysis/scenario_classifier.py` — Event-driven quantitative scenario classifier (6 market scenarios including Whale Escort Resonance)
- `nexus_core/database/watchlist.py` — Database CRUD operations for user watchlist symbols (100% deterministic rule-based zero-LLM architecture)
- `nexus_core/database/migrations/v039_add_notification_toggles.py` — migration registering the user_notification_settings table in SQLite
- `nexus_core/tests/unit/test_db_write_centralization.py` — AST 掃描強制「單一寫入者」不變式：除 `database/connection.py` 與 `database/core.py` 外，不得出現 `sqlite3.connect`、`conn.commit()`，或把連線當成 context manager 使用
- `nexus_core/database/migrations/v078_backfill_advisory_entry_signal.py` — migration 以 `heartbeat_symbol_deep` 回填 `advisory_entry_signal`（比照 `v070`），避免已靜音盤中推播的使用者在進場顧問上線後被自動訂閱
- `nexus_core/database/migrations/v081_split_notification_channels.py` — migration 依下行風險影響拆分通知頻道：10 個子頻道以母頻道明確設定回填（`INSERT OR IGNORE`，比照 `v070`／`v078`），刪除已拆解的 `defense_portfolio_risk` 列；對照表是凍結快照，須與註冊表 `parent_key` 一致（有測試把關）
- `nexus_core/database/migrations/v079_add_portfolio_mode.py` — migration 新增 `user_settings.portfolio_mode`（`TEXT DEFAULT 'COMMAND'`，B&H 持倉顧問模式帳戶層開關；預設值 = 現行行為，單檔可由 `assets.metadata.advisory_only` 三態覆寫）
- `nexus_core/tests/unit/test_kv_cache_dedup_whitelist.py` — AST 掃描強制：`save_kv_cache(f"…", True/1)` 形態的每日去重旗標，其鍵前綴必須登記於 `database/cache.py::_KV_CACHE_DEDUP_KEY_PREFIXES`（否則旗標永久堆積）；`_PENDING_WRITERS` 豁免尚未實作寫入點的前綴，實作後須移除
- `nexus_core/tests/unit/test_left_side_entry.py` — unit tests for the left-side six-rule entry gate (per-condition pass/fail/fail-safe, candle-pattern primitive, confirmed-bar guard)
- `nexus_core/tests/unit/test_regime_classifier.py` — unit tests for the 6-regime classifier boundaries, the Regime V vs Regime IV priority split, and its fail-safe default to Regime II
- `nexus_core/tests/unit/test_regime_iii_b_trend_continuation.py` — Regime III-B 趨勢延續路徑：分類優先序（III 必須壓過 III-B）、5/6 容差、同一交易時段約束、條件一／條件四兩項放寬（含「同一份資料在嚴格模式下必須不通過」的對照測項）、前向蒐集 direction/decision、跨情境乾跑閘門
- `nexus_core/tests/unit/test_room_threshold.py` — unit tests for the three dynamic-threshold formulas, the four degradation paths, and the √26 ATR scaling ladder
- `nexus_core/tests/unit/test_short_side_entry.py` — unit tests for the short-side six-rule entry gate (per-condition pass/fail/fail-safe, both sub-modes, signature parity with the left/right gates)
- `nexus_core/tests/unit/test_short_exit_matrix.py` — unit tests for the mirrored short SL/TP matrix and the position-side dispatch (long path must stay bit-identical)
- `nexus_core/tests/unit/test_short_position_risk.py` — unit tests for the portfolio-level direction-awareness remediation: Reg-T short-stock margin, `classify_trade_intent()` / `position_delta_sign()` / `is_short_exposure_strategy()`, gross-vs-net capital, and the two-sided hedge threshold
- `nexus_core/tests/unit/test_short_entry_sizing.py` / `test_short_entry_scenario.py` — SHORT_ENTRY levels, sizing constraints, candidate sourcing and scenario gates (strategy mode, margin-defense suppression, VIX extreme, per-cycle cap, cross-user memoization)
- `nexus_core/tests/unit/test_pyramid_add.py` — PYRAMID_ADD's eight conditions (condition 2's `ratchet_stop >= avg_cost` invariant is the highest-priority test), max-adds/cooldown gating, exposure-cap downsizing, short-position exclusion, delayed state-commit assertion, and an end-to-end `check_satellite_rebalancing` integration test
- `nexus_core/tests/unit/test_tp1_trend_exemption.py` — TP1 trend exemption's seven conditions plus regression coverage for the `previous_call_wall` metrics-wiring gap, the outer gate's grey-band (1%–3% wall migration) firing, and the `dynamic_state_patch`/`asset_id` threading
- `nexus_core/tests/unit/test_blue_sky_ceiling.py` — `resolve_effective_target()` (room_threshold.py 公式 D) degradation paths, near-high triggering, and the deliberate fail-open exception for missing `high_60d`
- `nexus_core/tests/unit/test_kelly_priors.py` / `test_trade_intent_gates.py` — direction-aware Kelly priors and the intent-aware Stage 1 / VTR VIX gates
- `nexus_core/tests/unit/test_regime_evaluation_forward_collection.py` / `test_outcome_labeling.py` — v075 schema, recorder, labeler job and the shared labeling definition
- `nexus_core/tests/unit/test_exit_tier_forward_collection.py` — 出場分層前向蒐集（`EXIT_*` evaluator）：記錄在顧問模式轉換之前、`direction` 為訊號押注方向、加上記錄點後指令輸出逐位元不變、`forward-report` 洗盤率分組
- `nexus_core/tests/unit/test_calibration_*.py` — calibration harness on synthetic data (scanner replica parity, no look-ahead, guardrails, deterministic offline run with a socket guard)；`test_calibration_backtest_feature_flags.py` 覆蓋 2025 回測複刻的 1A／1B／逃頂分級開關（PYRAMID_ADD 條件二不變式為最高優先測項）
- `nexus_core/tests/unit/test_transition_engine.py` — unit tests for the four transition paths, entry-bar-low capture, and the anti_washout coordination/Track-2 universality guards
- `nexus_core/tests/unit/test_wti_alert.py` — unit tests for WTI crude oil price alert system, technicals, and embed rendering
- `nexus_core/tests/unit/test_fundamental_filing_monitor.py` — unit tests for the automated daily SEC filing scanner (dedup cursor, is_broken dispatch gating, per-user notification toggle, multi-holder symbol dedup)
- `nexus_core/tests/unit/test_edge_detection_sentiment.py` — unit tests for Edge Detection, Reddit sentiment classification, VWBP, and dual-tab layout
- `nexus_core/tests/unit/test_intraday_pipeline.py` — heartbeat and phase-B gating tests
- `nexus_core/tests/unit/test_watchlist_advisor.py` — 自選標的進場顧問：`scenario` Literal 不變、呼叫點在 `engine_enabled` 之前、非 green／通知關閉／乾跑皆不推播且不燒去重旗標、Regime 升級不被去重、radar 保鮮／過期走 Semaphore、四種策略分派與跨使用者記憶化
- `nexus_core/tests/unit/test_advisory_mode.py` — B&H 持倉顧問模式：顧問 SPOT 持倉遍歷全部 SL/TP/比例控管路徑後不得產生 `LIQUIDATE`／`REDUCE`（`RolloverInstruction.action` 為純 `str`，mypy 攔不到，此為唯一防護）、轉換規則（結構失效告知／目標區摺疊／丟棄項）、機會成本與逃頂減碼跳過顧問持倉但 `MARGIN_DEFENSE` 不受影響、`PYRAMID_ADD` 顧問模式下仍為指令、三態解析（帳戶層 `portfolio_mode` 每使用者只讀一次 + 單檔 `advisory_only` 覆寫）、派發端 `advisory_core_levels` 通知與 `advisory_exit_` 去重、`portfolio_mode='COMMAND'` 預設逐位元不變回歸測試
- `nexus_core/tests/unit/test_calibration_edge_history.py` — edge GEX／EM 歷史轉換：每日取盤中最後一個分桶（盤外、半日市收盤後、國定假日濾除）、淨 GEX 支撐牆、成交額／ATR 無前視偏差、以 edge schema 的 DB 檔端到端轉換
- `nexus_edge_scraper/tests/test_em_snapshot.py` — edge 收盤後 EM 快照：資料表首筆為準、時間窗、每日只執行一次（重啟以 DB 為準）、零寫入時重試、端點以交易日分頁
- `nexus_core/tests/unit/test_calibration_microstructure.py` — D-03 週 EM 到期日選擇（排除 0/1-DTE、取最接近 7 DTE、無合格到期日回 None）、D-04 成交額正規化薄牆門檻（大型股變嚴、小型股不低於 500k、缺成交額回退）、edge GEX 公式複刻、^SKEW 代理分位無前視偏差、前向蒐集校準特徵
- `nexus_core/tests/unit/test_canonical_resampling.py` — 日級規範母體：重採樣（盤前盤後／週末／半日市／舊 `SKEW` 排除）、midrank 與 IQR 下限、規範母體優先與高頻池回退、`as_of_date` 無前視偏差、收盤快照冪等、v080 回填、0~100 百分位契約、門檻數值不變回歸、UOA／`sentiment_history` 交易日保留期
- `nexus_core/tests/unit/test_intraday_consistency.py` — `/x` 日內資料一致性：VWAP／15m K 棒／日高低點不變式、K 棒凍結與多根合併偵測、期權成交價無套利下界（fixture 取自實盤回報）
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

### 部位方向語意（多空共存）

**全系統以「股數／口數為負」辨識空頭部位**，不另設方向欄位。此慣例原本只用於
空頭選擇權（Covered Call / CSP），做空進場系統上線後擴及空頭現貨。

- **量值 vs 帶號**：凡是回答「佔用多少資本／曝險多大」的計算一律取 `abs()`
  （帳戶規模、配置比例、保證金、熱度）；凡是回答「方向性曝險是多少」的計算
  一律保留正負號（Beta 加權 Delta、各階 Greeks 加總）。混用是這一區最常見的
  缺陷來源——帶號加總會讓多空互相抵銷，使帳戶看起來變小、其餘部位的配置比例
  同步高估。
- **不得用 `if qty > 0` 過濾持倉列**。那會讓空頭部位對整條下游管線隱形。
  需要排除的是 `qty == 0`。
- **候選交易的方向判定**集中在 `market_analysis/risk_engine.py`，三個函式回答
  三個**不同**的問題，不要混用，也不要再寫 `-1 if "STO" in strategy else 1`：
  | 函式 | 回答的問題 | 用途 |
  |---|---|---|
  | `position_delta_sign()` | 這筆交易是**買進還是賣出**該工具 | 乘在帶號合約 Delta 上投影部位 Delta。買進 Put 回 `+1`（合約 Delta 已為負） |
  | `classify_trade_intent()` | `PREMIUM_SELL` / `DIRECTIONAL_LONG` / `DIRECTIONAL_SHORT` | VIX 戰情階梯閘門與倉位乘數、凱利先驗的分流鍵 |
  | `is_short_exposure_strategy()` | 淨方向是否為空頭（布林） | 顯示與判斷用。**不可當 Delta 乘數**——曾因此把 `BTO_PUT` 投影成多頭 Delta |
  已成交部位一律以 `quantity` 正負號為準，策略字串只是未成交候選的代理。
- **負的組合 Delta 不必然是對沖**。也可能是刻意建立的做空 alpha 部位。
  區分依據是 `trade_category == "HEDGE"`（見 `hedging._sum_hedge_only_delta`）；
  誤判的代價是系統建議使用者平掉自己的論點。

- **VIX 戰情階梯與凱利先驗已方向感知化**：新增任何 VIX 閘門或倉位邏輯時，以
  `classify_trade_intent()` 分流，不要以 `"STO"`／`"BTO"` 字串為鍵（`BTO_PUT` 是
  方向性做空）。做空走 `config.get_short_vix_multiplier()`（倒 U 形、上限 1.0、
  VIX 未知回 0.5 而非 Ready 的 1.0）與 `kelly_priors.py` 的 `SHORT` 表。數值皆為
  校準前的保守值，調整走 `calibration/` 報告 → 人工審核 → PR。
- **進場確認帶方向**：Scenario 2 回傳 `EntryConfirmation`（含 `direction`）。
  `direction == "SHORT"` 的確認不得進入任何「買進候選」的下游（機會成本轉倉、
  核心資金部署）；做空一律由 `SHORT_ENTRY` 情境處理。

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

Backtest calibration harness (dev machine only; the image runs as uid 1001 and
cannot write the bind-mounted repo, so run as your own uid). Cache goes to
`nexus_core/.calibration_cache/`, reports to `nexus_core/reports/` (both gitignored):

```bash
cd nexus_core
docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker python -m calibration fetch --max-symbols 40
docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker python -m calibration run --offline --seed 7
# forward-collection report against a copied production snapshot (never the live DB):
#   on the VPS: sqlite3 data/nexus_data.db ".backup /tmp/snapshot.db"; copy it to nexus_core/.calibration_cache/
docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e NEXUS_DB_NAME=/app/.calibration_cache/snapshot.db nexus-seeker python -m calibration forward-report
```

Microstructure / Skew calibration studies (D-03 weekly EM expiry, D-04 wall depth, Skew thresholds). `micro-report` reads edge's forward-collected history by default (`--source edge`, via `TUNNEL_URL` or `--edge-db <copied edge_cache.db>`); `micro-snapshot` is an optional ad-hoc dev-machine measurement. Wall-hold labels are only assigned once a date's 5-session window has elapsed. Criteria: `docs/architecture/05_calibration_harness_and_forward_collection.md` §5.13.

```bash
cd nexus_core
docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker python -m calibration micro-report
# or, from a copy of the edge DB placed in nexus_core/.calibration_cache/:
docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker python -m calibration micro-report --edge-db /app/.calibration_cache/edge_cache.db
docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker python -m calibration skew-proxy
```

What to watch after deploying the forward collection / SHORT_ENTRY, the criteria for flipping `SHORT_ENTRY_DRY_RUN` or changing calibratable constants, and the 2026-09 trial-run baseline all live in [`docs/architecture/05_calibration_harness_and_forward_collection.md`](docs/architecture/05_calibration_harness_and_forward_collection.md) §5.7–§5.9; the analogous observation queries and pass/tighten/reject thresholds for flipping `WATCHLIST_ADVISOR_DRY_RUN` are in §5.11.

Edge scraper tests:

```bash
PYTHONPATH=nexus_edge_scraper nexus_core/.venv/bin/pytest nexus_edge_scraper/tests
```

---

## Deployment Notes

- `nexus_core/docker-compose.yml` currently defines the core bot service
- `nexus_edge_scraper/docker-compose.yml` defines the optional edge scraper + cloudflared sidecar
- production release flow is tag-driven (`v*`)
- pre-commit hooks run ruff lint/format, strict mypy, and general quality checks
- pre-push hooks run semgrep and dockerized tests (core-test and scraper-test)
- The Droplet runs **only** the Discord bot. Calibration data collection (GEX history every 15 min during market hours, post-close weekly-EM straddles) runs in `nexus_edge_scraper`'s scheduler; reports are produced on a dev machine with `python -m calibration micro-report` (reads edge via `TUNNEL_URL` or a copied `edge_cache.db` with `--edge-db`). Do not add calibration jobs to the Droplet

---

## Documentation Guidance

- `docs/README.md` 是所有量化模型／交易策略／風控引擎／平台功能敘述的 SSOT；本檔案（`AGENTS.md`）只負責貢獻者導覽（服務架構、模組地圖）與工程慣例（DB／測試／型別／部署），**不應**再累積功能敘事或修復歷史。
- 新增或修改一項功能時：
  - 若屬於既有 33 篇規格書（`docs/{strategies,microstructure,valuation_pricing,risk_portfolio,macro_sentiment,architecture}/`）的既有主題，更新該檔案對應段落即可，並維持其強制的 6 段式結構（核心哲學／數學模型 LaTeX／Mermaid 決策圖／具名常數表／邊界條件／程式碼路徑）；修改後執行 `python3 scripts/verify_docs_integrity.py` 驗證。
  - 若屬於全新量化主題且值得獨立成篇，新增前請評估是否應納入 `scripts/verify_docs_integrity.py` 的 `EXPECTED_SPECIFICATIONS` 強制清單（需符合 6 段式模板）。
  - 若屬於 UX／排程／通知／委託單等非量化平台功能，寫入或更新 `docs/platform/` 下對應文件（格式較自由，但仍需維持繁體中文、不含 Docker/`.env`/quickstart 內容、內部連結有效），並在 [`docs/README.md`](docs/README.md) 的平台文件索引中加入條目。
- 撰寫任何文件時：
  1. 區分**實際執行流程**與輔助模組
  2. 明確區分 **watchlist 心跳**與 **Analyst Agent**（兩者是完全獨立的報告族群）
  3. 反映現行的欄位式（field-based）embed 格式
  4. 討論通知行為時提及持久化 DM 佇列
  5. `docs/` 保持功能／業務邏輯導向，`AGENTS.md` 保持貢獻者工作流程導向
  6. **純文件更新**（僅修改 `AGENTS.md`、`README.md`、或 `.md` 檔案）**不需要跑測試套件**，但若改動的是 `docs/` 下的 33 篇規格書或新增／修改 `docs/platform/` 文件，仍應手動執行一次 `python3 scripts/verify_docs_integrity.py`
