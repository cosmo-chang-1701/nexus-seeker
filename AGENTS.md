# 🌌 Nexus Seeker - AGENTS.md

## Project Overview

Nexus Seeker is a multi-tenant **Discord-first options risk-control and trading operations platform**. It combines technical structure, Black-Scholes-Merton pricing, Greeks-based portfolio risk, event-aware calendar defenses, and LLM-assisted structured commentary.

Current released core version: **`1.7.33`**

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
   - Used for Reddit scraping without exposing the bot runtime directly

### Important Runtime Distinction

- **Watchlist 半小時心跳** is currently emitted by `cogs/trading.py` via `SchedulerCog.dynamic_market_scanner()`
- **Analyst Agent** is a separate report family in `cogs/analyst_agent.py`
- `market_analysis/intraday_pipeline.py` currently serves as the **shared watchlist evaluation / option-plan / engine helper module**, and also contains the reusable `IntradayScanPipeline` class and gamma squeeze engine logic

Do **not** assume that enabling Analyst Agent is required for the watchlist heartbeat; in current code, those are separate paths.

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

### In `cogs/trading.py`

- `daily_reddit_update` — **08:30 ET**
- `pre_market_risk_monitor` — **09:00 ET**
- `dynamic_market_scanner` — **every 30 minutes during market hours**
- `monitor_real_portfolio_task` — **every 30 minutes during market hours**
- `dynamic_after_market_report` — **16:15 ET**
- `weekly_vtr_report_task` — **Friday 17:05 ET**

### In `cogs/analyst_agent.py`

- `pre_market_loop` — **30 minutes before market open**
- `intra_day_loop` — **every 120 minutes while market is open**
- `post_market_loop` — **post-market report flow**

### In `bot.py`

- persistent DM queue worker
- health worker
- memory manager start/stop
- hedge monitor start/stop
- polymarket service start/stop

---

## Quantitative Radar Terminal & Cache-Aside Architecture

The system features a low-latency, high-information-density **Trader Terminal Radar Panel** (accessed via `/x scan_type: ALL | HOLDINGS | ORDERS | OPTIONS | WATCHLIST`). To ensure Discord response times are strictly under **100ms** and prevent 3-second timeouts, the first-level radar panel runs completely **without LLM calls** or real-time option chain requests.

### 1. Pre-market SQLite Pre-warming & Database Schema
A daily pre-market task runs asynchronously (at 18:00 UTC+8) to fetch option Open Interest (OI), Implied Volatility (IV), and compute weekly expected moves and max pain for all watchlist symbols. These values are cached locally in the `market_cache` table:
```sql
CREATE TABLE IF NOT EXISTS market_cache (
    symbol TEXT PRIMARY KEY,
    max_pain REAL,
    expected_move_lower REAL,
    expected_move_upper REAL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

During the market session, `/x` command reads from this local cache. If a cache miss occurs, the system calculates the metrics in a non-blocking Cache-Aside manner and writes them back.

### 2. Local Rules Engine (Zero-LLM Latency)
Instead of invoking LLM on the first-level radar panel, a lightweight rules engine evaluates spot prices against the SQLite cache bounds:
- **超跌磁吸 🚀**: Triggered if `price <= expected_move_lower` and `Delta MP% > 5%`.
- **需防壓回 ⚠️ / 籌碼斷層 ⚠️**: Triggered if `abs(Delta MP%) > 10%`.
- **Real-time Insights**: Automatically matches active pending orders or option protection strategies (e.g., triggering pull-back alerts or tail-risk warnings).

### 3. Rendering Layer (`build_radar_scan_embed`)
The ANSI terminal radar card is built inside `cogs/embed_builder.py` using `build_radar_scan_embed()`, keeping with the **Single Source of Truth** for embeds. It prints an interactive ANSI plain-text grid showing current price, IVR, Expected Move range, Max Pain, and D-MP% deviation.

### 4. 避免 Discord 回應錯誤的長度分段與分頁原則
為防範當自選標的 (Watchlist) 或持倉 (Holdings) 數量過大時，因 Embed Description 超過 Discord 的 4096 字元上限而導致 `400 Bad Request (error code: 50035): Invalid Form Body` 系統錯誤，系統實施以下長度分段與分頁原則：
- **最大分段間距 (Chunk Size)**：批次掃描結果一律以每頁最多 **15 個標的**進行分組封裝。
- **返回多個 Embed 列表**：`build_radar_scan_embed()` 的返回型別升級為 `List[discord.Embed]`。
- **動態分頁標題**：若分頁數量大於 1，系統會在每個 Embed 的 Title 後方自動標註頁碼，格式為 `(第 X/Y 頁)`（例如：`(第 1/2 頁)`）。
- **呼叫端分流處理**：
  - **Discord 互動指令 (如 `/x`)**：對分頁後的 Embed 列表進行迭代，逐頁調用 `interaction.followup.send(embed=emb, view=msg_view)` 作為獨立的 Ephemeral 訊息發送，並將互動選單 (`BatchScanView`) 僅附掛在最後一個分頁。這能徹底繞過單一訊息中所有 Embed 累計字數不得超過 6000 字元的 Discord 限制。
  - **背景排程與 DM 隊列 (如 Watchlist 30分鐘心跳)**：呼叫端會自動對 Embed 列表進行迭代，逐頁調用 `queue_dm` 加入發送佇列，確保每一頁皆能穩定投遞且不觸發 Discord API 的字數限制。

---

## Watchlist Half-Hour Heartbeat

### Actual current flow

`SchedulerCog.dynamic_market_scanner()`:

1. checks market-open state
2. calls `_dispatch_watchlist_heartbeat()`
3. then runs `_run_market_scan_logic()`

### Watchlist heartbeat build path

The heartbeat currently reuses logic from `market_analysis/intraday_pipeline.py`:

- `evaluate_watchlist_symbol()`
- `derive_watchlist_option_guidance()`
- `build_watchlist_option_plan()`

### Current heartbeat output

The active embed builder is `create_watchlist_signal_embed()` in `cogs/embed_builder.py`.

Current sections:

1. **📊 技術 / 期權快照** (Technical/Options Snapshot ANSI Panel)
2. **📐 Skew 與市場判讀** (Skew Interpretation ANSI Panel - aligned with Sentiment Scan style)
3. **🤖 LLM Skew 解說** (LLM Skew Commentary)
4. **🗓️ 事件風控** (Event Risk Management Summary)
5. **💼 持倉與操作指引** (Holdings & Trading Guide ANSI Panel - dynamically calculates suitable entry/exit prices and shares sizing)
6. **🎯 執行建議** (Execution Suggestions - with options suggestions aligned with calculated pricing strikes)
7. **🧾 可執行期權合約** (Executable Options Contracts)

### Current heartbeat logic details

- sent **per user, per symbol**
- includes:
  - ANSI snapshot and enriched Unusual Option Activity (UOA) table:
    - UOA entries are processed with `trade_type` (`SWEEP` or `BLOCK`) and `oi_change_net`.
    - Presentational layer tags UOA records visually with `🔥 SWEEP` or `📦 BLOCK` and the corresponding daily Open Interest net change.
  - skew / IV structure interpretation
  - event risk summary
  - executable option plan
  - LLM-generated skew commentary
  - **Dynamic Stock Pricing & Share Sizing**:
    - Unheld tickers: Calculates a dynamic `suitable_buy_price` based on RSI and Skew (downside fear discount factor) and corresponding shares budget based on user `capital` and `risk_limit`.
    - Held tickers: Calculates a dynamic `suitable_sell_price` and recommended sell shares (25%, 33%, 50%, or 100% exit ratio depending on RSI and scenario like `hard-hedge`).
  - **Strike-Aligned Options Guidance**:
    - Option guidelines are dynamically mapped to target strikes (e.g. CSP at `suitable_buy_price` or Covered Call at `suitable_sell_price`).
  - **Visual Panel Consistency**:
    - Dotted lines ` ----------------------------------` and ` └─ ` indent prefixes matching the Option Sentiment Scan (Sentiment Scan) format.
- option plans are event-aware:
  - earnings proximity reduces risk
  - pre-event windows prefer defined-risk structures
  - macro events shrink size / bias toward debit spreads or protection

### LLM skew commentary

`services.llm_service.generate_watchlist_skew_commentary()`:

- receives the single-symbol heartbeat snapshot
- summarizes skew / IV / event risk in short Traditional Chinese
- is protected by the global memory-safety gate
- degrades explicitly when RAM usage is too high

### Pre-market IV Sentiment Scan & Fallback

During pre-market hours (before 09:30 ET), the options market is closed and live implied volatility (IV) is unavailable. In standard setups, this causes `IV Rank` and `IV Percentile` calculations to fail or return a misleading `0.0%` (which users might mistake for historically cheap IV).

We resolve this via a comprehensive pre-market optimization workflow:
1. **Trading Hours Detection**: The engine checks the market state using `market_time.is_market_open()`.
2. **Database Fallback**: If the market is closed (`not is_market_open()`), it automatically queries the SQLite database `historical_iv` table for the last known closing IV of the symbol and sets it as `current_iv`.
3. **Historical Volatility (HV) Fallback**: If the DB has no history for the symbol, the engine calculates the standard 30-day Historical Volatility (HV) using historical stock close prices as a proxy.
4. **Degradation Gating**: If all options and historical data are unavailable, the engine gracefully degrades and sets the `is_premarket` flag to `True` on the returned `IVMetrics` model.
5. **Presentation Layer Customization**: In `embed_builder.py`, if `is_premarket` is `True`:
   - **Complete Data Absence (`current_iv == 0.0`)**: Appends ` [盤前數據未更新]` to the title and displays friendly placeholders (`--%` and `等待開盤`) to prevent user confusion.
   - **Successful Fallback (`current_iv > 0.0`)**: Appends ` [盤前/前日收盤]` to the title and tags the IV values with `(前日收盤 / 歷史波動率代理)` to clearly report that the data reflects previous closing levels.

---

## Intraday Quant / Execution Logic

`market_analysis/intraday_pipeline.py` contains:

- watchlist metrics construction
- event-context resolution
- option guidance derivation
- executable option-leg planning
- `NexusGammaSqueezeEngine`
- `IntradayScanPipeline`

Relative Strength (RS) & Tactical Routing:
- Relative Strength formula is implemented in `risk_engine.py`:
  $$RS_{Ticker} = \frac{Price_{Ticker}(t) / Price_{Ticker}(t-n)}{Price_{Benchmark}(t) / Price_{Benchmark}(t-n)}$$
  using sectoral ETFs (e.g., `SMH` for semiconductor tickers) as benchmarks.
- In `ExecutionRouter`, overextended bullish assets (Price/MA20 Deviation > 10% AND RSI > 65) with high Relative Strength (RS > 1.2) are routed to **SPEAR** mode (suggesting Bull Put Spreads or OTM Covered Calls) instead of SHIELD grid shorting.

Important current rule inside `IntradayScanPipeline`:

- `盤中量化掃描 & 避險執行指南` is gated to **Phase B only**
- it is sent **at most once per user + ticker + trading day**

This gating is tested in `tests/unit/test_intraday_pipeline.py`.

---

## Index Microstructure & Macro Risk Engine Upgrade

The platform implements an advanced macro risk-control layer that dynamically adapts portfolio parameters based on market liquidity, rate projections, and cash risk constraints.

### 1. Index Microstructure & Gamma Flip Gating (`index_microstructure.py`)
- **Regime Evaluation**: Evaluates index liquidity conditions via `get_market_regime()`.
- **SHORT_GAMMA_CRITICAL Detection**:
  - Triggers when: $VIX > 20$ AND $vts\_ratio = \frac{VIX}{VIX3M} \ge 1.0$ (in backwardation) AND $\text{SPY Spot} < \text{Gamma Flip Line}$ (as estimated by the Playwright edge scraper `/scrape/macro/gex` route).
- **Tactical Scaling**:
  - Under `SHORT_GAMMA_CRITICAL`, the watchlist scanner in `intraday_pipeline.py` automatically scales `dynamic_grid_step` by **$1.5\times$** to slow down capital depletion during market washouts.

### 2. CME FedWatch Forecasting & Escape Windows
- **Rate Probabilities**: Crawls FOMC rate probabilities via `/scrape/macro/fedwatch` and saves to SQLite (`consensus_value` and `fedwatch_probability` fields in `economic_calendar_events`).
- **Dynamic Escape Window**:
  - The pre-market analyst loop (`analyst_agent.py`) evaluates the probability of rates remaining high ($> 70\%$).
  - If rates remain high (hawkish), it dynamically offsets the user's customized "rebound escape window" (反彈逃頂窗口，支援自訂並自動判定「上/中/下旬」) by **5** business days.
  - If rate cuts are expected, the escape window adjusts forward (risk-on) by **5** business days.

### 3. Active Order Stress Testing (`/stress_test`)
- **Risk Math**:
  $$\text{Total Cash Deficit} = \sum (\text{Active Buy Grid Limit Price} \times \text{Quantity})$$
- **BOXX Buffer and Alerts**:
  - Compares the total cash deficit with the user's available cash + maximum liquidatable $BOXX$ position (capped at 180 shares $\approx \$21,000$).
  - If the potential deficit exceeds the $BOXX$ liquidation limit ($21,000 USD), a critical warning embed is dispatched alerting the user that the payout threshold ($13,000 USD) is endangered.
  - Results are rendered in Traditional Chinese using `NexusEmbed` and integrated into the `/dash` workspace panel.

### 4. Average Cost Basis & Covered Call Recovery Rules
- **New Cost Basis Calculation**:
  $$\text{New Cost Basis} = \frac{(\text{Current Shares} \times \text{Current Cost}) + \sum (\text{GTC Grid Shares} \times \text{Limit Price})}{\text{Current Shares} + \sum \text{GTC Grid Shares}}$$
- **Covered Call Unlock Recommendations**:
  - When assets are locked up, the system generates covered call guidelines to help rebuild runway.
  - Filters option chains for: DTE 30-50 days, Strike > `New Cost Basis`, Delta < 0.15, and annualized yield >= 10.0% (or single premium >= 1.0% of spot).
  - Utilizes 30-day Historical Volatility (HV) or last closing IV as fallback pricing inputs if live option chains are unavailable.

### 5. Manual Macro Update Controls (Added in v1.7.3)
- **Discord Slash Command**: Administrators can manually update GEX and FedWatch data via `/force_macro_update` in Discord.
- **CLI Command**: Developers or scripts can manually trigger macro crawlers via:
  ```bash
  python cli.py admin force-macro-update
  ```

---

## Analyst Agent Reporting

`cogs/analyst_agent.py` is responsible for:

- macro scan
- pre-market earnings / valuation adjustment report
- intraday execution guide
- post-market summary
- sector flow / rotation report
- next-day strategy report

### Pre-Market Earnings & Valuation Data Integration & Concurrency Optimizations
- **Data Source Integration**: The pre-market earnings scan automatically resolves technical evaluations (`evaluate_watchlist_symbol`), option PCR metrics (`SentimentEngine.calculate_pcr`), and company profile details (`get_company_profile`) for all target tickers.
- **Resource Triage Scan (資源分級掃描)**: To avoid redundant computations and API limits, deep scans (calculating technical indicators, IV rank, option skew, and PCR) are strictly gated to near-term tickers (`days_left <= 2`). Long-dated tickers (`days_left > 2`) are lightweight scanned to resolve company sector profiles only.
- **LLM Context Pruning (Token 裁剪)**: Non-essential presentational data (like buy/sell zone statuses) are stripped from the payload fed to the LLM, leaving only critical validation indicators to save up to 40% of Prompt Token overhead.
- **Rate Limit Semaphore Protection**: Requests are throttled using `asyncio.Semaphore(3)` to shield third-party endpoints from API burst blocking, ensuring stability on 1GB VPS environments.

Prompt Refactoring & Constraints:
- The system prompt in `generate_analyst_report` enforces:
  - 100% fluent, finance-grade Traditional Chinese (繁體中文) using Taiwanese market terminology (`選擇權` for Options, `履約價` for Strike, `權利金` for Premium, `價差期權/價差策略` for Spreads, `隱含波動率` for Implied Volatility, `乖離率` for Deviation).
  - Explicit Markdown formatting structure with headers:
    1. 📊 多空大盤交叉驗證解讀
    2. ⚠️ 潛在陷阱與風險提示
    3. 🛡️ 高勝率交易策略推薦
  - Mathematical cross-validation:
    - **IV Bubble Validation**: If Technical Overheating (Deviation > 10% or RSI > 65) occurs while `IV Rank > 90%` and `days_to_earnings > 20`, flag an artificial IV bubble and avoid single-leg long options.
    - **Market Divergence Validation**: If `Option Skew` is negative but `PCR > 1.5`, explain this divergence as retail momentum vs. institutional hedging.

Important current behavior:

- report dispatch uses `split_embed_by_fields()`
- large multi-section reports are split into **one message per field block**
- this avoids Discord embed/content limits

---

## Active Order Management & Telemetry Alignment

To support dynamic tactical order adjustments and "trap setting" for spot assets, the system features a dedicated SQLite state engine paired with a dynamic Discord modal setup pipeline and quantitative price alignment logic.

### 1. Database Schema (`database/orders.py`)

Pending orders are tracked using the `active_orders` table:
- `user_id` (INTEGER) and `symbol` (TEXT)
- `quantity` (REAL) and `order_type` (TEXT: `MARKET`, `LIMIT`, `STOP`, `STOP_LIMIT`, `TRAILING_STOP_USD`, `TRAILING_STOP_PCT`)
- `validity` (TEXT: `DAY`, `EXT_DAY`, `NIGHT`, `GTC_90`)
- `limit_price` (REAL), `stop_price` (REAL), and `trailing_value` (REAL)
- SQLite schema migrations are managed chronologically under `database/migrations/`.

### 2. UI & Interaction Layer (`cogs/order_ui.py`)

Users manage setup, adjustment, and cancellation of pending orders directly via interactive Discord interfaces:
- **Order Setup Panel (`/order_panel`)**: Populates a dynamic dropdown view. Selecting an order type triggers a customized `DynamicOrderModal` containing base fields (Symbol, Quantity, Validity) and conditional price fields (Limit, Stop, or Trailing values).
- **Active Orders Listing (`/list_orders`)**: Displays current active orders in a detailed Traditional Chinese embed, equipped with:
  - `❌ 取消委託 (Cancel Order)` button: Triggers `CancelOrderModal` for low-latency cancellation.
  - `✏️ 編輯委託單 (Edit Order)` button: Triggers `EditOrderModal` to edit pending order price and side (BUY/SELL).
- **Telemetry Price & Size Alignment (`/telemetry_alert`)**: Implements dynamic telemetry price and size alignment alerts, offering:
  - `⚡ 一鍵套用遙測建議價 (Apply Telemetry Price)` button: Automatically updates **both** the price and the quantity/shares of active orders to safer alignments in SQLite, matching the telemetry pricing engine's latest calculations. It features built-in `[⚠️ Tail Risk Mitigation]` log notification if size downscaling was triggered.

### 3. Telemetry Pricing Engine (`services/telemetry_pricing_engine.py`)

The engine calculates recommended limit/stop pricing offsets along three operational vectors:
1. **Option Flow & Gravity**:
   - **Max Pain Migration**: Gravity index offsets aligned with options Max Pain migrations.
   - **Extreme Skew Tail Risk Linkage**: When options Skew percentile hits extreme tails (`skew_percentile < 0.05` or `skew_percentile > 0.95`), the engine shifts the pending order's price **1.5% closer to the spot price** (intercepting the shadow of a panic/squeeze) and dynamically applies a **defensive multiplier of `0.75`** to the quantity/shares (`[⚠️ Tail Risk Mitigation]`) to protect capital liquidity and prevent reservoir depletion.
2. **Statistical Volatility Boundaries**: Pullbacks driven by short-term IV spikes (3% price buffer pullback) or crush (floor to EM Lower Bound), scaled by Expected Move (EM) limits.
3. **Technical & Liquidity Anchors**: Support zone offsets aligned with previous close gap-fills and心理整數關卡 (Psychological round number levels, e.g., offset by `Round Level - 0.75`).


---

## Interactive Configurations & Notification Preferences Center

To provide seamless configurations and avoid parameter-heavy slash command interfaces, the platform employs a fully interactive settings architecture. It separates core account metrics from alert settings, utilizes Discord Views/Modals for dynamic input, and preserves backward compatibility for automated tests.

### 1. Parameter Segregation & Database Schema
Configurations are strictly segregated into two functional areas to maximize separation of concerns:
- **Core Account Settings (`/settings`)**: Tracks high-level financial parameters saved in the `user_settings` table:
  - `capital` (Total capital, must be `> 0`)
  - `risk_limit` (Base risk percentage limit, bounded between `1.0` and `50.0`)
  - `enable_vtr` (GhostTrader Virtual Trading Room toggler)
  - `enable_psq_watchlist` (PowerSqueeze watch tracker toggler)
  - `monthly_expense` (Monthly survival expense for runway metrics)
  - `tax_reserve_rate` (Tax reserve ratio, bounded between `0.0` and `1.0`)
  - `cash_reserve` (Cash reserve value for runway calculation)
- **Notification Preferences (`/notif_settings`)**: Manages individual toggles stored in a key-value style `user_notification_settings` table (designed with composite primary key `(user_id, notification_key)` for infinite schema-less extensibility).
- **Polymarket Settings Migration**: To keep `/settings` focused entirely on portfolio financial metrics, Polymarket monitoring preferences (whale alert toggler `polymarket_whale_alert`, threshold `polymarket_threshold`, AI analysis switch `polymarket_use_llm`, and slippage threshold `polymarket_slippage`) are migrated to `/notif_settings` under their own dedicated selector.

### 2. UI Component Pipeline (`cogs/terminal.py`)
Both `/settings` and `/notif_settings` utilize ephemeral Discord Views. Interactive flows are built as follows:
- **Boolean Switches & Toggles**: Selecting a boolean setting (e.g., `enable_vtr` or notification toggles) instantly flips the state in the SQLite database, triggers `.refresh_items()` to regenerate the select choices (with state emojis: `🟢` for ON, `🔴` for OFF), and edits the active Discord message with the updated embed.
- **Dynamic Text Input Modals**: Selecting a numeric field triggers a Discord Modal popup (`AccountSettingsModal` or `NotificationSettingsModal`).
  - **Client-Side Validation & Sanitization**: The Modal's `on_submit()` performs rigorous validation. E.g., verifying numerical bounds, verifying `capital > 0`, and sanitizing user inputs (such as automatically dividing percentages if a user enters `20` instead of `0.20` for `tax_reserve_rate`).
  - **View Refreshing**: On successful validation and persistence, the modal dynamically triggers a re-draw on the parent View to refresh the dashboard instantly without sending extra message blocks.
- **Global Preferences Control**: `/notif_settings` features global helper buttons `⚡ 全部開啟 (Enable All)` and `💤 全部關閉 (Disable All)` to turn all 18+ alert switches on or off in a single batch query.

### 3. Integration Test Compatibility Design
Discord slash command callbacks in `discord.app_commands.Command` are read-only. To allow the slash command to be parameter-free for Discord UI users while retaining fully-parameterized programmatic execution for integration tests, we dynamically wrap the command's private `_callback` reference during `TerminalCog` initialization:
```python
async def compat_callback(cog, interaction, **kwargs):
    return await cog._update_settings_impl(interaction, **kwargs)
self.update_settings._callback = compat_callback
```
This elegant shim dynamically routes test-driven calls passing keyword arguments directly to the database writer, while standard user invocations cleanly trigger the interactive `AccountSettingsView`.

### 4. Output Centralization
To adhere to output centralization rules and prevent `test_output_centralization.py` failures:
- Neither cogs, views, nor modals construct `discord.Embed` objects directly.
- The entire presentation layer is centralized under `cogs/embed_builder.py` using standard wrappers:
  - `create_account_settings_embed(details_list: list[str]) -> discord.Embed`
  - `create_notification_settings_embed(scheduled_list: list[str], realtime_list: list[str], polymarket_list: list[str]) -> discord.Embed`

---

## Notification and Delivery Layer

`nexus_core/bot.py` owns the persistent DM queue.

Current important behavior:

- pending notifications are stored before send
- startup/shutdown will attempt to recover and flush queue state
- long text is automatically split
- fenced code blocks are preserved during splitting
- this protects against Discord `content <= 2000` failures

When documenting notification behavior, treat the DM queue as **persistent and retry-oriented**, not fire-and-forget.

---

## Event Calendar Architecture

`services/calendar_service.py` is the shared calendar gateway.

Current design:

- macro events are cached by **month**
- earnings are cached by **symbol**
- watchlist heartbeat, calendar views, pre-market alerting, and analyst flows all share the same SQLite-backed cache path

Do **not** add raw market-calendar API calls directly to feature code when calendar helpers already exist.

---

## Embed Architecture

All production embed construction should remain centralized in:

- `nexus_core/cogs/embed_builder.py`

This is enforced by:

- `tests/unit/test_output_centralization.py`

Current repository rule:

- cogs should **not** construct `discord.Embed` directly
- cogs should **not** use the `queue_dm(message=...)` shortcut
- push/report messages should prefer **field-based embeds**
- ANSI tables belong inside a field, not dumped into the full description when avoidable
- **Visual Consistency & Subclassing (`NexusEmbed`)**:
  - To maintain absolute visual consistency across all modules, all instantiated embeds in `cogs/embed_builder.py` are dynamically wrapped via the `NexusEmbed` subclass.
  - **Curated Color Palette**: All standard colors are mapped to cohesive, premium palettes:
    - Primary system/info: Curated blue `0x3498DB`
    - Danger/risk alerts: Curated red `0xE74C3C`
    - Settlement/profits: Curated green `0x2ECC71`
    - Warning/observation: Curated orange `0xF39C12`
    - Secondary: Curated blurple `0x5865F2`
  - **Standardized Footer Signature**: Every embed footer is dynamically formatted as `"🌌 Nexus Seeker • [Module Description]"`, clean of duplicate prefixes, and synchronized with a system timestamp.
  - **Pagination Compatibility (`from_dict`)**: The `.from_dict()` classmethod is overridden to seamlessly convert serialized dictionary payloads back into fully styled `NexusEmbed` instances.

---

## Core Modules to Know

- `nexus_core/bot.py` — bot bootstrap, DM queue, service lifecycle
- `nexus_core/cogs/trading.py` — active runtime scheduler and watchlist heartbeat sender
- `nexus_core/cogs/analyst_agent.py` — analyst report scheduler and dispatcher
- `nexus_core/cogs/order_ui.py` — active orders setting panel, list views, cancellation/adjustment modals, and telemetry alignment buttons
- `nexus_core/cogs/embed_builder.py` — single source of truth for embeds
- `nexus_core/database/orders.py` — active orders SQLite database state CRUD operations
- `nexus_core/database/migrations/v038_add_active_orders.py` — migration registering the active_orders table in SQLite
- `nexus_core/database/migrations/v047_remediate_missing_structures.py` — migration remediating/adding economic calendar columns consensus_value and fedwatch_probability
- `nexus_core/database/migrations/v048_add_escape_window_settings.py` — migration adding escape window configuration columns to user settings
- `nexus_core/market_analysis/intraday_pipeline.py` — watchlist evaluation, option-plan logic, intraday engine helpers
- `nexus_core/market_analysis/index_microstructure.py` — market regime determination (SHORT_GAMMA_CRITICAL) using VIX, VIX3M, and zero-gamma line GEX
- `nexus_core/market_analysis/sentiment_engine.py` — skew / UOA / IV stack
- `nexus_core/market_analysis/telemetry_pricing_engine.py` — central alignment alert pipeline and decision gating logic (stale-lock, deep sea gap limits, pure stock gate, UOA squeeze classification)
- `nexus_core/market_analysis/ghost_trader.py` — GhostTrader Virtual Trading Room execution and monitoring logic
- `nexus_core/services/calendar_service.py` — shared event cache entrypoint
- `nexus_core/services/llm_service.py` — structured LLM outputs and memory-safe degradation
- `nexus_core/services/trading_service.py` — scan / report / validation data orchestration
- `nexus_core/services/telemetry_pricing_engine.py` — dynamic telemetry pricing calculation covering Max Pain, EM, Skew, IV Spikes, and psychological round numbers
- `nexus_core/database/notifications.py` — custom user notification preferences database operations
- `nexus_core/database/virtual_trading.py` — Database interface for virtual trades (VTR)
- `nexus_core/database/watchlist.py` — Database CRUD operations for user watchlist symbols
- `nexus_core/database/migrations/v039_add_notification_toggles.py` — migration registering the user_notification_settings table in SQLite
- `nexus_core/tests/unit/test_intraday_pipeline.py` — heartbeat and phase-B gating tests
- `nexus_core/tests/unit/test_embed_builder.py` — embed contract tests
- `nexus_core/tests/unit/test_output_centralization.py` — embed-centralization enforcement
- `nexus_core/tests/unit/test_order_ui.py` — unit tests for order UI, active order database, and telemetry pricing alignment
- `nexus_core/tests/unit/test_settings_interactive.py` — unit tests for interactive settings view and modals
- `nexus_core/tests/unit/test_notification_toggles.py` — unit tests for notification preferences database toggles and views
- `nexus_core/tests/unit/test_macro_risk_upgrade.py` — unit tests for macro risk upgrade, index microstructure, and covered call unlocking
- `nexus_core/tests/unit/test_telemetry_pricing_engine.py` — unit tests for telemetry pricing alignment pipeline and gating

---

## Development Conventions

### User-facing output

- All user-facing strings should be **Traditional Chinese**
- Private settings / sensitive account operations should use `ephemeral=True`

### Database changes

- Never edit schema manually
- Add a migration file in `nexus_core/database/migrations/`

### Memory / VPS safety

- prefer `BoundedCache` for recurring hot data
- respect the 85% RAM memory gate for non-core LLM work
- keep features safe for 1GB RAM deployment

### Type safety

- prefer explicit Pydantic models / aliases over loose dicts
- keep literal types consistent with model fields
- avoid `Any` unless truly unavoidable at integration boundaries
- **Union & Nullability Safety**: Always perform explicit check-guards (e.g. `if obj is not None:`) before accessing properties on optional/nullable objects (like `interaction.message` or `self.view` on Discord items) to avoid Mypy `union-attr` check failures.
- **Dynamic Property Reflection**: Use safe dynamic helpers `getattr(obj, "attr", default)` or `setattr(obj, "attr", val)` when passing or querying dynamic custom states across UI components (e.g. tracking pre-selected states in views before triggering modals).
- **Mypy Exclusion Configuration**: Stale build directories (`build/`, `dist/`) must be kept clean and explicitly ignored in `[tool.mypy]` `exclude` configuration under `pyproject.toml` to prevent build-pipeline duplicate scans.
- **型別自我檢測 (Pre-commit Type Check)**：在提交程式碼前，開發人員應在包含完整依賴的 Docker 容器中手動跑一次全域型別檢查（在 `nexus_core` 目錄下執行 `docker compose run --rm nexus-seeker python -m mypy --config-file pyproject.toml .`），以確保所有第三方套件（如 `discord.py`）的型別解析正確無誤，避免型別錯誤進入遠端倉庫。

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
- post-push hooks run lint, mypy, semgrep, and dockerized tests

---

## Documentation Guidance

When updating docs in this repository:

1. distinguish **actual runtime flow** from helper modules
2. separate **watchlist heartbeat** from **Analyst Agent**
3. reflect the current field-based embed format
4. mention the persistent DM queue when discussing notifications
5. keep README user-oriented and AGENTS contributor-oriented
