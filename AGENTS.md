# 🌌 Nexus Seeker - AGENTS.md

## Project Overview

Nexus Seeker is a multi-tenant **Discord-first options risk-control and trading operations platform**. It combines technical structure, Black-Scholes-Merton pricing, Greeks-based portfolio risk, event-aware calendar defenses, and LLM-assisted structured commentary.

Current released core version: **`1.6.47`**

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
- **Active Orders Listing (`/orders`)**: Displays current active orders in a detailed Traditional Chinese embed, equipped with:
  - `❌ 取消委託 (Cancel Order)` button: Triggers `CancelOrderModal` for low-latency cancellation.
  - `⚙️ 快速微調價格 (Quick Adjust)` button: Triggers `AdjustOrderModal` to quickly adjust pending limits/stops.
- **Telemetry Price Alignment (`/telemetry_alert`)**: Implements dynamic telemetry price alignment alerts, offering:
  - `⚡ 一鍵套用遙測建議價 (Apply Telemetry Price)` button: Automatically updates the prices of active orders to safer alignments matching the telemetry pricing engine's calculations.

### 3. Telemetry Pricing Engine (`services/telemetry_pricing_engine.py`)

The engine calculates recommended limit/stop pricing offsets along three operational vectors:
1. **Option Flow & Gravity**: Gravity index offsets aligned with options Max Pain and downside fear Option Skew percentiles.
2. **Statistical Volatility Boundaries**: Pullbacks driven by short-term IV spikes or crush, scaled by Expected Move (EM) limits.
3. **Technical & Liquidity Anchors**: Support zone offsets aligned with previous close gap-fills and心理整數關卡 (Psychological round number levels, e.g., offset by `Round Level - 0.75`).

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

---

## Core Modules to Know

- `nexus_core/bot.py` — bot bootstrap, DM queue, service lifecycle
- `nexus_core/cogs/trading.py` — active runtime scheduler and watchlist heartbeat sender
- `nexus_core/cogs/analyst_agent.py` — analyst report scheduler and dispatcher
- `nexus_core/cogs/order_ui.py` — active orders setting panel, list views, cancellation/adjustment modals, and telemetry alignment buttons
- `nexus_core/cogs/embed_builder.py` — single source of truth for embeds
- `nexus_core/database/orders.py` — active orders SQLite database state CRUD operations
- `nexus_core/database/migrations/v038_add_active_orders.py` — migration registering the active_orders table in SQLite
- `nexus_core/market_analysis/intraday_pipeline.py` — watchlist evaluation, option-plan logic, intraday engine helpers
- `nexus_core/market_analysis/sentiment_engine.py` — skew / UOA / IV stack
- `nexus_core/services/calendar_service.py` — shared event cache entrypoint
- `nexus_core/services/llm_service.py` — structured LLM outputs and memory-safe degradation
- `nexus_core/services/trading_service.py` — scan / report / validation data orchestration
- `nexus_core/services/telemetry_pricing_engine.py` — dynamic telemetry pricing calculation covering Max Pain, EM, Skew, IV Spikes, and psychological round numbers
- `nexus_core/tests/unit/test_intraday_pipeline.py` — heartbeat and phase-B gating tests
- `nexus_core/tests/unit/test_embed_builder.py` — embed contract tests
- `nexus_core/tests/unit/test_output_centralization.py` — embed-centralization enforcement
- `nexus_core/tests/unit/test_order_ui.py` — unit tests for order UI, active order database, and telemetry pricing alignment

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
