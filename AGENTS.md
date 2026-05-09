# 🌌 Nexus Seeker - AGENTS.md

## Project Overview
Nexus Seeker is a multi-tenant **Options Quant Risk-Control & Trading Operations Platform** driven by Discord. It combines technical analysis, Black-Scholes-Merton pricing models, LLM-based NLP risk sentiment analysis, a custom **Nexus Risk Optimizer (NRO)** for portfolio exposure精算, and a **6-tier VIX Battle Ladder** system for dynamic risk appetite scaling. The project is structured as a dual-service architecture to handle complex market data processing and edge-scraping tasks efficiently.

### Key Technologies
- **Language:** Python 3.12
- **Frameworks:** `discord.py` (Discord Bot), `FastAPI` (Edge Scraper API)
- **Market Data:** `finnhub-python`, `yfinance`, `pandas-ta`, `py_vollib` (Greeks/Pricing)
- **Data Science:** `numpy`, `pandas`, `pandas_market_calendars`
- **AI/LLM:** OpenAI-compatible API with `pydantic` structured outputs
- **Scraping:** `playwright`, `BeautifulSoup4`
- **Database:** SQLite with a custom automated migration engine
- **Infrastructure:** Docker, Docker Compose, Cloudflare Tunnel

---

## Architecture
The system is divided into two main services:
1.  **`nexus_core`**: The central Discord Bot. Handles user commands, portfolio management, risk engine calculations, and periodic market scans.
2.  **`nexus_edge_scraper`**: A specialized service (intended to run locally or via tunnel) that uses Playwright to scrape Reddit sentiment and consensus scores without triggering bot detection on cloud IPs.

### Core Modules (`nexus_core`)
- **`config.py`**: Global configuration constants including `VIX_LADDER_CONFIG` (6-tier system: Dormant/Caution/Ready/Aggressive/Heavy/All-in), `VIX_QUANTILE_BOUNDS`, and the `get_vix_tier()` helper function (includes NaN/None robustness with "Ready" fallback).
- **`market_analysis/`**: The quant engine.
  - **`strategy.py`**: Core strategy logic with VIX ladder gating and delta capping.
  - **`ddp_inspector.py`**: **Davis Double Play (DDP)** detection engine. Implements EPS momentum (>15% YoY), P/E historical range analysis (bottom 25th percentile), and revenue acceleration checks.
  - **`psq_engine.py`**: PowerSqueeze (PSQ) scoring with VIX-aware momentum labeling.
  - **`risk_engine.py`**: NRO risk optimization with dynamic Kelly scaling.
  - **`ghost_trader.py`**: Virtual Trading Room (VTR) and autonomous DITM defense.
- **`database/`**: Persistent storage layer with an automated migration engine. Includes aggregate Greeks tracking, DDP signals, and the **`pending_notifications`** table for cross-deployment message reliability (v026+).
- **`services/`**: Business logic layer (`TradingService`, `LLMService`, `PolymarketService`, `MarketDataService`, `NewsService`, `RedditService`) that decouples the Discord UI from core computations.
- `cogs/`: Discord extensions implementing slash commands and background tasks.
  - **`terminal.py`**: High-impact professional terminal commands (`/runway_check`, `/scan`, `/ddp_scan`, `/settings`, `/vtr_list`).
  - **`intelligence.py`**: Market intelligence and edge detection terminal (`/poly_list`, `/scan_news`, `/scan_reddit`, `/quote`).
  - **`trading.py`**: Automated market scanning (NRO + DDP) and background risk auditing.
  - **`analyst_agent.py`**: Scheduled Wall Street Quantitative Analyst Agent.

- **`ui/`**: Reusable Discord UI components and views for interactive commands.

---

## Building and Running

### Prerequisites
- Docker & Docker Compose
- Discord Bot Token & Finnhub API Key
- (Optional) OpenAI-compatible LLM endpoint
- (Optional) Cloudflare Tunnel for Reddit scraping

### Development Setup
1.  **Configure Environment:**
    ```bash
    cp nexus_core/.env.example nexus_core/.env
    # Fill in DISCORD_TOKEN, FINNHUB_API_KEY, etc.
    ```
2.  **Start Services:**
    ```bash
    # Start Core Bot
    cd nexus_core
    docker compose up -d --build

    # Start Edge Scraper (if needed)
    cd ../nexus_edge_scraper
    docker compose up -d --build
    ```

### Deployment Strategy
The system utilizes **Docker Swarm** with a `start-first` update configuration to achieve a Blue-Green style handoff:
1.  **Green (New)** instance is launched and verifies health.
2.  **Graceful Handoff**: The old instance receives `SIGTERM` and enters a 60-second `stop_grace_period`. It drains its persistent notification queue and completes ongoing quant scans before terminating.
3.  **Persistence**: Unsent messages are stored in SQLite and automatically picked up by the new instance upon startup, ensuring zero message loss during version transitions.
4.  Automatic **Rollback** is triggered if the new instance fails to connect.

### Testing
Tests are located in `nexus_core/tests/`.
- **Mandate:** All tests and debug scripts MUST be executed within the Docker container environment.
- **Run all tests:**
  ```bash
  docker compose run --rm nexus-seeker python -m unittest discover -s tests -v
  ```
- **Integration Tests:** Focused on database migrations, trading flows, Polymarket whale monitoring (`test_polymarket_integration.py`), and LLM/Risk engine integration.
- **Unit Tests:** Covers core logic for Greeks, PSQ, and the **DDP Inspector** (`test_ddp_inspector.py`).

---

## Development Conventions

### 1. Database Migrations
Never modify the database schema manually. Use the migration engine:
- Create a new file in `nexus_core/database/migrations/` (e.g., `v026_add_pending_notifications.py`).
- Export `version` (int), `description` (str), and `sql` (str).
- The bot will automatically apply it on the next startup.

### 2. Discord Commands (Cogs)
New commands should be added as **Slash Commands** within a Cog in `nexus_core/cogs/`.
- Use `discord.app_commands` for slash command definitions.
- All replies should ideally be **ephemeral** (`ephemeral=True`) to maintain multi-tenant privacy.
- For long-running tasks, use `bot.queue_dm(user_id, message, embed)` to send asynchronous notifications via the background message worker.

### 3. Market Analysis & Strategy
- Core logic belongs in `nexus_core/market_analysis/strategy.py`.
- **Davis Double Play (DDP)**: Implemented in `ddp_inspector.py`. Identifies stocks with simultaneous EPS growth (>15%) and P/E expansion potential (current P/E < 25th percentile of 3Y range). Validates via revenue acceleration and forward P/E alignment.
- Use the `AlertFilter` in `services/alert_filter.py` to implement noise reduction (e.g., EMA crossovers, multi-timeframe alignment).
- `GhostTrader` (`market_analysis/ghost_trader.py`) handles the Virtual Trading Room (VTR) logic, simulating entries and tracking virtual performance. VTR auto-entry is gated by VIX tier permissions (`vtr_entry_allowed`). Implements autonomous **DITM Profit Lock** defense.
- `PSQ Engine` (`market_analysis/psq_engine.py`) provides the PowerSqueeze momentum indicator with VIX-aware labeling (`OVEREXTENDED_RISK`, `HIGH_CONVICTION_RECOVERY`). Supports legacy aliases (`is_breakout_high`) for stability.
- `NRO Risk Engine` (`market_analysis/risk_engine.py`) provides portfolio risk optimization with inverted VIX weights (high VIX = offensive posture), dynamic Kelly scaling (1/4 to 1/2 Kelly), and All-in bypass for VIX > 35. Enforces **Dormant Tier (VIX < 15)** STO rejection.
- `Portfolio Management` (`market_analysis/portfolio.py`) handles Greeks refresh with **Implied Volatility (IV) back-solving** from market price (Mid/Last) if data feeds are missing.
- `Financial Analytics` (`market_analysis/pro_management.py`) handles professional metrics like **Financial Survival Runway** and **Position Evolution (Transition) Simulations**.

### 4. Service Layer & Decision Pipeline
- `TradingService` (`services/trading_service.py`) implements a **4-stage validation pipeline**: **Macro -> Alpha -> Risk -> Financials**.
- **Alpha Filtering**: Enforces 15% minimum AROC for STO signals.
- **Exposure Monitoring**: Generates actionable SPY hedge directives (e.g., "Sell 4 shares of SPY") when portfolio Delta exceeds ±50.
- **Unit Standardization**: The system stores **Annual Greeks** (BSM standard) in the DB but aggregates and displays **Daily Theta** in the UI and `UserContext`.

### 5. VIX Battle Ladder
The VIX Battle Ladder is a 6-tier system defined in `config.py` (`VIX_LADDER_CONFIG`) that dynamically governs risk appetite across all analysis modules:
- **Dormant** (VIX < 15): Hard-reject all STO signals and VTR entries. `w_vix = 0.0`.
- **Caution** (15-18): Cap delta at -0.12, reduce sizing by 50%. `w_vix = 0.5`.
- **Ready** (18-24): Standard mode. `w_vix = 1.0`.
- **Aggressive** (24-30): Offensive posture, sizing 1.2x. `w_vix = 1.2`.
- **Heavy** (30-35): Sizing 1.5x, dynamic Kelly scaling. `w_vix = 1.5`.
- **All-in** (>= 35): Sizing 2.0x, 1/2 Kelly override, bypass oil/regime dampening. `w_vix = 2.0`.

VIX spot is fetched once per scan cycle in `TradingService.run_market_scan()` and propagated to `analyze_symbol()`, `analyze_psq()`, and VTR entry gating. The `vix_battle_status` dict is injected into result data for UI rendering via `embed_builder.py`. The `get_vix_tier()` helper ensures system resilience by defaulting to the "Ready" tier if input is `None` or `NaN`.

### 6. Localization & Copywriting
The terminal's Discord output is localized to **Professional Traditional Chinese (Taiwan)** to maintain a "Mission Critical" tone.
- **Tone:** Imperative, high-signal, and authoritative. Avoid conversational prose.
- **Standards:** 
  - Preserve Greek letters and professional acronyms (Delta, Gamma, Theta, Vega, AROC, DTE, STO, DITM, VIX, NRO) in English.
  - Standard Taiwan Financial Terminology: Margin -> 保證金, Portfolio -> 投資組合 / 部位, Hedge -> 對沖 / 避險, Convexity -> 凸性, Liquidity -> 流動性, Exposure -> 曝險.
  - High-signal keywords: "攔截成功" (Intercepted), "審計完成" (Audit Complete), "執行指令" (Execution Command), "DITM 凸性防護" (Convexity Guard).

### 7. Code Style
- **Type Hinting:** Strictly define types for all functions and class members.
- **Logging:** Use the project-wide logger (`logging.getLogger(__name__)`).
- **Async/Await:** Ensure all I/O bound operations (API calls, DB queries) are non-blocking. Use `asyncio.to_thread` for blocking yfinance calls.

---

## Key Files Summary
- `nexus_core/main.py`: Application entry point.
- `nexus_core/bot.py`: Main Bot class and background worker initialization.
- `nexus_core/config.py`: Global configuration — env vars, strategy Delta params, **VIX Battle Ladder** tier definitions (`VIX_LADDER_CONFIG`), and `get_vix_tier()` helper.
- `nexus_core/market_time.py`: NYSE market calendar and timezone-aware scheduling.
- `nexus_core/services/trading_service.py`: Centralized business logic orchestrator. Implements the **4-stage validation pipeline** and manages VIX propagation, as well as sorted pre-market earnings alerts.
- **`services/polymarket_service.py`**: Real-time Polymarket whale monitoring service with **L2 Order Book Sync**. Features **Dynamic Slippage-based Thresholds** and **LLM Structured Output** for background analysis.
- `nexus_core/market_analysis/strategy.py`: Quant scanning and filtering pipeline. VIX ladder gating, delta capping, and **AROC yield calculation**.
- `nexus_core/market_analysis/psq_engine.py`: PowerSqueeze momentum calculation engine with VIX-aware labeling.
- `nexus_core/market_analysis/risk_engine.py`: NRO risk optimizer — inverted VIX macro weights, dynamic Kelly scaling, and **Dormant tier enforcement**.
- `nexus_core/market_analysis/hedging.py`: Portfolio Beta-Weighted Delta tracking and **actionable hedge directives**.
- `nexus_core/market_analysis/ghost_trader.py`: Virtual Trade Replicator and VTR logic. Implements **Profit Lock (DITM)** defensive actions.
- `nexus_core/market_analysis/portfolio.py`: Handles portfolio Greeks refresh with **IV back-solving** and standardized DB updates.
- `nexus_core/market_analysis/pro_management.py`: Quantitative survival analysis (**Financial Runway**) and **Position Evolution** simulations.
- `nexus_core/database/user_settings.py`: User profile management and **robust SQL aggregation**.
- `nexus_core/database/notifications.py`: Persistent notification queue logic for **Graceful Handoff**.
- `nexus_core/cogs/embed_builder.py`: Discord UI/UX generator.
- **`nexus_core/cogs/analyst_agent.py`**: Scheduled Wall Street Quantitative Analyst Agent.
- `nexus_core/database/core.py`: SQLite migration engine core logic.
- `nexus_edge_scraper/local_api.py`: Playwright-based scraping endpoint.
