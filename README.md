# 🌌 Nexus Seeker

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](nexus_core/docker-compose.yml)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Nexus Seeker 是一個 **Discord-first 的多租戶選擇權風控與交易營運平台**。本平台深度結合了技術指標分析、Black-Scholes-Merton 期權定價、希臘字母（Greeks）投資組合風險管理、事件日曆防禦及大型語言模型（LLM）輔助分析，旨在低內存 VPS 部署環境下，為實盤交易者提供最即時、可持續、自動化的高勝率風控操作指南。

> 核心版本（nexus_core）：**v1.11.28** (請參考最新 Release)

---

## ✨ 核心特色 (Key Features)

- **自動化戰場心跳 (Watchlist Heartbeat)**：盤中每半小時主動推送自選標的之技術與期權快照，包含 UOA (異動期權)、暗池 (Dark Pool) 磁吸點位與 LLM 分析解說。
- **戰場情境轉折警報與進階雷達 (Market Scenario & Advanced Radar)**：整合獨立事件驅動引擎（5 階 GEX 決策矩陣）與進階雷達過濾器 (UOA Barrier, Gravity Filter, Divergence Gate)。具備 K 棒影線誤差穿透檢測與爆量動能過濾，精準捕捉「結構破位」、「假性支撐」、「黃金波段止盈」等轉折點。
- **動態轉倉與基本面防禦閘門 (Dynamic Rollover & Global Defense Gate)**：支援透過 `/verify_thesis` 手動觸發，並新增**互動式 SEC 財報選擇介面**，讓使用者自由選擇近期 (10-K, 10-Q, 8-K) 進行分析（若超時未選則自動分析最新報告）。Edge Scraper 現具備 **SEC 財報結構化區塊擷取** 能力，能針對 Forward Guidance、Margin 等 5 大區塊進行抽取。並以 Advanced CoT (Chain of Thought) 系統提示詞引導 LLM 進行護城河破滅驗證。若基本面破滅，將無條件攔截量化買入訊號，強制執行清算與轉倉。
- **動態量化避險引擎**：依據現價與 RSI、Skew 等指標，動態計算適合的建倉/出場價格，並於大盤極端行情時啟動網格防禦 (Shield) 與流動性滑價保護閘門。
- **總體經濟與事件日曆防護**：自動抓取 CME FedWatch 利率機率、FRED 關鍵總經數據與財報日曆，結合避險邏輯進行動態逃頂窗口前置。
- **大盤微觀結構解析**：計算零 Gamma 線 (Gamma Flip Line) 與 GEX 分佈，在市場進入高壓 $VIX > 20$ 時動態縮小合約建倉口數（Kelly Criterion 調節）與拉大網格距離。
- **互動式 UI 介面**：所有交易參數（資本、風險上限、虛擬交易室等）與推送偏好均透過 Discord 內建的 Buttons / Select Menu / Modal 進行管理。

> 💡 **進階量化策略與運作邏輯**：關於 TDP 估值三擊、暗池防禦共振、動態均價 Covered Call 解鎖等深度架構細節，請參閱專案內的 [`AGENTS.md`](AGENTS.md) 說明文件。

---

## 🏗️ 系統架構 (Architecture)

本專案採用雙服務（Microservices）架構，透過 API 與資料庫非同步通信：

- **`nexus_core/`**：主 Discord Bot。管理所有的 slash commands、背景排程、量化策略引擎、主動推播佇列與 SQLite 資料庫。
- **`nexus_edge_scraper/`**：邊緣爬蟲服務 (FastAPI + Playwright)。負責隔離高耗能的網頁渲染作業（如 Reddit 輿情監控、SPY 選擇權鏈抓取），並透過 Cloudflare Tunnel 暴露給 Core 調用。

```mermaid
graph TD
    User((Discord User))

    subgraph Core["nexus_core (Discord Bot)"]
        Bot["NexusBot"]
        Cogs["Cogs: trading / terminal / sentiment / order_ui"]
        Services["Services: calendar / llm / telemetry"]
        Queue["Persistent DM Queue"]
        Leader["SQLite leader lock"]
        DB[(SQLite)]
    end

    subgraph Edge["nexus_edge_scraper (FastAPI)"]
        EdgeAPI["FastAPI + Playwright"]
        Tunnel["Cloudflare Tunnel"]
    end

    User --> Bot
    Bot --> Cogs
    Cogs --> Services
    Services --> DB
    Queue --> DB
    Leader --> DB
    Queue --> User

    Services --> EdgeAPI
    Tunnel --> EdgeAPI
```

---

## 📂 專案結構 (Project Structure)

```text
nexus-seeker/
├── nexus_core/              # 核心 Discord Bot 服務
│   ├── bot.py               # Bot 進入點與健康監控
│   ├── cogs/                # Discord 交互指令、背景排程與 UI 元件
│   ├── market_analysis/     # 核心量化引擎 (Scan Pipeline, Gamma Squeeze)
│   ├── services/            # 外部 API 封裝 (Calendar, LLM 等)
│   ├── database/            # SQLite Schema 與 Migrations
│   ├── risk_engine/         # SDDM 戰術路由與風險控制 (NRO)
│   ├── formatters/          # Embed 格式化與輸出模組
│   ├── ui/                  # ANSI 字符格式化與表格渲染
│   └── tests/               # 單元與整合測試
├── nexus_edge_scraper/      # 邊緣爬蟲服務 (Playwright)
│   ├── local_api.py         # FastAPI 應用程式路由
│   └── tests/               # 爬蟲端測試
├── scripts/                 # 輔助腳本與部署工具
├── docs/                    # 附加文件
└── README.md                # 專案主說明檔
```

---

## 🚀 快速上手 (Getting Started)

專案支援 Docker 容器化部署，預設資料庫將存在 Named Volume 中以確保資料不遺失。

### 1. 必備條件
- Docker 與 Docker Compose
- Discord Bot Token
- Finnhub API Key
- Cloudflare Tunnel Token (用於邊緣爬蟲)

### 2. 啟動 `nexus_edge_scraper` (邊緣爬蟲服務)
```bash
git clone https://github.com/cosmo-chang-1701/nexus-seeker.git
cd nexus-seeker/nexus_edge_scraper

# 設定環境變數
cp .env.example .env
# 編輯 .env 設定您的 CF_TUNNEL_TOKEN

# 啟動容器
docker compose up -d --build
```

### 3. 啟動 `nexus_core` (主機器人服務)
取得 `nexus_edge_scraper` 在公網上的 HTTPS URL 後，填入 Core 的 `.env` 中。
```bash
cd ../nexus_core

# 設定環境變數
cp .env.example .env
# 編輯 .env 填入 DISCORD_TOKEN, FINNHUB_API_KEY, 以及 TUNNEL_URL

# 啟動容器
docker compose up -d --build
```

---

## ⚙️ 環境變數設定

### `nexus_core/.env`
| 變數名稱 | 必填 | 說明 |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Discord 機器人 Token |
| `DISCORD_ADMIN_USER_ID` | ✅ | 系統管理員的 Discord 帳號 ID（允許使用 admin 指令） |
| `FINNHUB_API_KEY` | 建議 | Finnhub 數據源 API Key |
| `TUNNEL_URL` | ✅ | Edge Scraper 的公開 API URL |
| `LLM_API_BASE` | 選用 | OpenAI 相容的 LLM API 網址 |
| `LLM_MODEL_NAME` | 選用 | LLM 模型名稱 |
| `API_KEY` | 選用 | LLM 驗證金鑰 |
| `LOG_LEVEL` | 選用 | 預設為 `WARNING` (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

### `nexus_edge_scraper/.env`
| 變數名稱 | 必填 | 說明 |
|---|---|---|
| `CF_TUNNEL_TOKEN` | ✅ | Cloudflare Tunnel token（用以將 API 安全暴露於公網） |

---

## 🔌 常用指令與操作 (Usage)

透過 Discord 頻道輸入斜線指令即可與機器人互動：

- **`/settings`**：帳戶核心參數面板（資本、風險上限設定、虛擬交易室等）。
- **`/notif_settings`**：戰術型通知管理中控台，支援以模組分類自訂雷達與推播偏好（如 Alpha 訊號、極端防禦、總經警報等）。
- **`/x`**：批次量化雷達掃描，支援統一雷達面板進行多層次過濾。
- **`/dash`**：交易員主控板，檢視持倉、備用流動性與極限跑道天數。
- **`/stress_test`**：委託單壓力測試與現金赤字警報。
- **`/list_orders` / `/order_panel`**：檢視活躍委託單與新增委託單。
- **`/cc_recovery`**：根據現有持倉自動過濾與顯示最佳的 OTM Covered Call 合約 (收租解套策略)。
- **`/calendar` / `/market`**：查詢總經與個股財報事件日曆、大盤宏觀狀態。
- **`/force_macro_update`**：`(Admin 專用)` 強制更新大盤 GEX 與 FedWatch 數據快取。

> 在主機本機端也提供 `cli.py` 開發者工具，供管理員手動觸發爬蟲或強制執行掃描任務。

---

## 🛠️ 開發與測試 (Development & Testing)

本專案啟用 Mypy 嚴格模式，所有提交皆應維持型別安全與通過單元測試。請務必在 Docker 環境中執行以確保依賴一致。

**執行型別檢查 (Mypy)**：
```bash
cd nexus_core
docker compose run --rm nexus-seeker python -m mypy --config-file pyproject.toml .
```

**執行單元與整合測試 (Pytest)**：
```bash
# 執行所有測試
docker compose run --rm nexus-seeker python -m pytest tests

# 執行特定功能測試
docker compose run --rm nexus-seeker python -m pytest tests/unit/test_intraday_pipeline.py
```

---

## 📄 授權條款 (License)

本專案採用 [MIT License](LICENSE) 授權。
