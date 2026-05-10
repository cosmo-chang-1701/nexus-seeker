# 🌌 Nexus Seeker - AGENTS.md

## Project Overview
Nexus Seeker is a multi-tenant **Options Quant Risk-Control & Trading Operations Platform** driven by Discord. It combines technical analysis, Black-Scholes-Merton pricing models, LLM-based NLP risk sentiment analysis, and a custom **Nexus Risk Optimizer (NRO)** for advanced portfolio exposure management. The system utilizes a **6-tier VIX Battle Ladder** for dynamic risk scaling and is optimized for stable operation on memory-constrained (1GB RAM) VPS environments.

### Key Technologies
- **Language:** Python 3.12
- **Frameworks:** `discord.py` (Discord Bot), `FastAPI` (Edge Scraper API)
- **Market Data:** `finnhub-python`, `yfinance`, `pandas-ta`, `py_vollib` (Greeks/Pricing)
- **Quant Math:** `numpy`, `pandas`, `scipy`
- **AI/LLM:** OpenAI-compatible API with `pydantic` structured outputs and memory safety gates
- **Database:** SQLite (v032+) with an automated migration engine and JSON metadata support
- **Infrastructure:** Docker, Docker Compose, Cloudflare Tunnel, psutil (System Health)

---

## Architecture
The system is divided into two main services:
1.  **`nexus_core`**: The central Discord Bot. Handles user commands, portfolio management, risk engine calculations, and autonomous market monitoring.
2.  **`nexus_edge_scraper`**: A specialized service (intended to run locally or via tunnel) that uses Playwright to scrape Reddit sentiment and consensus scores without triggering bot detection.

### Core Modules (`nexus_core`)
- **`config.py`**: Global configuration and the **VIX Battle Ladder** (Dormant/Caution/Ready/Aggressive/Heavy/All-in).
- **`market_analysis/`**: The quant engine.
  - **`strategy.py`**: Core strategy logic with VIX ladder gating and delta capping.
  - **`sentiment_engine.py`**: **Volatility Strategist**. Calculates Skew, PCR, Max Pain, and detects Unusual Options Activity (UOA).
  - **`risk_engine.py`**: NRO risk optimization with dynamic Kelly scaling and Vega-adjusted Delta (Vanna) calculations.
  - **`attribution.py`**: **Self-Evolving Attribution System**. Analyzes hedge efficiency (Protection Score) and provides NRO parameter feedback.
  - **`ghost_trader.py`**: Virtual Trading Room (VTR) and autonomous DITM defense.
  - **`ddp_inspector.py`**: Davis Double Play (DDP) detection (EPS Momentum + P/E expansion).
  - **`psq_engine.py`**: PowerSqueeze (PSQ) scoring with VIX-aware momentum labeling.
- **`database/`**: Persistent storage layer with an automated migration engine. Includes unified asset lifecycle tracking and sentiment history.
- **`services/`**: Business logic layer.
  - **`hedge_monitor_service.py`**: Automated Hedging & Alert Pipeline. Monitors VIX spikes and pushes actionable SPY hedge instructions.
  - **`memory_manager.py`**: System health watchdog optimized for 1GB RAM. Handles periodic GC and emergency OOM alerts.
  - **`polymarket_service.py`**: Prediction market whale monitoring with real-time snapshot mechanisms for attribution.
  - **`llm_service.py`**: Structured AI analysis with memory safety gates.
- **`cogs/`**: Discord extensions.
  - **`terminal.py`**: High-impact commands (`/scan`, `/vtr_stats`, `/sys_health`, `/add_holding`).
  - **`sentiment.py`**: Sentiment analytics terminal (`/skew_scan`, `/max_pain`).
  - **`hedging.py`**: Risk settlement and attribution commands (`/settle_hedge`, `/hedge_list`).
  - **`intelligence.py`**: Market edge detection (`/poly_list`, `/scan_news`, `/quote`).

---

## Building and Running

### Prerequisites
- Docker & Docker Compose
- Discord Bot Token & Finnhub API Key
- OpenAI-compatible LLM endpoint

### Development Setup
1.  **Configure Environment:**
    ```bash
    cp nexus_core/.env.example nexus_core/.env
    # Fill in DISCORD_TOKEN, FINNHUB_API_KEY, LLM_API_BASE, etc.
    ```
2.  **Start Services:**
    ```bash
    # Start Core Bot
    cd nexus_core
    docker compose up -d --build
    ```

### Deployment Strategy
The system is optimized for **Low-RAM VPS** deployment:
1.  **Bounded Caching**: All in-memory caches (SMA/EMA/Poly) use LRU policies with a 500-entry limit to prevent memory leaks.
2.  **Memory Gates**: LLM tasks are automatically downgraded if system RAM usage exceeds 85%.
3.  **Graceful Handoff**: Docker Swarm `start-first` configuration with 60s grace period for notification queue drainage.

### Testing
Tests are located in `nexus_core/tests/`.
- **Mandate:** All tests MUST be executed using `pytest`.
- **Framework:** `pytest` with `pytest-asyncio` and `pytest-mock`.
- **Coverage:** Core engines (NRO/Sentiment) must achieve >90% code coverage.
- **Run all tests:**
  ```bash
  cd nexus_core && .venv/bin/python -m pytest --cov=market_analysis --cov=services --cov-report=term-missing
  ```
- **Persistence:** Tests use a shared in-memory SQLite database (`file:testdb?mode=memory&cache=shared`) with full migration (v032+) support.

---

## Development Conventions

### 1. Database Migrations
Never modify the database schema manually. Use the migration engine:
- Create a new file in `nexus_core/database/migrations/` (e.g., `v032_add_hedge_logs.py`).
- Export `version`, `description`, and `sql`. Use `migrate_data` for JSON transformations.

### 2. Discord Commands (Cogs)
- All user-facing strings MUST be **Traditional Chinese (zh-tw)**.
- Use `ephemeral=True` for private settings and portfolio commands.
- Long-running analytics should use `bot.queue_dm()` to prevent interaction timeouts.

### 3. Unified Asset Lifecycle
Assets transition through a persistent state machine in the `assets` table (v028+):
- **WATCH**: tickers monitored for technical setup.
- **TRADE**: Active option positions with real-time Greek tracking ($\Delta, \Gamma, \nu, \theta$).
- **HOLDING**: Settled equity assets contributing to Beta-Weighted Delta.

### 4. Memory Optimization
- Use `ConfigDict(slots=True)` for all Pydantic models.
- Prefer `BoundedCache` over standard `dict` for frequently updated data.
- Trigger `gc.collect()` after large batch operations to reclaim RAM.

---

## Key Files Summary
- `nexus_core/bot.py`: Main Bot and service orchestrator.
- `nexus_core/config.py`: Global constants and **VIX Ladder**.
- `nexus_core/services/trading_service.py`: 4-stage validation pipeline.
- `nexus_core/services/hedge_monitor_service.py`: Automated risk defense.
- `nexus_core/market_analysis/sentiment_engine.py`: Skew/PCR/UOA logic.
- `nexus_core/market_analysis/risk_engine.py`: NRO & Vanna adjustment.
- `nexus_core/market_analysis/attribution.py`: Protection scoring & self-evolution.
- `nexus_core/services/memory_manager.py`: VPS stability watchdog.
- `nexus_core/cogs/embed_builder.py`: Centralized UI/UX generator.
