# 🌌 Nexus Seeker - GEMINI.md

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
- **`market_analysis/`**: The quant engine. Contains strategy logic (with VIX ladder gating and delta capping), Greek calculations, PowerSqueeze (PSQ) scoring (with VIX-aware momentum labeling), hedging simulations, NRO risk optimization (with dynamic Kelly scaling and All-in bypass), and margin analysis.
- **`database/`**: Persistent storage layer with an automated migration engine (`database/core.py`) that scans `database/migrations/` on startup.
- **`services/`**: Business logic layer (TradingService, LLMService, PolymarketService, MarketDataService, NewsService, RedditService) that decouples the Discord UI from core computations.
- **`cogs/`**: Discord extensions implementing slash commands and background tasks (Market Scanning, VTR monitoring, Daily Reports, Analyst Agent).
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

### Testing
Tests are located in `nexus_core/tests/`.
- **Run all tests:**
  ```bash
  docker compose run --rm nexus-seeker python -m unittest discover -s tests -v
  ```
- **Integration Tests:** Focused on database migrations, trading flows, and LLM/Risk engine integration.

---

## Development Conventions

### 1. Database Migrations
Never modify the database schema manually. Use the migration engine:
- Create a new file in `nexus_core/database/migrations/` (e.g., `v017_new_feature.py`).
- Export `version` (int), `description` (str), and `sql` (str).
- The bot will automatically apply it on the next startup.

### 2. Discord Commands (Cogs)
New commands should be added as **Slash Commands** within a Cog in `nexus_core/cogs/`.
- Use `discord.app_commands` for slash command definitions.
- All replies should ideally be **ephemeral** (`ephemeral=True`) to maintain multi-tenant privacy.
- For long-running tasks, use `bot.queue_dm(user_id, message, embed)` to send asynchronous notifications via the background message worker.

### 3. Market Analysis & Strategy
- Core logic belongs in `nexus_core/market_analysis/strategy.py`.
- Use the `AlertFilter` in `services/alert_filter.py` to implement noise reduction (e.g., EMA crossovers, multi-timeframe alignment).
- `GhostTrader` (`market_analysis/ghost_trader.py`) handles the Virtual Trading Room (VTR) logic, simulating entries and tracking virtual performance. VTR auto-entry is gated by VIX tier permissions (`vtr_entry_allowed`).
- `PSQ Engine` (`market_analysis/psq_engine.py`) provides the PowerSqueeze momentum indicator with VIX-aware labeling (`OVEREXTENDED_RISK`, `HIGH_CONVICTION_RECOVERY`).
- `NRO Risk Engine` (`market_analysis/risk_engine.py`) provides portfolio risk optimization with inverted VIX weights (high VIX = offensive posture), dynamic Kelly scaling (1/4 to 1/2 Kelly), and All-in bypass for VIX > 35.

### 5. VIX Battle Ladder
The VIX Battle Ladder is a 6-tier system defined in `config.py` (`VIX_LADDER_CONFIG`) that dynamically governs risk appetite across all analysis modules:
- **Dormant** (VIX < 15): Hard-reject all STO signals and VTR entries. `w_vix = 0.0`.
- **Caution** (15-18): Cap delta at -0.12, reduce sizing by 50%. `w_vix = 0.5`.
- **Ready** (18-24): Standard mode. `w_vix = 1.0`.
- **Aggressive** (24-30): Offensive posture, sizing 1.2x. `w_vix = 1.2`.
- **Heavy** (30-35): Sizing 1.5x, dynamic Kelly scaling. `w_vix = 1.5`.
- **All-in** (>= 35): Sizing 2.0x, 1/2 Kelly override, bypass oil/regime dampening. `w_vix = 2.0`.

VIX spot is fetched once per scan cycle in `TradingService.run_market_scan()` and propagated to `analyze_symbol()`, `analyze_psq()`, and VTR entry gating. The `vix_battle_status` dict is injected into result data for UI rendering via `embed_builder.py`. The `get_vix_tier()` helper ensures system resilience by defaulting to the "Ready" tier if input is `None` or `NaN`.

When modifying VIX ladder behavior:
- Tier definitions: Edit `VIX_LADDER_CONFIG` in `config.py`.
- Strategy gating: See `apply_vix_ladder()` in `strategy.py`.
- Risk scaling: See `optimize_position_risk()` in `risk_engine.py`.
- PSQ labeling: See `analyze_psq()` in `psq_engine.py`.
- UI rendering: See `_add_vix_battle_status_field()` in `embed_builder.py`.
- Tests: `tests/unit/test_vix_ladder.py` (34 cases).

### 4. Code Style
- **Type Hinting:** Strictly define types for all functions and class members.
- **Logging:** Use the project-wide logger (`logging.getLogger(__name__)`).
- **Async/Await:** Ensure all I/O bound operations (API calls, DB queries) are non-blocking. Use `asyncio.to_thread` for blocking yfinance calls.

---

## Key Files Summary
- `nexus_core/main.py`: Application entry point.
- `nexus_core/bot.py`: Main Bot class and background worker initialization.
- `nexus_core/config.py`: Global configuration — env vars, strategy Delta params, **VIX Battle Ladder** tier definitions (`VIX_LADDER_CONFIG`), and `get_vix_tier()` helper (with NaN robustness).
- `nexus_core/market_time.py`: NYSE market calendar and timezone-aware scheduling.
- `nexus_core/services/trading_service.py`: Centralized business logic orchestrator. Propagates `vix_spot` through scan pipeline, gates VTR entry by tier.
- **`services/polymarket_service.py`**: Real-time Polymarket whale monitoring service via WebSocket. Handles filtering, market metadata fetching, and LLM integration. **Includes connection status tracking and health monitoring.**

- `nexus_core/market_analysis/strategy.py`: Quant scanning and filtering pipeline. VIX ladder gating (`apply_vix_ladder()`), delta capping, and sizing multiplier.
- `nexus_core/market_analysis/psq_engine.py`: PowerSqueeze momentum calculation engine with VIX-aware labeling.
- `nexus_core/market_analysis/risk_engine.py`: NRO risk optimizer — inverted VIX macro weights, dynamic Kelly scaling, All-in bypass.
- `nexus_core/market_analysis/ghost_trader.py`: Virtual Trade Replicator and VTR logic.
- `nexus_core/cogs/embed_builder.py`: Discord UI/UX generator — renders VIX Battle Status field, momentum labels, and tier-colored embeds.
- **`nexus_core/cogs/analyst_agent.py`**: Scheduled Wall Street Quantitative Analyst Agent that pushes macro and quantitative reports using NYSE dynamic scheduling (Pre-market, Intra-day heartbeat, Post-market). Features multi-factor macro alerts (Yield Curve Spread, DXY, VIX).
- `nexus_core/database/core.py`: SQLite migration engine core logic.
- `nexus_edge_scraper/local_api.py`: Playwright-based scraping endpoint.
