# 🌌 Nexus Seeker - AGENTS.md

## Project Overview

Nexus Seeker is a multi-tenant **Discord-first options risk-control and trading operations platform**. It combines technical structure, Black-Scholes-Merton pricing, Greeks-based portfolio risk, event-aware calendar defenses, and LLM-assisted structured commentary.

Current released core version: **`1.13.11`**


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

- **Watchlist 15 分鐘心跳** is currently emitted by `cogs/trading.py` via `SchedulerCog.dynamic_market_scanner()`
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

- `kv_cache_dedup_purge` — **03:00 ET** (off-peak; deletes stale one-shot daily anti-spam dedup flags in `kv_cache` — see `database.cache.purge_stale_kv_cache_dedup_keys` — past a 3-day retention window; scoped to a whitelist of known dedup-only key prefixes so permanent caches/config are never touched)
- `fundamental_filing_scan` — **08:00 ET** (holdings-only, skips non-trading days)
- `daily_reddit_update` — **08:30 ET**
- `pre_market_risk_monitor` — **08:45 ET** (staggered pre-warming of quant metrics, IV, Max Pain & Squeeze before 09:00 Analyst Agent)
- `dynamic_market_scanner` — **every 15 minutes (:00, :15, :30 & :45) during market hours**
- `wti_oil_monitor` — **every 30 minutes (24/7, 00:00–06:00 ET quiet hours)**
- `price_volume_alert_monitor` — **every 15 minutes during market hours** (with `Semaphore(3)` concurrent K-line bar retrieval)
- `monitor_real_portfolio_task` — **every 15 minutes (:05, :20, :35 & :50) during market hours** (staggered 5 minutes after dynamic scanner to consume shared in-memory radar cache)
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
    symbol TEXT NOT NULL,
    expiry TEXT NOT NULL,
    max_pain REAL,
    expected_move_lower REAL,
    expected_move_upper REAL,
    reference_spot_price REAL,
    is_stale INTEGER DEFAULT 0,
    calculation_mode TEXT DEFAULT 'OI',
    is_degraded INTEGER DEFAULT 0,
    circuit_breaker_triggered INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, expiry)
);
```

During the market session, `/x` command reads from this local cache. If a cache miss occurs, the system calculates the metrics in a non-blocking Cache-Aside manner and writes them back.

### 2. Local Rules Engine (Zero-LLM Latency)
Instead of invoking LLM on the first-level radar panel, a lightweight rules engine evaluates spot prices against the SQLite cache bounds:
- **超跌磁吸 🚀**: Triggered if `price <= expected_move_lower` and `Delta MP% > 5%`.
- **需防壓回 ⚠️ / 籌碼斷層 ⚠️**: Triggered if `abs(Delta MP%) > 10%`.
- **Unified Radar Filters**: The terminal UI consolidates Risk Defense and Alpha Signal filters into a single dropdown, fully integrated with `ScanParams` for deep evaluation:
  - **Risk Defenses**: Excludes martial law bounds (`exclude_martial_law`) and prevents silent period events (`avoid_silent_period`).
  - **Alpha Signals & Advanced Gates**: Filters for Triple Discount Pricing (`tdp_mode`), Volatility Squeeze firing (`squeeze_mode`), Strict UOA institutional activity (`uoa_mode`), High-Deviation Magnetic Filters (`magnetic_filters`), plus advanced defensive layers including **UOA Barrier, Gravity Filter, Micro-Divergence Gate, Volume PCR Cascades Gate, LVN Vacuum Slip Defense, and GEX Paper-Wall Filters**.
- **Real-time Insights**: Automatically matches active pending orders or option protection strategies (e.g., triggering pull-back alerts, PCR顺向殺盤背離, LVN 真空暴跌, 負 Gamma 泥淖, or paper-thin wall warnings). Now rendered inside a dedicated ANSI markdown code block for easy one-click copying.

### 3. Rendering Layer (`build_radar_scan_embed`)
The terminal radar card is built inside `cogs/embed_builders/` using `build_radar_scan_embed()`, keeping with the **Single Source of Truth** for embeds. It prints an interactive Markdown table (which replaced the legacy ANSI format for better aesthetics) showing key quantitative and Alpha fields:
- **`標的`**: Dynamic `⚠️` prefix for >10% deviation, negative gamma, or structural anomalies for instant visual triage.
- **`G/P-Wall(±)`**: Call Wall + Put Wall with dynamic Net GEX, PutWall break polarity, GEX depth thickness tag (e.g. `$9.5(薄)` for thin paper-walls with GEX < 500k), and overhead negative GEX resistance swamp tags (e.g. `阻$500` or `$495.0(阻$500)`).
- **`Skw%`**: True Skew percentile alongside actual Skew value or Volume PCR cascade alert when PCR $\ge 1.2$ (e.g. `5% (PCR:1.81⚠️)`), accurately evaluating dealer tail-risk pricing vs pure option volume directional dump.
- **`SQZ向量`**: Squeeze Momentum Vector with timer/squeezing indicator (e.g. `⏱️🟢+12.7`, `🟢+19.6`, or `⚪+0.0`), dynamically integrated with **UOA Barrier Index** (downgrading bullish vectors to `⚪` if massive institutional call walls block upside).
- **`Neg-GEX`**: Net GEX deviation distance percentage.
- **`STO 鎖死`**: Formatted Short-to-Open strikes (e.g. `C$227.5 / P$237.5`, `C$885.0`, or `P$110.0`) or Straddle STO density.
- **`IV 策略`**: IV Strategy Match with strict Negative Gamma circuit breaker forcing `🔴賣方禁售` during dealer sell-off cascades, `🔴CSP 禁售` for $IVR < 15\%$, and `🟢適宜賣方` for healthy environments.
- **`EM Z-Score`**: Normalized Expected Move standard deviation position (e.g. `+0.00σ`, `+0.05σ`).
- **`Top UOA`**: Single strongest whale print with ratio tags (e.g. `🛡️ 08/28 $885.0C (STO 304)` or `🔥 08/15 $220.0C (BTO 15k)`), with high-IV noise filter (`N/A (無主力)` for high IV stocks without institutional footprints).
- **`防洗盤絕對防守位 (Anti-Washout Stop)`**: Dynamically calculated as $PutWall - 1.5 \times ATR_{14}$, providing solid buffers against liquidity grabs.
- **`離場判定鐵律`**: Enforces `"🛑 離場判定鐵律：嚴守 15 分鐘實體 K 線收盤撤退線 (過濾下影線流動性獵殺)"` in table notes.
- **`灰階戰術建議 (Gray-scale Tactical Guidance)`**: Multi-dimensional evaluation engine preventing binary stop-outs, dynamically integrating `🚨 破位殺盤 (PCR 1.81)`, `🛑 跌穿LVN真空區($488.6)`, `⚠️ $9.5 僅單薄紙牆`, `🟡 護航網支撐，現貨續抱，防守退至 $103.80 (嚴守15分K收盤)`, etc. Redundant markdown bold formatting has been removed for consistent ANSI rendering.
  - **三重結構性風險合流 (Triple Structural Risk Confluence)**: A dedicated composite gate fires `🚨 三重結構性風險合流：避險背離+痛點引力+機構真空，嚴禁抄底` when Option Skew percentile hits an extreme structural hedging divergence (`skew_percentile >= 98.0`), the Multi-DTE Gravity Filter (current-week or next-week Max Pain) confirms strong downward magnetism, AND no substantial institutional buy-side UOA support exists at `DTE >= 7` (defined identically to the existing `is_uoa_aligned` check in `cogs/trading/heartbeat.py`: a `BTO CALL` or `STO PUT`). This branch is evaluated ahead of the plain `skew_percentile > 90.0` fallback (since the composite condition is strictly narrower), so it only overrides the generic skew warning when all three signals align. All three underlying values are already fetched/cached by the existing pre-market pre-warm and 15-minute batch scan cycle (`sentiment_history`, `market_cache`/`radar_terminal_{sym}` for near/far Max Pain, and `uoa_{SYMBOL}` kv_cache) — this gate is a pure in-memory boolean derivation with no additional network or option-chain fetch cost.

### 4. 避免 Discord 回應錯誤的長度分段與分頁原則
為防範當自選標的 (Watchlist) 或持倉 (Holdings) 數量過大時，因 Embed Description 超過 Discord 的 4096 字元上限而導致 `400 Bad Request (error code: 50035): Invalid Form Body` 系統錯誤，系統實施以下長度分段與分頁原則：
- **最大分段間距 (Chunk Size)**：批次掃描結果一律以每頁最多 **10 個標的**進行分組封裝。
- **返回多個 Embed 列表**：`build_radar_scan_embed()` 的返回型別升級為 `List[discord.Embed]`。
- **動態分頁標題與 Footer**：若分頁數量大於 1，系統會在每個 Embed 的 Title 後方自動標註頁碼，格式為 `(第 X/Y 頁)`（例如：`(第 1/2 頁)`），並將頁碼與總項目數寫入 Footer（`頁次: X/Y ｜ 📊 總項目: N`）。
- **呼叫端分流處理**：
  - **Discord 互動指令 (如 `/x` 單訊息就地翻頁)**：多頁掃描結果採用 `BatchScanPaginatedView`（`cogs/unified_terminal/batch_scan_view.py`），透過 `interaction.response.edit_message()` 於單一 Ephemeral 訊息中進行 `◀ 上一頁` / `下一頁 ▶` 就地翻頁，徹底避免逐頁發送 followup 訊息觸發 Discord 40094 限制。該 View 同時掛載 `⚡ 批次分析警示標的` (`BatchScanWarningButton`，以 `Semaphore(3)` 併行分析並透過 `chunk_embeds` 依字數分段安全發送) 與 `🔄 返回控制面板`（切回 `UnifiedRadarView`）。
  - **背景排程與 DM 隊列 (如 Watchlist 15分鐘心跳)**：呼叫端會自動對 Embed 列表進行迭代，逐頁調用 `queue_dm` 加入發送佇列，確保每一頁皆能穩定投遞且不觸發 Discord API 的字數限制。

### 5. 個股深度分析面板 (Symbol Hub / `/x symbol:`)

單一標的深度分析（`create_tactical_symbol_embed()`，`cogs/embed_builders/portfolio_embeds.py`；資料組裝於 `cogs/unified_terminal/symbol_deep_dive.py`）在既有的技術/期權快照基礎上，持續擴充以下量化與帳戶層級欄位：

- **15 分鐘微觀結構 (15m Microstructure)**：除既有的已收盤 15m K棒 OHLC、15m 成交量、SMA20 均量與 RVOL_15m 外，另補上：
  - **15m ATR (EMA14)**：沿用 `market_analysis/atr_utils.py::fetch_atr_15m()` 既有計算結果獨立顯示一行（原本只用於防洗盤停損公式，未曾單獨渲染）。
  - **Session VWAP**：`market_analysis/vwap_utils.py::fetch_session_vwap()` 以 `period="1d", interval="15m"` 單次抓取當前/最近一個交易時段的 K棒（刻意不沿用其他 15m 抓取共用的 `period="5d"`，因為 VWAP 定義本身只需要單一 session 即可自然涵蓋），計算累積成交量加權均價與現價偏離%。
- **GEX 結構邊界接線**：`fetch_symbol_gex_metrics()` 原本就會回傳 `net_gex`、`call_wall`、`gex_profile`，但先前只有 `put_wall` 被實際渲染。現已接上：
  - **Net GEX Regime**：依 `net_gex` 正負分類為 🟢 LONG_GAMMA（自穩定壓制波動）/ 🔴 SHORT_GAMMA（助漲助跌）——這是個股層級的展示分類，與 `index_microstructure.get_market_regime()` 的大盤層級 `SHORT_GAMMA_CRITICAL` 是獨立概念，不應混淆。
  - **個股 GEX Flip 線**：呼叫既有的 `index_microstructure.estimate_symbol_gamma_flip(gex_profile, spot)`（累積 GEX 曝險零交叉點估算），顯示翻轉價位與現價緩衝%；找不到零交叉點時顯示 `--` 而非誤導性的 `$0.00`。
  - **GEX CallWall**：水位、該履約價的淨 GEX 深度，以及距現價空間%（< 5% 時標註 `❌ 不足5%` 空間不足警示）。
- **UOA 訂單流強化**：
  - **權利金金額排序 (Notional-Value Ranking)**：`uoa_detector.py::detect_uoa()` / `detect_uoa_with_physical_caps()` 回傳的前 5 大 UOA 清單，排序依據已由**成交量降序**改為**權利金金額（名目價值，`trade_price * volume * 100`）降序**，並將該值以 `notional_value` 欄位一併存入輸出 dict（複用候選過濾階段已算好的名目價值，避免重算）。這讓「量大但單價低」的雜訊不再蓋過「量沒特別突出但金額龐大」的真正機構大單，更貼近真實資金規模。此變更會連帶影響所有取清單第一筆/前 N 筆作為「最強主力單」的下游渲染（Radar Terminal `Top UOA` 欄位、心跳 UOA 表格前 3 筆、Polymarket UOA 關聯比對），以及 `dynamic_rollover/opportunity_cost.py::_confirm_entry_condition4_uoa_dte` 認定「主力買盤」的依據（該條件同時要求 DTE>=7 與 ratio(Volume/OI)>=0.8x）；純存在性判斷（如 `heartbeat.py::is_uoa_aligned`）則不受順序影響，僅可能因入選前 5 的成員變動而間接改變結果。
  - **權利金欄位 (Notional Premium)**：`generate_uoa_ascii_table()`（`market_analysis/uoa_telemetry.py`）新增「權利金」欄位，由 `trade_price * volume * 100` 動態算出，`>= $1M` 顯示 `M`，否則顯示 `k`。
  - **SWEEP/BLOCK/CROSS 三分類**：`_format_uoa_field()`（`cogs/embed_builders/portfolio_embeds.py`）合併兩套原本各自獨立、從未互相參照的既有啟發式訊號——`uoa_detector.py` 的**成交量整數手數形狀**（`vol > 1500` 且為 100 的倍數 → BLOCK，否則 SWEEP）與 `classify_uoa_trade()` 的 **Bid/Ask 執行價位置**（MIDPOINT 視為暗池對倒代理訊號）——輸出 🔥 SWEEP / 📦 BLOCK / ⚖️ CROSS 標籤（CROSS 判定優先於量體形狀，因為對倒印花本質上比單純大單形狀更具決定性）。**這仍是啟發式代理，不是真實 order-type tape 資料**；`cogs/embed_builders/watchlist_embeds.py` 心跳 embed 的 SWEEP/BLOCK 兩分類是獨立渲染路徑，尚未同步擴充為三分類。
- **🏦 資產端保證金與購買力 (使用者自填參考)**：本平台是純現金紙上帳戶模型，沒有真實券商保證金/購買力資料。`user_settings` 新增 `option_buying_power`（期權購買力上限）與 `margin_used`（目前佔用保證金）兩個使用者自填欄位（比照既有 `cash_reserve` 模式，`/settings` 可編輯，migration `v067`），Symbol Hub 據此顯示可用現金佔比（沿用既有 `cash_reserve`/`capital`）、期權購買力與保證金使用率（🟢 < 50% / 🟡 50-80% / 🔴 >= 80%，門檻沿用 `risk_engine.get_macro_risk_metrics()` 既有的 `portfolio_heat_limit` 80% 慣例）。此區塊明確標註「使用者自填數據，非即時券商保證金/購買力數據」，避免與其餘即時市場數據混淆。
- **🔐 進場鐵律檢核頁籤 (`SymbolHubView.btn_entry_rules`)**：`SymbolHubView`（`cogs/unified_terminal/symbol_view.py`）新增第二排按鈕（`row=1`，與首頁/輿情社群/即時整理/一鍵對沖的第一排區隔開），點擊後由 `create_entry_rules_embed()`（`cogs/embed_builders/portfolio_embeds.py`）呈現進場六重鐵律（`market_analysis/dynamic_rollover/opportunity_cost.py::_confirm_entry_signal()`，本專案既有機會成本轉倉候選標的確認的生產路徑，含即時 15m K線/總經風控/財報行事曆/選擇權到期日等 I/O）的即時 Pass/Fail 判定（純接線，未新增任何量化邏輯）。原先並列渲染的「進場四重鐵律」（`market_analysis/entry_ironclad.py`，純函式、零 I/O 的獨立閘門）已移除；其中放量倍數 1.5x、UOA 物理封頂 ratio 門檻與主力買盤 ratio>=0.8x 檢查已直接整併進六重鐵律條件一/三/四本身（見上方 `dynamic_rollover/constants.py::_ENTRY_*` 具名常數），條件二現亦額外要求現價站上正 Gamma 支撐牆。條件一、條件三已進一步對齊分析中心 (`create_tactical_symbol_embed()`) 的判讀語意：條件一除既有的收盤價/放量代數條件外，新增要求該根 15m K 棒須為實體陽線 (`close > open`) 且個股淨 GEX 須為 LONG_GAMMA (`net_gex > 0`)，避免負 Gamma 泥淖下的陰線放量被誤判為右側突破；條件三的 Call Wall 距現價空間檢查改為帶正負號的絕對距離 `(call_wall - spot) / spot < 5%`，不再要求 Call Wall 必須還在現價之上，現價已觸及或跌破 Call Wall 同樣視為壓制仍在、空間不足。
  - 因為是獨立頁籤/獨立 Embed，不會與主頁「🌌 標的分析中心」既有欄位共用同一個 Discord Embed 5800 字元總長預算，不影響主頁既有欄位的顯示完整性。

---

## Watchlist 15-Minute Heartbeat

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

## High-Performance Background Scheduling & Cache-Sharing Architecture

To guarantee high scalability on low-RAM VPS deployments and maintain zero-latency user experiences across multi-tenant Discord workloads, the background subsystem enforces strict concurrency control, schedule staggering, and cross-module cache sharing:

### 1. Bounded Concurrency & Controlled Gather (`asyncio.Semaphore(3)`)
- **Heartbeat Pass 2**: Replaced legacy serial sleep intervals (`1.5s ~ 2.0s` per ticker) with `asyncio.Semaphore(3)` concurrent `asyncio.gather`, slashing batch execution times from 40+ seconds down to under 5 seconds while safely respecting Finnhub/yfinance rate limiters (`AsyncLimiter(20, 60)`).
- **Sector Rotation Data (`SECTORS`)**: 11 major sectoral ETFs are gathered concurrently via `Semaphore(3)` in `gather_sector_rotation_data()`, reducing analyst reporting preparation time from 15+ seconds to <2 seconds.
- **Price/Volume 15m Monitor (`PriceVolumeAlertMonitorCog`)**: Evaluates confirmed 15-minute K-line bars across all user watches concurrently using `Semaphore(3)`, finishing evaluations within 1 second.

### 2. Cross-Module Shared Radar Cache (`bot._latest_radar_data_cache`)
- When `SchedulerCog.dynamic_market_scanner()` executes at `:00`, `:15`, `:30` and `:45`, Heartbeat Pass 2 populates `bot._latest_radar_data_cache` and sets `bot._latest_radar_cache_time = time.time()`.
- When `PortfolioMonitorCog.monitor_real_portfolio_task()` fires 5 minutes later (at `:05`, `:20`, `:35` and `:50`), it directly consumes `bot._latest_radar_data_cache` if it is fresh (< 300 seconds), achieving **100% in-memory cache hits** for all overlapping holdings and eliminating duplicate network requests. Any non-watchlist holding is fetched concurrently via `Semaphore(3)` fallback.

### 3. Multi-User Market Scan De-duplication ($O(U \times S) \to O(S)$)
- In `TradingService.run_market_scan()`, skew, PCR, and earnings dates (`calendar_service.get_symbol_earnings`) are pre-cached at the unique-symbol level (`symbol_sentiment_cache`) before iterating over users.
- Reuses `skew_data` already extracted during single-target scanning, ensuring that $U$ users monitoring $S$ identical symbols triggers exactly **1 calculation per symbol**, eliminating redundant Greeks and calendar parsing.

### 4. GEX Cloudflare Tunnel SWR & SingleFlight (`SingleFlightManager`)
- `fetch_symbol_gex_metrics(symbol)` utilizes a Stale-While-Revalidate (SWR) cache strategy:
  - If a stale cache exists (past `_EDGE_SNAPSHOT_MAX_AGE_SECONDS`, currently 30 minutes — shared with the edge-snapshot freshness gate it wraps, see §1 above), it is returned immediately with `_is_stale_cache = True` to prevent blocking the caller.
  - A background refresh task is dispatched using `SingleFlightManager.run(f"scrape_gex_{symbol.upper()}", ...)`, coalescing duplicate concurrent requests and preventing Playwright worker stampedes. The 15-second bound on the underlying scrape comes from the `httpx.AsyncClient(timeout=15.0, ...)` call inside `_scrape_symbol_gex_raw()` itself, not from `SingleFlightManager` (see below — the manager has no timeout of its own by default).
  - **SWR Refresh Backoff**: because the background refresh is fire-and-forget and a failed attempt does not touch the kv_cache timestamp, every call arriving while the cache is stale used to re-dispatch a brand-new background scrape with no rate limit — a thundering-herd risk against a persistently failing edge scraper. A per-symbol `_gex_swr_last_attempt` timestamp (`index_microstructure.py`) now gates re-dispatch behind a `_GEX_SWR_REFRESH_BACKOFF_SECONDS` (60s) cooldown: calls arriving inside the cooldown still get the stale cached data immediately, they just skip triggering another background attempt.
- **`SingleFlightManager.run()` optional `timeout`**: `services/single_flight.py` now accepts an opt-in `timeout: float | None` kwarg. When set, it bounds only *that specific caller's* wait via `asyncio.wait_for(asyncio.shield(task), timeout=...)` — the underlying shared task is never cancelled by one caller's timeout, so other coalesced callers (and the eventual cache write-back) are unaffected. Default remains `None` (wait indefinitely), preserving prior behavior for every existing call site; it's available for future callers on interaction-latency-sensitive paths to opt into.

### 5. Staggered Cron Schedule
- `08:00 ET`: `fundamental_filing_scan` (holdings SEC filings)
- `08:30 ET`: `daily_reddit_update` (RSS sentiment pre-fetch)
- `08:45 ET`: `pre_market_risk_monitor` (pre-warms IV, Max Pain, Expected Move, and Squeeze cache into SQLite `market_cache` before Analyst Agent and market open)
- `09:00 ET`: `AnalystAgent.pre_market_loop` (consumes pre-warmed cache)
- `:00 / :15 / :30 / :45 ET`: `dynamic_market_scanner` (populates `_latest_radar_data_cache` and dispatches heartbeat)
- `:05 / :20 / :35 / :50 ET`: `monitor_real_portfolio_task` (consumes fresh `_latest_radar_data_cache` without network overhead)

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

### 4. Volume-Weighted Bullish Probability (VWBP) 演算法 (`cogs/unified_terminal/utils.py`)
為量化多個預測市場對單一美股標的的總體共識，系統實作了**成交量加權看多勝率 (Volume-Weighted Bullish Probability, VWBP)**：
- **事件方向性標準化 (Directional Normalization)**：
  - 解析市場問題文本，若偵測為看跌事件（如包含 `drop below`, `fall below`, `crash`, `bearish`, `fail`），則自動將 `Yes` 機率轉換為看多勝率：$P_{bullish} = 1.0 - P_{yes}$。
  - 看漲或目標達成事件則維持 $P_{bullish} = P_{yes}$。
- **成交量加權公式**：
  $$\text{VWBP} = \frac{\sum_{i=1}^{n} (P_{bullish, i} \times \text{Volume}_i)}{\sum_{i=1}^{n} \text{Volume}_i}$$
- **標籤與視覺渲染**：
  - $\text{VWBP} \ge 55.0\%$：標註為 `🟢 XX.X% 巨鯨看多 (N檔加權 · 池量 $X.XXM)`
  - $\text{VWBP} \le 45.0\%$：標註為 `🔴 XX.X% 巨鯨偏空 (N檔加權 · 池量 $X.XXM)`
  - $45.0\% < \text{VWBP} < 55.0\%$：標註為 `⚖️ XX.X% 巨鯨中性 (N檔加權 · 池量 $X.XXM)`

### 5. 雙頁籤輿情社群終端與共振雷達 (`SymbolHubView` & `create_media_sentiment_embed`)
個股分析面板 (`/x <symbol>` 與 `SymbolHubView`) 實施 **1-to-1 資料同步之雙頁籤架構**：
- **`🏠 核心指標` (Core Tab)**：
  - `create_tactical_symbol_embed()` 僅在 `📐 情緒與邊緣偵測 (Edge Detection)` 的 ANSI 區塊中保留 Polymarket 與 Reddit 的計算結果摘要（如 `Polymarket: 🟢 75.0% 巨鯨看多` 與 `Reddit: 🚀 樂觀 (Bullish)`）。
  - 徹底移除文章列表與超連結，保持核心量化數據（Greeks, Skew, PutWall, GEX）的清晰整潔。
- **`🎭 輿情社群` (Media Tab)**：
  - `create_media_sentiment_embed()` 提供完整的情報下鑽視圖，包含：
    1. **`📊 輿情與期權共振雷達`**：ANSI 控制台即時交叉驗證巨鯨定價 (Polymarket)、散戶風向 (Reddit) 與期權微觀結構 (Greeks & Skew)，輸出共振狀態判定（如「同步」、「背離 (散戶樂觀 vs 專業避險)」、「背離 (現價暴跌但波動率極低)」）。
    2. **`🐋 Polymarket 預測事件`**：列出匹配之預測市場事件標題、勝率與資金池超連結。
    3. **`🔥 Reddit 社群熱門討論`**：展示前 3 名精確匹配之 Reddit 熱門貼文（帶有 `[r/wallstreetbets]`, `[r/stocks]`, `[r/options]` 等看板前綴與直接超連結）。
    4. **`📰 即時市場新聞與權威報導`**：結構化展示權威新聞媒體（Bloomberg, Reuters, Yahoo Finance 等）、標題超連結與時間戳（如 `25分鐘前`）。
- **1-to-1 資料集一致性**：核心頁籤與輿情頁籤共享相同的底層抓取與計算資料集，確保使用者切換頁籤時數據完全對齊且無延遲。

### 6. Interactive Commands & Unified Terminal Integration
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
  - Individual symbol GEX profiles (Put Wall, Call Wall, Net GEX) fetched from `/api/v1/scrape/options/{symbol}/gex` are cached in `kv_cache`, keyed by `gex_metrics_{symbol}`, to optimize rendering speed for the `/x` terminal and reduce edge scraper overhead. This `kv_cache` freshness gate deliberately reuses the same `_EDGE_SNAPSHOT_MAX_AGE_SECONDS` constant (`market_data_service.py`, currently 30 minutes) as the underlying edge-snapshot layer it wraps, rather than an independent, longer-lived TTL of its own — an earlier hardcoded 4-hour gate on this outer layer was found to silently mask fresher data the edge background poller had already written every ~30 minutes, so the two layers now share one freshness definition by construction instead of two constants that can drift apart.
- **Tactical Scaling**:
  - Under `SHORT_GAMMA_CRITICAL`, the watchlist scanner in `intraday_pipeline.py` automatically scales `dynamic_grid_step` by **$1.5\times$** to slow down capital depletion during market washouts.
- **Macro GEX Fetch Degradation & Last-Known-Good Cache Fallback**: `fetch_gex_metrics()` (the SPY-level macro GEX fetch behind `get_market_regime()`) persists every successful scrape into a dedicated `macro_gex_metrics_cache` kv_cache entry. If the live `/api/v1/scrape/macro/gex` call to `TUNNEL_URL` fails or times out (a broad `except Exception`, logged as `無法從 Tunnel Scraper 獲取 GEX 數據`), the function now replays the **last successfully fetched** `gamma_flip`/`spy_spot`/`put_wall` (tagged with `_is_stale_cache: True`) instead of the hardcoded constants (`gamma_flip=515.0`, calibrated to an old SPY price level). This matters because `get_market_regime()` and `cogs/unified_terminal/utils.py`'s `gamma_flip_line` comparison both pit this value against the **live** spot price — a stale hardcoded constant far below current market levels made `spy_spot < gamma_flip` structurally almost impossible to trigger true during exactly the high-load/high-volatility windows when a real negative-gamma regime is most likely and most important to detect. The hardcoded constants are still used as the absolute last resort when no prior successful fetch has ever been cached (e.g. first boot). This mirrors the pre-existing stale-cache pattern already used by the per-symbol `fetch_symbol_gex_metrics()` in the same file.

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
  - **Existing Short Call Coverage Gate**: `recommend_covered_calls()` (`market_analysis/trading_orchestration.py`) cross-checks the user's existing options positions (`context_type = 'TRADE'`) via `get_covered_shares()`, summing shares already collateralized by open short calls (`opt_type == "call"` and `quantity < 0`) on the same symbol. `uncovered_shares = current_shares - covered_shares` is used to (a) skip the recommendation entirely (return `None`) when fewer than 100 uncovered shares remain, and (b) cap the number of surfaced contracts to `uncovered_shares // 100`, so the engine never recommends building a new covered call against shares already committed to an existing one. The returned dict now also carries `covered_shares`, `uncovered_shares`, `max_new_contracts`, and `existing_calls` (list of `{strike, expiry, quantity, shares_covered}`), which `create_covered_call_unlock_embed()` renders in a dedicated `🔒 既有備兌覆蓋狀態` field.
  - **Daily Dispatch Dedup**: The 15-minute dispatch site (`cogs/trading/portfolio_monitor.py::monitor_real_portfolio_task`) applies the same daily `kv_cache` anti-spam pattern used by the WTI and price-volume alerts (`cc_unlock_{user_id}_{symbol}_{YYYYMMDD}`), guaranteeing at most one covered-call-unlock DM per user, per symbol, per day, regardless of how many 15-minute scan cycles the underlying condition remains true.

### 5. Manual Macro Update Controls (Added in v1.7.3)
- **Discord Slash Command**: Administrators can manually update GEX and FedWatch data via `/force_macro_update` in Discord.
- **CLI Command**: Developers or scripts can manually trigger macro crawlers via:
  ```bash
  python cli.py admin force-macro-update
  ```
- **Stale-Cache Marker**: Both the `/force_macro_update` slash command (`cogs/trading/admin_commands.py`) and the CLI equivalent (`cli.py`) check the `_is_stale_cache` flag on the dict returned by `fetch_gex_metrics()` and append a ` ⚠️ [使用快取資料]` marker to the reported GEX line whenever the live edge-scraper fetch failed and the response is the last-known-good cached value (see §1 above) rather than a fresh scrape — this is not treated as an error (the command still reports overall success), it only flags that the displayed GEX numbers are not live.

### 6. Volume Profile & Triple Discount Pricing (TDP)
- **V-POC Calculation**: The engine calculates the Volume Point of Control (POC) using `pandas-ta` volume profile functions. No live dark-pool data source is wired up (the previous `nexus_edge_scraper` darkpool endpoints returned synthetic/mock data and have been removed); the Radar Terminal's `dp_poc` field, used by its `magnetic_filters` quant filter and absolute-support-resonance check, is a fallback proxy resolved from this same Volume-POC signal (or the cached HVN price) — not genuine dark-pool print data.
- **Absolute Support Resonance** (Radar Terminal only, `build_radar_scan_embed`): If the fallback `dp_poc` closely overlaps with the Market Maker's PutWall (< 1% deviation), an absolute support resonance alert (`🧲 共振磁吸`) is flagged.
- **TDP Signal — Symbol Hub (`/x symbol:`)**: When the current spot price falls below the EMA 21, the option Max Pain level, AND the V-POC, a `✨ TDP 估值三擊 (Triple Discount Pricing)` signal is activated, highlighting an immensely discounted structural entry point backed by volume-profile support. (A fourth, dark-pool-backed condition was removed from this gate — Symbol Hub had no real dark-pool data source, so that condition could never be satisfied.)
- **TDP Signal — Radar Terminal (`tdp_mode` quant filter)**: A separate four-condition check (`ma20`, `max_pain`, `volume_poc`, and the fallback `dp_poc` above) inside `evaluate_advanced_filters()`; unaffected by the Symbol Hub TDP change above.

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

## Event Calendar Architecture & Translation Engine

`services/calendar_service.py` is the shared calendar gateway.

Current design:

- macro events are cached by **month** (fetched dynamically from `nexus_edge_scraper` querying TradingView)
- earnings are cached by **symbol** (fetched via Finnhub API)
- watchlist heartbeat, calendar views, pre-market alerting, and analyst flows all share the same SQLite-backed cache path

Do **not** add raw market-calendar API calls directly to feature code when calendar helpers already exist.

### Macro Economic Calendar Translation and Normalization Engine (`market_analysis/macro_calendar_translator.py`)
為徹底解決 TradingView 原始總經事件英文名稱繁雜、縮寫不一與聯準會官員演講解析困難的問題，系統建置了全域統一的中文化與正規化引擎：
1. **標準 150+ 總經事件中英對照庫 (`_RAW_MACRO_EVENT_TRANSLATIONS`)**：
   - 涵蓋通膨物價（CPI, Core CPI, PCE, PPI 年增/月增率）、就業市場（Nonfarm Payrolls, Initial Jobless Claims, Unemployment Rate, JOLTs）、GDP 與經濟成長、房地產市場（Existing/New Home Sales, Building Permits）、國庫券與公債拍賣（4-Week ~ 30-Year Treasury Auction）、ISM / S&P PMI 採購經理人指數、密西根大學消費者信心指數等。
   - 支援不分大小寫與多種常見別名變體映射，確保事件名稱 100% 符合台灣與華語金融市場慣用翻譯。
2. **聯準會官員動態演講解析 (`FED_OFFICIALS_MAP` & `translate_macro_event`)**：
   - 內建 30+ 位現任與歷任聯準會官員名冊（包括 Powell 鮑爾, Waller 華勒, Bowman 鮑曼, Williams 威廉斯, Brainard 布蘭納德, Yellen 葉倫等）。
   - 透過 Regex 自動擷取「Fed [Name] Speaks / Speech / Testifies」模式，動態組合為標準化中文，例如：`"Fed Waller Speech"` ➔ `"聯準會華勒發表演說"`。
3. **優雅降級與保底機制**：若遭遇未在庫內之罕見事件，系統會保留原始英文名稱並自動清理冗餘後綴，確保永不拋出異常。

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
  - **啟發式代理數據揭露 (Heuristic Proxy Disclosure)**：當某項使用者可見的判定/標籤是由**啟發式規則或代理指標**推算而來（而非真實的第一手數據源），必須在該欄位/圖例附近明確揭露，不能讓使用者誤以為是即時精確數據。現行案例：
    - UOA SWEEP/BLOCK/CROSS 分類（`_format_uoa_field()`、`watchlist_embeds.py` 心跳 UOA 表格）：由成交量整數手數形狀 + Bid/Ask 執行價位置兩套啟發式訊號組合而成，非真實 order-type tape 資料，表格下方固定附註揭露文字。
    - 🧲 共振磁吸 / 高階磁吸過濾（Radar Terminal `build_radar_scan_embed` 圖例、`magnetic_filters` 下拉選單描述）：`dp_poc` 是 Volume-POC/HVN 的代理指標，本平台無真實暗池數據源，固定附註揭露。
    - 🏦 資產端保證金與購買力（Symbol Hub）：`option_buying_power`/`margin_used` 是使用者自填數值，非即時券商保證金數據，區塊開頭固定附註揭露。
    - 新增此類代理判定時，比照上述既有案例的措辭與位置（表格/圖例附近、簡短一行）加入揭露，而非省略。
  - **結構化網格與戰術意圖映射 (Structured Grid & Tactical Intent Mapping)**：數據表格（如異常交易流、委託單列表、持倉明細）必須動態計算每列的最大字元寬度以對齊網格。底層的原始數據流或交易類別應被映射轉換為直觀的戰術意圖描述，使終端使用者能迅速判讀意圖與支撐/阻力物理界線。
  - **字數上限與分頁保護 (Pagination & Message Splitting)**：
    - **批次分頁限制**：當批次掃描或查詢的標的/項目數量過大時，為避免超出 Discord 的 4096 (Description) 與 6000 (Total Size) 字元上限而導致 `400 Bad Request` 系統錯誤，單一頁面最多僅能承載 **10 個標的**。
    - **動態頁碼與分流投遞**：分頁後 Title 後方應標註 `(第 X/Y 頁)`。呼叫端對於互動指令應透過 Ephemeral 迭代發送，排程背景任務則應經由 `queue_dm` 作為獨立訊息進行分開投遞，嚴禁在單一訊息中過度堆疊。

---

## Dynamic Rollover Engine (動態轉倉引擎)

The platform features an automated **Dynamic Rollover Engine** (`market_analysis/dynamic_rollover/`, a package with a facade `__init__.py` assembling `DynamicRolloverEngine` from per-scenario submodules) that monitors the real portfolio every 15 minutes. It evaluates holdings across five core scenarios to defensively rebalance assets or shift momentum based on Specification by Example (SBE) guidelines.

### Scenarios
1. **Fundamental Thesis Broken (原型假設破滅)**: Leverages `nexus_edge_scraper` via the SEC EDGAR API (`httpx` + BS4 decomposition + Regex tag filtering). Specifically, `section_extractor.py` provides **structured extraction** (Forward Guidance, Margin & Cost, Market Share, Financial Results, Operational Disruption) to prevent token explosion. It uses an **Advanced CoT** (Chain of Thought) system prompt to validate the moat. If broken, completely liquidates the asset into the Core holding (e.g., VOO).
2. **Opportunity Cost & EV Comparison (機會成本轉換)**: Compares the `PowerSqueeze` indicator and Expected Value (EV) between a decaying holding and a breakout watchlist target. Recommends tactical option spreads (e.g., Bull Call Spreads) to roll the capital.
3. **Core vs Satellite Rebalancing (核心與衛星比例再平衡)**: Prevents concentration risk. If a SATELLITE asset (e.g., NVDA) exceeds its `max_allocation_pct` due to a rally, the engine partially sells it back to the `target_allocation_pct` and buys the CORE asset.
4. **Leverage & Margin Defense (槓桿與維持率防禦)**: Triggers when the broad-market macro regime enters `SHORT_GAMMA_CRITICAL` / `SYSTEMIC_LIQUIDITY_CRISIS` (大盤宏觀風控紅線亮起 — GEX Flip substantively broken, market makers flipped into negative-gamma stampede) AND account margin pressure is detected (reusing the `/stress_test` GTC cash-deficit proxy). The engine then checks **every SATELLITE holding individually** for structural no-edge conditions (structural breakdown or whale STO block — reusing the same thresholds as Scenario 3, not new ones). Holdings with no edge are force-liquidated 100% with a Sell-To-Close (STC) or Buy-To-Close (BTC) signal; holdings that still show edge are left untouched (no forced reduction). When there is an actual GTC cash deficit, the freed capital is routed to `"CASH"` to immediately close the margin gap. Otherwise (pure defensive parking, no real deficit), the engine picks between two destinations:
   - **反向ETF (Inverse ETF) — third `target_core` destination (`market_analysis/dynamic_rollover/inverse_hedge.py`)**: `get_inverse_symbol()` resolves the no-edge symbol to a liquid inverse ETF via a three-tier fallback — direct single-stock map (`SINGLE_STOCK_INVERSE_MAP` in `constants.py`, e.g. `NVDA`→`NVDD`/`NVD`, `TSLA`→`TSLS`/`TSDD`; curated and periodically re-verified by the user, since these small, thinly-traded single-stock inverse ETPs get liquidated/renamed by issuers over time) → direct broad-index map (`INDEX_INVERSE_MAP`, e.g. `QQQ`→`SQQQ`, `SPY`→`SH`) → sector fallback via `risk_engine.get_sector_benchmark()` + `SECTOR_INVERSE_MAP` (e.g. an uncovered semiconductor name resolves to `SMH`→`SOXS`), with `INDEX_INVERSE_MAP["SPY"]` (`SH`) as the final fallback so every symbol always resolves to *some* candidate. `select_inverse_leverage_tier()` then picks between a symbol's `"1x"`/`"2x"` variant (where both exist) based on conviction: structural breakdown **and** whale STO block both firing (double confirmation) selects the higher `"2x"` leverage tier; either condition alone stays at the lower-decay `"1x"` tier. The candidate is only actually used if `confirm_inverse_hedge_spot_momentum()` passes — a pure-spot (no options-chain) check of the inverse ETF's own RSI(14) > 50, last close above its 10-day MA, and average dollar volume over its own 20-day lookback clearing a minimum liquidity floor — deliberately skipping the options-chain-based engines (GEX/Skew/UOA) used elsewhere, since leveraged/inverse ETP option chains are typically too thin and would misfire against the existing 15%-spread illiquidity gate. Any fetch/computation failure or a failed confirmation fails closed (returns `False`) and falls through to the pre-existing **BOXX** destination below — this scenario never guesses.
   - **BOXX** (unchanged pre-existing behavior, also the fallback above): rolled into BOXX to lock in risk-free interest. This is deliberately distinct from Scenarios 2/3's default idle-capital target of **VOO**: during a systemic macro sell-off VOO itself drops in sympathy, so only the genuine cash-equivalent BOXX is treated as a true defensive parking spot. BOXX's existing collateral haircut, `/stress_test` liquidity-buffer math, and zero-beta/fixed-income whitelisting are unrelated and unaffected by this scenario.
5. **Core Capital Deployment (核心資金部署)** (`market_analysis/dynamic_rollover/core_deployment.py::evaluate_core_deployment`): Scenarios 2/3's holding loops only ever treat CORE holdings (e.g. VOO) as a rollover *destination*, never a *source*. This scenario fills that gap: for any CORE holding where the user has explicitly set `target_allocation_pct` via `/edit_holding` (strict opt-in — no fallback default, so a fully-invested VOO position never gets deployed unless the user opted in) and current allocation exceeds that target by more than `_CORE_EXCESS_MIN_TRADE_PCT` (0.5%), the excess position is deployed in full to one of two destinations, chosen per-holding by comparing `boxx_allocation_pct` (0-100, `/edit_holding`-settable, stored as a 0.0-1.0 fraction like `max_allocation_pct`) against `_BOXX_DEFENSE_THRESHOLD` (50.0):
   - **`>= 50` → Defense branch**: the full excess is routed to **BOXX** to lock in risk-free interest (same wording pattern as Scenario 4's BOXX routing). This branch does **not** require a candidate satellite target or entry confirmation — BOXX is a pure defensive cash-equivalent parking spot and shouldn't be gated on "was a good opportunity found."
   - **`< 50` → Opportunity branch**: unchanged pre-existing behavior — the excess is deployed into the single high-EV candidate satellite target already pre-screened by Scenario 2 (`opportunity_cost.py`), gated behind the same six-rule `_confirm_entry_signal` breakout confirmation Scenario 2 uses.
   - If the user leaves `boxx_allocation_pct` unset, the engine substitutes a live suggestion from `index_microstructure.suggest_boxx_allocation_pct()` for that evaluation round instead of requiring a manual decision: 70 when `get_market_regime()` returns `SYSTEMIC_LIQUIDITY_CRISIS`/`SHORT_GAMMA_CRITICAL`, 60 when the Fear & Greed index (`fetch_core_macro_metrics()['fear_greed']`) is `<= 25` (Extreme Fear), 20 when `>= 75` (Extreme Greed), else 30 (baseline — normal markets keep defaulting to the opportunity branch, preserving the pre-BOXX-threshold behavior for the common case). Both the regime check and the auto-suggestion are computed at most once per evaluation run, shared across every CORE holding being evaluated that round.
   - Both destinations still produce exactly one instruction dict per triggering holding (`target_core` is always a single scalar, either `"BOXX"` or the candidate symbol) — the engine never splits a single holding's excess capital proportionally across two destinations in one run, so the existing "one instruction → one embed → one DM → one audit-log row" pipeline in `portfolio_monitor.py` and `create_dynamic_rollover_embed` needed no changes.
   - **`target_allocation_pct` gets an advisory-only macro suggestion too, but it is deliberately never auto-applied.** `index_microstructure.suggest_target_allocation_pct()` mirrors `suggest_boxx_allocation_pct()`'s four-tier output (70/60/30/50 for Crisis/Extreme Fear/Extreme Greed/Normal, vs. BOXX's 70/60/20/30) but is consumed only by `/list_holdings`, which — for any CORE holding still missing `target_allocation_pct` — computes the suggestion once per invocation and renders it in a separate "💡 核心資金部署建議" field, clearly distinct from the actual-configured-value column in the holdings table. It is intentionally *not* wired into `evaluate_core_deployment()`'s opt-in gate: doing so would silently start deploying capital for users who never explicitly set `target_allocation_pct` via `/edit_holding`, defeating the strict opt-in protection `test_evaluate_core_deployment_no_target_allocation_is_noop` guards. `boxx_allocation_pct`'s auto-suggestion is safe to auto-apply because it only ever fires *after* that opt-in gate has already been satisfied by the user (it decides where the excess goes, not whether deployment happens at all); `target_allocation_pct`'s suggestion has no such downstream gate, so it stays advisory-only.
   - Both suggestion functions share one private classifier, `_resolve_core_deployment_macro_tier()` (evaluates `get_market_regime()` then, if not already in crisis, `fetch_core_macro_metrics()['fear_greed']`, at most once per call), so `suggest_boxx_allocation_pct()` and `suggest_target_allocation_pct()` are structurally guaranteed to read the same regime/Fear & Greed snapshot and can never classify the same market moment into conflicting tiers — verified directly by `test_suggest_target_and_boxx_allocation_pct_never_diverge_on_same_input` in `test_macro_risk_upgrade.py`.
6. **Macro Top-Escape Anticipatory Defense (宏觀逃頂前瞻防禦)** (`market_analysis/dynamic_rollover/macro_top_escape_defense.py::evaluate_macro_top_escape_defense`): The lowest-confidence, most speculative of the six scenarios — a purely probabilistic, leading-indicator score rather than the price/margin-confirmed reactive triggers Scenarios 3/4 use. Deliberately dispatched **last** in the per-user evaluation order (3→2→5→4→6) so it can never pre-empt a more certain signal on the same symbol; it still consumes the cumulative `already_flagged_symbols` set built from Scenarios 2/3/4/5.
   - **Three gates, all required**: (1) strict opt-in via `user_settings.enable_macro_top_escape_defense` (same philosophy as Scenario 5's `target_allocation_pct` — any feature that moves the user's capital defaults off); (2) `index_microstructure.evaluate_macro_top_escape_score()` must resolve to its `CRITICAL` tier, computed from up to 5 factors — VIX term-structure backwardation (`vts_ratio`), CNN Fear & Greed extreme reading, FedWatch hawkish rate-cut probability, a negative-gamma regime flag (`get_market_regime()`), and an optional satellite-portfolio "euphoria breadth" ratio (`_compute_satellite_euphoria_ratio()`, reusing the exact same profit-unlock/euphoria-skew formulas as Scenario 3's Call Wall Euphoria gate — with at least 3 of these factors required to align); (3) exclusion of `already_flagged_symbols`.
   - **Scope & action**: SATELLITE holdings only (same as Scenarios 3/4; CORE excess is Scenario 5's job). Trims a bounded `_MACRO_TOP_ESCAPE_TRIM_RATIO` (25%) of the position — deliberately far lower than Scenario 3's 90% or Scenario 4's 100%, since no individual structural breakdown has actually been confirmed yet and false-positive risk is materially higher than the two reactive, price/margin-confirmed scenarios.
   - **Destination is always BOXX, never CASH or VOO**: reuses the same reasoning as Scenario 4's inverse-hedge fallback — a systemic macro top-escape environment tends to drag VOO down in sympathy, so only a genuine cash-equivalent (BOXX) counts as a defensive parking spot; there is no Scenario-4-style margin-deficit concept here, so no CASH branch exists.
   - Fully covered by `tests/unit/test_macro_top_escape_defense.py` (pure `evaluate_macro_top_escape_defense_impl` logic) and dispatch-wiring is asserted in `tests/unit/test_trading_output.py::test_monitor_real_portfolio_task_invokes_macro_top_escape_defense`.

### GEX Mapping & Anti-Washout Stop Engine
- **GEX & Market Maker Intent Mapping Engine (`index_microstructure.classify_gex_wall`)**: Evaluates individual strike GEX exposures against the maximum positive GEX wall across the option chain. Classifies levels into `SUPPORT_GEX_WALL` (MM support floor / dip buying hedging), `RESISTANCE_CALL_WALL` (overhead resistance ceiling / negative gamma pinning / heavy OTM call clusters), or `NEUTRAL`.
- **Anti-Washout Stop Engine (防洗盤動態停損引擎)**:
  - **Anchor Wall Priority**: Dynamic stop loss anchors to `support_wall` > `gex_put_wall` > `hvn` > `spot`.
  - **15m ATR Buffer, No Hardcoded Bounds**: Base stop is calculated purely as `anchor_wall - 1.5 * atr_15m`, with **no** artificial percentage clamp applied — an earlier `[spot * 0.95, spot * 0.98]` (2%~5%) boundary was removed because it forcibly widened or tightened the stop whenever `anchor_wall` sat unusually close to or far from spot, distorting the microstructure-derived level. The stop now reflects the formula output directly; only the LVN magnetic snap below may still adjust it for genuine liquidity-topology reasons.
  - **LVN Magnetic Snapping Algorithm (量價拓撲吸附演算法)**: When stop loss falls within 1.5% of an LVN vacuum, fixed percentage shifting (`* 0.985`) is strictly forbidden. The engine penetrates downward and magnetically snaps to the upper edge of the secondary HVN cluster: $\text{Secondary HVN} + (0.2 \times ATR_{15m})$, using liquidity volume to prevent cascading slippage.
- **DTE Three-Tier State Machine (`structural_signals.evaluate_option_dte_tier(dte, position_intent)`)**: Replaces the earlier "0/1 DTE Risk-Parity Dynamic Sizing" mechanism (which widened the stop to $3.0 \times ATR_{15m}$ and halved position sizing) with an explicit three-tier classification, parameterized by whether the caller is evaluating a **new** entry/rollover (`NEW_OPPORTUNITY`) or **existing**-position risk management (`MANAGE_EXISTING`). Applies only to `OPTIONS` positions; `SPOT`/`HOLDING` `dte` defaults to a `99` sentinel and always resolves `NORMAL_EXECUTION`.
  - **`NORMAL_EXECUTION`** (`dte >= 7`): Unaffected, all existing logic applies as-is.
  - **`LOCKOUT_SKIP`** (`1 < dte < 7`, `NEW_OPPORTUNITY` only): The position is "end-of-life liquidity noise" — Scenario 2 (`evaluate_opportunity_cost_for_satellites`) silently skips it (no rollover computed from a decaying near-expiry contract), and Scenario 3's Euphoria branch is barred from opening a *new* Bear Call Spread against it. Existing dual-track stop-loss/structural-breakdown monitoring is **not** affected — `MAINTAIN_RISK_MONITORING` (the `MANAGE_EXISTING` counterpart for the same DTE range) intentionally imposes no restriction, so a near-expiry holding still gets protected on the way down.
  - **`EXPIRATION_SETTLEMENT_ALERT`** (`dte <= 1`, regardless of `position_intent`): Unconditionally short-circuits to `_build_forced_settlement_instruction()` at the very top of `check_satellite_rebalancing_impl`'s per-asset loop, before any anchor/breakdown computation runs. Forces `LIQUIDATE` 100% with `target_core` set to the **same** underlying symbol (rolled to its next-month front-month contract, ~21-45 DTE, mirroring the existing "next month" convention used elsewhere in `opportunity_cost.py`/Covered Call recommendations) rather than switching to a different symbol — explicitly forbidding the old "widen the stop to resist unwinding" behavior.
- **Asset Class Dual-Track Exit Mechanism (現貨與期權雙軌裁決機制)**:
  - **SPOT Assets (15m Candle Close Track)**: A liquidation (`LIQUIDATE`) directive is strictly executed only if the 15-minute candle close (`price_15m_close`) falls below the calculated stop loss AND confirmed by the **Gamma Cliff Confirmation Engine**. Intraday lower wick breaches are treated as market maker noise and held as `HOLD` (推播安心防守卡).
  - **OPTIONS Contracts (3-5m Fast Track)**: To prevent catastrophic Delta collapse and Vega crush, options contracts bypass the 15-minute candle close wait. If spot price breaches the stop loss or an IV crash occurs ($\Delta IVR \ge 20\%$), the engine immediately triggers fast market/limit exit (`LIQUIDATE` / `STC` / `BTC`).
  - **Extreme Tick Breach Urgency Marking**: When the independent Track 2 extreme stop (`anchor_base - 3.0 * ATR_{15m}`, all asset classes, ignores the 15m close wait) is what actually fired — as opposed to a routine 15m-close breakdown — the resulting Discord DM is visually escalated: `create_dynamic_rollover_embed(is_extreme_tick_breach=True)` prefixes the title with `🆘【立即人工執行】`, forces the color to red regardless of the scenario's normal palette, swaps in a dedicated "act now, don't wait for the next 15-minute cycle" description, and renders an extra "🆘 極端瞬時停損詳情" field (via `extreme_breach_detail_block`, assembled in `_generate_rule_based_rebalance_report`) reporting trigger price, the breached extreme line, the anchor wall, ATR, penetration depth in both % and ATR-multiples, and the negative-gamma regime callout. This stays purely advisory — no live tick stream or broker order submission was added; it only sharpens how the existing 15-minute-cadence detection is presented.
- **Call Wall Euphoria & Momentum Exhaustion Gate (極端亢奮區雙重動能衰竭確認制)**:
  - When spot $\ge \text{Call Wall}$ or in extreme Euphoria (`Skew Percentile <= 20%`), 90% is liquidated into benchmark (e.g. VOO).
  - For the remaining 10%: Establishing a `Bear Call Spread` is strictly gated by **Dual-Exhaustion Criteria**: (1) 15m SQZ MOM turns negative (`sqz_mom < 0`, momentum topping), (2) Skew leaves euphoria (`skew_percentile >= 30.0%`), AND (3) the DTE three-tier state machine resolves `NORMAL_EXECUTION` for `NEW_OPPORTUNITY` intent (opening a brand-new short-option structure is barred in the `LOCKOUT_SKIP` window).
  - If exhaustion is NOT met (momentum still positive), opening short calls is strictly forbidden to prevent being crushed by a **Gamma Squeeze**. The remaining 10% is instead switched to **Trailing Stop (移動止盈)** to let profit ride — this branch is unaffected by the DTE gate since it manages the existing position rather than opening a new structure.
- **Form-Type-Aware Analysis (10-K/10-Q/8-K)**: `evaluate_fundamental_thesis` (`market_analysis/dynamic_rollover/fundamental_thesis.py`) accepts optional `form_type` and `sections` parameters that customize the LLM prompt per filing type. 10-K supplements weight full-year trends and board-reviewed Risk Factor changes; 10-Q applies extra skepticism toward single-quarter noise (only a cross-quarter *trend* should trigger `is_broken=true`); 8-K (an event-driven Current Report with no MD&A) is judged by *which* Item number fired (e.g. Item 2.05 divestiture, Item 4.02 restatement, and abrupt Item 5.02 CEO/CFO departures are high-signal; Item 7.01/8.01 are usually low-signal). The edge scraper (`nexus_edge_scraper/section_extractor.py`, `local_api.py`) routes its extraction anchor and structured `sections` dict (including a dedicated `key_events` field for 8-K dotted Item headers) by `form_type` as well. Both parameters are optional — empty/legacy inputs keep the prompt byte-identical to the pre-form-type-aware behavior.
- **Manual Trigger (`/verify_thesis`)**: Any user can manually trigger Scenario 1 for any symbol via the `/verify_thesis <symbol>` Discord slash command, regardless of holdings. This command features an interactive UI: it first triggers the Edge Scraper to fetch a list of recent SEC filings (10-K, 10-Q, 8-K), and presents them in a dropdown menu. If the user selects one, or if the 60-second timeout expires, it fetches the specified (or latest) SEC EDGAR report (up to 10,000 characters) and sends it to the LLM.
- **Automated Daily Filing Scan (`cogs/trading/fundamental_filing_monitor.py`)**: A dedicated daily scheduler (`fundamental_filing_scan`, 08:00 ET, skips non-trading days via `market_time.nyse_calendar`) automatically scans **holding-only** symbols (`database.get_all_holdings()`; watchlist symbols are intentionally excluded to bound LLM/API cost) for new SEC filings. For each unique symbol (deduplicated across all holders so a symbol held by multiple users is only analyzed once, throttled via `asyncio.Semaphore(3)`), it compares the latest filing's `accession_number` against a dedicated dedup-cursor table, `fundamental_scan_state` (migration `v062` — distinct from `fundamental_cache`, which stores the LLM verdict itself and has no accession-number column). If a new filing is detected, it's routed through the same form-type-aware `evaluate_fundamental_thesis` pipeline used by `/verify_thesis`. The whole run is gated by `is_memory_safe()` up front (1GB VPS protection). **Only `is_broken=True` results trigger a DM** (via `bot.queue_dm`, reusing `build_fundamental_broken_embed()` — the same embed-construction helper shared with `/verify_thesis`) to holders who have the notification enabled; passing results are written silently to `fundamental_cache` with no DM, to avoid alert fatigue. The scan cursor is only advanced on a successful (non-`None`) LLM result — if the memory-safety gate or LLM call fails mid-scan, the cursor is left untouched so the same filing is retried on the next day's run rather than being silently skipped.
- **Global Defense Gate (全域防禦閘門)**: LLM moat verdicts (`is_broken`, `confidence`, `reasoning`) are written to the SQLite `fundamental_cache` table (via migration `v057`). During the intraday 30-minute heartbeat (`intraday_pipeline.py`), the `evaluate_watchlist_symbol` engine acts as a **Global Defense Gate**. If a symbol is flagged as broken, the engine forcefully intercepts and overwrites any quantitative BTO (Buy-To-Open) or Grid Accumulation signals, replacing them with a strict `wait` scenario and a `LIQUIDATE` directive. This guarantees that technical blindspots (e.g., heavily oversold RSI traps) cannot override fundamentally deteriorating assets.
- **Lightweight Triage Strategy (Scenarios 2, 3, 4)**: Lightweight rule-based tasks execute during the intraday 15-minute `monitor_real_portfolio_task` to ensure zero API blocking.
- **Covered Call Premium Decay Profit-Lock (`market_analysis/dynamic_rollover/covered_call_profit_lock.py`, `RolloverScenario.COVERED_CALL_PROFIT_LOCK`)**: A standalone engine, fully independent of Scenarios 2/3/4/5/6, answering one question only — should an *existing* short CALL position (a Covered Call the user already sold) be bought back early to lock in the collected premium? It is deliberately distinct from `risk_engine.evaluate_ditm_defense`'s DITM "50% profit-lock" (a long-position, deep-ITM concept) and from `recommend_covered_calls()` (which recommends *opening* new covered calls, not closing existing ones).
  - **Scope**: Strictly limited to existing short `CALL` positions (`opt_type=="call"`, `quantity<0`). Short `PUT` (CSP) positions are out of scope for now.
  - **Decay Thresholds**: `decay_pct = (entry_premium - current_premium) / entry_premium`. `>= 50%` triggers a partial `REDUCE` (BTC 50% of the position, `_COVERED_CALL_PROFIT_LOCK_PARTIAL_RATIO`); `>= 80%` triggers a full `LIQUIDATE` (BTC 100%). Below 50%, or when the live quote is unavailable, no instruction is produced (fail-safe, never guesses).
  - **DTE Override**: Reuses the same `evaluate_option_dte_tier()` classifier from the DTE three-tier state machine — `dte <= 1` unconditionally forces a full BTC regardless of decay %, independent of the decay-threshold branch.
  - **Data Plumbing**: `cogs/trading/portfolio_monitor.py` was previously discarding all short option positions before they ever reached the rollover engine (`get_all_trade_positions()` filtered to `quantity > 0` only, feeding just `long_option_trades`). It now also collects short `CALL` positions (`short_call_trades`) and merges their contracts into the *same* `Semaphore(3)` batched `get_option_chain_mid_iv()` quote fetch already used for long positions, avoiding duplicate network requests, before calling `evaluate_covered_call_profit_lock()` per user.
  - **Presentation**: Uses a dedicated `create_covered_call_profit_lock_embed()` (green palette) rather than `create_dynamic_rollover_embed`'s sell/buy rollover framing — same rationale as the existing Covered Call Overlay embed: there is no second "buy into" target, only a BTC close.
  - **Dedup**: Reuses the existing `rollover_alert_...` daily dedup key, extended with `strike`/`expiry` for this scenario only (a user can hold multiple Covered Calls on the same symbol at different strikes/expiries, and the generic `(symbol, scenario, action)` key alone would let one alert suppress another).
  - **Gating**: Ingestion stays behind the existing `config.ENABLE_OPTIONS_ROLLOVER_INGESTION` flag and `config.OPTIONS_ROLLOVER_DRY_RUN` dispatch gate, matching the conservative rollout posture already used for `long_option_trades`.
- **Discord UI**: All rollover actions generate a stylized embed (`create_dynamic_rollover_embed`) packed with terminal execution guidelines, strategy type, and strict buy/sell directions (e.g., BTC for short puts). Title/color are keyed off an explicit `scenario` identifier (`RolloverScenario`) rather than free-text substring matching, so `MARGIN_DEFENSE` alerts always render as critical red regardless of the underlying action. A genuine Track 2 extreme-tick-breach trigger (see above) additionally overrides this with a maximum-urgency `🆘【立即人工執行】` red styling, independent of the underlying scenario's normal color.
- **Toggle Settings**: Users can opt out of rollover alerts via `/notif_settings` under the Defense module (`defense_option_rollover` — also covers Covered Call Premium Decay Profit-Lock alerts), and specifically out of the automated daily filing scan's alerts via `defense_fundamental_thesis` (both keys are independent; `/verify_thesis`'s manual, interactive results are always shown regardless of either toggle).

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

### 2. Threshold Comparison & Pure Price Alert Support (`evaluate_watch_trigger`)
Deliberately separated from bar-fetching so multiple users watching the same symbol share a single yfinance call per scan cycle:
- `direction = "above"` → `Close >= target_price` (breakout).
- `direction = "below"` → `Close <= target_price` (breakdown).
- **雙模支援 (Dual-Mode Watch)**:
  - **價量突破模式 (`volume_multiplier > 0`, 預設 1.5x)**: 要求當根 15 分鐘收盤價達標且成交量大於前 20 根均量的倍數，防止假突破/雜訊。
  - **純價格警報模式 (`volume_multiplier = 0`)**: 若將 `volume_multiplier` 設為 `0`，量能檢查條件將無條件通過 (`volume_condition = True`)，轉為單純的實體 K 線價格觸及/破位警報。

### 3. Per-User Watch Config (`database/price_volume_watch.py`, migration `v063`)
Uses a dedicated SQLite table (`price_volume_watches`, PK `(user_id, symbol)`) rather than `kv_cache`, because the scheduler needs a cross-user, cross-symbol batch query (`get_all_watches()`) that a single-key KV store can't efficiently support. `upsert_watch()` enforces a `_MAX_WATCHES_PER_USER = 15` cap (VPS memory/API-call protection) that only applies to *new* symbols — updating an existing watch's price/direction/multiplier never counts against the cap.

### 4. Scheduler (`cogs/trading/price_volume_alert_monitor.py`)
- `@tasks.loop(minutes=15)`, gated by `market_time.is_market_open()` (unlike WTI's 24/7 cadence, since equity intraday candles are meaningless outside market hours).
- Groups all registered watches by symbol first, fetching each unique symbol's confirmed bar exactly once per cycle even if multiple users watch it.
- **KV Cache Anti-Spam Deduplication**: `price_volume_alert_{user_id}_{symbol}_{YYYYMMDD}` — one alert per user, per symbol, per day (same pattern as WTI's daily dedup keys).

### 5. Interactive Commands & `/notif_settings`
- `/price_alert_set <symbol> <target_price> <direction> [volume_multiplier=1.5]`: upserts a watch (parameterized command, not a modal, since all fields are simple scalars; 可設 `volume_multiplier: 0` 開啟純價格警報).
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
- `nexus_core/market_analysis/macro_calendar_translator.py` — Macro calendar 150+ translation dictionary & dynamic Fed speech parsing engine
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
- `nexus_core/services/polymarket_service.py` — Polymarket whale tracking, VWBP aggregation, and AI summary service
- `nexus_core/services/order_telemetry_service.py` — Order telemetry scanning service
- `nexus_core/database/notifications.py` — custom user notification preferences database operations
- `nexus_core/database/virtual_trading.py` — Database interface for virtual trades (VTR)
- `nexus_core/market_analysis/dynamic_rollover/` — Dynamic rollover engine package (facade `__init__.py` + `fundamental_thesis.py` / `opportunity_cost.py` / `anti_washout.py` / `margin_defense.py` / `structural_signals.py` (also houses the DTE three-tier state machine, `evaluate_option_dte_tier`) / `covered_call_profit_lock.py` / `inverse_hedge.py` (Scenario 4's third `target_core` destination: symbol→inverse-ETF resolution + pure-spot momentum confirmation) / `core_deployment.py` (Scenario 5 + Covered Call Overlay) / `macro_top_escape_defense.py` (Scenario 6: probabilistic leading-indicator defense, dispatched last in the evaluation order) / `models.py` / `constants.py`), anti-washout stop engine, and asset class bifurcation logic. Public import path stays `market_analysis.dynamic_rollover`.
- `nexus_core/market_analysis/signal_calculator.py` — Dynamic trading signal calculator (1.5x ATR buffers, capital allocation models)
- `nexus_core/market_analysis/scenario_classifier.py` — Event-driven quantitative scenario classifier (6 market scenarios including Whale Escort Resonance)
- `nexus_core/database/watchlist.py` — Database CRUD operations for user watchlist symbols (100% deterministic rule-based zero-LLM architecture)
- `nexus_core/database/migrations/v039_add_notification_toggles.py` — migration registering the user_notification_settings table in SQLite
- `nexus_core/tests/unit/test_wti_alert.py` — unit tests for WTI crude oil price alert system, technicals, and embed rendering
- `nexus_core/tests/unit/test_fundamental_filing_monitor.py` — unit tests for the automated daily SEC filing scanner (dedup cursor, is_broken dispatch gating, per-user notification toggle, multi-holder symbol dedup)
- `nexus_core/tests/unit/test_edge_detection_sentiment.py` — unit tests for Edge Detection, Reddit sentiment classification, VWBP, and dual-tab layout
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
- strictly gate background tasks and LLM workflows with `is_memory_safe()` (85% RAM memory gate)
- keep all features safe for 1GB RAM deployment

### Type safety

- prefer explicit Pydantic models / aliases over loose dicts
- keep literal types consistent with model fields
- avoid `Any` unless truly unavoidable at integration boundaries
- **Strict Annotations for Empty Collections**: Always provide explicit type annotations when initializing empty collections (e.g. `_my_set: set[str] = set()`, `_my_list: list[str] = []`, `_my_dict: dict[str, Any] = {}`).
- **Union & Nullability Safety**: Always perform explicit check-guards (e.g. `if obj is not None:`) before accessing properties on optional/nullable objects (like `interaction.message` or `self.view` on Discord items) to avoid Mypy `union-attr` check failures.
- **Dynamic Property Reflection**: Use safe dynamic helpers `getattr(obj, "attr", default)` or `setattr(obj, "attr", val)` when passing or querying dynamic custom states across UI components (e.g. tracking pre-selected states in views before triggering modals).
- **Mypy Exclusion Configuration**: Stale build directories (`build/`, `dist/`) must be kept clean and explicitly ignored in `[tool.mypy]` `exclude` configuration under `pyproject.toml` to prevent build-pipeline duplicate scans.
- **型別自我檢測 (Pre-commit Type Check)**：Mypy 已開啟嚴格模式（Strict Mode），並遞迴檢查所有單元與整合測試模組。在提交程式碼前，開發人員應在包含完整依賴的 Docker 容器中手動跑一次全域型別檢查（在 `nexus_core` 目錄下執行 `docker compose run --rm nexus-seeker python -m mypy --config-file pyproject.toml .`），以確保所有第三方套件（如 `discord.py`）的型別解析正確無誤，避免型別錯誤進入遠端倉庫。

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
6. **pure documentation updates** (e.g., modifying only `AGENTS.md`, `README.md`, or `.md` files) **do not require running test suites or containerized testing**
