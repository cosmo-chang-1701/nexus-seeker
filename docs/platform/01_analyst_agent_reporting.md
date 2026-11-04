# Analyst Agent 報告排程與盤前盤後智能簡報

## 1. 功能總覽

`cogs/analyst_agent.py` 負責排程並派送以下報告族群：

- 宏觀掃描（內建 14 天前瞻財報風險視窗）
- 盤前財報／估值調整報告
- 盤後綜合摘要
- 板塊資金流向／輪動報告
- 隔日策略報告

Analyst Agent 是與「Watchlist 30 分鐘深度心跳」（見 [`../architecture/01_dual_watchlist_pipelines.md`](../architecture/01_dual_watchlist_pipelines.md)）完全獨立的報告族群，兩者共用部分底層量化引擎但排程與觸發時機不同，切勿混為一談。

## 2. 盤前財報與估值調整：正式路徑與孤兒路徑

### 2.1 正式生產路徑（實際發送）
`AnalystAgent.pre_market_loop` → `dispatch_pre_market_briefing()`（`cogs/analyst_agent.py`），實際送出 `🌅 報告：盤前綜合宏觀與自選股` DM：

- **14 天前瞻財報風險視窗**：盤前財報掃描視窗固定為 **14 天**（`warning_days = 14`），直接併入宏觀報告。標的來源為每位使用者持倉＋自選股的聯集，透過 `calendar_service.get_symbol_earnings_batch()` 解析，過濾至 `0 <= days_left <= 14`，並由 `TradingService.get_pre_market_alerts_data()`（`services/trading_service.py`）依到期日升冪排序——此步驟回傳**完整、無上限**的警示清單，不做 triage／深度掃描篩選。
- **無顯示上限**：`build_pre_market_briefing_embed()`（`cogs/embed_builders/order_embeds/pre_market_briefing.py`）將財報雷達渲染為每 **10 檔標的**一個 Discord embed field（維持在 Discord 1024 字元欄位值上限內），超過一個 chunk 時在欄位名稱標註 `(第 X/Y 批)`。所有符合條件的標的均會顯示，不會被靜默捨棄。

- **宏觀區塊（`macro_runner.fetch_macro_data()` / `build_macro_alerts()` / `build_macro_healthy_status()`）**：
  - **資料來源**：`DXY`、`^TNX`（10Y）、`^VIX` 走 yfinance（`call_yf`）；**2Y 殖利率與 10Y−2Y 利差**不抓網路，改讀 16:15 ET FRED 排程已存入 `macro_series_observation` 的 `DGS2`／`DGS10`（`database/macro_signal_log.load_observations`，經 `asyncio.to_thread`）。2Y 取 `available_date <= 今日` 的最新值並標註觀測日（`MM/DD`）；利差取 DGS10 − DGS2 **同一觀測日**的最新值。觀測日距今超過 `_FRED_YIELD_STALE_DAYS = 5` 個日曆天（`# PRE_CALIBRATION`）、DB 讀取例外或序列缺值時為 `None`，簡報以 `--` 顯示（讀取失敗記 warning，不中斷簡報）。`DGS10` 僅供記錄與此利差，不參與任何總經指標判定；不得再由 `tnx` 推算 2Y 或利差。
  - **部署後首次簡報**：`DGS10` 於本次改動才加入 `FRED_SERIES`，沒有回補，故部署後第一份盤前簡報的利差顯示 `--`，直到下一次 16:15 ET FRED 排程存入 DGS10 為止。
  - **五條警示規則**：（1）曲線深度倒掛：利差 < -0.2；（2）曲線接近解除倒掛：-0.1 ≤ 利差 ≤ 0.2 且 10Y 當日變動 < 0 bps（兩條皆含利差數值與觀測日，利差缺值時略過）；（3）10Y > 4.5% 且當日升幅 > 8 bps；（4）VIX > 20 且單日升幅 > 2.0；（5）DXY > 105。
  - **文字原則**：僅陳述已檢查的事實與風險（例如「高 Beta／高估值成長股波動可能放大」），**不得建議減碼、降低曝險或轉進防禦型資產**（使用者為長期持有，系統僅提供顧問資訊）；狀態文字只描述實際檢查過的數值（VIX 分級、10Y 水位與 bp 變動、利差與觀測日或「2Y 無資料」、DXY ≤ 103 溫和／103–105 偏強），指引為「未觸發總經警示，維持既有持有計畫」。利差缺值有兩種寫法：2Y 本身缺值寫「2Y 無資料」；2Y 有值但利差缺值寫「利差無資料（10Y 未入庫或與 2Y 無同日資料）」。2Y 觀測日標在表格列標籤 `US2Y MM/DD 2Y`，利差備註標的是利差的觀測日 `curve_date`（兩者可不同日）。
  - **降級規則**：標題 `[盤前數據未更新/降級模式]`（孤兒 `create_macro_scan_embed` 為 `[降級模式]`）只由 `dxy`、`tnx`、`vix` 缺值觸發；僅 2Y／利差缺失時只在該列顯示 `--`，不降級、也不改變宏觀狀態判讀。

- **第二則 DM：宏觀逃頂簡報**：同一個 `dispatch_pre_market_briefing()` 對每位使用者另送 `🧭 宏觀逃頂：總經流動性狀態與逃頂評分 (Macro Escape Matrix)`（`strategy_runner.run_fomc_escape_window_analysis` → `create_fomc_escape_window_embed`，通知類型 `briefing_pre_market`）。內容為四因子流動性狀態（收縮／寬鬆／均衡，因子 4 取自 `get_market_regime()`，與進場閘門同源）與逃頂綜合評分，並依逃頂分級給單一動作指引（NORMAL 維持持有；WATCH／ELEVATED／CRITICAL 考慮 SPY 保護性 Put；UNKNOWN 資料不足）。不再有「逃頂窗口」與位移天數，也不含任何減倉建議；規格見 [`../macro_sentiment/01_macro_escape_top_matrix.md`](../macro_sentiment/01_macro_escape_top_matrix.md) §2.8。

### 2.2 孤兒路徑（未接入生產排程，僅供測試）
`market_analysis/analyst_runners/earnings_runner.py::run_premarket_earnings()` 實作了一套更重量級的管線（標的分析數上限 10 檔、`days_left <= 2` 深/淺掃描分流、透過 `SentimentEngine.calculate_pcr` 計算 PCR、公司檔案解析、LLM 上下文剪裁、`asyncio.Semaphore(3)` 速率限制），輸出獨立的 `create_earnings_report_embed()`（`📊 Nexus Seeker 盤前財報與估值調整`）。`AnalystAgent.run_premarket_earnings` 只是它的一層薄包裝，但**目前排程、slash command 或任何 `dispatch_*` 方法都沒有呼叫它**——只有 `tests/unit/test_analyst_agent.py` 會執行到。應視為未接線的遺留程式碼，而非生產環境實際的財報雷達行為，除非未來重新接回某條實際的派送路徑。

## 3. 系統提示詞規範與數學交叉驗證

`generate_analyst_report` 的系統提示詞強制：

- 100% 流暢、金融等級的繁體中文，使用台灣期權市場慣用術語（`選擇權` = Options、`履約價` = Strike、`權利金` = Premium、`價差期權/價差策略` = Spreads、`隱含波動率` = Implied Volatility、`乖離率` = Deviation）。
- 明確的 Markdown 標題結構：
  1. 📊 多空大盤交叉驗證解讀
  2. ⚠️ 潛在陷阱與風險提示
  3. 🛡️ 高勝率交易策略推薦
- 數學交叉驗證規則（**條件式**：僅在對應欄位存在且非 None 時才套用，缺欄位不得推論）：
  - **IV 泡沫驗證**：若技術面過熱（乖離率 > 10% 或 RSI > 65）同時 `IV Rank > 90%` 且 `days_to_earnings > 20`，標記為人為 IV 泡沫，避免建議單腿買方期權。
  - **市場背離驗證**：若 `Option Skew` 為負但 `PCR > 1.5`，需解釋此背離為散戶動能 vs. 機構避險的分歧，而非單純看多訊號。

報告派送使用 `split_embed_by_fields()`，大型多段報告會被拆分成**每個欄位區塊一則訊息**，以避免觸及 Discord embed／content 長度限制。

## 4. 盤後綜合風險結算與現貨部位整合

盤後風險報告（`post_market_intelligence`）由 `cogs/analyst_agent.py` 產生、`build_post_market_intelligence_embed()`（`cogs/embed_builders/order_embeds/post_market_intelligence.py`）格式化，整合完整量化投組與 LLM 分析：

1. **現貨資產（`HOLDING`）完整整合**：
   - `database.get_all_portfolio()` 同時查詢 `TRADE`（期權）與 `HOLDING`（現貨）兩類資產。
   - 現貨持倉被標準化為 `PERPETUAL` 合約，以 `avg_cost` 作為履約價、Delta 乘數固定為 `1.0`。
   - `PortfolioStatusOrchestrator` 計算現貨未實現損益與 Beta 加權 Delta（`quantity * beta * (spot_price / spy_price)`），累加進 `total_beta_delta`，確保即使是純現貨帳戶也一定會產出 **🌐 宏觀風險 (Macro Risks)** 區塊。
2. **Target Center 2.0 視覺階層與精簡 ANSI 儀表板**：
   - **標頭儀表板**：整合於高密度 ANSI 區塊內，含 UTC+8 時間戳、結算狀態、財務生存跑道天數，以及未實現損益的語意化 ANSI 配色（綠 `+$`、紅 `-$`、中性 `$0.00`）。
   - **持倉樹狀卡片**：使用標準 ` ├─ ` 與 ` └─ ` 樹狀縮排，區分現貨（`💎 NVDA 現貨 HOLDING | 10 股`）與期權（`🎯 AAPL 期權 BTO CALL | 1 口`）標籤，正確計算現貨 1 倍與期權 100 倍的現金佔用。
   - **可操作的空手狀態**：對於 100% 現金投組，渲染具指引性的防禦確認卡片，附零下行風險確認與 `/x` 雷達掃描建議。
   - **結算失敗卡片**：`get_after_market_report_data()` 對讀取使用者設定失敗（含 `load_failed` 佔位 context）或結算計算失敗的使用者回傳 `{"report_failed": True}`（整批彙整失敗則全員視為失敗），`build_post_market_intelligence_embed(report_failed=True)` 渲染「持倉結算失敗」卡片取代空手卡片，且不帶對沖／下行欄位；`cogs/trading/after_market.py` 備援／dry-run 路徑同樣略過並計入 `users_failed`。
   - **對稱對沖歸因**：以獨立 ANSI 配色分別呈現 Alpha 選股損益 vs 對沖損益、對沖比率、有效性百分比與健康狀態評估（`OPTIMAL (對沖結構健康)`）。歸因範圍為**實單持倉（含現貨）、自建倉起累計**（`analyze_hedge_performance(uid, include_virtual=False, include_holdings=True)`），無法計價的部位數以 `unpriced_count` 標示。
   - **相關性與 Theta**：板塊相關性採 60 個交易日 Pearson 係數，共同交易日不足時顯示「資料不足，未評估」；Theta 依狀態分級（無 Theta 部位／淨買方時間價值流失／收益率 < 0.05% 過低／> 0.30% 過度收租／健康）。
   - **LLM `raw_data` 契約**（與嵌入同一次結算，由 `run_portfolio_status()` 提供 `MacroRiskMetrics` 與逐部位摘要）：`report_status`（`ok`／`failed`／`no_positions`）、`macro_snapshot`（`vix`、`vix_tier`、`spy_price`，價格 ≤ 0 為 None）、`pnl_attribution_since_entry`（`scope`、`total_net_pnl`、`alpha_selection_pnl`、`hedge_pnl`、`unpriced_count`）、`aggregate_risk_metrics`（`total_theta`、`total_beta_delta`、`net_delta_exposure_pct`、`margin_heat_pct`、`total_margin_used`、`withdrawal_runway`）、`positions`（至多 20 筆：`symbol`、`type`、`direction`、`quantity`、`dte`、`iv_pct`、`iv_rank_pct`、`pnl_pct`）、`sectors`、`poly_events`、`spy_max_pain`。
   - **Vega 單位**：報告 Vega 為未加權的「$ / IV +1 點」，Vanna 仍為 Beta 加權（顯示 ÷100）。
   - **板塊輪動聚焦矩陣**：將 11 大廣義市場板塊分組為 `🔥 領漲板塊 (Top Inflows)` 與 `❄️ 領跌板塊 (Top Outflows)`，優化行動裝置可讀性。

## 5. 核心程式碼檔案路徑關聯

- `nexus_core/cogs/analyst_agent.py`：`pre_market_loop`, `post_market_loop`, `dispatch_pre_market_briefing()`, `run_premarket_earnings`（孤兒路徑薄包裝）
- `nexus_core/market_analysis/analyst_runners/earnings_runner.py`：`run_premarket_earnings()`（未接線的重量級孤兒路徑）
- `nexus_core/services/trading_service.py`：`get_pre_market_alerts_data()`
- `nexus_core/cogs/embed_builders/order_embeds/pre_market_briefing.py`：`build_pre_market_briefing_embed()`
- `nexus_core/cogs/embed_builders/order_embeds/post_market_intelligence.py`：`build_post_market_intelligence_embed()`
- `nexus_core/tests/unit/test_analyst_agent.py`：孤兒路徑目前唯一的呼叫方
