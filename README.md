這是一份為升級版 **Nexus Seeker (樞紐尋覓者)** 重新編寫的 `README.md`。

我已經將「多租戶架構 (Multi-tenant)」、「私訊分發引擎 (DM Dispatcher)」以及「隱私保護 (Ephemeral)」等最新架構特性完全寫入文件中，並移除了舊版對單一頻道的依賴設定。

你可以直接點擊右上角的「複製」按鈕，將以下內容貼入並覆蓋你專案根目錄的 `README.md` 檔案：

```markdown
# 🌌 Nexus Seeker (樞紐尋覓者)

![Version](https://img.shields.io/badge/version-2.0.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.11-green.svg)
![Architecture](https://img.shields.io/badge/architecture-Multi--tenant-purple.svg)
![Docker](https://img.shields.io/badge/docker-ready-blue.svg)

**Nexus Seeker** 是一個基於 Python 與 Docker 建構的「多租戶選擇權量化交易專屬助理」。它結合了技術分析、Black-Scholes 選擇權定價模型與全自動化的 NYSE 交易日曆排程，旨在協助交易員實現高勝率的選擇權賣方策略（The Wheel / Credit Spreads）。

透過最新的**私訊分發引擎 (DM Dispatcher)**，本系統支援多人同時使用，為每位使用者提供完全獨立的隱私資料庫、獨立的觀察清單，並透過 Discord 私訊 (Direct Message) 進行一對一的機構級部位風控與盤後結算報告。



## ✨ 核心功能 (Key Features)

* 🔐 **多租戶與極致隱私 (Multi-tenant & Privacy)**：捨棄傳統的群組廣播。所有終端指令皆為「僅限本人可見 (Ephemeral)」，且系統會自動綁定使用者的 Discord ID，確保每個人的資金、持倉與策略絕對保密。
* 📨 **私訊分發引擎 (DM Dispatcher)**：背景排程在執行全市場掃描時，會先進行「API 請求去重 (De-duplication)」以節省資源，隨後在記憶體中根據訂閱清單進行路由，將專屬的量化報告精準推播至使用者的 Discord 私人對話框。
* 🎯 **Delta 精準掃描 (Greeks Engine)**：內建 Black-Scholes 模型 (`py_vollib`)，自動根據當前隱含波動率 (IV) 算出指定 Delta (如 -0.20) 的最佳履約價，告別固定百分比盲猜。
* 📡 **全自動美股排程 (NYSE Scheduler)**：整合 `pandas_market_calendars`，自動處理夏令時間與國定假日。
    * `09:00` (美東)：盤前財報地雷掃描，倒數 3 天內發出 IV Crush 避險私訊警報。
    * `09:45` (美東)：開盤流動性收斂後，自動執行專屬觀察清單的技術面掃描。
    * `16:15` (美東)：盤後結算報告，自動精算未實現損益，並給出停利 (50%) 或轉倉防禦建議。
* 💾 **資料持久化 (Data Persistence)**：使用 SQLite 搭配 Docker Volume，確保重啟容器時持倉與觀察清單資料零遺失。

---

## 🛠️ 技術棧 (Tech Stack)

* **核心語言**：Python 3.11
* **Discord 框架**：`discord.py` (支援 Slash Commands & DM Routing)
* **量化與數據庫**：`yfinance` (報價), `pandas-ta` (技術指標), `py_vollib` (定價模型)
* **時間與排程**：`pandas_market_calendars`, `zoneinfo` (時區處理)
* **基礎設施**：SQLite (具備 User ID 複合唯一鍵設計), Docker, Docker Compose

---

## 🚀 快速部署 (Quick Start)

### 1. 取得專案與建立環境
```bash
git clone [https://github.com/yourusername/nexus_seeker.git](https://github.com/yourusername/nexus_seeker.git)
cd nexus_seeker
mkdir data  # 建立資料夾供 SQLite 持久化掛載

```

### 2. 環境變數設定

在專案根目錄建立 `.env` 檔案，你現在只需要填入 Discord Bot Token，系統即可自動為所有伺服器內的成員提供專屬服務：

```env
DISCORD_TOKEN=your_discord_bot_token_here

```

### 3. 使用 Docker 啟動服務

```bash
docker-compose up -d --build

```

*(註：若為舊版升級，請務必先刪除 `data/` 目錄下的舊版 SQLite 檔案，讓系統自動重建具備 `user_id` 欄位的新版資料表)*

---

## ⌨️ 終端機指令 (Discord Commands)

本系統完全捨棄傳統的 CLI 終端機，將 Discord 打造為全功能的量化交易中控台。所有指令皆採用 Discord 原生**斜線指令 (Slash Commands)**，內建參數防呆，且**回覆訊息僅限觸發者本人可見**。

### 📡 雷達觀察清單管理 (Watchlist)

觀察清單中的標的，會在每個交易日的 `09:45 (美東)` 進行自動化技術指標與 Delta 掃描，並**私訊**結果給您。

| 指令 | 說明 | 參數範例與說明 |
| --- | --- | --- |
| `/add_watch` | 將股票加入您的專屬自動雷達掃描清單。 | `symbol: TSLA` |
| `/list_watch` | 檢視您目前雷達監控中的所有標的。 | 無 |
| `/remove_watch` | 將標的從您的觀察清單中移除。 | `symbol: ONDS` |
| `/scan` | 盤中手動對特定標的執行 Delta 中性掃描。 | `symbol: SMR` |

### 💼 持倉部位管理 (Portfolio Management)

當您在券商（如 IBKR, TD, 嘉信）實際下單建倉後，請立即使用此系列指令將部位寫入您的專屬資料庫。

| 指令 | 說明 | 參數範例與說明 |
| --- | --- | --- |
| `/add_trade` | 將真實交易部位寫入本地資料庫進行盤後私訊監控。 | 詳見下方參數詳解 |
| `/list_trades` | 查看您目前持倉部位、未實現損益狀態與紀錄 ID。 | 無 |
| `/remove_trade` | 停利/平倉後，使用資料庫 ID 移除該筆監控紀錄。 | `trade_id: 1` |

#### `/add_trade` 參數詳解：

* **symbol**: 股票代號 (例: `SOFI`)
* **opt_type**: 選擇權類型，下拉選擇 `Put (賣權)` 或 `Call (買權)`
* **strike**: 履約價，支援浮點數 (例: `7.5`)
* **expiry**: 合約到期日，必須符合 `YYYY-MM-DD` 格式 (例: `2026-04-17`)
* **entry_price**: 當初建倉時，單口收付的權利金成本 (例: `0.55`)
* **quantity**: 持倉數量。**賣方 (Short) 請務必輸入負數** (例: `-5`)，買方 (Long) 輸入正數。

---

## 🔄 持倉管理生命週期 (Portfolio Workflow)

Nexus Seeker 的設計理念是「**進場靠技術，出場靠紀律**」。以下是標準的操作流程：

1. **[開倉] 接收訊號與下單**：早上 09:45，Discord 收到機器人**私訊推播**的 Delta -0.20 高勝率 Put 訊號，打開券商 APP 手動下單。
2. **[建檔] 寫入資料庫**：在 Discord 輸入 `/add_trade`，將剛成交的部位參數記錄進系統。
3. **[監控] 盤後自動結算**：機器人會在每個交易日的 `16:15 (美東)` 甦醒，自動抓取當日收盤價與您的成本進行比對。
4. **[決策] 執行防禦或停利** (透過私訊接收建議)：
* 🟢 **獲利 50% 以上**：系統發出停利警報 (Buy to Close)。
* 🔴 **到期日 < 14 天且虧損**：系統發出轉倉 (Rolling) 防禦警報。
* ⚫ **虧損達 150%**：系統發出黑天鵝停損警報。


5. **[平倉] 移除紀錄**：依據系統建議在券商平倉後，輸入 `/remove_trade`，完成該筆交易的閉環。

---

## 📈 交易策略濾網 (Strategy Logic)

Nexus Seeker 的核心量化引擎 (位於 `market_math.py`) 內建四種技術面觸發邏輯，並結合 Black-Scholes 模型進行嚴格的風險量化。

### 1. 🟢 Sell To Open Put (超賣收租 / 準備接股)

* **策略本質**：在恐慌中提供流動性，賺取時間價值 (Theta) 與波動率收斂 (Vega) 的紅利。
* **技術面觸發**：`RSI (14) < 35` (處於極度超賣區間)。
* **合約挑選邏輯**：尋找 `30 ~ 45` 天到期，且 Delta 最接近 **`-0.20`** 的合約 (約 80% 勝率)。

### 2. 🔴 Sell To Open Call (超買收租 / 掩護性買權)

* **策略本質**：在狂熱中鎖定利潤，適合搭配 Covered Call 策略。
* **技術面觸發**：`RSI (14) > 65` (處於超買區間)。
* **合約挑選邏輯**：尋找 `30 ~ 45` 天到期，且 Delta 最接近 **`+0.20`** 的合約。

### 3. 🚀 Buy To Open Call (動能突破作多)

* **策略本質**：順勢交易，利用選擇權的高槓桿 (Gamma) 參與主升段爆發。
* **技術面觸發**：股價站上 `20MA` + `50 <= RSI (14) <= 65` + `MACD 柱狀圖 > 0`。
* **合約挑選邏輯**：尋找 `14 ~ 30` 天到期，且 Delta 最接近 **`+0.50`** 的近價平 (ATM) 合約。

### 4. ⚠️ Buy To Open Put (動能跌破作空 / 避險)

* **策略本質**：順勢作空或為多頭部位進行短期避險。
* **技術面觸發**：股價跌破 `20MA` + `35 <= RSI (14) <= 50` + `MACD 柱狀圖 < 0`。
* **合約挑選邏輯**：尋找 `14 ~ 30` 天到期，且 Delta 最接近 **`-0.50`** 的近價平 (ATM) 合約。

---

## 🔮 未來展望 (Roadmap)

Nexus Seeker 目前的架構已達到**多租戶穩定產出交易訊號**與**自動化部位管理**的目標。下一個階段 (v3.0) 將聚焦於將本機端的高階運算資源 (NVIDIA 5070 Ti) 導入系統，打造具備認知能力的交易引擎：

* [ ] **Argo Cortex (本機 LLM 情緒分析引擎)**：
* 整合 `vLLM` 框架，部署 Qwen 或 Llama 等開源模型於本地 GPU。
* 在觸發技術面訊號前，自動爬取標的近三天的財經新聞與總經數據。
* 由本地 LLM 進行情緒分析 (Sentiment Analysis)，輸出信心分數。若偵測到毀滅性基本面利空，將自動攔截 (Veto) 交易訊號，防止技術指標的「盲目接刀」。


* [ ] **Model Context Protocol (MCP) 伺服器對接**：
* 將 Nexus Seeker 的核心量化模組打包為標準的 MCP Tools。
* 允許外部的 AI Agent 透過自然語言直接調用計算資源。


* [ ] **券商 API 深度整合 (Interactive Brokers)**：
* 透過 IB Gateway 串接真實交易環境，從「推播訊號、手動下單」進化為全自動化閉環系統。



---

*Built with ❤️ by an Engineer for Quantitative Freedom.*

```

```