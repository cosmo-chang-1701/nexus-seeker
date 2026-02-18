# 🌌 Nexus Seeker

**多租戶選擇權量化交易助手 — 由 Discord 驅動**

[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
[![Deploy](https://github.com/cosmo-chang-1701/nexus-seeker/actions/workflows/deploy.yml/badge.svg)](https://github.com/cosmo-chang-1701/nexus-seeker/actions/workflows/deploy.yml)
[![Architecture](https://img.shields.io/badge/architecture-multi--tenant-purple.svg)](#architecture)

> 一個以 Python 與 Docker 建構的**多租戶選擇權量化助手**。
> 結合技術分析、**Black-Scholes-Merton** 定價模型（含股息率校正）與全自動化 NYSE 交易日曆，協助交易者執行高勝率的選擇權賣方策略（The Wheel / 信用價差）。

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
| 📨 **私訊分發器** | 背景排程器對所有使用者執行 **API 去重**，再將個人化的量化報告發送至各使用者的私訊。 |
| 🔔 **啟停通知** | Bot 啟動與關閉時自動私訊通知所有已註冊使用者，確保服務可視性。 |
| 🎯 **Delta 精準掃描** | 內建 Black-Scholes-Merton 引擎（`py_vollib`，含股息率 `q` 校正）自動計算目標 Delta 的最佳履約價（例：−0.20 ≈ 80% 勝率）。 |
| 📡 **NYSE 自動排程器** | 整合 `pandas_market_calendars` 並處理日光節約時間與假日 — 每日三次觸發：09:00 / 09:45 / 16:15 ET。 |
| 📊 **造市商預期波動** | 計算基於 ATM 跨式組合的預期波動（MMM），在財報前標示「地雷區」履約價。 |
| ⚖️ **四分之一 Kelly 倉位** | 以 ¼-Kelly 準則計算倉位大小，每檔標的上限 5%。 |
| 📈 **IV 期限結構** | 偵測 30D/60D IV 逆價差作為恐慌性拋售訊號。 |
| 📐 **垂直偏態濾網** | 分析 25-Delta Put/Call IV 比率，≥ 1.30 標示警告，≥ 1.50 時硬性否決 STO Put 訊號，規避尾部崩盤風險。 |
| 💧 **流動性濾網** | 自動檢測買賣價差（Bid-Ask Spread），絕對價差 > $0.20 且佔比 > 10% 時剔除流動性陷阱。 |
| 🧪 **波動率風險溢酬 (VRP)** | 比較隱含波動率與歷史波動率，當 VRP < 0（IV 被低估）時拒絕賣方策略，確保風險溢酬為正。 |
| 🎯 **隱含預期波動區間** | 以 `現價 × IV × √(DTE/365)` 計算 1σ 預期波動幅度，確認賣方損益兩平點建構於機率圓錐外，否則硬性剔除。 |
| 💰 **AROC 資金效率濾網** | 計算賣方合約的年化資本回報率 `(權利金 / 保證金) × (365 / DTE)`，低於 15% 的標的自動剔除，僅保留資金效率達標的收租機會。 |
| 🌐 **Beta 加權宏觀風險** | 盤後報告計算投資組合等效 SPY Delta（Beta-Weighted），當淨曝險超過 ±50 股時觸發避險建議。 |
| � **Gamma 脆性評估** | 以二階 Beta-Weighted 平方加權追蹤投資組合淨 Gamma，偵測非線性加速度風險；淨 Gamma < −20 時觸發脆性警告，建議注入正 Gamma 緩衝。 |
| 🔥 **資金熱度極限** | 計算投資組合保證金佔總資金比例（Portfolio Heat），> 30% 警戒、> 50% 爆倉預警，防止過度槓桿。 |
| 💹 **Theta 現金流精算** | 每日 Theta 收益率精算，對照機構級 0.05%–0.3% 標準，確保時間價值曝險合理。 |
| �🕸️ **相關性矩陣風險** | 下載 60 日收盤價建立 Pearson 相關係數矩陣，偵測 ρ > 0.75 的高度重疊板塊並提示集中風險。 |
| 💾 **資料持久化** | SQLite 搭配 Docker Volume — 容器重啟零資料遺失。 |

---

## 🏗 架構

```
Discord 使用者 ──► Discord API ──► NexusBot (bot.py)
                                       │
                     ┌─────────────────┼──────────────────┐
                     │                 │                  │
              斜線指令           私訊分發器          NYSE 排程器
              (臨時訊息)        (背景執行)         (每日三次任務)
                     │                 │                  │
                     └────────┬────────┘                  │
                              │                           │
                     ┌────────▼────────┐          ┌───────▼───────┐
                     │    database     │          │  market_math  │ ← Facade
                     │    (SQLite)     │          │  (re-export)  │
                     └────────────────┘          └───────┬───────┘
                                                         │
                                               ┌─────────▼─────────┐
                                               │  market_analysis  │
                                               │  (Python Package) │
                                               ├───────────────────┤
                                               │  strategy.py      │
                                               │  portfolio.py     │
                                               │  greeks.py        │
                                               │  data.py          │
                                               └───────────────────┘
```

### 排程任務

| 時間 (ET) | 任務 | 說明 |
|---|---|---|
| **09:00** | 盤前風險監控 | 掃描持倉與觀察清單的財報日曆；若財報 ≤ 3 天內，私訊 ⚠️ IV 崩跌警報（區分持倉高風險 vs 觀察清單標的）。 |
| **09:45** | Delta 中性掃描 | 對每位使用者的觀察清單執行技術面 + Greeks + VRP + 偏態 + 流動性全方位掃描；私訊可操作訊號與凱利建議倉位。 |
| **16:15** | 盤後報告 | 動態結算損益、Delta 擴張轉倉建議、Gamma 脆性防禦；附帶 SPY Beta-Weighted 宏觀風險評估、Theta 收益率、資金熱度極限與 Pearson 相關性矩陣。 |

---

## 🛠 技術棧

| 層級 | 技術 |
|---|---|
| **語言** | Python 3.12 |
| **Discord** | `discord.py` ≥ 2.3 — 斜線指令、私訊路由 |
| **市場數據** | `yfinance`（報價）、`pandas-ta`（指標）、`py_vollib`（Black-Scholes-Merton + Greeks） |
| **數值計算** | `numpy`（對數報酬率、波動率）、`pandas`（數據處理） |
| **排程** | `pandas_market_calendars`、`zoneinfo` |
| **資料庫** | SQLite — 以 `user_id` 為複合唯一鍵 |
| **基礎架構** | Docker、Docker Compose、GitHub Actions CI/CD → DigitalOcean |

---

## 🚀 快速開始

### 前置需求

- [Docker](https://docs.docker.com/get-docker/) & [Docker Compose](https://docs.docker.com/compose/install/)
- 一組 [Discord Bot Token](https://discord.com/developers/applications)

### 1. 複製並準備

```bash
git clone https://github.com/cosmo-chang-1701/nexus-seeker.git
cd nexus-seeker
mkdir -p data          # SQLite 持久化掛載目錄
```

### 2. 設定環境變數

```bash
cp .env.example .env
```

編輯 `.env` 並填入你的 Token：

```env
DISCORD_TOKEN=your_discord_bot_token_here
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

> **從 v1 升級？** 請刪除 `data/` 中的舊 SQLite 檔案，以便使用包含 `user_id` 欄位的新 schema 重建資料庫。

---

## ⌨️ Discord 指令

所有指令使用 Discord 原生**斜線指令**，內建參數驗證。
回覆皆為**臨時訊息** — 僅觸發指令的使用者可見。

### 📡 觀察清單

| 指令 | 說明 | 範例 |
|---|---|---|
| `/add_watch` | 將標的加入觀察清單 | `symbol: TSLA` |
| `/list_watch` | 檢視所有觀察中的標的 | — |
| `/remove_watch` | 移除標的 | `symbol: ONDS` |
| `/scan` | 手動對標的執行 Delta 中性掃描 | `symbol: SMR` |

### 💼 投資組合

| 指令 | 說明 | 範例 |
|---|---|---|
| `/add_trade` | 記錄實際交易以進行監控 | 見下方 |
| `/list_trades` | 檢視持倉、損益與交易 ID | — |
| `/remove_trade` | 依 ID 移除已平倉的持倉 | `trade_id: 1` |
| `/set_capital` | 設定總資金以供 Kelly 倉位計算 | `capital: 50000` |

### 🛠️ 管理員

| 指令 | 說明 |
|---|---|
| `/force_scan` | 立即手動執行全站掃描（不論開盤時間），結果私訊分發給所有使用者 |

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

</details>

---

## 🔄 投資組合工作流程

```
┌───────────────┐     ┌────────────────┐     ┌─────────────────┐
│  1. 訊號      │────►│  2. 記錄       │────►│  3. 監控        │
│  接收私訊     │     │  /add_trade    │     │  每日 16:15 自動│
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

量化引擎（`market_analysis/strategy.py`）實作四種策略，每種皆以技術面篩選為門檻，並經過**八道量化濾網**精煉。

### 共用濾網管線

所有通過策略觸發條件的合約皆須依序通過以下濾網：

| # | 濾網 | 規則 | 適用策略 |
|---|---|---|---|
| 1 | HV Rank | 波動率位階 ≥ 30（一年內相對百分位） | STO Put / STO Call |
| 2 | IV 期限結構 | 30D/60D IV 比率偵測逆價差（Backwardation ≥ 1.05） | 全部 |
| 3 | 垂直偏態 | 25Δ Put/Call IV 比率 ≥ 1.50 → 硬性否決 STO Put | STO Put |
| 4 | 流動性 | Bid-Ask 絕對價差 > $0.20 **且**佔比 > 10% → 剔除 | 全部 |
| 5 | VRP | 隱含波動率 < 歷史波動率（VRP < 0）→ 拒絕 | STO Put / STO Call |
| 6 | 隱含預期波動區間 | `現價 × IV × √(DTE/365)` 算出 1σ 預期波動幅度；STO Put 損益兩平 `(Strike − Bid)` 必須 ≤ 預期下緣，STO Call 損益兩平 `(Strike + Bid)` 必須 ≥ 預期上緣 — 落入圓錐內即剔除 | STO Put / STO Call |
| 7 | AROC | 年化資本回報率 `(Bid / 保證金) × (365 / DTE)` < 15% → 剔除；保證金 = `Strike − Bid` | STO Put / STO Call |
| 8 | ¼ Kelly | 凱利倉位上限 5%，單口保證金 = 履約價 − 權利金 | STO Put / STO Call |

### 🟢 賣出開倉 Put — *超賣收入*

- **觸發條件：** `RSI(14) < 35` + `HV Rank ≥ 30`
- **合約：** 30–45 DTE，Delta ≈ **−0.20**（約 80% OTM 機率）
- **篩選：** 垂直偏態 < 1.50、VRP > 0、流動性通過、損益兩平 ≤ 1σ 預期下緣、`AROC ≥ 15%`、Kelly 倉位

### 🔴 賣出開倉 Call — *超買收入*

- **觸發條件：** `RSI(14) > 65` + `HV Rank ≥ 30`
- **合約：** 30–45 DTE，Delta ≈ **+0.20**
- **篩選：** VRP > 0、流動性通過、損益兩平 ≥ 1σ 預期上緣、`AROC ≥ 15%`、Kelly 倉位

### 🚀 買入開倉 Call — *動能突破*

- **觸發條件：** 價格 > `20 SMA` + `50 ≤ RSI(14) ≤ 65` + `MACD 柱狀圖 > 0`
- **合約：** 14–30 DTE，Delta ≈ **+0.50**（ATM）
- **篩選：** 流動性通過

### ⚠️ 買入開倉 Put — *跌破 / 避險*

- **觸發條件：** 價格 < `20 SMA` + `35 ≤ RSI(14) ≤ 50` + `MACD 柱狀圖 < 0`
- **合約：** 14–30 DTE，Delta ≈ **−0.50**（ATM）
- **篩選：** 流動性通過

---

## 📁 專案結構

```
nexus-seeker/
├── main.py                  # 進入點 — 初始化資料庫、註冊訊號處理、啟動 Bot
├── bot.py                   # NexusBot 類別 — 擴充模組載入、啟停通知分發
├── config.py                # 環境變數、無風險利率、策略目標 Delta 參數
├── database.py              # SQLite CRUD — 多租戶（依 user_id 區分）
├── market_math.py           # Facade — 統一 re-export market_analysis 子模組
├── market_time.py           # NYSE 日曆與動態休眠排程器
├── market_analysis/         # 核心量化引擎 (Python Package)
│   ├── __init__.py          # 公開 API 匯出
│   ├── data.py              # 財報日期查詢 (yfinance)
│   ├── greeks.py            # Black-Scholes-Merton Delta 計算 (含股息率 q)
│   ├── strategy.py          # 技術面掃描 + 八道濾網管線 + 合約篩選
│   └── portfolio.py         # 盤後結算引擎、防禦決策樹、宏觀風險評估、相關性矩陣
├── cogs/
│   └── trading.py           # 斜線指令、私訊分發器、排程任務、Embed 格式化
├── tests/
│   ├── __init__.py
│   ├── test_strategy.py     # 策略模組單元測試 (Mock 依賴)
│   ├── test_market_time.py  # 排程時間計算測試
│   └── verify_market_functions.py  # 量化函數整合驗證
├── data/                    # SQLite 資料庫（Docker Volume 掛載）
├── .github/
│   └── workflows/
│       └── deploy.yml       # CI/CD — 建構 → GHCR → DigitalOcean Swarm
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
└── LICENSE
```

---

## 🧪 測試

測試在 Docker 容器中執行：

```bash
docker compose run --rm nexus_seeker python -m pytest tests/ -v
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

- [ ] **Argo Cortex** — 本地 LLM（vLLM + Qwen/Llama 於 NVIDIA 5070 Ti）用於情緒分析；自動否決具破壞性基本面新聞的訊號。
- [ ] **MCP Server** — 將核心量化模組封裝為標準 Model Context Protocol 工具，供外部 AI 代理使用。
- [ ] **券商 API 整合** — Interactive Brokers Gateway 實現全自動下單執行（訊號 → 執行 → 平倉，零人工介入）。

---

## 📄 授權條款

本專案採用 [MIT 授權條款](LICENSE)。

---

<div align="center">

*由 [Cosmo Chang](https://github.com/cosmo-chang-1701) 以 ❤️ 打造，追求量化自由。*

</div>