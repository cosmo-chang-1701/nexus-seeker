# Analyst Agent 報告排程與盤前盤後智能簡報

## 1. 功能總覽

`cogs/analyst_agent.py` 負責排程並派送以下報告族群：

- 宏觀掃描（內建 14 天前瞻財報風險視窗）
- 盤前財報／估值調整報告
- 盤後綜合摘要
- 板塊資金流向／輪動報告
- 隔日策略報告

Analyst Agent 是與「Watchlist 15 分鐘雷達心跳」（見 [`../architecture/01_dual_watchlist_pipelines.md`](../architecture/01_dual_watchlist_pipelines.md)）完全獨立的報告族群，兩者共用部分底層量化引擎但排程與觸發時機不同，切勿混為一談。

## 2. 盤前財報與估值調整：正式路徑與孤兒路徑

### 2.1 正式生產路徑（實際發送）
`AnalystAgent.pre_market_loop` → `dispatch_pre_market_briefing()`（`cogs/analyst_agent.py`），實際送出 `🌅 報告：盤前綜合宏觀與自選股` DM：

- **14 天前瞻財報風險視窗**：盤前財報掃描視窗固定為 **14 天**（`warning_days = 14`），直接併入宏觀報告。標的來源為每位使用者持倉＋自選股的聯集，透過 `calendar_service.get_symbol_earnings_batch()` 解析，過濾至 `0 <= days_left <= 14`，並由 `TradingService.get_pre_market_alerts_data()`（`services/trading_service.py`）依到期日升冪排序——此步驟回傳**完整、無上限**的警示清單，不做 triage／深度掃描篩選。
- **無顯示上限**：`build_pre_market_briefing_embed()`（`cogs/embed_builders/order_embeds.py`）將財報雷達渲染為每 **10 檔標的**一個 Discord embed field（維持在 Discord 1024 字元欄位值上限內），超過一個 chunk 時在欄位名稱標註 `(第 X/Y 批)`。所有符合條件的標的均會顯示，不會被靜默捨棄。

### 2.2 孤兒路徑（未接入生產排程，僅供測試）
`market_analysis/analyst_runners/earnings_runner.py::run_premarket_earnings()` 實作了一套更重量級的管線（標的分析數上限 10 檔、`days_left <= 2` 深/淺掃描分流、透過 `SentimentEngine.calculate_pcr` 計算 PCR、公司檔案解析、LLM 上下文剪裁、`asyncio.Semaphore(3)` 速率限制），輸出獨立的 `create_earnings_report_embed()`（`📊 Nexus Seeker 盤前財報與估值調整`）。`AnalystAgent.run_premarket_earnings` 只是它的一層薄包裝，但**目前排程、slash command 或任何 `dispatch_*` 方法都沒有呼叫它**——只有 `tests/unit/test_analyst_agent.py` 會執行到。應視為未接線的遺留程式碼，而非生產環境實際的財報雷達行為，除非未來重新接回某條實際的派送路徑。

## 3. 系統提示詞規範與數學交叉驗證

`generate_analyst_report` 的系統提示詞強制：

- 100% 流暢、金融等級的繁體中文，使用台灣期權市場慣用術語（`選擇權` = Options、`履約價` = Strike、`權利金` = Premium、`價差期權/價差策略` = Spreads、`隱含波動率` = Implied Volatility、`乖離率` = Deviation）。
- 明確的 Markdown 標題結構：
  1. 📊 多空大盤交叉驗證解讀
  2. ⚠️ 潛在陷阱與風險提示
  3. 🛡️ 高勝率交易策略推薦
- 數學交叉驗證規則：
  - **IV 泡沫驗證**：若技術面過熱（乖離率 > 10% 或 RSI > 65）同時 `IV Rank > 90%` 且 `days_to_earnings > 20`，標記為人為 IV 泡沫，避免建議單腿買方期權。
  - **市場背離驗證**：若 `Option Skew` 為負但 `PCR > 1.5`，需解釋此背離為散戶動能 vs. 機構避險的分歧，而非單純看多訊號。

報告派送使用 `split_embed_by_fields()`，大型多段報告會被拆分成**每個欄位區塊一則訊息**，以避免觸及 Discord embed／content 長度限制。

## 4. 盤後綜合風險結算與現貨部位整合

盤後風險報告（`post_market_intelligence`）由 `cogs/analyst_agent.py` 產生、`build_post_market_intelligence_embed()`（`cogs/embed_builders/order_embeds.py`）格式化，整合完整量化投組與 LLM 分析：

1. **現貨資產（`HOLDING`）完整整合**：
   - `database.get_all_portfolio()` 同時查詢 `TRADE`（期權）與 `HOLDING`（現貨）兩類資產。
   - 現貨持倉被標準化為 `PERPETUAL` 合約，以 `avg_cost` 作為履約價、Delta 乘數固定為 `1.0`。
   - `PortfolioStatusOrchestrator` 計算現貨未實現損益與 Beta 加權 Delta（`quantity * beta * (spot_price / spy_price)`），累加進 `total_beta_delta`，確保即使是純現貨帳戶也一定會產出 **🌐 宏觀風險 (Macro Risks)** 區塊。
2. **Target Center 2.0 視覺階層與精簡 ANSI 儀表板**：
   - **標頭儀表板**：整合於高密度 ANSI 區塊內，含 UTC+8 時間戳、結算狀態、財務生存跑道天數，以及未實現損益的語意化 ANSI 配色（綠 `+$`、紅 `-$`、中性 `$0.00`）。
   - **持倉樹狀卡片**：使用標準 ` ├─ ` 與 ` └─ ` 樹狀縮排，區分現貨（`💎 NVDA 現貨 HOLDING | 10 股`）與期權（`🎯 AAPL 期權 BTO CALL | 1 口`）標籤，正確計算現貨 1 倍與期權 100 倍的現金佔用。
   - **可操作的空手狀態**：對於 100% 現金投組，渲染具指引性的防禦確認卡片，附零下行風險確認與 `/x` 雷達掃描建議。
   - **對稱對沖歸因**：以獨立 ANSI 配色分別呈現 Alpha 選股損益 vs 對沖損益、對沖比率、有效性百分比與健康狀態評估（`OPTIMAL (對沖結構健康)`）。
   - **板塊輪動聚焦矩陣**：將 11 大廣義市場板塊分組為 `🔥 領漲板塊 (Top Inflows)` 與 `❄️ 領跌板塊 (Top Outflows)`，優化行動裝置可讀性。

## 5. 核心程式碼檔案路徑關聯

- `nexus_core/cogs/analyst_agent.py`：`pre_market_loop`, `post_market_loop`, `dispatch_pre_market_briefing()`, `run_premarket_earnings`（孤兒路徑薄包裝）
- `nexus_core/market_analysis/analyst_runners/earnings_runner.py`：`run_premarket_earnings()`（未接線的重量級孤兒路徑）
- `nexus_core/services/trading_service.py`：`get_pre_market_alerts_data()`
- `nexus_core/cogs/embed_builders/order_embeds.py`：`build_pre_market_briefing_embed()`, `build_post_market_intelligence_embed()`
- `nexus_core/tests/unit/test_analyst_agent.py`：孤兒路徑目前唯一的呼叫方
