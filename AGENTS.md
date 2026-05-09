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
  - **`volatility_inspector.py`**: **Volatility Strategist Agent**. Detects "Cheap Volatility" (IVP < 25%, IV < HV) with momentum alignment and earnings shielding.
  - **`psq_engine.py`**: PowerSqueeze (PSQ) scoring with VIX-aware momentum labeling.
  - **`risk_engine.py`**: NRO risk optimization with dynamic Kelly scaling.
  - **`ghost_trader.py`**: Virtual Trading Room (VTR) and autonomous DITM defense.
- **`database/`**: Persistent storage layer with an automated migration engine. Includes aggregate Greeks tracking, DDP signals, and the **`holdings`** table for independent equity asset accounting (v027+).
- **`services/`**: Business logic layer (`TradingService`, `LLMService`, `PolymarketService`, `MarketDataService`, `NewsService`, `RedditService`) that decouples the Discord UI from core computations.
- `cogs/`: Discord extensions implementing slash commands and background tasks.
  - **`terminal.py`**: High-impact professional terminal commands (`/runway_check`, `/scan`, `/ddp_scan`, `/iv_scan`, `/add_holding`, `/list_holdings`, `/remove_holding`, `/add_watch`, `/edit_watch`, `/remove_watch`, `/list_watch`, `/settings`, `/vtr_list`).
  - **`intelligence.py`**: Market intelligence and edge detection terminal (`/poly_list`, `/scan_news`, `/scan_reddit`, `/quote`).
  - **`trading.py`**: Automated market scanning (NRO + DDP + Volatility) and background risk auditing.
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
- **Unit Tests:** Covers core logic for Greeks, PSQ, **DDP Inspector** (`test_ddp_inspector.py`), and **Volatility Inspector** (`test_volatility_inspector.py`).

---

## Development Conventions

### 1. Database Migrations
Never modify the database schema manually. Use the migration engine:
- Create a new file in `nexus_core/database/migrations/` (e.g., `v027_refactor_watchlist_add_holdings.py`).
- Export `version` (int), `description` (str), and `sql` (str).
- The bot will automatically apply it on the next startup.

### 2. Discord Commands (Cogs)
New commands should be added as **Slash Commands** within a Cog in `nexus_core/cogs/`.
- Use `discord.app_commands` for slash command definitions.
- All replies should ideally be **ephemeral** (`ephemeral=True`) to maintain multi-tenant privacy.
- For long-running tasks, use `bot.queue_dm(user_id, message, embed)` to send asynchronous notifications via the background message worker.

### 3. Market Analysis & Strategy
- Core logic belongs in `nexus_core/market_analysis/strategy.py`.
- **Holdings Management**: Equity assets are tracked independently in `holdings.py`. These positions contribute to the total portfolio Delta but have 0 Theta/Gamma. Refreshed via `refresh_portfolio_greeks()`.
- **Davis Double Play (DDP)**: Implemented in `ddp_inspector.py`. Identifies stocks with simultaneous EPS growth (>15%) and P/E expansion potential.
- **Volatility Strategist (IV)**: Implemented in `volatility_inspector.py`. Detects undervalued options (IVP < 25%, IV < HV) with technical momentum alignment.
- Use the `AlertFilter` in `services/alert_filter.py` to implement noise reduction.
- `GhostTrader` (`market_analysis/ghost_trader.py`) handles the Virtual Trading Room (VTR).
- `PSQ Engine` (`market_analysis/psq_engine.py`) provides the PowerSqueeze momentum indicator.
- `NRO Risk Engine` (`market_analysis/risk_engine.py`) provides portfolio risk optimization.
- `Portfolio Management` (`market_analysis/portfolio.py`) handles Greeks refresh with **IV back-solving**.
- `Financial Analytics` (`market_analysis/pro_management.py`) handles **Financial Survival Runway**.

### 4. Service Layer & Decision Pipeline
- `TradingService` (`services/trading_service.py`) implements a **4-stage validation pipeline**: **Macro -> Alpha -> Risk -> Financials**.
- **Alpha Filtering**: Enforces 15% minimum AROC for STO signals.
- **Exposure Monitoring**: Generates actionable SPY hedge directives.

### 5. VIX Battle Ladder
The VIX Battle Ladder is a 6-tier system defined in `config.py` (`VIX_LADDER_CONFIG`) that dynamically governs risk appetite.

### 6. Localization & Copywriting
The terminal's Discord output is localized to **Professional Traditional Chinese (Taiwan)**. All user-facing Embed content, command descriptions, and error messages strictly follow Traditional Chinese standards.

### 7. Code Style
- **Type Hinting:** Strictly define types for all functions and class members.
- **Logging:** Use the project-wide logger.
- **Async/Await:** Ensure all I/O bound operations are non-blocking.

---

## Key Files Summary
- `nexus_core/main.py`: Application entry point.
- `nexus_core/bot.py`: Main Bot class and persistent message worker.
- `nexus_core/config.py`: Global configuration and **VIX Battle Ladder**.
- `nexus_core/market_time.py`: NYSE market calendar.
- `nexus_core/services/trading_service.py`: Centralized business logic orchestrator.
- **`services/polymarket_service.py`**: Polymarket whale monitoring.
- `nexus_core/market_analysis/strategy.py`: Quant scanning and filtering pipeline.
- **`nexus_core/market_analysis/volatility_inspector.py`**: IV Opportunity Detection engine (Volatility Strategist).
- `nexus_core/market_analysis/psq_engine.py`: PowerSqueeze engine.
- `nexus_core/market_analysis/risk_engine.py`: NRO risk optimizer.
- `nexus_core/market_analysis/hedging.py`: Beta-Weighted Delta tracking.
- `nexus_core/market_analysis/ghost_trader.py`: Virtual Trade Replicator.
- `nexus_core/market_analysis/portfolio.py`: Portfolio Greeks refresh.
- `nexus_core/market_analysis/pro_management.py`: Financial Runway analysis.
- `nexus_core/database/user_settings.py`: User profile and context management.
- `nexus_core/database/notifications.py`: Persistent notification queue.
- `nexus_core/database/holdings.py`: Independent equity asset accounting.
- `nexus_core/cogs/embed_builder.py`: Discord UI/UX generator.
- **`nexus_core/cogs/analyst_agent.py`**: Wall Street Analyst Agent.
- `nexus_core/database/core.py`: SQLite migration engine.
- `nexus_edge_scraper/local_api.py`: Playwright-based scraping endpoint.
