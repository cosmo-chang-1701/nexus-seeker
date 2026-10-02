# 🌌 Nexus Seeker - AGENTS.md

## Project Overview

Nexus Seeker is a multi-tenant **Discord-first options risk-control and trading operations platform**. It combines technical structure, Black-Scholes-Merton pricing, Greeks-based portfolio risk, event-aware calendar defenses, and LLM-assisted structured commentary.

Current released core version: **`1.15.1`**

The codebase is optimized for:
- **Low-RAM VPS deployment** (1GB–2GB RAM safe, 85% RAM memory gate)
- **Persistent Discord DM delivery** with rate-limit backoff and code-block friendly splitting
- **Field-based, centralized embed output** (`NexusEmbed`)
- **SQLite-first caching for recurring event and market data**

---

## Runtime Architecture & Service Boundaries

### Services
1. **`nexus_core`**: Main Discord bot container (`docker-compose.yml`). Owns all slash commands, background schedulers, embeds, portfolio risk engines, watchlist heartbeats, and DM queueing.
2. **`nexus_edge_scraper`**: Optional FastAPI + Playwright edge container (`docker-compose.yml`). Handles Reddit scraping, SEC section extraction, and acts as a local proxy tunnel for `yfinance` to gracefully bypass datacenter IP blocks (HTTP 403/429).

### Watchlist & Pipeline Distinctions
- **15-minute Intraday Patrol** (`dynamic_market_scanner` in `cogs/trading/scheduler.py`): macro cache + VIX tail-risk alert, edge watchlist sync (`_sync_edge_watchlist`, no-op without `TUNNEL_URL`), and NRO/DDP/IV option scan. The former watchlist radar DM (`heartbeat_watchlist`) and market-scenario alerts (`intel_market_scenario`) were removed; the radar panel is on-demand via `/x` only, and `bot._latest_radar_data_cache` has no writer (readers fall back to fetching).
- **Watchlist 30-minute Deep Dive Heartbeat**: Emitted by `IntradayScanPipeline` in `market_analysis/intraday_pipeline/`, focuses on Gamma squeeze, Volume Profile (POC), options flow, and the entry advisor. Independent `asyncio.Task`, gated by leader status and `is_memory_safe()`; sole writer of `uoa_history`.
- **Analyst Agent**: Independent scheduled report family in `cogs/analyst_agent.py` (09:00 ET pre-market briefing, post-market summary).
- Detailed comparison and channel routing: [`docs/architecture/01_dual_watchlist_pipelines.md`](docs/architecture/01_dual_watchlist_pipelines.md).

---

## Key Technologies

- **Runtime & Bot**: Python 3.12, `discord.py`, `FastAPI`, `Pydantic v2` (strict), `mypy` (strict)
- **Market Data & Quant**: `finnhub-python`, `yfinance`, `pandas-ta`, `py_vollib`, `numpy`, `pandas`, `scipy`
- **Persistence & Architecture**: SQLite WAL, single-writer thread queue, schema migration engine
- **Infra & Quality**: Docker, Docker Compose, `pytest-xdist`, `ruff`, `semgrep`

---

## Background Schedulers & Pipelines

All background schedules follow `US/Eastern` time. Heavy jobs require `_is_leader_instance is True` and pass the `is_memory_safe()` (85% RAM) gate. Full 24-hour timeline specification: [`docs/platform/08_scheduled_jobs_and_background_pipelines.md`](docs/platform/08_scheduled_jobs_and_background_pipelines.md).

| Time (ET) / Interval | Job Name | Primary Responsibility |
| :--- | :--- | :--- |
| **03:00** | `kv_cache_dedup_purge` & Maintenance | Purge stale dedup keys (3d), UOA history (10d), sentiment retention, SQLite checkpoint |
| **03:30** | `regime_outcome_labeler` | Leader-only forward path labeling for `regime_evaluation_log` (5d) & notification dispatch (20d/60d) |
| **08:00** | `fundamental_filing_scan` | Holdings-only SEC 10-K/10-Q/8-K automated scanning (Scenario 7: notify-only, no liquidation command) |
| **08:30** | `daily_reddit_update` | Ingest Reddit market sentiment from edge scraper |
| **08:45** | `pre_market_risk_monitor` | Pre-warm quant metrics, IV, Max Pain, Squeeze, and portfolio downside return series |
| **09:00** | `pre_market_loop` | Analyst Agent pre-market outlook and macro briefings |
| **09:30–16:00 (:00,:15,:30,:45)** | `dynamic_market_scanner` | 15m patrol: macro cache & VIX tail-risk alert, edge watchlist sync, NRO/DDP/IV option scan (no watchlist radar DM) |
| **09:30–16:00 (every 15m, not clock-aligned)** | `price_volume_alert_monitor` | 15m price-volume breakout (`tasks.loop(minutes=15)`, `Semaphore(3)`) |
| **09:30–16:00 (every 30m, not clock-aligned)** | `monitor_order_telemetry_alignment_task` | Pending-order telemetry alignment (`telemetry_orders`) |
| **09:30–16:00 (:05,:20,:35,:50)** | `monitor_real_portfolio_task` | Staggered portfolio Greeks & downside drawdown check (shared radar cache has no writer; fetches via `Semaphore(3)`) |
| **09:30–16:00 (every 30m)** | `IntradayScanPipeline` | 30m deep watchlist scan (Gamma squeeze & Volume Profile POC), `is_memory_safe()` gated |
| **16:15** | `dynamic_after_market_report` | Close maintenance, daily sentiment snapshot, NAV history, CVaR tail risk check, and macro-signal dry-run log (record-only, no notifications) |
| **Post-market / Fri 17:05** | Analyst Post-Market & VTR | Comprehensive post-market summary and weekly Virtual Trading Room Brinson attribution |
| **24/7 (30m / 4h / Workers)** | WTI, Calendar & Workers | 24/7 WTI crude oil monitor, 4h macro/FedWatch checker, persistent DM queue, health & stream workers |

---

## Business Logic & Spec Index (SSOT)

All quantitative models, risk matrices, and platform designs are specified in [`docs/README.md`](docs/README.md). **Always update the corresponding specification under `docs/` instead of expanding this file:**
- **Trading Strategies** ([`docs/strategies/`](docs/strategies/)): 6-Regime routing matrix (`01`), Right-side momentum (`02`), Left-side mean-reversion (`03`), Dynamic Rollover slimmed scenarios (`04`), Dual-track anti-washout SL (`05`), Dynamic adaptive room threshold (`06`), Short-side breakdown (`07`); offline-only replacement candidates for the rollover engine, not wired into production: Regime-switched momentum rotation (`08`), Static allocation & rebalance / macro 3-state switch (`09`).
- **Microstructure** ([`docs/microstructure/`](docs/microstructure/)): Net GEX topology & walls (`01`), Physical wall constraint $K < \text{Spot}$ (`02`), Gamma flip (`03`), UOA paced ratio (`04`), Volume Profile & POC (`05`), Gamma squeeze & SPEAR (`06`).
- **Valuation & Volatility** ([`docs/valuation_pricing/`](docs/valuation_pricing/)): TDP/DDP valuation (`01`), Expected move & Max Pain gravity (`02`), Skew 25-Delta & PCR divergence (`03`), IVR & seller lockout gate (`04`).
- **Portfolio & Risk** ([`docs/risk_portfolio/`](docs/risk_portfolio/)): Beta-weighted Greeks (`01`), VIX battle ladder & Kelly (`02`), AROC gate (`03`), DITM convexity (`04`), Runway & liquidity (`05`), Brinson attribution (`06`), Downside risk (Sortino/MDD/CVaR) (`07`).
- **Macro & Sentiment** ([`docs/macro_sentiment/`](docs/macro_sentiment/)): Macro escape top (`01`), SEC filing moat scanner (`02`), WTI crude monitor (`03`), Polymarket VWBP sentiment (`04`).
- **Architecture & Platform** ([`docs/architecture/`](docs/architecture/) & [`docs/platform/`](docs/platform/)): Dual watchlist pipelines (`arch/01`), Pre-market cache-aside (`arch/02`), Dual service & proxy (`arch/03`), Engineering standards & DB single writer (`arch/04`), Calibration harness (`arch/05`), Notification center (`platform/03`), Scheduled jobs (`platform/08`).

---

## Codebase Architecture by Layer

- `nexus_core/cogs/`: Discord presentation layer (`bot.py` bootstrap/DM queue, `trading/` schedulers & heartbeats, `analyst_agent.py`, `unified_terminal/`, `calendar.py`, `order_ui.py`, `settings_ui.py`, `hedging.py`).
- `nexus_core/market_analysis/`: Pure quantitative algorithms and decision engines (`intraday_pipeline/`, `dynamic_rollover/` slimmed scenarios (B&H-first advisory engine), `room_threshold.py` adaptive volatility leaf, `structural_signals.py` GEX walls, `downside_risk.py` leaf, `sentiment_engine.py`).
- `nexus_core/services/`: Asynchronous service orchestrators and I/O pipelines (`downside_risk_service.py`, `notification_dispatcher.py`, `single_flight.py` request deduplication, `alpaca_stream_service.py`, `regime_outcome_labeler.py`, `llm_service.py`).
- `nexus_core/database/`: SQLite WAL persistence layer (`connection.py` single-writer queue worker & `connect_db()`, `core.py` migration engine, `portfolio.py`, `orders.py`, `cache.py`, `notification_channels.py`).
- `nexus_core/calibration/`: Offline backtest and parameter calibration harness (`microstructure.py`, `notif_report.py`, daily-bar strategy backtests `regime_momentum_backtest.py` / `static_allocation_backtest.py` / `macro_regime.py`; runs on dev machine only, never writes to live DB).
- `nexus_edge_scraper/`: Standalone scraper and proxy microservice (Playwright scrapers, yfinance proxy endpoints, SEC section extraction).

---

## Critical Development Conventions

### 1. Database Single-Writer Architecture & Migrations
- **Single-Writer Invariant**: Never open independent write connections. All writes must route through `nexus_core/database/connection.py` (`execute_write_async`, `execute_write_many_async`, `execute_write_rowcount_async`). AST scans in CI strictly enforce this rule (`tests/unit/test_db_write_centralization.py`).
- **Dedicated Worker Thread**: Writes execute on the dedicated `nexus-db-writer` thread. Never perform synchronous SQLite calls inside the Discord asyncio event loop.
- **No Network I/O in Worker / No Await in Transactions**: Resolve network data in callers before queueing; stage data in memory and batch write.
- **Event Loop Thread Guard**: `put_task_sync()` raises `RuntimeError` if called from the event loop thread (use `execute_write_async` or `asyncio.to_thread`).
- **Connection Lifecycle & Coalescing**: Always obtain connections via `connect_db()` with unified `_BUSY_TIMEOUT_MS` (15s). SQLite context managers do NOT close connections; always use `try ... finally: conn.close()`. Coalesce hot-path reads via `asyncio.to_thread` (`get_kv_cache_many`, `_load_symbol_caches`). Read functions must have zero write side-effects.
- **Cross-Process Retries & Maintenance**: Blue-green container overlap uses 4 retries with jittered exponential backoff. Routine WAL checkpoints and `PRAGMA optimize` execute in `run_maintenance()` at 03:00 ET.
- **Migration Contract**: Migrations in `database/migrations/` must export `version`, `description`, and `sql` module attributes (functions like `upgrade(cursor)` are silently skipped). The runner only executes versions $> \text{MAX}(version)$.

### 2. Position Direction Semantics (Long/Short Coexistence)
- **Signed Quantities**: Short positions are represented solely by negative quantities (`qty < 0`). Never filter active holdings with `qty > 0` (use `qty != 0`).
- **Magnitude vs. Direction**: Use `abs()` for capital allocation, margin, and portfolio heat; preserve signs for directional exposure (Beta-weighted Delta, Greeks summation).
- **Direction Helpers in `market_analysis/risk_engine.py`**:
  - `position_delta_sign()`: Buy vs. sell sign for projecting contract Delta. Buy Put returns `+1`.
  - `classify_trade_intent()`: `PREMIUM_SELL` / `DIRECTIONAL_LONG` / `DIRECTIONAL_SHORT` (drives VIX ladder & Kelly priors). Never use `"STO"` / `"BTO"` strings.
  - `is_short_exposure_strategy()`: Boolean net short direction for display/decision (never as a Delta multiplier).
- **Negative Portfolio Delta != Hedge**: Distinguish short alpha from hedges via `trade_category == "HEDGE"`.
- **Directional Confirmations**: Entry-confirmation gates return `EntryConfirmation(direction=...)`. Confirmations with `direction == "SHORT"` must never enter buy-candidate downstream flows; short entries route solely through `SHORT_ENTRY`.

### 3. Market Data & Timezone Conventions
- **Timezone Invariant**: `get_history_df` returns **tz-naive US/Eastern** indexes (never UTC). Treating them as UTC shifts intraday bars by 4–5 hours and daily bars by one day.
- **Single-Flight Deduplication**: Concurrent market requests must route through `services/single_flight.py::SingleFlightManager.run()` to prevent redundant inflight API calls.

### 4. Memory & VPS Safety (1GB–2GB RAM)
- Use `BoundedCache` for high-frequency in-memory caching.
- Gate all background loops and LLM pipelines with `is_memory_safe()` (85% RAM threshold).
- Reuse shared in-memory caches across modules (e.g., `bot._latest_radar_data_cache`).

### 5. Strict Typing & Code Quality
- Strict mypy compliance (`disallow_untyped_defs = true`). Explicit types on empty collections (`_my_set: set[str] = set()`, `_my_list: list[str] = []`).
- Nullability safety: explicit check-guards (`if obj is not None:`) before accessing properties on optional objects (e.g. `interaction.message`, `self.view`).
- Dynamic UI state passing: use safe dynamic helpers `getattr(obj, "attr", default)` / `setattr(obj, "attr", val)`.
- Security: parameterized SQL queries only; never use raw string interpolation in SQL execution.
- User-facing text must be **100% Traditional Chinese**. Private settings use `ephemeral=True`.

---

## Testing & Quality Gates

All automated tests run containerized in Docker. Worker count is bound by `mem_limit: 850m` (use `-n 2`, never `-n auto`):
```bash
cd nexus_core
# Full test suite (CI gate)
docker compose run --rm nexus-seeker python -m pytest tests -n 2 --dist loadfile
# Fast test subset (pre-push hook gate)
docker compose run --rm nexus-seeker python -m pytest tests -m "not slow and not integration" -n 2 --dist loadfile
# Type-checking pre-commit check
docker compose run --rm nexus-seeker python -m mypy --config-file pyproject.toml .
# Edge scraper tests
PYTHONPATH=nexus_edge_scraper pytest nexus_edge_scraper/tests
```
- **Markers & Pre-Push Hook**: Tests taking $\ge 1\text{s}$ must be marked `@pytest.mark.slow`. `tests/integration/` is auto-marked `integration`. The 4 AST invariant tests (`test_db_write_centralization.py`, `test_output_centralization.py`, `test_notification_dispatch_centralization.py`, `test_kv_cache_dedup_whitelist.py`) must remain in the fast subset. `NEXUS_FULL_TESTS=1 git push` forces the full test suite.
- **Focused Unit Runs**: `docker compose run --rm nexus-seeker python -m pytest tests/unit/test_intraday_pipeline.py` (or `test_embed_builder.py`, `test_order_ui.py`, `test_settings_interactive.py`).
- **Documentation Exemption & Verification**: Pure documentation updates (`.md` files) are exempt from Docker test suite runs. When modifying `docs/`, run `python3 scripts/verify_docs_integrity.py` to audit 100% compliance across all 7 batteries.
- **Offline Calibration**: Run on dev machines via `python -m calibration` (runs as host UID/GID with `-e HOME=/tmp`, see [`docs/architecture/05_calibration_harness_and_forward_collection.md`](docs/architecture/05_calibration_harness_and_forward_collection.md)):
```bash
docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker python -m calibration micro-report
docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker python -m calibration forward-report
docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker python -m calibration macro-forward-report --snapshot-db /app/.calibration_cache/snapshot.db
```

---

## Deployment & Documentation SSOT Guidance

- **Production Droplet**: Runs **only** `nexus_core` bot. Calibration studies run offline on development machines using read-only database copies.
- **Deployment Flow**: Production releases are tag-driven (`v*`). Pre-commit hooks enforce formatting (`ruff`), strict type checking (`mypy`), and security. Pre-push hooks enforce fast tests.
- **Documentation SSOT**: `docs/` is the single source of truth for technical models and system architecture; `AGENTS.md` is contributor and AI onboarding guide.
