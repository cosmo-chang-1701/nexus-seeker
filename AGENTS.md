# 🌌 Nexus Seeker - AGENTS.md

## Project Overview

Nexus Seeker is a multi-tenant **Discord-first options risk-control and trading operations platform**. It combines technical structure, Black-Scholes-Merton pricing, Greeks-based portfolio risk, event-aware calendar defenses, and LLM-assisted structured commentary.

Current released core version: **`1.12.36`**


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

### In `cogs/trading/`

- `fundamental_filing_scan` — **08:00 ET** (holdings-only, skips non-trading days)
- `daily_reddit_update` — **08:30 ET**
- `pre_market_risk_monitor` — **09:00 ET**
- `dynamic_market_scanner` — **every 30 minutes during market hours**
- `wti_oil_monitor` — **every 30 minutes (24/7, 00:00–06:00 ET quiet hours)**
- `price_volume_alert_monitor` — **every 15 minutes during market hours**
- `monitor_real_portfolio_task` — **every 30 minutes during market hours**
- `dynamic_after_market_report` — **16:15 ET**
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

---

## Quantitative Radar Terminal & Cache-Aside Architecture

The system features a low-latency, high-information-density **Trader Terminal Radar Panel** (accessed via `/x`). To ensure Discord response times are strictly under **100ms** and prevent 3-second timeouts, the `/x` command initially renders an interactive **Unified Radar Panel** UI with scope selection and quant filters. It completely avoids LLM calls and real-time database queries during UI initialization. For power users, the legacy parameter bypass (`/x scan_type: ALL | HOLDINGS | ORDERS | OPTIONS | WATCHLIST`) remains available.

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
- **Unified Radar Filters**: The terminal UI consolidates Risk Defense and Alpha Signal filters into a single dropdown, fully integrated with `ScanParams` for deep evaluation:
  - **Risk Defenses**: Excludes martial law bounds (`exclude_martial_law`), prevents silent period events (`avoid_silent_period`), and shields against extreme dark pool distribution (`dp_skew_defense` filtering `skew < -0.3`).
  - **Alpha Signals & Advanced Gates**: Filters for Triple Discount Pricing (`tdp_mode`), Volatility Squeeze firing (`squeeze_mode`), Strict UOA institutional activity (`uoa_mode`), High-Deviation Magnetic Filters (`magnetic_filters`), plus new defensive layers including **UOA Barrier, Gravity Filter, and Divergence Gate**.
- **Real-time Insights**: Automatically matches active pending orders or option protection strategies (e.g., triggering pull-back alerts or tail-risk warnings). Now rendered inside a dedicated ANSI markdown code block for easy one-click copying.

### 3. Rendering Layer (`build_radar_scan_embed`)
The terminal radar card is built inside `cogs/embed_builders/` using `build_radar_scan_embed()`, keeping with the **Single Source of Truth** for embeds. It prints an interactive Markdown table (which replaced the legacy ANSI format for better aesthetics) showing key quantitative and Alpha fields:
- **`標的`**: Dynamic `⚠️` prefix for >10% deviation, negative gamma, or structural anomalies for instant visual triage.
- **`G/P-Wall(±)`**: Call Wall + Put Wall with dynamic Net GEX and PutWall break polarity (e.g. `(+) $227.5 / $220.0` or `(-) $109.0 / $108.0`), automatically switching to `(-)` when price breaks below PutWall or global Net GEX is negative, with `N/A` fallback when open interest data is unavailable.
- **`Skw%`**: True Skew percentile alongside actual Skew value (e.g. `51% (-0.29%)`), accurately evaluating dealer tail-risk pricing.
- **`SQZ向量`**: Squeeze Momentum Vector with timer/squeezing indicator (e.g. `⏱️🟢+12.7`, `🟢+19.6`, or `⚪+0.0`), dynamically integrated with **UOA Barrier Index** (downgrading bullish vectors to `⚪` if massive institutional call walls block upside).
- **`Neg-GEX`**: Net GEX deviation distance percentage.
- **`STO 鎖死`**: Formatted Short-to-Open strikes (e.g. `C$227.5 / P$237.5` or `P$110.0`) or Straddle STO density.
- **`IV 策略`**: IV Strategy Match with strict Negative Gamma circuit breaker forcing `🔴賣方禁售` during dealer sell-off cascades, `🔴CSP 禁售` for $IVR < 15\%$, and `🟢適宜賣方` for healthy environments.
- **`EM Z-Score`**: Normalized Expected Move standard deviation position (e.g. `+0.00σ`, `+0.05σ`).
- **`Top UOA`**: Single strongest whale print (e.g. `🛡️ 08/15 $227.5C (STO 261k)` or `🔥 08/15 $220.0C (BTO 15k)`).
- **`暗池大宗交易 (Dark Pool Block Prints)`**: Automatically alerts users in Real-time Insights when block prints $\ge \$5\text{M}$ appear (e.g. `• 🧱 CRWV: 暗池在 $101.68 爆出 $48.85M 巨額大宗買盤，形成籌碼水泥牆支撐。`).
- **`防洗盤絕對防守位 (Anti-Washout Stop)`**: Dynamically calculated as $PutWall - 1.5 \times ATR_{14}$, providing solid buffers against liquidity grabs.
- **`離場判定鐵律`**: Enforces `"🛑 離場判定鐵律：嚴守 15 分鐘實體 K 線收盤撤退線 (過濾下影線流動性獵殺)"` in table notes.
- **`灰階戰術建議 (Gray-scale Tactical Guidance)`**: Multi-dimensional evaluation engine preventing binary stop-outs (e.g. if price breaks PutWall but remains above anti-washout stop with positive gamma support, recommends `🟡 護航網支撐，現貨續抱，防守退至 $103.80 (嚴守15分K收盤)`). Redundant markdown bold formatting has been removed for consistent ANSI rendering.

### 4. 避免 Discord 回應錯誤的長度分段與分頁原則
為防範當自選標的 (Watchlist) 或持倉 (Holdings) 數量過大時，因 Embed Description 超過 Discord 的 4096 字元上限而導致 `400 Bad Request (error code: 50035): Invalid Form Body` 系統錯誤，系統實施以下長度分段與分頁原則：
- **最大分段間距 (Chunk Size)**：批次掃描結果一律以每頁最多 **10 個標的**進行分組封裝。
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

The active embed builder is `create_watchlist_signal_embed()` in `cogs/embed_builders/`.

The embed title is dynamically injected with the ticker's active tags fetched from the multi-tenant `watchlist_tags` table (e.g. `標的分析中心 2.0: AAPL 每半小時戰場心跳 🏷️ TECH | CORE`).
The delivery of watchlist heartbeat is controlled by the unified `heartbeat_watchlist` toggle in `/notif_settings` (which delivers a complete, rich multi-layered snapshot including options structure, deterministic Skew, event risk, stock pricing, share sizing, and UOA whale prints).

Current sections:

1. **🧱 心跳：期權結構與波動率** (Technical/Options Snapshot ANSI Panel)
2. **📐 Skew 與市場判讀** (Skew Interpretation ANSI Panel - aligned with Sentiment Scan style)
3. **⚙️ 量化 Skew 解析** (Deterministic Skew Rules)
4. **🗓️ 事件風控** (Event Risk Management Summary)
5. **🛡️ 心跳：操盤指引與委託風控** (Holdings & Trading Guide ANSI Panel - dynamically calculates suitable entry/exit prices and shares sizing)
6. **🎯 執行建議** (Execution Suggestions - with options suggestions aligned with calculated pricing strikes)
7. **🧾 可執行期權合約與 UOA 巨鯨大單** (Executable Options Contracts & Whale Prints)

### Current heartbeat logic details

- sent **per user, per symbol**
- includes:
  - ANSI snapshot and enriched Unusual Option Activity (UOA) table:
    - UOA entries are processed with `trade_type` (`SWEEP` or `BLOCK`) and `oi_change_net`.
    - Presentational layer tags UOA records visually with `🔥 SWEEP` or `📦 BLOCK` and the corresponding daily Open Interest net change.
    - Top Dark Pool block prints are dynamically displayed along with absolute support resonance (DP-POC overlapping with PutWall). Dirty data (price deviation > 5%) is explicitly filtered out, and the number of filtered records is reported.
  - skew / IV structure interpretation
  - event risk summary
  - executable option plan
  - deterministic rule-based skew commentary
  - **Dynamic Stock Pricing, Share Sizing & Capital Allocation**:
    - Unheld tickers: Calculates a dynamic `suitable_buy_price` based on RSI and Skew (downside fear discount factor) and corresponding shares budget based on user `capital` and `risk_limit`.
    - Held tickers: Calculates a dynamic `suitable_sell_price` and recommended sell shares (25%, 33%, 50%, or 100% exit ratio depending on RSI and scenario like `hard-hedge`).
    - **1.5x ATR 防洗盤緩衝與關卡避開**: Both buy and sell price calculations dynamically apply a `1.5 * atr_14` anti-washout buffer (warning users to enforce the 15-minute candle closing break as final exit line) and automatically avoid psychological round numbers (`.00`, `.50`, `.99`).
    - **動態資金藍圖演算法 (Capital Allocation Model)**: If "機構避險背離" (Skew Divergence) or "負 Gamma" is active in the tactical route, capital allocation dynamically caps the budget and forces 70%~85% of funds to retreat to broad market liquidity assets (such as VOO), reserving only 10%~15% for tactical/arbitrage trading.
    - **(Textual Martial Law)**: If spot drops below the Market Maker PutWall into Negative Gamma, `suitable_buy_price` is locked to N/A, shares size to 0, and all "buy the dip" (Narrative Trap) optimistic wording is forcefully blocked and overwritten with strict "Delta Negative Feedback" warnings.
  - **Strike-Aligned Options Guidance**:
    - Option guidelines are dynamically mapped to target strikes (e.g. CSP at `suitable_buy_price` or Covered Call at `suitable_sell_price`).
  - **Visual Panel Consistency**:
    - Dotted lines ` ----------------------------------` and ` └─ ` indent prefixes matching the Option Sentiment Scan (Sentiment Scan) format.
- option plans are event-aware:
  - earnings proximity reduces risk
  - pre-event windows prefer defined-risk structures
  - macro events shrink size / bias toward debit spreads or protection

### Deterministic Skew Interpretation

`market_analysis.intraday_pipeline.build_watchlist_skew_rule_commentary()`:

- Replaced legacy LLM commentary to ensure 0-latency execution.
- Deterministically evaluates `option_skew`, `skew_percentile`, and `pcr`.
- Triggers absolute tail-risk routes (e.g. Put Panic, Call FOMO) and structural divergence warnings without external API calls.

### Pre-market IV Sentiment Scan & Fallback

During pre-market hours (before 09:30 ET), the options market is closed and live implied volatility (IV) is unavailable. In standard setups, this causes `IV Rank` and `IV Percentile` calculations to fail or return a misleading `0.0%` (which users might mistake for historically cheap IV).

We resolve this via a comprehensive pre-market optimization workflow:
1. **Trading Hours Detection**: The engine checks the market state using `market_time.is_market_open()`.
2. **Database Fallback**: If the market is closed (`not is_market_open()`), it automatically queries the SQLite database `historical_iv` table for the last known closing IV of the symbol and sets it as `current_iv`.
3. **Historical Volatility (HV) Fallback**: If the DB has no history for the symbol, the engine calculates the standard 30-day Historical Volatility (HV) using historical stock close prices as a proxy.
4. **Degradation Gating**: If all options and historical data are unavailable, the engine gracefully degrades and sets the `is_premarket` flag to `True` on the returned `IVMetrics` model.
5. **Presentation Layer Customization**: In `cogs/embed_builders/`, if `is_premarket` is `True`:
   - **Complete Data Absence (`current_iv == 0.0`)**: Appends ` [盤前數據未更新]` to the title and displays friendly placeholders (`--%` and `等待開盤`) to prevent user confusion.
   - **Successful Fallback (`current_iv > 0.0`)**: Appends ` [盤前/前日收盤]` to the title and tags the IV values with `(前日收盤 / 歷史波動率代理)` to clearly report that the data reflects previous closing levels.

---

### Event-Driven Market Scenario Alerts

The `dynamic_market_scanner` in `cogs/trading.py` now includes an independent, event-driven alert system that dynamically triggers when a symbol enters one of six highly specific quantitative market scenarios based on a precise decision tree:

**Branch A: Positive Gamma (Spot > Gamma Flip)**
- **💎 巨鯨護航共振 (Whale Escort Resonance)**: Candle High/Low tests PutWall (GEX Positive Gamma Wall confirmed), Skew Percentile < 50.0% (downside fear is minimal), and UOA institutional flow is aligned (`BTO CALL` or `STO PUT`). Dispatches a dedicated purple alert embed (`discord.Color.purple()`) recommending high win-rate defensive positioning: spot scale-in or Sell Put Spread ("【勝率極值共振】巨鯨實質硬地板成型，建議可於此防禦水位建倉做多或賣出 Put Spread。").
- **黃金左側加碼 (Golden Left-Side)**: Candle High/Low tests PutWall (within 1.5% margin), PutWall overlaps with HVN, and IV Rank > 50%.
- **黃金波段止盈 (Golden Take-Profit)**: Candle High/Low tests CallWall (within 1.5% margin), and CallWall overlaps with HVN. (Suggests Sell Call Spread if IVR > 50%).
- **強勢突破加碼 (Strong Breakout)**: Spot breaks CallWall, lands in an LVN (vacuum acceleration zone), Volume > 1.5x MA20, and IV Rank < 30%.

**Branch B: Negative Gamma / Defense Mode (Spot < Gamma Flip)**
- **假性支撐陷阱 (Fake Support Trap)**: Candle High/Low touches PutWall, strictly blocking narrative "buy the dip" logic.
- **結構破位與轉倉 (Structural Breakdown)**: Spot falls below PutWall AND Gamma Flip, triggering an absolute 100% liquidation directive to QQQ/SPY ETFs. This is protected by the **Gamma Cliff Confirmation Engine** with 15-minute candle close confirmation.

**Architecture & Rate Limiting**:
- **Classifier**: `market_analysis/scenario_classifier.py` mathematically evaluates the market state using live price, PutWall, CallWall, Gamma Flip, IV Rank (IVR), Volume Profile (HVN/LVN), Skew Percentile, and UOA intent alignment.
- **Zero-Latency VP**: Reuses the `df_hist` fetched for PSQ/EMA via `calculate_volume_profile_from_df` to avoid redundant `yfinance` network requests.
- **Embed**: `cogs/embed_builders/alert_embeds.py` generates `create_scenario_alert_embed()`.
- **KV Cache Protection**: To prevent spam during high-volatility boundary oscillations, the system sets an SQLite cache key (`scenario_alert_{user_id}_{symbol}_{date}_{scenario}`) to guarantee a maximum of **one alert per scenario, per symbol, per day**.

---

## StockAliasMatrix & Polymarket Intelligence Engine 2.0

### 1. Abstracted Entity & Alias Matrix (`market_analysis/stock_alias_matrix.py`)
To prevent keyword fragmentation across prediction markets, retail sentiment scrapers, and terminal lookup interfaces, all ticker-to-entity mappings are unified under `StockAliasMatrix`:
- **4-Tier Auto-Populating Resolution Architecture**:
  1. **Tier 1 (Static Map - `STOCK_ALIAS_MAP`)**: Pre-compiled dictionary covering 100+ US tech, biotech, energy, financial equities, and broad market ETFs (0ms lookup).
  2. **Tier 2 (In-Memory LRU Cache - `_dynamic_alias_cache`)**: Caches resolved unlisted symbols in memory for instant subsequent access.
  3. **Tier 3 (Persistent SQLite Cache - `kv_cache`)**: Persists dynamically populated aliases across service restarts (`stock_aliases_{symbol}`).
  4. **Tier 4 (Finnhub / yfinance Profile Auto-Derivation)**: When unlisted symbols (e.g. `RKLB`, `ASTS`, `SOFI`) are queried, the engine dynamically fetches the company profile, cleans legal suffixes (`Inc.`, `Corp.`, `Ltd.`, `Holdings`) using `clean_company_name()`, preserves generic two-word brands (`Super Micro`, `Taiwan Semiconductor`), and automatically writes back to memory and SQLite.
- **Strict Boundary Matching (`is_text_matching_symbol`)**: Uses regex word boundaries (`\b`) for tickers and case-insensitive substring checks for long company aliases to prevent false-positive matching on generic words.

### 2. Polymarket Service Hard Gate & Live Fallback (`services/polymarket_service.py`)
- **Category Hard Gate Overhaul (`_is_relevant_market`)**:
  - Purged keyword bans that falsely rejected legitimate US stocks (removed `NETFLIX`, `SONY`, `RELEASE`, `GAME`, `DATE`, `TIME`, `ACTOR`, `TRAILER`).
  - Retained strict exclusions for pure sports events (NBA, NFL, Premier League, UFC, F1) and entertainment celebrity gossip (Oscar, Grammy, Kardashian, MrBeast).
  - Expanded whitelists for macro rate decisions (FOMC, Rate Cut, PCE, CPI), financial metrics (EPS, Revenue, Buyback, Dividend, Antitrust, Merger), AI breakthroughs (OpenAI, Blackwell, Robotaxi, FSD), and key equities.
- **Gamma API Live Online Fallback**:
  - `search_markets(query, limit, active_only)`: Searches local active markets; if fewer than `limit`, queries `https://gamma-api.polymarket.com/public-search?q={query}`.
  - `get_symbol_markets(symbol, limit, active_only)`: Uses `StockAliasMatrix` to expand ticker and aliases, filters by `is_text_matching_symbol`, and sorts by trading volume descending (`volumeNum`).

### 3. Reddit Sentiment Boolean Search Optimization (`reddit_service.py` & `local_api.py`)
- **Boolean OR Query Generator (`build_reddit_query`)**: Formats ticker and aliases into precise Boolean search expressions (e.g. `("NVDA" OR "$NVDA" OR "NVIDIA")`).
- **Edge API Integration**: Passes `custom_query` to `/api/v1/scrape/reddit/{symbol}`, ensuring Reddit RSS search matches actual company discussions rather than noisy single-word stems (e.g. searching "Super" for `SMCI`).

### 4. Interactive Commands & Unified Terminal Integration
- **`/poly_list [query]`**: Supports ticker/keyword prediction market queries with volume tags (`💵 $12.5M`, `💵 $54.2k`) and paginated embeds. When results exceed `chunk_size=8`, a `PolymarketPaginatedView` (◀ page indicator ▶) is attached to a **single ephemeral message**, enabling in-place `edit_message()` page navigation instead of sending multiple separate messages.
- **`/market` → 🐋 預測市場 按鈕 (`PulseHubView`)**: Multi-page results are dispatched as a single `followup.send()` with `PolymarketPaginatedView`, preserving the original `PulseHubView` buttons (📊 📅 🔥) on the parent message. `_reset_loading()` includes an `embed=None` guard to prevent accidentally clearing the original embed when the Polymarket result is sent as a separate message.
- **`/x` Terminal Odds Lookup**: `find_matching_polymarket_odds` evaluates multi-alias matches with fallback to online `get_symbol_markets` search.

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
- **Dark Pool Skew Override**: If the derived Dark Pool Skew is strongly negative (`dark_pool_skew < -0.3`), indicating heavy institutional distribution, the router overrides all aggressive strategies and forces a downgrade to **SHIELD** mode.
- **IVR Strategy Gate (IVR 硬鎖閘門)**: If the Implied Volatility Rank (IVR) drops strictly below 10.0%, all selling strategies are hard-locked. The router forces a downgrade to `STANDBY` for sellers or restricts operations to Spot Buy, ITM Call BTO, or Debit Spreads, explicitly preventing physically deadlocked short premium entries in a zero-premium environment.
- **Skew Divergence Gate (機構避險背離/尾部風險警戒)**: If `metrics.skew_percentile > 90.0`, the pipeline automatically sets `sddm_route = "WAIT (機構避險背離/尾部風險警戒)"` and `alert_level = "red"`, blocking all optimistic ratings and enforcing defensive capital allocation (70%~85% back to broad market assets).
- **Momentum Vector Gate (負 Gamma 疊加空頭動能發散)**: If `net_gex < 0` and `metrics.squeeze_momentum < 0`, the pipeline forces `sddm_route = "WAIT (空頭動能發散)"` and `alert_level = "red"`, strictly forbidding range-bound defense or buy signals due to risk of cascade selloffs.

---

## Index Microstructure & Macro Risk Engine Upgrade

The platform implements an advanced macro risk-control layer that dynamically adapts portfolio parameters based on market liquidity, rate projections, and cash risk constraints.

### 1. Index Microstructure & Gamma Flip Gating (`index_microstructure.py`)
- **Regime Evaluation**: Evaluates index liquidity conditions via `get_market_regime()`.
- **SHORT_GAMMA_CRITICAL Detection**:
  - Triggers when: $VIX > 20$ AND $vts\_ratio = \frac{VIX}{VIX3M} \ge 1.0$ (in backwardation) AND $\text{SPY Spot} < \text{Gamma Flip Line}$ (as estimated by the Playwright edge scraper `/api/v1/scrape/macro/gex` route).
- **Symbol GEX Profile Caching**:
  - Individual symbol GEX profiles (Put Wall, Call Wall, Net GEX) fetched from `/api/v1/scrape/options/{symbol}/gex` are cached in `kv_cache` with a 4-hour TTL (`14400` seconds) to optimize rendering speed for the `/x` terminal and reduce edge scraper overhead.
- **Tactical Scaling**:
  - Under `SHORT_GAMMA_CRITICAL`, the watchlist scanner in `intraday_pipeline.py` automatically scales `dynamic_grid_step` by **$1.5\times$** to slow down capital depletion during market washouts.

### 2. CME FedWatch Forecasting, FRED Metrics & Escape Windows
- **Rate Probabilities & Dual Caching**:
  - Crawls FOMC rate probabilities via `/api/v1/scrape/macro/fedwatch` and saves to SQLite (`consensus_value` and `fedwatch_probability` fields in `economic_calendar_events`).
  - Simultaneously caches the latest probability into `kv_cache` (`macro_fedwatch_probability`), ensuring `< 5ms` zero-latency retrieval during dashboard loads.
  - Automatically fetched **daily at 09:00 ET** (pre-market analyst loop) and periodically refreshed **every 4 hours** via `CalendarCog.event_checker`.
- **Core Macro Metrics**: The Edge API (`/api/v1/scrape/macro/core_metrics`) actively fetches live FRED data (RRP, Fed Balance, Unemployment Rate, Sahm Rule Recession Indicator) and CNN Fear & Greed index. This fully automates the Macro Risk Intelligence Center and resolves all static fallback values.
- **Global Macro Hub Integration (`/market`)**:
  - `/market` (`build_market_macro_overview_embed`) integrates `FOMC 利率定價 (FedWatch)` directly into its `📈 流動性與總經指標` ANSI panel, dynamically reporting rate stance (`鷹派高位`, `降息確立`, `均衡定價`, or `[備援]`).
  - Connects rate pricing to `🛡️ 聯動風控引擎狀態` displaying the live `利率逃頂窗口` offset status.
- **Dynamic Escape Window**:
  - The pre-market analyst loop (`analyst_agent.py`) evaluates the probability of rates remaining high ($> 70\%$).
  - If rates remain high (hawkish), it dynamically offsets the user's customized "rebound escape window" (反彈逃頂窗口，支援自訂並自動判定「上/中/下旬」) by **5** business days.
  - If rate cuts are expected ($\le 40\%$), the escape window adjusts forward (risk-on) by **5** business days.

### 3. Active Order Stress Testing (`/stress_test`)
- **Risk Math**:
  $$\text{Total Cash Deficit} = \sum (\text{Active Buy Grid Limit Price} \times \text{Quantity})$$
- **BOXX Buffer and Alerts**:
  - Compares the total cash deficit with the user's available cash + maximum liquidatable $BOXX$ position (capped at 180 shares $\approx \$21,000$).
  - If the potential deficit exceeds the $BOXX$ liquidation limit ($21,000 USD), a critical warning embed is dispatched alerting the user that the payout threshold ($13,000 USD) is endangered.
  - Results are rendered in Traditional Chinese using `NexusEmbed` and integrated into the `/dash` workspace panel (which now unifies the strategic dashboard and financial survival runway analysis).

### 4. Average Cost Basis & Covered Call Recovery Rules
- **New Cost Basis Calculation**:
  $$\text{New Cost Basis} = \frac{(\text{Current Shares} \times \text{Current Cost}) + \sum (\text{GTC Grid Shares} \times \text{Limit Price})}{\text{Current Shares} + \sum \text{GTC Grid Shares}}$$
- **Covered Call Unlock Recommendations**:
  - When assets are locked up, the system generates covered call guidelines to help rebuild runway.
  - Filters option chains for: DTE 30-50 days, Strike > `New Cost Basis`, Delta < 0.15, and annualized yield >= 10.0% (or single premium >= 1.0% of spot).
  - Utilizes 30-day Historical Volatility (HV) or last closing IV as fallback pricing inputs if live option chains are unavailable.
  - **Zero IV Premium & Strong Bullish Momentum Block**: Covered Call alerts are physically blocked if `IVR <= 5.0`, `Squeeze Momentum > 10.0`, and `Spot > PutWall`. This prevents locking up shares against strong momentum rallies when option premiums are artificially cheap.

### 5. Manual Macro Update Controls (Added in v1.7.3)
- **Discord Slash Command**: Administrators can manually update GEX and FedWatch data via `/force_macro_update` in Discord.
- **CLI Command**: Developers or scripts can manually trigger macro crawlers via:
  ```bash
  python cli.py admin force-macro-update
  ```

### 6. Volume Profile, Dark Pool & Triple Discount Pricing (TDP)
- **V-POC & DP-POC Calculation**: The engine calculates the Volume Point of Control (POC) using `pandas-ta` volume profile functions, and fetches the Dark Pool Point of Control (DP-POC).
- **Absolute Support Resonance**: If the DP-POC closely overlaps with the Market Maker's PutWall (< 1% deviation), an absolute support resonance alert is flagged.
- **TDP Signal**: When the current spot price falls below the EMA 21, the option Max Pain level, the V-POC, AND the DP-POC, a `✨ TDP 估值三擊 (Triple Discount Pricing)` signal is activated. This highlights an immensely discounted structural entry point backed by both volume profile and dark pool support.

### 7. Kelly Criterion Risk Sizing
- **Dynamic Allocations**: Utilizing user-configured `capital` and `risk_limit`, the system uses `risk_engine.optimize_position_risk` to calculate the safe number of contracts to buy.
- **Volatility Scaling**: Under extreme VIX conditions, the Kelly fraction is dynamically reduced (e.g., from 0.5 to 0.25), and a scaling penalty is applied to the final contract count. The system outputs these warnings directly into the terminal's `Target Lock` embed.

### 8. Bid-Ask Spread Liquidity Gate
- **Spread Ratio**: Evaluates the option spread against the mid-price: `Spread Ratio = (Ask - Bid) / Mid`.
- **Illiquidity Block**: If the ratio exceeds 15.0%, the contract is flagged as illiquid (`is_illiquid = True`). This prevents execution routing, adds a `⚠️ 流動性警告` (Liquidity Warning) overlay to the tactical terminal, and updates the watchlist heartbeat's Option Plan to a strict `WAIT` state to prevent severe slippage.

---

## Analyst Agent Reporting

`cogs/analyst_agent.py` is responsible for:

- macro scan (with integrated 14-day forward earnings risk window)
- pre-market earnings / valuation adjustment report
- post-market summary
- sector flow / rotation report
- next-day strategy report

### Pre-Market Earnings & Valuation Data Integration & Concurrency Optimizations

**Live path** — `AnalystAgent.pre_market_loop` → `dispatch_pre_market_briefing()` (`cogs/analyst_agent.py`), which is what actually ships in the `🌅 報告：盤前綜合宏觀與自選股` DM:
- **14-Day Forward Earnings Risk Window**: The pre-market earnings scan window is **14 days** (`warning_days = 14`), consolidated directly into the macro report. Symbols are sourced from the union of each user's holdings + watchlist, resolved via `calendar_service.get_symbol_earnings_batch()`, filtered to `0 <= days_left <= 14`, and sorted ascending by `TradingService.get_pre_market_alerts_data()` (`services/trading_service.py`) — this step returns the **full, uncapped** alert list, no triage/deep-scan gating.
- **No Display Cap**: `build_pre_market_briefing_embed()` (`cogs/embed_builders/order_embeds.py`) renders the earnings radar into one Discord embed field per **10-ticker chunk** (to stay under Discord's 1024-char field-value limit), tagging field names with `(第 X/Y 批)` when there is more than one chunk. All matching tickers are shown — none are silently dropped.

**Orphaned path** — `market_analysis/analyst_runners/earnings_runner.py::run_premarket_earnings()` implements a heavier pipeline (top-10 cap on symbols analyzed, `days_left <= 2` deep-vs-light scan triage, PCR via `SentimentEngine.calculate_pcr`, company profile resolution, LLM context pruning, and `asyncio.Semaphore(3)` rate limiting) feeding a separate `create_earnings_report_embed()` output (`📊 Nexus Seeker 盤前財報與估值調整`). `AnalystAgent.run_premarket_earnings` is a thin wrapper around it, but **nothing in the active scheduler, slash commands, or `dispatch_*` methods currently calls it** — it is exercised only by `tests/unit/test_analyst_agent.py`. Treat it as legacy/unwired code, not the production earnings-radar behavior, unless it gets wired back into a live dispatch path.

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

### Post-Market Intelligence & Spot Holdings Integration (盤後綜合風險結算與現貨部位整合)

The post-market risk report (`post_market_intelligence`) generated by `cogs/analyst_agent.py` and formatted by `build_post_market_intelligence_embed()` in `cogs/embed_builders/order_embeds.py` incorporates comprehensive quant portfolio and LLM analytics:

1. **Spot Assets (`HOLDING`) Full Integration**:
   - `database.get_all_portfolio()` queries both `TRADE` (options) and `HOLDING` (spot) assets from SQLite.
   - Spot holdings are standardized as `PERPETUAL` contracts with `avg_cost` as strike and `1.0` delta multiplier.
   - `PortfolioStatusOrchestrator` computes stock unrealized PnL and Beta-weighted Delta (`quantity * beta * (spot_price / spy_price)`), accumulating it into `total_beta_delta` to ensure **🌐 宏觀風險 (Macro Risks)** are always generated even for equity-only accounts.

2. **Target Center 2.0 Visual Hierarchy & Compact ANSI Dashboard**:
   - **Header Dashboard**: Consolidated inside a high-density ANSI box with UTC+8 timestamp, settlement status, Financial Runway days, and semantic ANSI coloring for Unrealized PnL (Green `+$`, Red `-$`, Neutral `$0.00`).
   - **Positions Tree Cards**: Uses standard ` ├─ ` and ` └─ ` tree indentations with distinct labeling for spot (`💎 NVDA 現貨 HOLDING | 10 股`) vs options (`🎯 AAPL 期權 BTO CALL | 1 口`), accurately calculating 1x cash debit for stocks and 100x for options.
   - **Actionable Empty State**: For 100% cash portfolios, renders an instructive defensive confirmation card with zero-downside confirmation and `/x` radar scan suggestions.
   - **Symmetric Hedge Attribution**: Displays Alpha selection PnL vs Hedge PnL with independent ANSI color coding, Hedge Ratio, Effectiveness %, and health state assessment (`OPTIMAL (對沖結構健康)`).
   - **Sector Rotation Focus Matrix**: Groups the 11 broad market sectors into `🔥 領漲板塊 (Top Inflows)` and `❄️ 領跌板塊 (Top Outflows)` to optimize mobile readability.

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
  - `✏️ 編輯委託單 (Edit Order)` button: Triggers `EditOrderModal` to edit pending order fields including symbol, quantity, side, and price. (Note: The direct `/edit_order` slash command additionally supports updating `order_type` and `validity`).
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
  - `enable_vtr`, `enable_psq_watchlist`, `monthly_expense`, `tax_reserve_rate`, and `cash_reserve`.
  - Also integrates the **Watchlist Tagging System**: Allows users to attach custom categorization tags (e.g., `TECH`, `CORE`) to watchlist assets via an interactive dropdown and modal (`ui/watchlist_tags.py`). This tagging engine is also fully exposed in the `/list_watch` command output via a localized "🏷️ 原地編輯標籤" shortcut button, enabling direct in-place editing that automatically rebuilds and replaces the original Discord view for a seamless, SPA-like experience.
- **Notification Preferences (`/notif_settings`)**: Manages individual toggles stored in a key-value style `user_notification_settings` table (designed with composite primary key `(user_id, notification_key)` for infinite schema-less extensibility). Fully consolidated into **4 Tactical Dimensions with 13 Core Channels** (Migration `v061` + WTI Alert + `defense_fundamental_thesis` + `alpha_price_volume_watch`):
  - **4 Tactical Modules**:
    1. `briefings` (📋 定時戰報與覆盤): `briefing_pre_market`, `briefing_post_market`, `briefing_weekly_vtr`
    2. `telemetry` (📡 盤中自選與掛單遙測): `heartbeat_watchlist`, `telemetry_orders`
    3. `defense` (🛡️ 持倉風控與極端防禦): `defense_portfolio_risk`, `defense_option_rollover`, `defense_fundamental_thesis`, `defense_macro_tail_risk`
    4. `alpha` (🎯 Alpha 策略與情報): `alpha_market_signals`, `alpha_polymarket`, `alpha_wti_oil`, `alpha_price_volume_watch`
  - **Dynamic Two-Tier Architecture with Preset Modes**: To provide a clean, uncluttered user experience:
    - Row 0 features the Category Selector (`briefings`, `telemetry`, `defense`, `alpha`).
    - Row 1 features toggle choices with real-time `🟢` / `🔴` indicators.
    - Row 2 features module batch controls (`⚡ 開啟本區`, `💤 關閉本區`).
    - Row 3 features 1-click Preset Quick Action buttons:
      - `🛡️ 戰備全開` (`all_on`): Enables all 13 risk and alpha channels.
      - `🎯 精準交易` (`focus`): Keeps scheduled briefings, real-time portfolio defenses, and always-on intelligence feeds (`alpha_wti_oil`, `alpha_polymarket`) on, while muting intraday scanner/Alpha noise (`heartbeat_watchlist`, `alpha_market_signals`, `alpha_price_volume_watch`).
      - `🔕 盤中靜音` (`mute_intraday`): Keeps pre/post briefings, margin & tail-risk alerts, the fundamental-thesis alert, and always-on WTI/Polymarket feeds on; mutes only intraday-cadence chatter (`heartbeat_watchlist`, `telemetry_orders`, `defense_option_rollover`, `alpha_market_signals`, `alpha_price_volume_watch`).
  - **Polymarket Parameter Separation**: Non-boolean account configs (`polymarket_threshold`, `polymarket_use_llm`, `polymarket_slippage`) are cleanly placed in `/settings` (`AccountSettingsView`), keeping `/notif_settings` pure and focused on notification channels.
  - **100% Backward-Compatible Alias Engine**: Queries or updates using legacy keys (e.g. `hb_options_structure`, `ddp_alert`, `profit_lock_alert`, `wti_oil_alert`, `oil_alert`) are transparently resolved to their new consolidated counterparts via `LEGACY_KEY_ALIASES`.

### 2. UI Component Pipeline (`cogs/settings_ui.py` & `cogs/terminal.py`)
Both `/settings` and `/notif_settings` (defined in `cogs/terminal.py`) utilize ephemeral Discord Views defined in `cogs/settings_ui.py`. Interactive flows are built as follows:
- **Boolean Switches & Toggles**: Selecting a boolean setting (e.g., `enable_vtr` or notification toggles) instantly flips the state in the SQLite database, triggers `.refresh_items()` to regenerate the select choices (with state emojis: `🟢` for ON, `🔴` for OFF), and edits the active Discord message with the updated embed.
- **Dynamic Text Input Modals**: Selecting a numeric field triggers a Discord Modal popup (`AccountSettingsModal`).
  - **Client-Side Validation & Sanitization**: The Modal's `on_submit()` performs rigorous validation. E.g., verifying numerical bounds, verifying `capital > 0`, and sanitizing user inputs.
  - **View Refreshing**: On successful validation and persistence, the modal dynamically triggers a re-draw on the parent View to refresh the dashboard instantly without sending extra message blocks.

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
- The entire presentation layer is centralized under `cogs/embed_builders/` (with `embed_builder.py` acting purely as a backwards-compatibility shim):
  - `create_account_settings_embed(details_list: list[str]) -> discord.Embed`
  - `create_notification_settings_embed(module_fields: list[tuple[str, str]]) -> discord.Embed`

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

- macro events are cached by **month** (fetched dynamically from `nexus_edge_scraper` querying TradingView)
- earnings are cached by **symbol** (fetched via Finnhub API)
- watchlist heartbeat, calendar views, pre-market alerting, and analyst flows all share the same SQLite-backed cache path

Do **not** add raw market-calendar API calls directly to feature code when calendar helpers already exist.

---

## Embed Architecture

All production embed construction should remain centralized in:

- `nexus_core/cogs/embed_builders/` (with `embed_builder.py` as shim)

This is enforced by:

- `tests/unit/test_output_centralization.py`

Current repository rule:

- cogs should **not** construct `discord.Embed` directly
- cogs should **not** use the `queue_dm(message=...)` shortcut
- push/report messages should prefer **field-based embeds**
- ANSI tables belong inside a field, not dumped into the full description when avoidable
- **Discord Embed Layout Best Practices**: When presenting multiline stats or dashboard metrics (e.g., `/sys_health`), prefer using a **Single Field Block List** (where `name` acts as the section header and `value` holds the multiline markdown stats with `inline=False`) instead of a 3-column `inline=True` grid. This prevents breaking on mobile devices and eliminates the need for empty `\u200b` filler fields.
- **Visual Consistency & Explicit Subclassing (`NexusEmbed`)**:
  - To maintain absolute visual consistency and truncation protection across all modules, **all builders in `cogs/embed_builders/` MUST explicitly import and construct `NexusEmbed` instead of `discord.Embed`.** (e.g. `from cogs.embed_builders._core import NexusEmbed`). Monkey patching is deprecated.
  - **Curated Color Palette**: All standard colors are mapped to cohesive, premium palettes:
    - Primary system/info: Curated blue `0x3498DB`
    - Danger/risk alerts: Curated red `0xE74C3C`
    - Settlement/profits: Curated green `0x2ECC71`
    - Warning/observation: Curated orange `0xF39C12`
    - Secondary: Curated blurple `0x5865F2`
  - **Standardized Footer Signature**: Every embed footer is dynamically formatted as `"🌌 Nexus Seeker • [Module Description]"`, clean of duplicate prefixes, and synchronized with a system timestamp.
  - **Pagination Compatibility (`from_dict`)**: The `.from_dict()` classmethod is overridden to seamlessly convert serialized dictionary payloads back into fully styled `NexusEmbed` instances.
- **量化控制台與多模組 Embed 排版原則 (Quantitative Console & Multi-Module Embed Layout Principles)**:
  - **控制台美學 (ANSI Wrapping)**：所有包含實時行情、量化數據、持倉或風險精算的內容，必須使用 ````ansi` 程式碼區塊包裹以進行控制台渲染。
  - **樹狀縮排與結構標籤**：子項目統一使用分叉角 ` ├─ ` 與結尾角 ` └─ ` 符號進行多級縮排，輔以長度適配的 `-` 分隔線，以維護純文字網格的視覺層級。
  - **欄位模組化與欄位分離 (Field-based Modularization)**：除了概覽摘要（Overview）或特定心跳大圖使用單一 Description 整合外，任何包含多個邏輯子模組的 Embed 必須以多個 Discord Embed Fields 進行物理隔離。各欄位 Title 應搭配適當的 Emoji 作為首碼，而其 Value 則獨立包裹各自的 ````ansi` 程式碼區塊，避免混雜。
  - **數據狀態後綴與降級防禦 (Data Suffix & Degradation Fallbacks)**：
    - 當非交易時段、市場封盤或網路/API 異常導致即時數據不可用時，Embed Title 必須追加狀態後綴（如 ` [盤前數據未更新/降級模式]`），且受影響的指標應自動降級顯示為公允的預設字元（如 `--%`、`--`、`N/A`、`封盤中`）。
    - 若成功讀取本地 SQLite 資料庫快取或歷史代理數據（如歷史波動率 proxy），Title 應註明來源屬性（如 ` [盤前/前日收盤]` 或 ` [盤前/HV代理]`），數值旁應附加 `(前日收盤 / 歷史波動率代理)` 標記，確保數據透明度。
    - 若核心比對數值偏離公允區間超出特定閥值（例如價格偏離痛點 >30% 觸發斷路器），下游的執行或操作指南需自動顯示 `N/A (已觸發斷路器)` 或相關警告，暫停輸出特定交易建議。
  - **結構化網格與戰術意圖映射 (Structured Grid & Tactical Intent Mapping)**：數據表格（如異常交易流、委託單列表、持倉明細）必須動態計算每列的最大字元寬度以對齊網格。底層的原始數據流或交易類別應被映射轉換為直觀的戰術意圖描述，使終端使用者能迅速判讀意圖與支撐/阻力物理界線。
  - **字數上限與分頁保護 (Pagination & Message Splitting)**：
    - **批次分頁限制**：當批次掃描或查詢的標的/項目數量過大時，為避免超出 Discord 的 4096 (Description) 與 6000 (Total Size) 字元上限而導致 `400 Bad Request` 系統錯誤，單一頁面最多僅能承載 **10 個標的**。
    - **動態頁碼與分流投遞**：分頁後 Title 後方應標註 `(第 X/Y 頁)`。呼叫端對於互動指令應透過 Ephemeral 迭代發送，排程背景任務則應經由 `queue_dm` 作為獨立訊息進行分開投遞，嚴禁在單一訊息中過度堆疊。

---

## Dynamic Rollover Engine (動態轉倉引擎)

The platform features an automated **Dynamic Rollover Engine** (`market_analysis/dynamic_rollover/`, a package with a facade `__init__.py` assembling `DynamicRolloverEngine` from per-scenario submodules) that monitors the real portfolio every 30 minutes. It evaluates holdings across four core scenarios to defensively rebalance assets or shift momentum based on Specification by Example (SBE) guidelines.

### Scenarios
1. **Fundamental Thesis Broken (原型假設破滅)**: Leverages `nexus_edge_scraper` via the SEC EDGAR API (`httpx` + BS4 decomposition + Regex tag filtering). Specifically, `section_extractor.py` provides **structured extraction** (Forward Guidance, Margin & Cost, Market Share, Financial Results, Operational Disruption) to prevent token explosion. It uses an **Advanced CoT** (Chain of Thought) system prompt to validate the moat. If broken, completely liquidates the asset into the Core holding (e.g., VOO).
2. **Opportunity Cost & EV Comparison (機會成本轉換)**: Compares the `PowerSqueeze` indicator and Expected Value (EV) between a decaying holding and a breakout watchlist target. Recommends tactical option spreads (e.g., Bull Call Spreads) to roll the capital.
3. **Core vs Satellite Rebalancing (核心與衛星比例再平衡)**: Prevents concentration risk. If a SATELLITE asset (e.g., NVDA) exceeds its `max_allocation_pct` due to a rally, the engine partially sells it back to the `target_allocation_pct` and buys the CORE asset.
4. **Leverage & Margin Defense (槓桿與維持率防禦)**: Triggers when the broad-market macro regime enters `SHORT_GAMMA_CRITICAL` / `SYSTEMIC_LIQUIDITY_CRISIS` (大盤宏觀風控紅線亮起 — GEX Flip substantively broken, market makers flipped into negative-gamma stampede) AND account margin pressure is detected (reusing the `/stress_test` GTC cash-deficit proxy). The engine then checks **every SATELLITE holding individually** for structural no-edge conditions (structural breakdown or whale STO block — reusing the same thresholds as Scenario 3, not new ones). Holdings with no edge are force-liquidated 100% with a Sell-To-Close (STC) or Buy-To-Close (BTC) signal and rolled into **BOXX** to lock in risk-free interest; holdings that still show edge are left untouched (no forced reduction). This is deliberately distinct from Scenarios 2/3's default idle-capital target of **VOO**: during a systemic macro sell-off VOO itself drops in sympathy, so only the genuine cash-equivalent BOXX is treated as a true defensive parking spot. BOXX's existing collateral haircut, `/stress_test` liquidity-buffer math, and zero-beta/fixed-income whitelisting are unrelated and unaffected by this scenario.

### GEX Mapping & Anti-Washout Stop Engine
- **GEX & Market Maker Intent Mapping Engine (`index_microstructure.classify_gex_wall`)**: Evaluates individual strike GEX exposures against the maximum positive GEX wall across the option chain. Classifies levels into `SUPPORT_GEX_WALL` (MM support floor / dip buying hedging), `RESISTANCE_CALL_WALL` (overhead resistance ceiling / negative gamma pinning / heavy OTM call clusters), or `NEUTRAL`.
- **Anti-Washout Stop Engine (防洗盤動態停損引擎)**:
  - **Anchor Wall Priority**: Dynamic stop loss anchors to `support_wall` > `gex_put_wall` > `hvn` > `spot`.
  - **15m ATR Buffer & Bounds**: Base stop is calculated as `anchor_wall - 1.5 * atr_15m`, bounded strictly within `[spot * 0.95, spot * 0.98]` (2%~5% boundary).
  - **LVN Magnetic Snapping Algorithm (量價拓撲吸附演算法)**: When stop loss falls within 1.5% of an LVN vacuum, fixed percentage shifting (`* 0.985`) is strictly forbidden. The engine penetrates downward and magnetically snaps to the upper edge of the secondary HVN cluster: $\text{Secondary HVN} + (0.2 \times ATR_{15m})$, using liquidity volume to prevent cascading slippage.
  - **0/1 DTE Risk-Parity Dynamic Sizing (末日結算日風險平價口數動態縮放)**: Expands stop loss buffer by $1.5 \times ATR_{15m}$ (total $3.0 \times ATR_{15m}$), while simultaneously enforcing a strict Risk-Parity position sizing scale factor ($\text{Scale} = \frac{1.5}{3.0} = 0.5$, cutting position size/shares by 50%) to keep Dollar Risk constant.
- **Asset Class Dual-Track Exit Mechanism (現貨與期權雙軌裁決機制)**:
  - **SPOT Assets (15m Candle Close Track)**: A liquidation (`LIQUIDATE`) directive is strictly executed only if the 15-minute candle close (`price_15m_close`) falls below the calculated stop loss AND confirmed by the **Gamma Cliff Confirmation Engine**. Intraday lower wick breaches are treated as market maker noise and held as `HOLD` (推播安心防守卡).
  - **OPTIONS Contracts (3-5m Fast Track)**: To prevent catastrophic Delta collapse and Vega crush, options contracts bypass the 15-minute candle close wait. If spot price breaches the stop loss or an IV crash occurs ($\Delta IVR \ge 20\%$), the engine immediately triggers fast market/limit exit (`LIQUIDATE` / `STC` / `BTC`).
- **Call Wall Euphoria & Momentum Exhaustion Gate (極端亢奮區雙重動能衰竭確認制)**:
  - When spot $\ge \text{Call Wall}$ or in extreme Euphoria (`Skew Percentile <= 20%`), 90% is liquidated into benchmark (e.g. VOO).
  - For the remaining 10%: Establishing a `Bear Call Spread` is strictly gated by **Dual-Exhaustion Criteria**: (1) 15m SQZ MOM turns negative (`sqz_mom < 0`, momentum topping), AND (2) Skew leaves euphoria (`skew_percentile >= 30.0%`).
  - If exhaustion is NOT met (momentum still positive), opening short calls is strictly forbidden to prevent being crushed by a **Gamma Squeeze**. The remaining 10% is instead switched to **Trailing Stop (移動止盈)** to let profit ride.
- **Zero-Gamma Flip & Microstructure Interrupt Handler (微觀異動事件中斷器)**:
  - In `portfolio_monitor.py`, `handle_microstructure_interrupt` listens for intraday micro-events (such as spot penetrating Zero Gamma into negative gamma territory or massive whale UOA spikes). It immediately interrupts the 30-minute scheduler timer, executing real-time rebalancing audits and pushing emergency defense embeds.
- **Form-Type-Aware Analysis (10-K/10-Q/8-K)**: `evaluate_fundamental_thesis` (`market_analysis/dynamic_rollover/fundamental_thesis.py`) accepts optional `form_type` and `sections` parameters that customize the LLM prompt per filing type. 10-K supplements weight full-year trends and board-reviewed Risk Factor changes; 10-Q applies extra skepticism toward single-quarter noise (only a cross-quarter *trend* should trigger `is_broken=true`); 8-K (an event-driven Current Report with no MD&A) is judged by *which* Item number fired (e.g. Item 2.05 divestiture, Item 4.02 restatement, and abrupt Item 5.02 CEO/CFO departures are high-signal; Item 7.01/8.01 are usually low-signal). The edge scraper (`nexus_edge_scraper/section_extractor.py`, `local_api.py`) routes its extraction anchor and structured `sections` dict (including a dedicated `key_events` field for 8-K dotted Item headers) by `form_type` as well. Both parameters are optional — empty/legacy inputs keep the prompt byte-identical to the pre-form-type-aware behavior.
- **Manual Trigger (`/verify_thesis`)**: Any user can manually trigger Scenario 1 for any symbol via the `/verify_thesis <symbol>` Discord slash command, regardless of holdings. This command features an interactive UI: it first triggers the Edge Scraper to fetch a list of recent SEC filings (10-K, 10-Q, 8-K), and presents them in a dropdown menu. If the user selects one, or if the 60-second timeout expires, it fetches the specified (or latest) SEC EDGAR report (up to 10,000 characters) and sends it to the LLM.
- **Automated Daily Filing Scan (`cogs/trading/fundamental_filing_monitor.py`)**: A dedicated daily scheduler (`fundamental_filing_scan`, 08:00 ET, skips non-trading days via `market_time.nyse_calendar`) automatically scans **holding-only** symbols (`database.get_all_holdings()`; watchlist symbols are intentionally excluded to bound LLM/API cost) for new SEC filings. For each unique symbol (deduplicated across all holders so a symbol held by multiple users is only analyzed once, throttled via `asyncio.Semaphore(3)`), it compares the latest filing's `accession_number` against a dedicated dedup-cursor table, `fundamental_scan_state` (migration `v062` — distinct from `fundamental_cache`, which stores the LLM verdict itself and has no accession-number column). If a new filing is detected, it's routed through the same form-type-aware `evaluate_fundamental_thesis` pipeline used by `/verify_thesis`. The whole run is gated by `is_memory_safe()` up front (1GB VPS protection). **Only `is_broken=True` results trigger a DM** (via `bot.queue_dm`, reusing `build_fundamental_broken_embed()` — the same embed-construction helper shared with `/verify_thesis`) to holders who have the notification enabled; passing results are written silently to `fundamental_cache` with no DM, to avoid alert fatigue. The scan cursor is only advanced on a successful (non-`None`) LLM result — if the memory-safety gate or LLM call fails mid-scan, the cursor is left untouched so the same filing is retried on the next day's run rather than being silently skipped.
- **Global Defense Gate (全域防禦閘門)**: LLM moat verdicts (`is_broken`, `confidence`, `reasoning`) are written to the SQLite `fundamental_cache` table (via migration `v057`). During the intraday 30-minute heartbeat (`intraday_pipeline.py`), the `evaluate_watchlist_symbol` engine acts as a **Global Defense Gate**. If a symbol is flagged as broken, the engine forcefully intercepts and overwrites any quantitative BTO (Buy-To-Open) or Grid Accumulation signals, replacing them with a strict `wait` scenario and a `LIQUIDATE` directive. This guarantees that technical blindspots (e.g., heavily oversold RSI traps) cannot override fundamentally deteriorating assets.
- **Lightweight Triage Strategy (Scenarios 2, 3, 4)**: Lightweight rule-based tasks execute during the intraday 30-minute `monitor_real_portfolio_task` to ensure zero API blocking.
- **Discord UI**: All rollover actions generate a stylized embed (`create_dynamic_rollover_embed`) packed with terminal execution guidelines, strategy type, and strict buy/sell directions (e.g., BTC for short puts). Title/color are keyed off an explicit `scenario` identifier (`RolloverScenario`) rather than free-text substring matching, so `MARGIN_DEFENSE` alerts always render as critical red regardless of the underlying action.
- **Toggle Settings**: Users can opt out of rollover alerts via `/notif_settings` under the Defense module (`defense_option_rollover`), and specifically out of the automated daily filing scan's alerts via `defense_fundamental_thesis` (both keys are independent; `/verify_thesis`'s manual, interactive results are always shown regardless of either toggle).

---

## WTI Crude Oil Price Alert System (Commodity Intelligence)

### 1. Dual-Trigger Gating Architecture (`wti_monitor.py`)
To prevent blind spots during high-impact geopolitical turbulence and inflation shocks, Nexus Seeker includes a dedicated 24/7 background scheduler for **WTI Crude Oil Futures (`CL=F`)**:
- **30-Minute Polling Loop**: Dispatches every 30 minutes at `:00` and `:30` past each hour across all 24 hours.
- **Overnight Quiet Hours Guard (00:00–06:00 ET)**: Automatically silences DM notifications during overnight hours to protect trader sleep schedules.
- **Dual Trigger Conditions**:
  1. **Absolute Price Thresholds**: User-defined `upper_price` (e.g., `$95.00` inflation spike / breakout alert) and `lower_price` (e.g., `$65.00` recession breakdown alert).
  2. **30-Minute Rolling % Volatility**: Triggers `PCT_SURGE` or `PCT_PLUNGE` when $|(Price_t - Price_{t-30m}) / Price_{t-30m}| \ge pct\_change\_threshold$ (default `±3.0%`).
- **KV Cache Anti-Spam Deduplication**: Implements daily rate-limiting keys (`wti_alert_{uid}_{YYYYMMDD}_{alert_type}`) to ensure a maximum of **one alert per trigger type, per user, per day**.

### 2. Multi-Dimensional Quant & Geopolitical Engine (`market_analysis/wti_analysis.py`)
When triggered, `analyze_wti()` executes an end-to-end analytical pipeline without LLM latency:
- **Technical Indicator Panel (`WtiTechnicals`)**: Calculates RSI(14), MA20, MA50, MA200, ATR(14), daily % change, weekly % change, and 5-level technical trend (`STRONG_BULLISH`, `BULLISH`, `NEUTRAL`, `BEARISH`, `STRONG_BEARISH`).
- **Energy Correlated Stock Matrix**: Concurrently fetches real-time quotes and daily performance for major energy proxies (`XLE`, `XOM`, `CVX`, `OXY`, `SLB`, `USO`), automatically tagging whether the user holds them in `portfolio` (`[HOLDING]`) or tracks them in `watchlist` (`[WATCH]`).
- **Geopolitical & OPEC Event Radar**: Dynamically scans high-impact economic/macro calendar events for oil-sensitive keywords (`OPEC`, `crude`, `sanctions`, `EIA`, `drilling`, `Middle East`, `SPR`).
- **Risk Engine Alignment**: Scales the portfolio oil risk multiplier:
  - $Price < \$75 \to 1.00\times$ (Safe zone, full seller risk limits)
  - $\$75 \le Price < \$85 \to 0.90\times$ (Mild compression, seller exposure tightened 10%)
  - $\$85 \le Price < \$95 \to 0.70\times$ (Moderate compression, seller exposure tightened 30%)
  - $Price \ge \$95 \to 0.50\times$ (Severe compression 🚨, seller limits halved)

### 3. Strict Field-Based + ANSI Container Presentation (`create_wti_alert_embed`)
Adheres 100% to the Nexus Seeker field-based embed architecture:
- `embed.description = None` (Zero loose markdown noise in body).
- All 4 logical sections are placed in `field.name` with values strictly encapsulated in ````ansi\n...\n```` codeblocks:
  1. `🚨 觸發事件與即時遙測`: Spot price, threshold value, 30m delta, and colored direction badge (`突破上限`, `跌破下限`, `劇烈飆漲`, `劇烈暴跌`).
  2. `📊 技術結構與量化指標`: ANSI tree structure showing RSI(14), MA20/50/200, ATR(14), daily/weekly %, and trend.
  3. `⛽ 能源板塊關聯股衝擊`: ASCII table of `XLE`, `XOM`, `CVX`, `OXY`, `SLB`, `USO` with ANSI color coding and portfolio badges.
  4. `🛡️ 投資組合風險與總經事件`: Risk weight multiplier, operational directives, and upcoming OPEC/geopolitical events.

### 4. Interactive Configuration (`/wti_config` & `/notif_settings`)
- `/wti_config`: Direct modal popup (`WtiConfigModal`) allowing users to update upper price, lower price, and 30-min volatility threshold on the fly.
- `/notif_settings`: Governed by canonical channel `alpha_wti_oil` under the **🎯 Alpha 策略與情報** module, with full legacy alias resolution (`wti_oil_alert`, `oil_alert`).

---

## Price-Volume Breakout Alert System (個股 15 分鐘價量突破警報)

Unlike the WTI monitor (a single fixed symbol, `CL=F`), this is a **per-user, multi-symbol** watchlist: each user can register up to 15 independent `(symbol, target_price, direction, volume_multiplier)` watches, and the scheduler evaluates every registered watch every 15 minutes during market hours.

### 1. Bar-Completeness Guard (`market_analysis/price_volume_alert.py::get_confirmed_15m_bar`)
The trigger condition is **15-minute real-body candle close** relative to a target price, combined with a volume-surge gate:
- Fetches real `interval="15m"` OHLCV via `services.market_data_service.get_history_df(symbol, period="5d", interval="15m", force_refresh=True)`.
- **`force_refresh=True` is mandatory**: `get_history_df` normally caches results for 6 hours (`_HISTORY_CACHE_TTL`), which would silently serve stale/partial candles to a 15-minute-cadence caller. This is a deliberate divergence from `market_analysis/dynamic_rollover/opportunity_cost.py::_confirm_entry_signal`, which reuses the same `interval="15m"` call *without* bypassing the cache and *without* checking bar completeness — do not copy that pattern for new intraday-candle logic.
- **Closed-candle detection**: yfinance's 15m bar index is the bar's *start* time. A bar is only "closed" once `bar_start + 15min <= now (ET)`; otherwise the engine falls back to the second-to-last bar so a still-forming candle's live price never triggers a false alert. This is distinct from `market_analysis/gamma_cliff_confirmation.py::is_gamma_cliff_confirmed`, which despite its "15分鐘" naming actually operates on 1-minute bars, not real 15m candles.
- **Volume surge**: compares the confirmed bar's volume against the mean of the preceding 20 bars (`_VOLUME_LOOKBACK_BARS`), mirroring the 20-bar lookback already used by `opportunity_cost.py`'s entry-confirmation logic (though that path uses a 1.2x multiplier vs. this feature's user-configurable default of 1.5x).

### 2. Threshold Comparison (`evaluate_watch_trigger`)
Deliberately separated from bar-fetching so multiple users watching the same symbol share a single yfinance call per scan cycle:
- `direction = "above"` → `Close >= target_price` (breakout).
- `direction = "below"` → `Close <= target_price` (breakdown).
- Volume-surge condition is direction-agnostic — both breakout and breakdown require confirming volume, otherwise treated as noise.

### 3. Per-User Watch Config (`database/price_volume_watch.py`, migration `v063`)
Uses a dedicated SQLite table (`price_volume_watches`, PK `(user_id, symbol)`) rather than `kv_cache`, because the scheduler needs a cross-user, cross-symbol batch query (`get_all_watches()`) that a single-key KV store can't efficiently support. `upsert_watch()` enforces a `_MAX_WATCHES_PER_USER = 15` cap (VPS memory/API-call protection) that only applies to *new* symbols — updating an existing watch's price/direction/multiplier never counts against the cap.

### 4. Scheduler (`cogs/trading/price_volume_alert_monitor.py`)
- `@tasks.loop(minutes=15)`, gated by `market_time.is_market_open()` (unlike WTI's 24/7 cadence, since equity intraday candles are meaningless outside market hours).
- Groups all registered watches by symbol first, fetching each unique symbol's confirmed bar exactly once per cycle even if multiple users watch it.
- **KV Cache Anti-Spam Deduplication**: `price_volume_alert_{user_id}_{symbol}_{YYYYMMDD}` — one alert per user, per symbol, per day (same pattern as WTI's daily dedup keys).

### 5. Interactive Commands & `/notif_settings`
- `/price_alert_set <symbol> <target_price> <direction> [volume_multiplier=1.5]`: upserts a watch (parameterized command, not a modal, since all fields are simple scalars).
- `/price_alert_list`: lists the caller's active watches.
- `/price_alert_remove <symbol>`: removes one watch.
- `/notif_settings`: governed by canonical channel `alpha_price_volume_watch` under the **🎯 Alpha 策略與情報** module.

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
- `nexus_core/cogs/settings_ui.py` — interactive account, notification settings views, and WtiConfigModal
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
- `nexus_core/market_analysis/wti_analysis.py` — WTI crude oil technicals, energy correlation, and event analysis engine
- `nexus_core/market_analysis/intraday_pipeline.py` — watchlist evaluation, option-plan logic, intraday engine helpers
- `nexus_core/market_analysis/index_microstructure.py` — market regime determination (SHORT_GAMMA_CRITICAL) using VIX, VIX3M, and zero-gamma line GEX
- `nexus_core/market_analysis/sentiment_engine.py` — Facade entrypoint for skew / UOA / IV stack
- `nexus_core/market_analysis/sentiment/` — Dedicated submodules (`iv_metrics`, `max_pain`, `options_flow`, `uoa_detector`, `history_storage`, `cache`)
- `nexus_core/market_analysis/telemetry_pricing_engine.py` — central alignment alert pipeline and decision gating logic (stale-lock, deep sea gap limits, pure stock gate, UOA squeeze classification)
- `nexus_core/risk_engine/nro.py` — WatchlistRiskController translating technical status to SDDM tactical routes (SHIELD, SPEAR, STANDBY)
- `nexus_core/formatters/execution_embeds.py` — embeds formatter separating execution decision view logic
- `nexus_core/market_analysis/ghost_trader.py` — GhostTrader Virtual Trading Room execution and monitoring logic
- `nexus_core/services/calendar_service.py` — shared event cache entrypoint
- `nexus_core/services/llm_service.py` — structured LLM outputs and memory-safe degradation
- `nexus_core/services/trading_service.py` — scan / report / validation data orchestration
- `nexus_core/services/telemetry_pricing_engine.py` — dynamic telemetry pricing calculation covering Max Pain, EM, Skew, IV Spikes, and psychological round numbers
- `nexus_core/services/polymarket_service.py` — Polymarket whale tracking and AI summary service
- `nexus_core/services/order_telemetry_service.py` — Order telemetry scanning service
- `nexus_core/database/notifications.py` — custom user notification preferences database operations
- `nexus_core/database/virtual_trading.py` — Database interface for virtual trades (VTR)
- `nexus_core/market_analysis/dynamic_rollover/` — Dynamic rollover engine package (facade `__init__.py` + `fundamental_thesis.py` / `opportunity_cost.py` / `anti_washout.py` / `margin_defense.py` / `structural_signals.py` / `models.py` / `constants.py`), anti-washout stop engine, and asset class bifurcation logic. Public import path stays `market_analysis.dynamic_rollover`.
- `nexus_core/market_analysis/signal_calculator.py` — Dynamic trading signal calculator (1.5x ATR buffers, capital allocation models)
- `nexus_core/market_analysis/scenario_classifier.py` — Event-driven quantitative scenario classifier (6 market scenarios including Whale Escort Resonance)
- `nexus_core/database/watchlist.py` — Database CRUD operations for user watchlist symbols (100% deterministic rule-based zero-LLM architecture)
- `nexus_core/database/migrations/v039_add_notification_toggles.py` — migration registering the user_notification_settings table in SQLite
- `nexus_core/tests/unit/test_wti_alert.py` — unit tests for WTI crude oil price alert system, technicals, and embed rendering
- `nexus_core/tests/unit/test_fundamental_filing_monitor.py` — unit tests for the automated daily SEC filing scanner (dedup cursor, is_broken dispatch gating, per-user notification toggle, multi-holder symbol dedup)
- `nexus_core/tests/unit/test_intraday_pipeline.py` — heartbeat and phase-B gating tests
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
- **型別自我檢測 (Pre-commit Type Check)**：Mypy 已開啟嚴格模式（Strict Mode）。在提交程式碼前，開發人員應在包含完整依賴的 Docker 容器中手動跑一次全域型別檢查（在 `nexus_core` 目錄下執行 `docker compose run --rm nexus-seeker python -m mypy --config-file pyproject.toml .`），以確保所有第三方套件（如 `discord.py`）的型別解析正確無誤，避免型別錯誤進入遠端倉庫。

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
- pre-commit hooks run ruff lint/format, strict mypy, and general quality checks
- pre-push hooks run semgrep and dockerized tests (core-test and scraper-test)

---

## Documentation Guidance

When updating docs in this repository:

1. distinguish **actual runtime flow** from helper modules
2. separate **watchlist heartbeat** from **Analyst Agent**
3. reflect the current field-based embed format
4. mention the persistent DM queue when discussing notifications
5. keep README user-oriented and AGENTS contributor-oriented
