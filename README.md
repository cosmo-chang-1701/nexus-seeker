# 🌌 Nexus Seeker

**Multi-tenant Options Quantitative Trading Assistant — powered by Discord**

[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
[![Deploy](https://github.com/cosmo-chang-1701/nexus-seeker/actions/workflows/deploy.yml/badge.svg)](https://github.com/cosmo-chang-1701/nexus-seeker/actions/workflows/deploy.yml)
[![Architecture](https://img.shields.io/badge/architecture-multi--tenant-purple.svg)](#architecture)

> A **multi-tenant options quantitative assistant** built with Python & Docker.
> It combines technical analysis, the **Black-Scholes** pricing model, and a fully automated NYSE trading calendar to help traders execute high-probability options selling strategies (The Wheel / Credit Spreads).

---

## Table of Contents

- [Key Features](#-key-features)
- [Architecture](#-architecture)
- [Tech Stack](#-tech-stack)
- [Quick Start](#-quick-start)
- [Discord Commands](#-discord-commands)
- [Portfolio Workflow](#-portfolio-workflow)
- [Strategy Logic](#-strategy-logic)
- [Project Structure](#-project-structure)
- [Testing](#-testing)
- [Contributing](#-contributing)
- [Roadmap](#-roadmap)
- [License](#-license)

---

## ✨ Key Features

| Feature | Description |
|---|---|
| 🔐 **Multi-tenant & Privacy** | All slash-command replies are **ephemeral** (visible only to the invoking user). Each user gets an isolated database namespace keyed by Discord User ID. |
| 📨 **DM Dispatcher** | Background schedulers perform **API de-duplication** across all users, then route personalised quantitative reports to each user's DM. |
| 🎯 **Delta Precision Scan** | Built-in Black-Scholes engine (`py_vollib`) auto-calculates the optimal strike for a target Delta (e.g. −0.20 ≈ 80 % win-rate). |
| 📡 **NYSE Auto-Scheduler** | Integrates `pandas_market_calendars` with DST & holiday handling — 3 daily triggers at 09:00 / 09:45 / 16:15 ET. |
| 📊 **Market Maker Move** | Computes ATM Straddle-based expected move (MMM) before earnings to flag "mine-field" strikes. |
| ⚖️ **Quarter-Kelly Sizing** | Calculates position size with a ¼-Kelly criterion, capped at 5 % per symbol. |
| 📈 **IV Term Structure** | Detects 30D/60D IV backwardation as a panic-selling signal. |
| 💾 **Data Persistence** | SQLite backed by Docker Volume — zero data loss across container restarts. |

---

## 🏗 Architecture

```
Discord Users ──► Discord API ──► Nexus Seeker Bot
                                       │
                     ┌─────────────────┼──────────────────┐
                     │                 │                  │
              Slash Commands     DM Dispatcher     NYSE Scheduler
              (ephemeral)       (background)       (3 daily tasks)
                     │                 │                  │
                     └────────┬────────┘                  │
                              │                           │
                        ┌─────▼──────┐            ┌───────▼───────┐
                        │  database  │            │  market_math  │
                        │  (SQLite)  │            │  (BS Model)   │
                        └────────────┘            └───────────────┘
```

### Scheduled Tasks

| Time (ET) | Task | Description |
|---|---|---|
| **09:00** | Pre-market Risk Monitor | Scans earnings calendar; DMs a ⚠️ IV-Crush alert if earnings ≤ 3 days away. |
| **09:45** | Delta Neutral Scan | Runs technical + Greeks scan on each user's watchlist; DMs actionable signals. |
| **16:15** | After-hours Report | Marks-to-market all positions; DMs P&L, stop-profit, and rolling defence suggestions. |

---

## 🛠 Tech Stack

| Layer | Technology |
|---|---|
| **Language** | Python 3.12 |
| **Discord** | `discord.py` ≥ 2.3 — Slash Commands, DM routing |
| **Market Data** | `yfinance` (quotes), `pandas-ta` (indicators), `py_vollib` (Black-Scholes) |
| **Scheduling** | `pandas_market_calendars`, `zoneinfo` |
| **Database** | SQLite — composite unique keys per `user_id` |
| **Infra** | Docker, Docker Compose, GitHub Actions CI/CD → DigitalOcean |

---

## 🚀 Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) & [Docker Compose](https://docs.docker.com/compose/install/)
- A [Discord Bot Token](https://discord.com/developers/applications)

### 1. Clone & prepare

```bash
git clone https://github.com/cosmo-chang-1701/nexus-seeker.git
cd nexus-seeker
mkdir -p data          # SQLite persistent volume mount
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your token:

```env
DISCORD_TOKEN=your_discord_bot_token_here
```

### 3. Launch

```bash
docker compose up -d --build
```

Verify the bot is running:

```bash
docker compose logs -f
```

> **Upgrading from v1?** Delete old SQLite files in `data/` so the schema is rebuilt with the `user_id` column.

---

## ⌨️ Discord Commands

All commands use Discord native **Slash Commands** with built-in parameter validation.
Responses are **ephemeral** — only the invoking user can see them.

### 📡 Watchlist

| Command | Description | Example |
|---|---|---|
| `/add_watch` | Add a symbol to your watchlist | `symbol: TSLA` |
| `/list_watch` | View all watched symbols | — |
| `/remove_watch` | Remove a symbol | `symbol: ONDS` |
| `/scan` | Manual Delta-neutral scan on a symbol | `symbol: SMR` |

### 💼 Portfolio

| Command | Description | Example |
|---|---|---|
| `/add_trade` | Record a real trade for monitoring | See below |
| `/list_trades` | View positions, P&L, and trade IDs | — |
| `/remove_trade` | Remove a closed position by ID | `trade_id: 1` |
| `/set_capital` | Set your total capital for Kelly sizing | `capital: 50000` |

<details>
<summary><strong><code>/add_trade</code> Parameters</strong></summary>

| Parameter | Type | Description | Example |
|---|---|---|---|
| `symbol` | string | Ticker symbol | `SOFI` |
| `opt_type` | choice | `Put` or `Call` | `Put` |
| `strike` | float | Strike price | `7.5` |
| `expiry` | string | Expiration date (`YYYY-MM-DD`) | `2026-04-17` |
| `entry_price` | float | Premium received/paid per contract | `0.55` |
| `quantity` | int | Positive = Long, **Negative = Short** | `-5` |

</details>

---

## 🔄 Portfolio Workflow

```
┌───────────────┐     ┌────────────────┐     ┌─────────────────┐
│  1. Signal    │────►│  2. Record     │────►│  3. Monitor     │
│  Receive DM   │     │  /add_trade    │     │  Auto at 16:15  │
└───────────────┘     └────────────────┘     └────────┬────────┘
                                                      │
                                                      ▼
                                             ┌─────────────────┐
                                             │  4. Decision    │
                                             │  via DM alert   │
                                             └────────┬────────┘
                                                      │
                      ┌───────────────────────────────┼───────────────────────────┐
                      │                               │                           │
               🟢 Profit ≥ 50%                 🔴 DTE < 14 & Loss        ⚫ Loss ≥ 150%
               Buy to Close                    Roll Defence              Stop Loss
                      │                               │                           │
                      └───────────────────────────────┼───────────────────────────┘
                                                      │
                                                      ▼
                                             ┌─────────────────┐
                                             │  5. Close       │
                                             │  /remove_trade  │
                                             └─────────────────┘
```

---

## 📈 Strategy Logic

The quantitative engine (`market_math.py`) implements four strategies, each gated by technical filters and refined by Black-Scholes Greeks.

### 🟢 Sell To Open Put — *Oversold Income*

- **Trigger:** `RSI(14) < 35` + `HV Rank ≥ 30`
- **Contract:** 30–45 DTE, Delta ≈ **−0.20** (~80 % OTM probability)
- **Filter:** `AROC ≥ 15 %`, Kelly-sized

### 🔴 Sell To Open Call — *Overbought Income*

- **Trigger:** `RSI(14) > 65` + `HV Rank ≥ 30`
- **Contract:** 30–45 DTE, Delta ≈ **+0.20**
- **Filter:** `AROC ≥ 15 %`, Kelly-sized

### 🚀 Buy To Open Call — *Momentum Breakout*

- **Trigger:** Price > `20 SMA` + `50 ≤ RSI(14) ≤ 65` + `MACD Histogram > 0`
- **Contract:** 14–30 DTE, Delta ≈ **+0.50** (ATM)

### ⚠️ Buy To Open Put — *Breakdown / Hedge*

- **Trigger:** Price < `20 SMA` + `35 ≤ RSI(14) ≤ 50` + `MACD Histogram < 0`
- **Contract:** 14–30 DTE, Delta ≈ **−0.50** (ATM)

---

## � Project Structure

```
nexus-seeker/
├── main.py                  # Bot entry point & extension loader
├── config.py                # Environment variables & model parameters
├── database.py              # SQLite CRUD — multi-tenant (user_id keyed)
├── market_math.py           # Quantitative engine (BS, Greeks, HVR, MMM, Kelly)
├── market_time.py           # NYSE calendar & dynamic sleep scheduler
├── cogs/
│   └── trading.py           # Slash commands, DM dispatcher, scheduled tasks
├── tests/
│   ├── __init__.py
│   └── verify_market_functions.py
├── data/                    # SQLite DB (Docker volume mount)
├── .github/
│   └── workflows/
│       └── deploy.yml       # CI/CD — Build → GHCR → DigitalOcean Swarm
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
└── LICENSE
```

---

## 🧪 Testing

Tests are run inside a Docker container:

```bash
docker compose run --rm nexus_seeker python -m pytest tests/ -v
```

---

## 🤝 Contributing

1. **Fork** this repository
2. Create a feature branch: `git checkout -b feat/awesome-feature`
3. Commit your changes: `git commit -m "feat: add awesome feature"`
4. Push to the branch: `git push origin feat/awesome-feature`
5. Open a **Pull Request**

Please follow [Conventional Commits](https://www.conventionalcommits.org/) for commit messages.

---

## 🔮 Roadmap

- [ ] **Argo Cortex** — Local LLM (vLLM + Qwen/Llama on NVIDIA 5070 Ti) for sentiment analysis; auto-veto signals on destructive fundamental news.
- [ ] **MCP Server** — Package core quantitative modules as standard Model Context Protocol tools for external AI agents.
- [ ] **Broker API Integration** — Interactive Brokers Gateway for fully automated order execution (signal → execution → close, zero human intervention).

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).

---

<div align="center">

*Built with ❤️ by [Cosmo Chang](https://github.com/cosmo-chang-1701) for Quantitative Freedom.*

</div>