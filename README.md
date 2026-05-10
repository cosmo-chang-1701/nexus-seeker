# 🌌 Nexus Seeker: Professional Liquidity & Risk Management Terminal

<div align="center">
  <img src="assets/hero.png" alt="Nexus Seeker Terminal Hero" width="800" />
</div>

**針對全職投資者打造的關鍵任務執行環境 — 專注於資產保護與系統性風險對沖**

[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](nexus_core/docker-compose.yml)
[![Architecture](https://img.shields.io/badge/architecture-dual--service-purple.svg)](#architecture)

> **Nexus Seeker** 是一款專為專業選擇權交易者設計的高效能終端，核心設計圍繞 **Financial Runway (財務跑道)**、**Greeks Integrity (希臘字母完整性)** 與 **Cross-Market Edge Detection (跨市場邊緣偵測)**。透過 **Black-Scholes-Merton** 精算、**Nexus Risk Optimizer (NRO)** 與 **Sentiment Engine**，本系統提供從信號偵測到自動化對沖的完整專業風控管線。

---

## 🛠 Technical Specifications

| 類別 | 技術規格 (Specifications) |
|---|---|
| **Runtime 環境** | Python 3.12 (WSL2 / Windows 11 / Low-RAM VPS 最佳化) |
| **量化定價引擎** | Black-Scholes-Merton (via `py_vollib`, 含股息率校正) |
| **風險精算核心** | Nexus Risk Optimizer (NRO) - 二階 Beta-Weighted 曝險模型 (含 Vanna 修正) |
| **情緒分析中心** | Sentiment Engine - Skew 偏斜、PCR、Max Pain 與 UOA 偵測 |
| **驗證與測試** | `pytest` + `pytest-asyncio` (核心引擎覆蓋率 > 90%, 支援三段式警報過濾) |
| **數據源 (Feeds)** | Finnhub (Real-time), yfinance (Options), Polymarket (WS L2), Reddit (Edge) |
| **持久化層** | SQLite 搭配自動化 Migration Engine (v032+) |
| **智能層** | Structured LLM Output (Pydantic Schema) 具備 Memory Safety Gates |
| **訊息傳遞** | Discord.py (持久化非同步訊息佇列，支援多租戶隔離) |

---

## 🏗 System Architecture

系統採用分散式雙服務架構，確保雲端執行效率與邊緣爬蟲的隱私性。最新版本針對 **1GB RAM 環境** 進行了深度優化，引入 LRU Bounded Cache 與緊急記憶體閘門。

```mermaid
graph TD
    subgraph Cloud_Environment [Cloud / Discord Service]
        Bot[NexusBot Core]
        TS[TradingService - Business Logic]
        RE[Risk Engine - NRO]
        SE[Sentiment Engine - Skew/PCR/UOA]
        MM[Memory Manager - VPS Health]
        DB[(SQLite DB v032)]
        S[Services - LLM/Polymarket]
        C[Bounded Cache - SMA/EMA/Poly]
    end

    subgraph Edge_Environment [Local / Edge Scraper]
        ES[Nexus Edge Scraper - Playwright]
    end

    User((Professional Trader)) -- Slash Commands --> Bot
    Bot --> TS
    TS --> RE
    TS --> SE
    TS --> DB
    TS --> S
    S -- Cloudflare Tunnel --> ES
    TS -- Async Notify --> User
```

---

## 🏁 Financial Intelligence

針對全職投資者量身打造的生存與效率指標：

*   **財務生存與跑道分析 (Financial Survival & Runway)**：
    系統自動對照使用者的 **Cash Reserve (現金儲備)** 與 **Monthly Expenses (每月支出)**，利用投資組合的 **Total Theta (每日總 Theta 收租額)** 動態估算「財務生存天數」。
*   **對沖歸因與自我進化 (Self-Evolving Attribution)**：
    系統會追蹤每一筆虛擬對沖 (VTR Hedge) 的 **Protection Score (保護評分)**。若保護效率偏低，AI 將自動建議調整 NRO 觸發閾值或相關性 Proxy，實現策略的動態迭代。

---

## 🛡️ Functional Pillars

### 1. Risk Integrity (NRO 引擎)
*   **Vanna-Adjusted Delta (隱含 Delta 修正)**：
    考慮 IV 劇烈變動對 Delta 的非線性影響 ($d\Delta/d\sigma$)。在 VIX 飆升時，系統會自動估算 **"Hidden Delta"** 並給出更精確的對沖口數建議。
*   **Automated Hedging Pipeline (自動化對沖管線)**：
    當 VIX 觸發戰情階梯跳級或單日移動 > 10% 時，系統主動推送 **「緊急對沖指令」**。使用者可透過 `/settle_hedge` 一鍵記錄執行結果。
*   **VIX 戰情階梯 (Battle Ladder)**：
    6 階段自適應風險調控系統，根據即時波動率動態縮放 **Kelly Criterion** 比例。

### 2. Market Sentiment (市場情緒引擎)
*   **Option Skew Strategist (偏斜策略家)**：
    監控 OTM Put 與 Call 的 IV 差值。當 Skew 進入 90th 百分位時，發出 **"Pre-emptive Hedge" (預警性對沖)** 訊號。
*   **UOA & Whale Intent Mapping (巨鯨意圖映射)**：
    將 Polymarket 的巨鯨交易與選擇權市場的 **Unusual Options Activity (UOA)** 進行關聯分析，判定是大規模方向性押注還是機構對沖行為。
*   **Max Pain Analysis (最大痛點分析)**：
    計算結算日前夕的 Max Pain 價格，評估標的是否趨於收斂以鎖定最終利潤。

### 3. Calendar-Aware Risk Guard (日曆感應風險防護)
*   **Event-Driven Vanna Weighting (事件驅動 Vanna 權重)**：
    自動監控 CPI、FOMC 及財報事件。當重大事件 TTE < 72h 時，NRO 引擎自動調高 **Vanna ($d\Delta/d\sigma$)** 權重，縮減曝險以應對「IV Run-up」。
*   **IV Crush Detection (波動率驟降偵測)**：
    當 IV Rank > 80% 且距離財報 < 24h 時，系統標註為「高風險波動率事件」，並建議採取風險中性或防禦性價差策略。
*   **Proactive Event Alerts (主動事件預警)**：
    後台監控器每 4 小時掃描一次，針對 48 小時內即將發生的重大衝擊事件向持倉用戶發送對沖預警。

### 4. Execution Automation & VPS Stability
*   **NYSE 動態調度器 (Dynamic Scheduler)**：
    精準對齊交易所交易時鐘，以 30 分鐘為心跳進行全自動掃描。
*   **VPS Performance Guard (1GB RAM 優化)**：
    針對低配 VPS 引入 **BoundedCache (max 500)** 與 **Memory Safety Gates**。當系統 RAM > 85% 時，自動延後非核心 AI 分析，優先確保風險計算與警報發送。
*   **獨立現貨持倉系統 (Independent Holdings System)**：
    解耦觀察清單與實際資產。長期股權會自動納入 NRO 全局風險精算。

---

## 🔄 Contract Lifecycle

系統管理期權合約從「偵測」到「對沖結算」的完整專業流程：

```mermaid
stateDiagram-v2
    [*] --> Detection: Signal Detection (EMA/PSQ/DDP/IV/Skew)
    Detection --> Audit: Risk/AROC Audit (15% Threshold)
    Audit --> Execution: VTR / Live Execution
    Execution --> Monitoring: Real-time Delta/Vanna/Vega/Gamma Tracking
    Monitoring --> Defense: DITM Convexity / Max Pain Proximity
    Monitoring --> Hedging: VIX Spike / Delta Deviation
    Defense --> ProfitLock: Automated Closing / Roll
    Hedging --> Adjust: Automated SPY Hedge Instructions
    Adjust --> Attribution: Protection Score Analysis
    Attribution --> Monitoring: Parameter Feedback
    Monitoring --> Exit: Target PnL (50%/100%) / Hard Stop
    Exit --> [*]
```

---

## ⌨️ Command Matrix (CLI)

| Command | Description | Input Schema (Summary) |
|---|---|---|
| `/settings` | 配置全域資產、風險、生存支出與 **三段式警報開關** | `capital`, `risk_limit`, `alert_mode` |
| `/runway_check` | 執行財務生存跑道分析 | — |
| `/skew_scan` | **[New]** 執行期權偏斜 (Skew) 與市場情緒掃描 | `symbol` |
| `/max_pain` | **[New]** 計算特定標的之最大痛點與收斂狀態 | `symbol`, `expiry` |
| `/vtr_stats` | 檢視 VTR 績效統計與 **對沖效能歸因** | — |
| `/settle_hedge` | **[New]** 確認並記錄已執行的對沖操作 (維持 Delta 中性) | `alert_id`, `qty` |
| `/hedge_list` | **[New]** 查看最近的對沖警報與執行狀態 | — |
| `/sys_health` | **[New]** [Hidden] 檢查 VPS 資源狀態與快取健康度 | — |
| `/scan` | 手動執行量化掃描與 What-if 曝險模擬 | `symbol` |
| `/ddp_scan` | 對觀察清單執行 Davis Double Play (DDP) 掃描 | — |
| `/add_holding` | 登錄實際現貨持倉 (納入 Beta-Delta 計算) | `symbol`, `quantity`, `avg_cost` |
| `/list_holdings` | 列出目前所有現貨持倉與分配比例 | — |
| `/poly_list` | 顯示 Polymarket 活躍市場清單與巨鯨意圖 | — |
| `/quote` | 獲取標的之即時報價與漲跌資訊 | `symbol` |
| `/scan_news` | 掃描特定標的之最新官方新聞 | `symbol` |
| `/scan_reddit` | 掃描特定標的之 Reddit 散戶情緒 | `symbol` |
| `/calendar` | **[New]** 顯示影響目前投資組合的即時重大事件 | — |
| `/iv_rank` | **[New]** 掃描觀察清單中具備高 IV Rank 或財報前夕的標的 | — |
| `/event_impact` | **[New]** 針對特定即時事件進行 Greeks (Delta, Vanna) 模擬 | `symbol`, `vol_move` |

---

## 🚀 Getting Started

### Prerequisites
*   Docker & Docker Compose
*   Finnhub API Key & Discord Bot Token
*   OpenAI-compatible API Key (用於智能分析)

### Quick Deployment
1.  `cp .env.example .env` (填寫 API Keys)
2.  `docker compose up -d --build`
3.  進入 Discord 使用 `/settings` 初始化配置。

---

## 📄 License
本專案採用 [MIT 授權條款](LICENSE)。

<div align="center">

*由 [Cosmo Chang](https://github.com/cosmo-chang-1701) 以 ❤️ 打造，追求量化自由。*

</div>
