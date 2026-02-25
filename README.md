# 🌌 Nexus Seeker

**多租戶選擇權量化交易助手 — 由 Discord 驅動**

[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
[![Deploy](https://github.com/cosmo-chang-1701/nexus-seeker/actions/workflows/deploy.yml/badge.svg)](https://github.com/cosmo-chang-1701/nexus-seeker/actions/workflows/deploy.yml)
[![Architecture](https://img.shields.io/badge/architecture-multi--tenant-purple.svg)](#architecture)

> 一個以 Python 與 Docker 建構的**多租戶選擇權量化助手**。
> 結合技術分析、**Black-Scholes-Merton** 定價模型（含股息率校正）、LLM NLP 風控審查與全自動化 NYSE 交易日曆，協助交易者執行高勝率的選擇權賣方策略（The Wheel / 信用價差）。

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
| 📰 **新聞聚合** | 透過 Yahoo Finance API 即時擷取標的近期官方新聞標題，作為 LLM 風控審查的輸入源。 |
| 🎯 **Delta 精準掃描** | 內建 Black-Scholes-Merton 引擎（`py_vollib`，含股息率 `q` 校正）自動計算目標 Delta 的最佳履約價（例：−0.20 ≈ 80% 勝率）。 |
| 📡 **NYSE 自動排程器** | 整合 `pandas_market_calendars` 並處理日光節約時間與假日 — 動態睡眠至下一個交易日目標時刻。 |
| 🔄 **30 分鐘動態巡邏** | 盤中掃描器以 30 分鐘心跳循環運作，僅在 NYSE 常規交易時段（10:00 ET 後）執行掃描，避開開盤初期造市商無報價期。 |
| 🧊 **4 小時推播冷卻** | 自動排程掃描結果依「使用者 × 標的」維度套用 4 小時冷卻機制，避免重複推播同一訊號；手動 `/force_scan` 不受冷卻限制且不重置計時器。 |
| 📊 **造市商預期波動** | 計算基於 ATM 跨式組合的預期波動（MMM），在財報前標示「地雷區」履約價。 |
| ⚖️ **四分之一 Kelly 倉位** | 以 ¼-Kelly 準則計算倉位大小，每檔標的上限 5%。 |
| 📈 **IV 期限結構** | 偵測 30D/60D IV 逆價差作為恐慌性拋售訊號。 |
| 📐 **垂直偏態濾網** | 分析 25-Delta Put/Call IV 比率，≥ 1.30 標示警告，≥ 1.50 時硬性否決 STO Put 訊號，規避尾部崩盤風險。 |
| 💧 **流動性濾網** | 自動檢測買賣價差（Bid-Ask Spread），絕對價差 > $0.20 且佔比 > 10% 時剔除流動性陷阱。 |
| 🧪 **波動率風險溢酬 (VRP)** | 比較隱含波動率與歷史波動率，當 VRP < 0（IV 被低估）時拒絕賣方策略，確保風險溢酬為正。 |
| 🎯 **隱含預期波動區間** | 以 `現價 × IV × √(DTE/365)` 計算 1σ 預期波動幅度，確認賣方損益兩平點建構於機率圓錐外，否則硬性剔除。 |
| 💰 **AROC 資金效率濾網** | 計算賣方合約的年化資本回報率 `(權利金 / 保證金) × (365 / DTE)`，低於 15% 的標的自動剔除，僅保留資金效率達標的收租機會。 |
| 🌐 **Beta 加權宏觀風險** | 盤後報告計算投資組合等效 SPY Delta（Beta-Weighted），當淨曝險超過 ±50 股時觸發避險建議。 |
| 📉 **Gamma 脆性評估** | 以二階 Beta-Weighted 平方加權追蹤投資組合淨 Gamma，偵測非線性加速度風險；淨 Gamma < −20 時觸發脆性警告，建議注入正 Gamma 緩衝。 |
| 🔥 **資金熱度極限** | 計算投資組合保證金佔總資金比例（Portfolio Heat），> 30% 警戒、> 50% 爆倉預警，防止過度槓桿。 |
| 💹 **Theta 現金流精算** | 每日 Theta 收益率精算，對照機構級 0.05%–0.3% 標準，確保時間價值曝險合理。 |
| 🕸️ **相關性矩陣風險** | 下載 60 日收盤價建立 Pearson 相關係數矩陣，偵測 ρ > 0.75 的高度重疊板塊並提示集中風險。 |
| 💾 **資料持久化** | SQLite 搭配 Docker Volume — 容器重啟零資料遺失。內建版本遷移引擎（Migration Engine），Schema 變更全自動化。 |

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
                     │    database/    │          │  market_time  │ ← NYSE 日曆
                     │  (SQLite PKG)   │          │  (動態排程)   │
                     │ ┌─────────────┐ │          └───────────────┘
                     │ │ migrations/ │ │                  │
                     │ └─────────────┘ │          ┌───────▼───────┐
                     └────────────────┘          │  market_math  │ ← Facade
                                                  │  (re-export)  │
                              │                   └───────┬───────┘
                              │                           │
                     ┌────────▼────────┐        ┌─────────▼─────────┐
                     │   services/     │        │  market_analysis  │
                     │ ┌─────────────┐ │        │  (Python Package) │
                     │ │ llm_service │ │        ├───────────────────┤
                     │ │ news_service│ │        │  strategy.py      │
                     │ │reddit_serv. │─│─ ─ ─ ► │  portfolio.py     │
                     │ └─────────────┘ │  feed  │  greeks.py        │
                     └────────────────┘        │  data.py          │
                              │                 └───────────────────┘
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
| **動態睡眠** → 開盤前 30 分 (≈ 09:00 ET) | 盤前風險監控 | 掃描持倉與觀察清單的財報日曆；若財報 ≤ 3 天內，私訊 ⚠️ IV 崩跌警報（區分持倉高風險 vs 觀察清單標的）。 |
| **每 30 分鐘心跳** (10:00 ET – 收盤) | 盤中動態掃描 | 每 30 分鐘偵測開盤狀態，僅在常規交易時段內執行：跳過非交易日/盤前盤後/開盤初期造市商無報價期 (09:30–09:59)。對每位使用者的觀察清單執行全方位掃描（含 LLM 風控審查）；訊號推播套用 **4 小時冷卻機制**（同一使用者 × 同一標的在冷卻期內不重複推播）。 |
| **動態睡眠** → 收盤後 15 分 (≈ 16:15 ET) | 盤後報告 | 動態結算損益、Delta 擴張轉倉建議、Gamma 脆性防禦；附帶 SPY Beta-Weighted 宏觀風險評估、Theta 收益率、資金熱度極限與 Pearson 相關性矩陣。 |

---

## 🛠 技術棧

| 層級 | 技術 |
|---|---|
| **語言** | Python 3.12 |
| **Discord** | `discord.py` ≥ 2.3 — 斜線指令、私訊路由、非同步訊息佇列 |
| **市場數據** | `yfinance`（報價 + 新聞）、`pandas-ta`（指標）、`py_vollib`（Black-Scholes-Merton + Greeks） |
| **數值計算** | `numpy`（對數報酬率、波動率）、`pandas`（數據處理） |
| **LLM 推論** | `openai` SDK（OpenAI-compatible API）、`pydantic`（Structured Output Schema） |
| **邊緣爬蟲** | `playwright`（Headless Chromium 渲染）、`beautifulsoup4` + `lxml`（HTML 解析）、`fastapi`（本地 API） |
| **網路** | `httpx`（非同步 HTTP 客戶端）、Cloudflare Tunnel（安全互連） |
| **排程** | `pandas_market_calendars`、`zoneinfo` |
| **資料庫** | SQLite — 以 `user_id` 為複合唯一鍵，內建版本遷移引擎 |
| **基礎架構** | Docker、Docker Compose、GitHub Actions CI/CD → DigitalOcean |

---

## 🚀 快速開始

### 前置需求

- [Docker](https://docs.docker.com/get-docker/) & [Docker Compose](https://docs.docker.com/compose/install/)
- 一組 [Discord Bot Token](https://discord.com/developers/applications)
- （可選）OpenAI-compatible LLM 推論端點（用於 NLP 風控）
- （可選）Cloudflare Tunnel（用於 Reddit 邊緣爬蟲互連）

### 1. 複製並準備

```bash
git clone https://github.com/cosmo-chang-1701/nexus-seeker.git
cd nexus-seeker
mkdir -p nexus_core/data          # SQLite 持久化掛載目錄
```

### 2. 設定環境變數

```bash
cp nexus_core/.env.example nexus_core/.env
```

編輯 `nexus_core/.env` 並填入你的 Token：

```env
DISCORD_TOKEN=your_discord_bot_token_here
DISCORD_ADMIN_USER_ID=your_discord_admin_user_id_here

LLM_API_BASE=your_llm_api_base_here        # 可選：自架 Inference Server URL
LLM_MODEL_NAME=your_llm_model_name_here      # 可選：模型名稱
API_KEY=your_api_key_here                    # 可選：LLM API Key
TUNNEL_URL=your_tunnel_url_here              # 可選：Cloudflare Tunnel URL

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
| `/scan` | 手動對標的執行 Delta 中性掃描（含 LLM 風控審查） | `symbol: SMR` |

### 💼 投資組合

| 指令 | 說明 | 範例 |
|---|---|---|
| `/add_trade` | 記錄實際交易以進行監控 | 見下方 |
| `/list_trades` | 檢視持倉、損益與交易 ID | — |
| `/remove_trade` | 依 ID 移除已平倉的持倉 | `trade_id: 1` |
| `/set_capital` | 設定總資金以供 Kelly 倉位計算 | `capital: 50000` |

### 🔬 研究

| 指令 | 說明 | 範例 |
|---|---|---|
| `/scan_news` | 快速掃描標的的 Yahoo Finance 官方新聞 | `symbol: TSLA` `limit: 5` |
| `/scan_reddit` | 即時爬取標的的 Reddit 散戶情緒（過去 24 小時） | `symbol: PLTR` `limit: 5` |

### 🛠️ 管理員

| 指令 | 說明 |
|---|---|
| `/force_scan` | 立即手動執行全站掃描（不論開盤時間），結果私訊分發給所有使用者（繞過 4 小時冷卻機制） |

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

量化引擎（`market_analysis/strategy.py`）以技術面篩選為門檻，並依 HV Rank 動態切換買賣方角色，經過**多道量化濾網**精煉。

### 共用濾網管線

所有通過策略觸發條件的合約皆須依序通過以下濾網：

| # | 濾網 | 規則 | 適用策略 |
|---|---|---|---|
| 1 | HV Rank | 波動率位階 ≥ 30（一年內相對百分位）— 作為賣方門檻 | STO Put / STO Call |
| 2 | IV 期限結構 | 30D/60D IV 比率偵測逆價差（Backwardation ≥ 1.05） | 全部 |
| 3 | 垂直偏態 | 25Δ Put/Call IV 比率 ≥ 1.50 → 硬性否決 STO Put | STO Put |
| 4 | 流動性 | Bid-Ask 絕對價差 > $0.20 **且**佔比 > 10% → 剔除 | 全部 |
| 5 | VRP（賣方） | 隱含波動率 < 歷史波動率（VRP < 0）→ 拒絕賣方 | STO Put / STO Call |
| 6 | VRP（買方） | VRP > 3%（保費遭恐慌暴拉）→ 拒絕買方建倉 | BTO Call / BTO Put |
| 7 | 隱含預期波動區間 | `現價 × IV × √(DTE/365)` 算出 1σ 預期波動幅度；STO Put 損益兩平 `(Strike − Bid)` 必須 ≤ 預期下緣，STO Call 損益兩平 `(Strike + Bid)` 必須 ≥ 預期上緣 — 落入圓錐內即剔除 | STO Put / STO Call |
| 8 | AROC（賣方） | 年化資本回報率 `(Bid / 保證金) × (365 / DTE)` < 15% → 剔除；保證金 = `Strike − Bid` | STO Put / STO Call |
| 9 | AROC（買方） | 年化資本回報率 `((預期波動 − Ask) / Ask) × (365 / DTE)` < 30% → 剔除 | BTO Call / BTO Put |
| 10 | ¼ Kelly（賣方） | 凱利倉位上限 **5%**，單口保證金 = 履約價 − 權利金 | STO Put / STO Call |
| 11 | ¼ Kelly（買方） | 凱利倉位上限 **3%**（更保守），單口成本 = 權利金 × 100 | BTO Call / BTO Put |

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
- **合約：** 14–30 DTE，Delta ≈ **+0.50**（ATM）
- **篩選：** 流動性通過、VRP ≤ 3%、`AROC ≥ 30%`、¼ Kelly（上限 3%）
- **動態切換：** 若 HV Rank ≥ 50（高波動），自動切換為 **STO Put**（30–45 DTE，Delta −0.20）賺取高溢價

### ⚠️ 買入開倉 Put — *跌破 / 避險*

- **觸發條件：** 價格 < `20 SMA` + `35 ≤ RSI(14) ≤ 50` + `MACD 柱狀圖 < 0` + `HV Rank < 50`
- **合約：** 14–30 DTE，Delta ≈ **−0.50**（ATM）
- **篩選：** 流動性通過、VRP ≤ 3%、`AROC ≥ 30%`、¼ Kelly（上限 3%）
- **動態切換：** 若 HV Rank ≥ 50（高波動），自動切換為 **STO Call**（30–45 DTE，Delta +0.20）做空賺溢價

---

## 📁 專案結構

```
nexus-seeker/                        # Monorepo 根目錄
├── nexus_core/                      # 核心 Discord Bot 服務
│   ├── main.py                      # 進入點 — 初始化資料庫、註冊訊號處理、啟動 Bot
│   ├── bot.py                       # NexusBot 類別 — 擴充模組載入、啟停通知、非同步訊息佇列
│   ├── config.py                    # 環境變數 — Token、LLM 端點、Tunnel URL、策略 Delta 參數
│   ├── market_math.py               # Facade — 統一 re-export market_analysis 子模組
│   ├── market_time.py               # NYSE 日曆、動態睡眠排程器與開盤狀態偵測
│   ├── market_analysis/             # 核心量化引擎 (Python Package)
│   │   ├── __init__.py              # 公開 API 匯出
│   │   ├── data.py                  # 財報日期查詢 (yfinance)
│   │   ├── greeks.py                # Black-Scholes-Merton Delta 計算 (含股息率 q)
│   │   ├── strategy.py              # 技術面掃描 + 多道量化濾網管線 + 合約篩選
│   │   └── portfolio.py             # 盤後結算引擎、防禦決策樹、宏觀風險評估、相關性矩陣
│   ├── database/                    # SQLite 資料庫層 (Python Package)
│   │   ├── __init__.py              # 統一匯出所有 CRUD 函數
│   │   ├── core.py                  # 版本遷移引擎 (Migration Engine) — 自動掃描 & 套用 Schema 變更
│   │   ├── portfolio.py             # 投資組合 CRUD
│   │   ├── watchlist.py             # 觀察清單 CRUD
│   │   ├── user_settings.py         # 使用者設定 CRUD (資金規模、跨表 User ID 聯集)
│   │   └── migrations/              # 版本遷移腳本目錄
│   │       ├── v001_init.py         # 初始 Schema — portfolio、watchlist、user_settings
│   │       ├── v002_add_stock_cost  # 新增現股成本欄位
│   │       ├── v003_remove_is_covered  # 移除 is_covered 欄位
│   │       └── v004_add_use_llm.py  # 新增 LLM 開關欄位
│   ├── services/                    # 外部服務整合層
│   │   ├── llm_service.py           # LLM NLP 風控審查 — Structured Output (Pydantic Schema)
│   │   ├── news_service.py          # Yahoo Finance 新聞擷取
│   │   └── reddit_service.py        # Reddit 情緒 — 透過 Cloudflare Tunnel 呼叫本地爬蟲
│   ├── cogs/                        # Discord 擴充模組 (Cog 分層)
│   │   ├── trading.py               # 背景排程任務 — 盤前風控、盤中巡邏、盤後報告、冷卻機制
│   │   ├── watchlist.py             # 觀察清單斜線指令 — CRUD + 手動掃描
│   │   ├── portfolio.py             # 投資組合斜線指令 — 新增/列出/移除持倉、設定資金
│   │   ├── research.py              # 研究斜線指令 — 新聞掃描、Reddit 情緒掃描
│   │   └── embed_builder.py         # Discord Embed 建構工廠 — 掃描結果、新聞、Reddit、觀察清單
│   ├── ui/                          # Discord UI 元件
│   │   └── watchlist.py             # 觀察清單分頁瀏覽 (Pagination View)
│   ├── tests/                       # 測試套件
│   │   ├── test_strategy.py         # 策略模組單元測試
│   │   ├── test_portfolio.py        # 盤後結算引擎單元測試
│   │   ├── test_market_time.py      # 排程時間計算測試
│   │   ├── test_database.py         # 資料庫 CRUD 測試
│   │   ├── test_news_service.py     # 新聞服務單元測試
│   │   ├── test_send_embed.py       # Embed 建構測試
│   │   ├── test_check_portfolio_status.py        # 持倉狀態檢查測試
│   │   ├── test_dynamic_after_market_report.py   # 盤後報告整合測試
│   │   ├── test_four_scenarios.py                # 四大策略場景測試
│   │   ├── test_pre_market_risk_monitor.py       # 盤前風控監控測試
│   │   └── verify_market_functions.py            # 量化函數整合驗證
│   ├── data/                        # SQLite 資料庫 (Docker Volume 掛載)
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   └── .env.example
│
├── nexus_edge_scraper/              # 邊緣爬蟲服務 (本地端獨立部署)
│   ├── local_api.py                 # FastAPI + Playwright — Reddit 頁面渲染與結構化爬取
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   └── .env.example
│
├── .github/
│   └── workflows/
│       └── deploy.yml               # CI/CD — 建構 → GHCR → DigitalOcean Swarm
├── .gitignore
├── README.md                        # ← 本文件
└── LICENSE
```

---

## 🧪 測試

測試使用 Python `unittest` 框架，在 Docker 容器中執行：

```bash
# 執行單一測試模組
docker compose run --rm -v "$(pwd):/app" nexus_seeker python -m unittest tests.test_strategy

# 執行所有測試
docker compose run --rm -v "$(pwd):/app" nexus_seeker python -m unittest discover -s tests -v
```

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
- [ ] **MCP Server** — 將核心量化模組封裝為標準 Model Context Protocol 工具，供外部 AI 代理使用。
- [ ] **券商 API 整合** — Interactive Brokers Gateway 實現全自動下單執行（訊號 → 執行 → 平倉，零人工介入）。

---

## 📄 授權條款

本專案採用 [MIT 授權條款](LICENSE)。

---

<div align="center">

*由 [Cosmo Chang](https://github.com/cosmo-chang-1701) 以 ❤️ 打造，追求量化自由。*

</div>