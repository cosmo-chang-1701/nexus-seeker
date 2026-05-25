# 🌌 Nexus Seeker

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](nexus_core/docker-compose.yml)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Nexus Seeker 是一個 **Discord 驅動的選擇權風控與交易營運平台**，把 watchlist 監控、期權結構判讀、事件風險防禦、盤中對沖與盤後報告整合進同一套工作流。

它要解決的核心痛點很直接：**把分散的市場監控、風險計算與交易提示，收斂成一套可持續運行、可主動推播、適合實盤節奏的作業系統。**

> **目標族群**
> - 全職或高頻關注市場的選擇權交易者
> - 需要盤中主動風控提醒的多標的投資人
> - 想把 Discord 當成交易營運面板的量化 / 半量化使用者

<!-- Add Demo GIF/Screenshot Here -->

```mermaid
graph TD
    User((Discord User))

    subgraph Core[nexus_core]
        Bot[NexusBot]
        Trading[SchedulerCog / TradingService]
        Analyst[AnalystAgent]
        Sentiment[Sentiment Engine]
        NRO[NRO / Risk Engine]
        Calendar[CalendarService + SQLite Cache]
        LLM[LLM Service]
        Queue[Persistent DM Queue]
        DB[(SQLite)]
    end

    subgraph Edge[nexus_edge_scraper]
        EdgeAPI[FastAPI + Playwright]
        Tunnel[Cloudflare Tunnel]
    end

    User --> Bot
    Bot --> Trading
    Bot --> Analyst
    Trading --> Sentiment
    Trading --> NRO
    Trading --> Calendar
    Trading --> LLM
    Trading --> Queue
    Analyst --> LLM
    Analyst --> Queue
    Queue --> User
    Bot -. Optional Reddit scraping .-> EdgeAPI
    Tunnel --> EdgeAPI
    Trading --> DB
    Analyst --> DB
```

---

## ✨ 核心特點

- **📡 主動式 Watchlist 心跳**：開盤期間每 30 分鐘逐檔推送 watchlist 戰報，不必手動輪流查圖、查鏈、查事件。
- **🧾 可執行期權合約建議**：不只顯示訊號，還直接給出策略、腿位、strike、expiry、mid、建議口數與最大風險。
- **🧠 LLM 輔助分析與 Skew 解讀**：自動進行 IV 泡沫與多空背離數學交叉驗證，並生成 100% 繁中金融級分析。
- **🗓️ 事件風險內建防禦**：財報、CPI、FOMC、NFP 會先經過事件快取與風控邏輯，避免在錯誤時機硬開倉。
- **🛡️ 盤中風控與對沖指引**：依 VIX、Vanna、Greeks、與 sectoral ETF 的相對強度 (Relative Strength) 與偏離度，支援 SHIELD / SPEAR 戰術路由，強勢股過度超買時優先轉入期權攻擊（如信用價差或備兌策略）。
- **📦 盤前到盤後的一致工作流**：從盤前財報掃描、盤中執行建議，到盤後風險結算與板塊輪動報告，全都在同一個 bot 裡完成。
- **💾 持久化 DM 佇列**：通知先寫入資料庫再發送，重啟後也能補發，避免 Discord 推播遺失。
- **🧱 低 RAM VPS 友善**：SQLite 快取、bounded cache、記憶體安全閘門讓系統能在 1GB RAM 級別環境穩定運行。

---

## 🚀 快速上手

### 先決條件

啟動 `nexus_core` 前，請先準備：

- **Docker** 與 **Docker Compose**
- **Python 3.12**（若你要直接在容器外執行程式）
- **Discord Bot Token**
- **Discord Admin User ID**
- **Finnhub API Key**
- **OpenAI-compatible LLM endpoint**
  - `LLM_API_BASE`
  - `LLM_MODEL_NAME`
  - `API_KEY`
- **可選**：Cloudflare Tunnel Token（若要啟動 `nexus_edge_scraper`）

### 安裝步驟

#### 1. 啟動核心 Bot

```bash
git clone https://github.com/cosmo-chang-1701/nexus-seeker.git
cd nexus-seeker/nexus_core
cp .env.example .env
docker compose up -d --build
```

核心 Bot 使用：

- `discord.py`
- SQLite
- `docker-compose.yml`
- `.env` 環境變數

#### 2. 啟動可選的 Edge Scraper

如果你要使用本地 / 邊緣 Reddit scraping：

```bash
cd ../nexus_edge_scraper
cp .env.example .env
docker compose up -d --build
```

這個服務使用：

- `FastAPI`
- `Playwright`
- `BeautifulSoup`
- optional `cloudflared` sidecar

### 最簡可行範例

下面這段是最小可啟動的核心 Bot 範例流程。填好 token 與 API 金鑰後可直接執行：

```bash
cd nexus-seeker/nexus_core

cat > .env <<'EOF'
DISCORD_TOKEN=your_discord_bot_token_here
DISCORD_ADMIN_USER_ID=123456789012345678
LLM_API_BASE=https://your-llm-endpoint.example.com/v1
LLM_MODEL_NAME=your-model-name
API_KEY=your_api_key_here
TUNNEL_URL=https://your-edge-api.example.com
FINNHUB_API_KEY=your_finnhub_api_key_here
LOG_LEVEL=WARNING
EOF

docker compose up -d --build
```

啟動後：

1. Bot 會載入核心 cogs 與背景排程
2. 建立 / 初始化 SQLite 資料庫
3. 啟動 DM queue、記憶體管理、對沖監控與 Polymarket 服務
4. 你可以在 Discord 內使用 `/settings`、`/x`、`/dash`、`/market`

> **提示**
> 若只想先驗證 bot 能啟動，`nexus_edge_scraper` 可以稍後再接；它不是核心 bot 的必要條件。

---

## 🛠️ 開發與貢獻

### 本地開發

核心程式位於 `nexus_core/`，主要組成如下：

- `bot.py`：Bot 啟動、DM queue、服務生命週期
- `cogs/`：Discord commands 與背景 scheduler
- `market_analysis/`：量化分析、watchlist 心跳、風控引擎
- `services/`：資料、LLM、calendar、trading orchestration
- `database/`：SQLite schema、migration、cache helpers

### 執行測試

目前專案的測試是以 **Docker 內 pytest** 為準：

```bash
cd nexus_core
docker compose run --rm nexus-seeker python -m pytest tests
```

若只想跑局部：

```bash
cd nexus_core
docker compose run --rm nexus-seeker python -m pytest tests/unit/test_intraday_pipeline.py
docker compose run --rm nexus-seeker python -m pytest tests/unit/test_embed_builder.py
docker compose run --rm nexus-seeker python -m pytest tests/unit/test_output_centralization.py
```

### 貢獻流程

1. Fork 這個 repository
2. 建立你的功能分支
3. 完成修改並確認測試通過
4. 推送到你的 fork
5. 建立 Pull Request，說明：
   - 改了什麼
   - 為什麼要改
   - 影響哪些使用者流程或推播內容

建議 branch naming：

```bash
git checkout -b feat/your-change-name
```

或：

```bash
git checkout -b fix/your-bug-name
```

---

## 📄 授權條款

本專案採用 [MIT License](LICENSE)。
