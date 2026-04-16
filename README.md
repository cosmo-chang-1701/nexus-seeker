# 🌌 Nexus Seeker

<div align="center">
  <img src="assets/hero.png" alt="Nexus Seeker Hero Image" width="800" />
</div>

**多租戶選擇權量化交易助手 — 由 Discord 驅動**

[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](nexus_core/docker-compose.yml)
[![Deploy](https://github.com/cosmo-chang-1701/nexus-seeker/actions/workflows/deploy.yml/badge.svg)](https://github.com/cosmo-chang-1701/nexus-seeker/actions/workflows/deploy.yml)
[![Architecture](https://img.shields.io/badge/architecture-multi--tenant-purple.svg)](#architecture)

> 一個以 Python 與 Docker 建構的**多租戶選擇權量化助手**。
> 結合技術分析、**Black-Scholes-Merton** 定價模型（含股息率校正）、LLM NLP 風控審查、**Nexus Risk Optimizer (NRO)** 曝險精算，以及全自動化 NYSE 交易日曆，協助交易者執行高勝率的選擇權方向策略與建構防禦組合。

---

## 目錄

- [核心功能](#-核心功能)
- [架構](#-架構)
- [技術棧](#-技術棧)
- [快速開始](#-快速開始)
- [Discord 指令](#️-discord-指令)
- [投資組合工作流程](#-投資組合工作流程)
- [策略邏輯](#-策略邏輯)
- [專案結構](#-專案結構)
- [測試](#-測試)
- [貢獻](#-貢獻)
- [路線圖](#-路線圖)
- [授權條款](#-授權條款)

---

## ✨ 核心功能

| 功能 | 說明 |
|---|---|
| 🔐 **多租戶與隱私** | 所有斜線指令回覆皆為**臨時訊息**（僅觸發指令的使用者可見）。每位使用者依 Discord User ID 獲得獨立的資料庫命名空間。 |
| 📨 **私訊分發器** | 背景排程器對所有使用者執行 **API 去重**，再將個人化的量化報告發送至各使用者的私訊。非同步訊息佇列確保高流量下不阻塞主迴圈。 |
| 🔔 **啟停通知** | Bot 啟動與關閉時自動私訊通知所有已註冊使用者，確保服務可視性。 |
| 📅 **排程推播** | 每項排程任務觸發前，自動以 Discord Timestamp 私訊通知所有使用者下次執行時間（自動轉換為使用者當地時區）。 |
| 🤖 **LLM NLP 風控** | 整合 OpenAI-compatible 推論引擎（支援自架 Inference Server），以 Structured Output（Pydantic Schema）對新聞與 Reddit 情緒進行毒性分析，黑天鵝事件或散戶狂熱時自動否決賣方訊號。 |
| 🕸️ **Reddit 邊緣爬蟲** | 透過 Cloudflare Tunnel 呼叫本地端 `nexus_edge_scraper`（Playwright + BeautifulSoup），即時爬取 Reddit 散戶情緒與共識分數。 |
| 📰 **新聞聚合** | 透過 Finnhub Company News API 即時擷取標的近期官方新聞標題，作為 LLM 風控審查的輸入源。 |
| 🎯 **Delta 精準掃描** | 內建 Black-Scholes-Merton 引擎（`py_vollib`，含股息率 `q` 校正）自動計算目標 Delta 的最佳履約價（例：−0.20 ≈ 80% 勝率）。 |
| 📡 **NYSE 自動排程器** | 整合 `pandas_market_calendars` 並處理日光節約時間與假日 — 動態睡眠至下一個交易日目標時刻。 |
| 🔄 **30 分鐘動態巡邏** | 盤中掃描器以 30 分鐘心跳循環運作，僅在 NYSE 常規交易時段（10:00 ET 後）執行掃描，避開開盤初期造市商無報價期。 |
| 🧊 **4 小時推播冷卻** | 自動排程掃描結果依「使用者 × 標的」維度套用 4 小時冷卻機制，避免重複推播同一訊號；手動 `/force_scan` 不受冷卻限制且不重置計時器。 |
| 📊 **造市商預期波動** | 計算基於 ATM 跨式組合的預期波動（MMM），在財報前標示「地雷區」履約價。 |
| ⚖️ **四分之一 Kelly 倉位** | 以 ¼-Kelly 準則計算倉位大小，每檔標的上限 5%。 |
| 📉 **個股 IV 期限結構** | 偵測 30D/60D IV 逆價差作為恐慌性拋售訊號。 |
| 🌐 **大盤 VIX 結構** | 即時追蹤 VIX 期限結構 (VTS) 與 30/60日 Z-Score 動能，遇逆價差或波動性暴增時觸發系統性防禦機制。 |
| 📐 **垂直偏態濾網** | 分析 25-Delta Put/Call IV 比率，≥ 1.30 標示警告，≥ 1.50 時硬性否決 STO Put 訊號，規避尾部崩盤風險。 |
| 🛡️ **尾部風險防護** | 結合 VIX 數據與偏態指數，當偵測出黑天鵝尾部風險時，動態將曝險斬半 (1/4 Kelly) 並縮減 NRO 保證金上限。 |
| 💧 **流動性濾網** | 自動檢測買賣價差（Bid-Ask Spread），絕對價差 > $0.20 且佔比 > 10% 時剔除流動性陷阱。 |
| 🧪 **波動率風險溢酬 (VRP)** | 比較隱含波動率與歷史波動率，當 VRP < 0（IV 被低估）時拒絕賣方策略，確保風險溢酬為正。 |
| 🎯 **隱含預期波動區間** | 以 `現價 × IV × √(DTE/365)` 計算 1σ 預期波動幅度，確認賣方損益兩平點建構於機率圓錐外，否則硬性剔除。 |
| 💰 **AROC 資金效率濾網** | 計算賣方合約的年化資本回報率 `(權利金 / 保證金) × (365 / DTE)`，低於 15% 的標的自動剔除，僅保留資金效率達標的收租機會。 |
| 🌐 **Beta 加權宏觀風險** | 盤後報告計算投資組合等效 SPY Delta（Beta-Weighted），當淨曝險超過 ±50 股時觸發避險建議。 |
| 📉 **Gamma 脆性評估** | 以二階 Beta-Weighted 平方加權追蹤投資組合淨 Gamma，偵測非線性加速度風險；淨 Gamma < −20 時觸發脆性警告，建議注入正 Gamma 緩衝。 |
| 🔥 **資金熱度極限** | 計算投資組合保證金佔總資金比例（Portfolio Heat），> 30% 警戒、> 50% 爆倉預警，防止過度槓桿。 |
| 🛡️ **What-if 曝險模擬** | 掃描期權機會時，Nexus Risk Optimizer (NRO) 預先模擬建倉後對整體投資組合的 Delta 衝擊，動態防範曝險破表風險。 |
| 🛡️ **自動對沖指令** | 當 NRO 偵測建倉計畫超標時，將反向下達精準的基準與數量避險指示（例：建議賣出 2.5 股 SPY），提供全盤化應對方案。 |
| 👻 **虛擬交易室 (VTR)** | 內建 GhostTrader 引擎，自動根據量化訊號建倉，並自動追蹤合約部位。達停利/停損條件會自動平倉，Delta 擴張時自動轉倉。 |
| 📊 **VTR 績效週報** | 每週五收盤後 (17:05 ET)，自動彙整個人專屬的 VTR 實測交易績效並透過私訊推送週報。 |
| ⚡ **Finnhub 高效報價** | 無縫整合 Finnhub 高效服務，取代不穩定的 Yahoo Finance，徹底排除 ETF 資料請求 404 問題，並大幅提昇股息與財報日的資料準確度。 |
| 💹 **Theta 現金流精算** | 每日 Theta 收益率精算，對照機構級 0.05%–0.3% 標準，確保時間價值曝險合理。 |
| 🕸️ **相關性矩陣風險** | 下載 60 日收盤價建立 Pearson 相關係數矩陣，偵測 ρ > 0.75 的高度重疊板塊並提示集中風險。 |
| 💾 **資料持久化** | SQLite 搭配 Docker Volume — 容器重啟零資料遺失。內建版本遷移引擎（Migration Engine），Schema 變更全自動化。 |
| 🧮 **Greeks 持久化與匯總** | 持倉的 Greeks（Weighted Delta、Theta、Gamma）持久化至資料庫，`UserContext` 一次性匯總真實持倉與虛擬交易的 Greeks 指標，極大化 I/O 效率。 |
| ⚙️ **個人化風險與推播** | 每位使用者可自訂風險限制（1%–50%），及切換 Option 推播、VTR 自動建倉與 PowerSqueeze 等專屬追蹤頻道。 |
| ⚡ **PowerSqueeze 動能追蹤** | 內建向量化 PSQ 數學模組，抓取盤基壓縮突破與能量擴張訊號，可作為獨立風向標並行於原有 Option 訊號。 |
| 💹 **即時報價查詢** | `/quote` 指令透過 Finnhub 即時取得標的報價（含現價、漲跌幅、今日高低與前收盤價）。 |
| 🏗️ **Service Layer 分治** | `TradingService` 集中式業務邏輯層，將 Discord UI 層與核心計算徹底解耦，職責分明。 |

---

## 🏗 架構

本專案採用**雙服務架構**：`nexus_core`（雲端 Discord Bot）與 `nexus_edge_scraper`（本地端邊緣爬蟲），透過 Cloudflare Tunnel 安全互連。

```
Discord 使用者 ──► Discord API ──► NexusBot (bot.py)
                                       │
                     ┌─────────────────┼──────────────────┐
                     │                 │                  │
              斜線指令           私訊分發器          NYSE 排程器
              (臨時訊息)        (背景佇列)         (動態睡眠排程)
                     │                 │                  │
                     └────────┬────────┘                  │
                              │                           │
                     ┌────────▼────────┐          ┌───────▼───────┐
                     │  TradingService │          │  market_time  │ ← NYSE 日曆
                     │ (業務邏輯中樞)  │          │  (動態排程)   │
                     └────────┬────────┘          └───────────────┘
                              │                           │
                     ┌────────▼────────┐          ┌───────▼───────┐
                     │    database/    │          │  market_math  │ ← Facade
                     │  (SQLite PKG)   │          │  (re-export)  │
                     │ ┌─────────────┐ │          └───────┬───────┘
                     │ │ migrations/ │ │                  │
                     │ └─────────────┘ │          ┌───────▼───────┐
                     └────────────────┘          │market_analysis │
                                                  │ (Python PKG)  │
                              │                   ├───────────────┤
                     ┌────────▼────────┐          │  strategy.py  │
                     │   services/     │          │  portfolio.py │
                     │ ┌─────────────┐ │          │  greeks.py    │
                     │ │trading_serv.│ │          │  risk_engine  │
                     │ │ llm_service │ │          │  ghost_trader │
                     │ │market_data  │ │          │  hedging.py   │
                     │ │ news_service│ │   feed   │  margin.py    │
                     │ │reddit_serv. │─│─ ─ ─ ►   │  data.py      │
                     │ └─────────────┘ │          │  report_fmt   │
                     └────────────────┘          └───────────────┘
                              │
                   Cloudflare Tunnel
                              │
                     ┌────────▼────────┐
                     │ nexus_edge_     │  (本地端，獨立容器)
                     │ scraper/        │
                     │ ┌─────────────┐ │
                     │ │ local_api   │ │  FastAPI + Playwright
                     │ └─────────────┘ │
                     └────────────────┘
```

### 排程任務

排程器採用**動態睡眠**架構 — 透過 `pandas_market_calendars` 精準算出下一個 NYSE 交易日的目標時刻，睡眠至該時刻再執行。每項任務觸發前，自動以 **Discord Timestamp** 私訊通知所有使用者。

| 排程模式 | 任務 | 說明 |
|---|---|---|
| **動態睡眠** → 開盤前 30 分 (≈ 09:00 ET) | 盤前風險監控 | 掃描持倉與觀察清單的財報日曆；若財報 ≤ 14 天內，私訊 ⚠️ 風險預警（區分持倉高風險 vs 觀察清單標的）。 |
| **每 30 分鐘心跳** (10:00 ET – 收盤) | 盤中動態掃描 | 每 30 分鐘偵測開盤狀態，僅在常規交易時段內執行（跳過 09:30–09:59 造市商無報價期）。執行策略掃描與 LLM 風險審查，經過 **AlertFilter 條件降噪 (防雙巴與多週期共振)** 後，將優質訊號推播並套用 4 小時冷卻，同時自動送入 VTR 建倉。 |
| **每 30 分鐘心跳** (盤中) | VTR 監控與對沖 | 盤中掃描虛擬交易室 (VTR) 持倉，當觸發獲利/停損/Delta 擴表條件時自動平倉或轉倉並即時通知；依據目標 Delta (Target Delta) 提供部位精準對沖建議。 |
| **動態睡眠** → 收盤後 15 分 (≈ 16:15 ET) | 盤後報告 | 動態結算實單與虛盤損益、Gamma 脆性防禦、計算 SPY Beta-Weighted 宏觀曝險，提出跨板塊相關性警告。同時執行背景快取清理維護資料庫效能。 |
| **每週五定時** (17:05 ET) | VTR 績效週報 | 收盤後彙總該週虛擬交易室 (VTR) 的勝率、總損益與盈虧比，發送專屬績效報表。 |

---

## 🛠 技術棧

| 層級 | 技術 |
|---|---|
| **語言** | Python 3.12 |
| **Discord** | `discord.py` ≥ 2.3 — 斜線指令、私訊路由、非同步訊息佇列 |
| **市場數據** | `finnhub-python`（即時報價、股息率與財報）、`yfinance`（選擇權鏈擷取）、`pandas-ta`（指標）、`py_vollib`（定價模型與 Greeks） |
| **數值計算** | `numpy`（對數報酬率、波動率）、`pandas`（數據處理） |
| **LLM 推論** | `openai` SDK（OpenAI-compatible API）、`pydantic`（Structured Output Schema） |
| **邊緣爬蟲** | `playwright`（Headless Chromium 渲染）、`beautifulsoup4` + `lxml`（HTML 解析）、`fastapi`（本地 API） |
| **網路** | `httpx`（非同步 HTTP 客戶端）、Cloudflare Tunnel（安全互連） |
| **排程** | `pandas_market_calendars`、`zoneinfo` |
| **資料庫** | SQLite — 以 `user_id` 為複合唯一鍵，內建版本遷移引擎（目前至 `v016`） |
| **基礎架構** | Docker、Docker Compose、GitHub Actions CI/CD → DigitalOcean |
| **套件管理** | `pyproject.toml`（PEP 621 標準） |

---

## 🚀 快速開始

### 前置需求

- [Docker](https://docs.docker.com/get-docker/) & [Docker Compose](https://docs.docker.com/compose/install/)
- 一組 [Discord Bot Token](https://discord.com/developers/applications)
- 一組 [Finnhub API Key](https://finnhub.io/)（必填：即時行情與基本面數據）
- （可選）OpenAI-compatible LLM 推論端點（用於 NLP 風控）
- （可選）Cloudflare Tunnel（用於 Reddit 邊緣爬蟲互連）

### 1. 複製並準備

```bash
git clone https://github.com/cosmo-chang-1701/nexus-seeker.git
cd nexus-seeker
mkdir -p nexus_core/data          # SQLite 持久化掛載目錄
cd nexus_core                     # 目前 docker-compose.yml 位於此目錄
```

### 2. 設定環境變數

```bash
cp .env.example .env
```

編輯 `.env` 並填入你的 Token：

```env
DISCORD_TOKEN=your_discord_bot_token_here
DISCORD_ADMIN_USER_ID=your_discord_admin_user_id_here

LLM_API_BASE=your_llm_api_base_here        # 可選：自架 Inference Server URL
LLM_MODEL_NAME=your_llm_model_name_here      # 可選：模型名稱
API_KEY=your_api_key_here                    # 可選：LLM API Key
TUNNEL_URL=your_tunnel_url_here              # 可選：Cloudflare Tunnel URL
FINNHUB_API_KEY=your_finnhub_api_key_here    # 必填：Finnhub 即時行情 API Key

LOG_LEVEL=WARNING          # 可選：DEBUG / INFO / WARNING (預設)
```

### 3. 啟動

```bash
docker compose up -d --build
```

確認 Bot 正在運行：

```bash
docker compose logs -f
```

如需啟動邊緣爬蟲服務（本地端）：

```bash
cd ../nexus_edge_scraper
cp .env.example .env
docker compose up -d --build
```

> **從 v1 升級？** 資料庫現已內建版本遷移引擎（Migration Engine），啟動時自動偵測並套用 Schema 變更，無須手動刪除舊資料庫。

---

## ⌨️ Discord 指令

所有指令使用 Discord 原生**斜線指令**，內建參數驗證。
回覆皆為**臨時訊息** — 僅觸發指令的使用者可見。

### 📡 觀察清單

| 指令 | 說明 | 範例 |
|---|---|---|
| `/add_watch` | 將標的加入觀察清單（支援 Covered Call 模式與 LLM 開關） | `symbol: TSLA` `stock_cost: 250.0` `use_llm: True` |
| `/list_watch` | 檢視所有觀察中的標的（含分頁瀏覽） | — |
| `/edit_watch` | 編輯標的設定（修改現股成本或切換 LLM 審查） | `symbol: TSLA` `stock_cost: 0` |
| `/remove_watch` | 移除標的 | `symbol: ONDS` |
| `/scan` | 手動執行量化掃描、What-if 模型模擬成交後曝險與防呆對沖建議 | `symbol: SMR` |

### 💼 投資組合

| 指令 | 說明 | 範例 |
|---|---|---|
| `/add_trade` | 記錄實際交易以進行監控 | 見下方 |
| `/list_trades` | 檢視持倉、損益與交易 ID | — |
| `/remove_trade` | 依 ID 移除已平倉的持倉 | `trade_id: 1` |
| `/settings` | 配置帳戶全域參數 (資金、風險與 PSQ/VTR 等個人化推播開關) | `capital: 50000 risk_limit: 15 enable_psq_watchlist: True` |
| `/vtr_list` | 列出虛擬交易室 (VTR) 開啟中的所有持倉 | — |
| `/vtr_stats` | 檢視虛擬交易室 (VTR) 的績效統計 (勝率、損益、盈虧比) | — |

### 🔬 研究

| 指令 | 說明 | 範例 |
|---|---|---|
| `/scan_news` | 快速掃描標的的 Finnhub 官方新聞 | `symbol: TSLA` `limit: 5` |
| `/scan_reddit` | 即時爬取標的的 Reddit 散戶情緒（過去 24 小時） | `symbol: PLTR` `limit: 5` |
| `/quote` | 透過 Finnhub 獲取即時報價（現價、漲跌幅、今日高低、前收盤） | `symbol: AAPL` |

### 🛠️ 管理員

| 指令 | 說明 |
|---|---|
| `/force_scan` | 立即手動執行全站掃描（不論開盤時間），結果私訊分發給所有使用者（繞過 4 小時冷卻機制）。僅限 `DISCORD_ADMIN_USER_ID` 使用。 |

### 🔧 開發者

| 指令 | 說明 |
|---|---|
| `/test_risk_ui` | 模擬高風險標的掃描資料，驗證 Beta、加權股數與風險 UI 渲染邏輯 |

<details>
<summary><strong><code>/add_trade</code> 參數</strong></summary>

| 參數 | 類型 | 說明 | 範例 |
|---|---|---|---|
| `symbol` | string | 股票代號 | `SOFI` |
| `opt_type` | choice | `Put` 或 `Call` | `Put` |
| `strike` | float | 履約價 | `7.5` |
| `expiry` | string | 到期日（`YYYY-MM-DD`） | `2026-04-17` |
| `entry_price` | float | 每口合約收取/支付的權利金 | `0.55` |
| `quantity` | int | 正值 = 買進，**負值 = 賣出** | `-5` |
| `stock_cost` | float | （可選）持有現股平均成本，用於 Covered Call 計算 | `250.0` |
| `category` | choice | （可選）`SPECULATIVE` 或 `HEDGE`，預設 `SPECULATIVE` | `HEDGE` |

</details>

---

## 🔄 投資組合工作流程

```
┌───────────────┐     ┌────────────────┐     ┌─────────────────┐
│  1. 訊號      │────►│  2. 記錄       │────►│  3. 監控        │
│  接收私訊     │     │  /add_trade    │     │  每日盤後自動   │
└───────────────┘     └────────────────┘     └────────┬────────┘
                                                      │
                                                      ▼
                                             ┌─────────────────┐
                                             │  4. 決策        │
                                             │  透過私訊警報   │
                                             └────────┬────────┘
                                                      │
              ┌───────────────┬───────────────┬───────┴───────┬───────────────┐
              │               │               │               │               │
       🟢 獲利 ≥ 50%  🚨 Delta 擴張    ⚠️ DTE ≤ 21      ⚫ 虧損 ≥ 150%  🌐 宏觀風險
       買回平倉        Roll Down/Up     迴避 Gamma       強制停損        SPY 避險
              │           and Out        陷阱 (轉倉)          │               │
              └───────────────┴───────────────┴───────────────┴───────────────┘
                                              │
                                              ▼
                                     ┌─────────────────┐
                                     │  5. 平倉        │
                                     │  /remove_trade  │
                                     └─────────────────┘
```

### 決策樹（賣方 vs 買方）

| 角色 | 條件 | 動作 |
|---|---|---|
| **賣方** | 獲利 ≥ 50% | ✅ Buy to Close 停利 |
| **賣方** | Put Delta ≤ −0.40 | 🚨 Roll Down and Out |
| **賣方** | Call Delta ≥ +0.40 | 🚨 Roll Up and Out |
| **賣方** | DTE ≤ 21 | ⚠️ 迴避 Gamma 陷阱，建議平倉或轉倉 |
| **賣方** | 虧損 ≥ 150% | ☠️ 黑天鵝警戒，強制停損 |
| **買方** | 獲利 ≥ 100% | ✅ Sell to Close 停利 |
| **買方** | DTE ≤ 21 | 🚨 動能衰竭，建議平倉保留殘值 |
| **買方** | 本金回撤 ≥ 50% | ⚠️ 停損警戒 |

---

## 📈 策略邏輯

量化引擎（`market_analysis/strategy.py`）以技術面篩選為門檻，並結合 `services/alert_filter.py` 的**動態降噪過濾**（多週期共振與防雙巴機制），經過疊加的**多道極端量化濾網**精煉出高勝率機會。

### 核心降噪過濾 (Alert Filter)

為解決頻繁警報帶來的疲勞，系統於推播發送前強制執行降噪判定：
- **動態趨勢濾網 (EMA 8/21)**：短天期合約若遭遇盤面強烈空頭，主動拒絕 BTO Call 與 STO Put 建倉。
- **多週期共振確認 (MTF Alignment)**：觸發 EMA 交叉時，強制要求大週期（日線 EMA 21）亦呈同向趨勢，方可視為有效突破。
- **Anti-Whipsaw 防雙巴機制**：針對連續 4 小時內之重複同向訊號，以及價格擺幅過小（< 1.5%）的無效糾纏，直接攔截降噪。
- **高勝率優先推播**：僅當 VIX 波動爆發（≥ 10%）、時框共振確認，或 VRP 溢酬高達 5% 以上，才打破靜默進行全域優先警報。

### 共用濾網管線

所有通過策略觸發與降噪條件的合約皆須依序通過以下濾網：

| # | 濾網 | 規則 | 適用策略 |
|---|---|---|---|
| 1 | HV Rank | 波動率位階 ≥ 30（一年內相對百分位）— 作為賣方門檻 | STO Put / STO Call |
| 2 | 個股 IV 期限結構 | 30D/60D IV 比率偵測逆價差（Backwardation ≥ 1.05） | 全部 |
| 3 | 大盤 VIX 期限結構 | VIX vs VIX3M 結構為逆價差 (VTS ≥ 1.0) → 觸發全域尾部防禦 (NRO 上限減半)、拒絕 STO Put | 全部 |
| 4 | VIX 動態 Z-Score | 偵測 Z30 > 0.5 且 Z60 > 0 (波動向北擴張) → 拒絕做多位階 (BTO Call / STO Put) | BTO Call / STO Put |
| 5 | 大盤 SPY 位階 | SPY 跌破 20MA 宣告空基調 → 拒絕做多位階 (BTO Call / STO Put) | BTO Call / STO Put |
| 6 | 垂直偏態 | 25Δ Put/Call IV 比率 ≥ 1.50 → 硬性否決 STO Put 或觸發 1/4 Kelly 尾部降規防護 | STO Put |
| 7 | 流動性 | Bid-Ask 絕對價差 > $0.20 **且**佔比 > 10% → 剔除 | 全部 |
| 8 | VRP（賣方） | 隱含波動率 < 歷史波動率（VRP < 0）→ 拒絕賣方 | STO Put / STO Call |
| 9 | VRP（買方） | VRP > 3%（保費遭恐慌暴拉）→ 拒絕買方建倉 | BTO Call / BTO Put |
| 10 | 隱含預期波動區間 | `現價 × IV × √(DTE/365)` 算出 1σ 預期波動幅度；STO Put 損益兩平 `(Strike − Bid)` 必須 ≤ 預期下緣，STO Call 損益兩平 `(Strike + Bid)` 必須 ≥ 預期上緣 — 落入圓錐內即剔除 | STO Put / STO Call |
| 11 | AROC（賣方） | 年化資本回報率 `(Bid / 保證金) × (365 / DTE)` < 15% → 剔除；保證金 = `Strike − Bid` | STO Put / STO Call |
| 12 | AROC（買方） | 年化資本回報率 `((預期波動 − Ask) / Ask) × (365 / DTE)` < 30% → 剔除 | BTO Call / BTO Put |
| 13 | ¼ Kelly（賣方） | 遭遇高尾部風險時，凱利最大倉位斬半。上限 5% (高危險 2.5%) | STO Put / STO Call |
| 14 | ¼ Kelly（買方） | 買方凱利倉位上限 3% | BTO Call / BTO Put |

### 🟢 賣出開倉 Put — *超賣收入*

- **觸發條件：** `RSI(14) < 35` + `HV Rank ≥ 30`
- **合約：** 30–45 DTE，Delta ≈ **−0.20**（約 80% OTM 機率）
- **篩選：** 垂直偏態 < 1.50、VRP > 0、流動性通過、損益兩平 ≤ 1σ 預期下緣、`AROC ≥ 15%`、¼ Kelly（上限 5%）

### 🔴 賣出開倉 Call — *超買收入*

- **觸發條件：** `RSI(14) > 65` + `HV Rank ≥ 30`
- **合約：** 30–45 DTE，Delta ≈ **+0.20**
- **篩選：** VRP > 0、流動性通過、損益兩平 ≥ 1σ 預期上緣、`AROC ≥ 15%`、¼ Kelly（上限 5%）

### 🚀 買入開倉 Call — *動能突破*

- **觸發條件：** 價格 > `20 SMA` + `50 ≤ RSI(14) ≤ 65` + `MACD 柱狀圖 > 0` + `HV Rank < 50`
- **合約：** 30–60 DTE，Delta ≈ **+0.50**（ATM）
- **篩選：** 流動性通過、VRP ≤ 3%、`AROC ≥ 30%`、¼ Kelly（上限 3%）
- **動態切換：** 若 HV Rank ≥ 50（高波動），自動切換為 **STO Put**（14–30 DTE，Delta −0.20）賺取高溢價

### ⚠️ 買入開倉 Put — *跌破 / 避險*

- **觸發條件：** 價格 < `20 SMA` + `35 ≤ RSI(14) ≤ 50` + `MACD 柱狀圖 < 0` + `HV Rank < 50`
- **合約：** 30–60 DTE，Delta ≈ **−0.50**（ATM）
- **篩選：** 流動性通過、VRP ≤ 3%、`AROC ≥ 30%`、¼ Kelly（上限 3%）
- **動態切換：** 若 HV Rank ≥ 50（高波動），自動切換為 **STO Call**（14–30 DTE，Delta +0.20）做空賺溢價

---

## 📁 專案結構

```
nexus-seeker/                        # Monorepo 根目錄
├── nexus_core/                      # 核心 Discord Bot 服務
│   ├── main.py                      # 進入點 — 初始化資料庫、註冊訊號處理、啟動 Bot
│   ├── bot.py                       # NexusBot 類別 — 擴充模組載入、啟停通知、非同步訊息佇列
│   ├── bot_healthy.py               # Docker HEALTHCHECK 探針 — 檢測心跳檔案存活狀態
│   ├── config.py                    # 環境變數 — Token、LLM 端點、Tunnel URL、策略 Delta 參數
│   ├── market_math.py               # Facade — 統一 re-export market_analysis 子模組
│   ├── market_time.py               # NYSE 日曆、動態睡眠排程器與開盤狀態偵測
│   ├── entrypoint.sh                # Docker entrypoint — 權限修正與 gosu 降權啟動
│   ├── market_analysis/             # 核心量化引擎 (Python Package)
│   │   ├── __init__.py              # 公開 API 匯出
│   │   ├── data.py                  # 財報日期查詢與選擇權價格獲取 (Finnhub & yfinance)
│   │   ├── margin.py                # 投資組合保證金耗能核算模組
│   │   ├── greeks.py                # Black-Scholes-Merton Delta 與 Greeks 計算引擎
│   │   ├── hedging.py               # 投資組合避險邏輯、Delta 中性計算與市場位階感知對沖
│   │   ├── ghost_trader.py          # GhostTrader — VTR 自動建倉、平倉、轉倉核心邏輯
│   │   ├── psq_engine.py            # PowerSqueeze 引擎 — BB/KC 壓縮偵測、動能線性回歸與擠壓釋放訊號
│   │   ├── risk_engine.py           # NRO 投資組合防禦管線、What-if 新增風險模擬與宏觀修正矩陣
│   │   ├── report_formatter.py      # 將量化數值格式化為 Discord Embed 文字流
│   │   ├── strategy.py              # 技術面掃描 + 多道量化濾網管線 + NRO 合約篩選
│   │   └── portfolio.py             # 盤後結算引擎流程編排 (Orchestrator)、宏觀風險評估
│   ├── database/                    # SQLite 資料庫層 (Python Package)
│   │   ├── __init__.py              # 統一匯出所有 CRUD 函數
│   │   ├── core.py                  # 版本遷移引擎 (Migration Engine) — 自動掃描 & 套用 Schema 變更
│   │   ├── portfolio.py             # 投資組合 CRUD
│   │   ├── watchlist.py             # 觀察清單 CRUD
│   │   ├── user_settings.py         # 使用者設定 CRUD (資金、風險上限、偏好開關、UserContext 匯總)
│   │   ├── virtual_trading.py       # 虛擬交易室 (VTR) 歷史與即時數據 CRUD
│   │   ├── financials.py            # 財務指標快取 CRUD (TTL 過期清理)
│   │   ├── cache.py                 # 快取模組 re-export Facade
│   │   └── migrations/              # 版本遷移腳本目錄
│   │       ├── v001_init.py         # 初始 Schema — portfolio、watchlist、user_settings
│   │       ├── v002_add_stock_cost.py  # 新增現股成本欄位
│   │       ├── v003_remove_is_covered.py  # 移除 is_covered 欄位
│   │       ├── v004_add_use_llm.py  # 新增 LLM 開關欄位
│   │       ├── v005_virtual_trading.py  # 建立 virtual_trades 表 (VTR)
│   │       ├── v006_update_watchlist_llm_default.py  # 將 watchlist.use_llm 預設值改為啟用
│   │       ├── v007_add_risk_limit.py  # 新增使用者風險限制欄位 (risk_limit_pct)
│   │       ├── v008_add_greeks_to_trades.py  # 為 portfolio/virtual_trades 新增 Greeks 欄位
│   │       ├── v009_add_cross_tracking.py  # 新增 EMA CROSSOVER 追蹤狀態
│   │       ├── v010_add_rehedge_tracking.py  # 新增自動回補警示鎖定欄位
│   │       ├── v011_add_trade_category.py  # 新增部位類別 (SPECULATIVE/HEDGE)
│   │       ├── v012_add_self_tuning_hedge.py  # 新增 STHE 動態 Tau 欄位
│   │       ├── v013_add_financials_cache.py  # 新增財報/財務快取表
│   │       ├── v014_financials_cache_schema_compat.py  # financials_cache schema 相容修補
│   │       ├── v015_add_vix_metrics.py  # 新增 VIX 結構與波動率動能指標狀態表
│   │       └── v016_add_preference_flags.py  # 新增使用者偏好開關 (Option/VTR/PSQ)
│   ├── services/                    # 外部服務整合與業務邏輯層
│   │   ├── alert_filter.py          # AlertFilter — 動態降噪過濾、多週期共振與防雙巴機制
│   │   ├── trading_service.py       # TradingService — 集中式業務邏輯 (掃描、VTR、盤後報告)
│   │   ├── market_data_service.py   # Finnhub 報價服務 (含 SMA/EMA 快取、Rate Limiting)
│   │   ├── llm_service.py           # LLM NLP 風控審查 — Structured Output (Pydantic Schema)
│   │   ├── news_service.py          # Finnhub 歷史 / 突發新聞擷取
│   │   └── reddit_service.py        # Reddit 情緒 — 透過 Cloudflare Tunnel 呼叫本地爬蟲
│   ├── cogs/                        # Discord 擴充模組 (Cog 分層)
│   │   ├── trading.py               # 背景排程任務 — 盤前風控、盤中掃描、VTR 監控、盤後結算與每週週報
│   │   ├── watchlist.py             # 觀察清單斜線指令 — CRUD + NRO 手動掃描與 What-if 展示
│   │   ├── portfolio.py             # 投資組合與 VTR 斜線指令 — 實單與虛擬交易追蹤、資金與風險設定
│   │   ├── research.py              # 研究斜線指令 — 新聞掃描、Reddit 情緒掃描、即時報價查詢
│   │   ├── debug.py                 # 開發者除錯與風險 UI 視覺驗證工具
│   │   └── embed_builder.py         # Discord UI/UX 生成器 — 渲染圖文並茂的量化戰情面板
│   ├── ui/                          # Discord UI 元件
│   │   └── watchlist.py             # 觀察清單分頁瀏覽 (Pagination View)
│   ├── tests/                       # 測試套件
│   │   ├── test_embed_builder.py    # Embed 生成器單元測試
│   │   ├── integration/             # 整合測試 (資料庫、交易流程、LLM/Risk)
│   │   │   ├── test_integration_database_and_greeks.py
│   │   │   ├── test_integration_trading_flows.py
│   │   │   └── test_integration_llm_and_risk.py
│   │   └── unit/                    # 單元測試
│   │       ├── test_market_data_service.py  # MarketDataService 單元測試
│   │       ├── test_market_data_vix306.py   # VIX 306 期限結構測試
│   │       ├── test_risk_engine_vix306.py   # VIX 風險引擎單元測試
│   │       └── test_scheduler_reschedule.py # 排程器動態重排測試
│   ├── data/                        # SQLite 資料庫 (Docker Volume 掛載)
│   ├── .dockerignore
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── pyproject.toml               # PEP 621 套件定義與依賴管理
│   └── .env.example
│
├── nexus_edge_scraper/              # 邊緣爬蟲服務 (本地端獨立部署)
│   ├── local_api.py                 # FastAPI + Playwright — Reddit 頁面渲染與結構化爬取
│   ├── .dockerignore
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── pyproject.toml               # PEP 621 套件定義與依賴管理
│   └── .env.example
│
├── assets/
│   └── hero.png                     # README Hero Image
├── .github/
│   └── workflows/
│       └── deploy.yml               # CI/CD — 建構 → GHCR → DigitalOcean Swarm
├── GEMINI.md                        # AI Agent 開發上下文規約
├── .gitignore
├── README.md                        # ← 本文件
└── LICENSE
```

---

## 🧪 測試

測試架構分為 **整合測試** 與 **單元測試** 兩層，全數於 Docker 容器內執行以確保環境一致性。

### 整合測試 (`tests/integration/`)

- `test_integration_database_and_greeks.py` — 資料庫遷移與 Greeks 持久化驗證
- `test_integration_trading_flows.py` — 完整交易流程（建倉→監控→平倉）端到端驗證
- `test_integration_llm_and_risk.py` — LLM 風控審查與 NRO 風險引擎整合驗證

### 單元測試 (`tests/unit/`)

- `test_market_data_service.py` — MarketDataService 報價與快取邏輯
- `test_market_data_vix306.py` — VIX 期限結構 (VTS) 與 Z-Score 計算
- `test_risk_engine_vix306.py` — VIX 306 風險引擎防禦管線觸發條件
- `test_scheduler_reschedule.py` — NYSE 排程器動態重排與邊界條件

### 其他測試

- `test_embed_builder.py` — Discord Embed 報告生成器欄位截斷與格式化

### 使用 Docker 執行單一測試檔

```bash
# 在 nexus_core 目錄執行
docker compose run --rm -v "$(pwd):/app" nexus_seeker python -m unittest tests.integration.test_integration_database_and_greeks
docker compose run --rm -v "$(pwd):/app" nexus_seeker python -m unittest tests.unit.test_risk_engine_vix306
```

### 一次執行全部測試

```bash
# 在 nexus_core 目錄執行
docker compose run --rm -v "$(pwd):/app" nexus_seeker python -m unittest \
       tests.integration.test_integration_database_and_greeks \
       tests.integration.test_integration_trading_flows \
       tests.integration.test_integration_llm_and_risk \
       tests.unit.test_market_data_service \
       tests.unit.test_market_data_vix306 \
       tests.unit.test_risk_engine_vix306 \
       tests.unit.test_scheduler_reschedule \
       tests.test_embed_builder
```

### 使用 discover 執行整個 tests 目錄

```bash
# 在 nexus_core 目錄執行
docker compose run --rm -v "$(pwd):/app" nexus_seeker python -m unittest discover -s tests -v
```

> 備註：若看到 migration 警告如 `V3 no such column: is_covered`、`V14 duplicate column name: data`，這是遷移相容性保護機制的容錯訊息（已標記為可繼續），不會阻斷測試流程。

---

## 🤝 貢獻

1. **Fork** 此儲存庫
2. 建立功能分支：`git checkout -b feat/awesome-feature`
3. 提交變更：`git commit -m "feat: add awesome feature"`
4. 推送至分支：`git push origin feat/awesome-feature`
5. 開啟一個 **Pull Request**

提交訊息請遵循 [Conventional Commits](https://www.conventionalcommits.org/) 規範。

---

## 🔮 路線圖

- [x] **LLM NLP 風控** — 整合 OpenAI-compatible 推論引擎，Structured Output 自動審查新聞毒性與散戶情緒。
- [x] **Reddit 邊緣爬蟲** — 獨立 `nexus_edge_scraper` 服務，透過 Cloudflare Tunnel 安全互連。
- [x] **資料庫遷移引擎** — 自動版本控管與 Schema 遷移，告別手動重建資料庫。
- [x] **Nexus Risk Optimizer (NRO)** — What-if 建倉模擬與部位重組，自動計算 SPY 避險對沖口數。
- [x] **Finnhub 行情升級** — 告別 yfinance 頻繁 404，全改接穩定金融級 API，含即時報價、股息率與財報日程。
- [x] **虛擬交易室 (VTR)** — 內建 GhostTrader，支援策略自動回測與實時虛盤模擬紀錄，並提供每週績效週報。
- [x] **Greeks 持久化** — 持倉 Greeks (Delta/Theta/Gamma) 寫入資料庫，UserContext 一站式匯總真實與虛擬交易指標。
- [x] **個人化風險上限** — 使用者可透過 `/settings` 自訂風險限制 (1%–50%)，NRO 動態調控。
- [x] **即時報價指令** — `/quote` 透過 Finnhub 即時查詢標的報價。
- [x] **Service Layer 重構** — `TradingService` 將 Discord UI 與業務邏輯徹底解耦。
- [x] **VIX 領域分析 (VIX306)** — 結合 VTS 期限結構與 30/60 日 Z-Score，偵測股市黑天鵝前兆與波動率擴張軌跡，動態觸發 1/4 Kelly 自動降規。
- [x] **PowerSqueeze 模組 (PSQ)** — 雙路徑解耦量化掃描，獨立於 Option 訊號提供基於 Squeeze 能量突破的即時戰情，支援 `/settings` 獨立開關。
- [ ] **MCP Server** — 將核心量化模組封裝為標準 Model Context Protocol 工具，供外部 AI 代理使用。
- [ ] **券商 API 整合** — Interactive Brokers Gateway 實現全自動下單執行（訊號 → 執行 → 平倉，零人工介入）。

---

## 📄 授權條款

本專案採用 [MIT 授權條款](LICENSE)。

---

<div align="center">

*由 [Cosmo Chang](https://github.com/cosmo-chang-1701) 以 ❤️ 打造，追求量化自由。*

</div>