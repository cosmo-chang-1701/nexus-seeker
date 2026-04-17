# 🌌 Nexus Seeker - GEMINI.md

## Project Overview
Nexus Seeker is a multi-tenant **Options Quant Risk-Control & Trading Operations Platform** driven by Discord. It combines technical analysis, Black-Scholes-Merton pricing models, LLM-based NLP risk sentiment analysis, and a custom **Nexus Risk Optimizer (NRO)** for portfolio exposure精算. The project is structured as a dual-service architecture to handle complex market data processing and edge-scraping tasks efficiently.

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
- **`market_analysis/`**: The quant engine. Contains strategy logic, Greek calculations, PowerSqueeze (PSQ) scoring, hedging simulations, and margin analysis.
- **`database/`**: Persistent storage layer with an automated migration engine (`database/core.py`) that scans `database/migrations/` on startup.
- **`services/`**: Business logic layer (TradingService, LLMService, MarketDataService, NewsService, RedditService) that decouples the Discord UI from core computations.
- **`cogs/`**: Discord extensions implementing slash commands and background tasks (Market Scanning, VTR monitoring, Daily Reports).
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
- `GhostTrader` (`market_analysis/ghost_trader.py`) handles the Virtual Trading Room (VTR) logic, simulating entries and tracking virtual performance.
- `PSQ Engine` (`market_analysis/psq_engine.py`) provides the PowerSqueeze momentum indicator.

### 4. Code Style
- **Type Hinting:** Strictly define types for all functions and class members.
- **Logging:** Use the project-wide logger (`logging.getLogger(__name__)`).
- **Async/Await:** Ensure all I/O bound operations (API calls, DB queries) are non-blocking. Use `asyncio.to_thread` for blocking yfinance calls.

---

## Key Files Summary
- `nexus_core/main.py`: Application entry point.
- `nexus_core/bot.py`: Main Bot class and background worker initialization.
- `nexus_core/market_time.py`: NYSE market calendar and timezone-aware scheduling.
- `nexus_core/services/trading_service.py`: Centralized business logic orchestrator.
- `nexus_core/market_analysis/strategy.py`: Quant scanning and filtering pipeline.
- `nexus_core/market_analysis/psq_engine.py`: PowerSqueeze momentum calculation engine.
- `nexus_core/market_analysis/ghost_trader.py`: Virtual Trade Replicator and VTR logic.
- `nexus_core/database/core.py`: SQLite migration engine core logic.
- `nexus_edge_scraper/local_api.py`: Playwright-based scraping endpoint.
