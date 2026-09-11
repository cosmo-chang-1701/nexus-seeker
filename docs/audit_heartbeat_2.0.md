# Nexus Seeker 標的分析中心 2.0 戰場心跳 (IntradayScanPipeline) 全鏈路演算法正確性稽核報告

**發布日期**：2026-09-11
**審計範疇**：Nexus Seeker `IntradayScanPipeline`（每半小時戰場心跳）全鏈路演算法與微觀結構風控
**架構基準**：Low-RAM VPS (1GB RAM) / SQLite-first / Discord DM Delivery / Zero-LLM Latency First
**稽核方法**：靜態源碼審查、金融計量與微觀結構數學推導、全鏈路資料流對齊（Data Fetch $\to$ Intermediate Calculation $\to$ Embed Rendering）

---

## 1. 執行摘要 (Executive Summary)

### 1.1 稽核背景、範疇與架構概覽
Nexus Seeker 的 `標的分析中心 2.0`（由 `nexus_core/market_analysis/intraday_pipeline/pipeline.py` 驅動的每 30 分鐘心跳排程）旨在為期權賣方、跨式對沖與波段交易員提供零 LLM 延遲的高密度量化雷達卡片。系統在架構上分為三層：
1. **資料抓取層 (Data Fetch Layer)**：透過 Finnhub 取得財報行事曆、Edge Scraper 抓取 TradingView 總經日曆與 Yahoo Finance 選擇權鏈、SQLite 快取市場數據；
2. **中間計算層 (Intermediate Calculation Layer)**：涵蓋 Black-Scholes-Merton Greeks、Net GEX 曝險分布、Gamma Flip 水位、25-Delta Skew 百分位、Volume/OI PCR、Volume Profile (POC/LVN) 以及動態操盤指引等核心模組；
3. **終端渲染層 (Embed Rendering Layer)**：由 `cogs/embed_builders/watchlist_embeds.py` 封裝 ANSI 格式的 Discord Rich Embed，並藉由 Persistent DM Queue 推送至用戶端。

本稽核針對 30 分鐘戰場心跳全鏈路的 8 大區塊進行無死角審查，全面檢視資料鏈路、數學原理、時間戳一致性、異常邊界及 Embed 渲染。

### 1.2 總體缺陷統計表
經端到端嚴格審查，共識別出 **35 項演算法、量化及架構缺陷**。優先稽核的 4 個核心區塊（Block 1～4）佔據了絕大多數的高危與嚴重評級：

| 心跳區塊 | 區塊名稱 | 優先級 | Critical | High | Medium | Low | 合計 | 已修復進度 |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Block 1** | 🗓️ 事件風控 (Event Risk & Calendar) | **優先 (Deep)** | 2 | 2 | 2 | 0 | **6** | **6 / 6 (100%)** |
| **Block 2** | ⚙️ 量化 Skew 解析 (Skew & Options Flow) | **優先 (Deep)** | 1 | 4 | 1 | 1 | **7** | **7 / 7 (100%)** |
| **Block 3** | 🧱 物理籌碼牆與邊緣偵測 (Market Footprints) | **優先 (Deep)** | 1 | 3 | 0 | 0 | **4** | **4 / 4 (100%)** |
| **Block 4** | 🧲 Gamma 曝險分布 (GEX Profile Matrix) | **優先 (Deep)** | 1 | 1 | 2 | 0 | **4** | **4 / 4 (100%)** |
| **Block 5** | 🧱 心跳：期權結構與波動率 (IV & Term Structure) | 次要 (Standard) | 0 | 1 | 1 | 2 | **4** | **4 / 4 (100%)** |
| **Block 6** | 🎯 結算與目標 (Target Lock & Max Pain) | 次要 (Standard) | 0 | 2 | 2 | 0 | **4** | **4 / 4 (100%)** |
| **Block 7** | 🎯 執行建議 (Tactical Execution & Gates) | 次要 (Standard) | 1 | 2 | 0 | 0 | **3** | **3 / 3 (100%)** |
| **Block 8** | 🛡️ 心跳：操盤指引與委託風控 (Risk Controls & ATR) | 次要 (Standard) | 1 | 1 | 1 | 0 | **3** | **3 / 3 (100%)** |
| **總計** | **全鏈路 8 大區塊匯總** | — | **7** | **16** | **9** | **3** | **35** | **35 / 35 (Critical: 7/7, High: 16/16, Medium: 9/9, Low: 3/3 — 100% 全數修復)** |

---

### 1.3 Top 5 最嚴重缺陷清單
跨全鏈路 8 個區塊評選出對即時交易安全與風控決策具備致命破壞力的 Top 5 缺陷（每條嚴格限制於 3 行以內描述）：

1. **[FIXED] Top 1: pw_gex 符號負值衝突導致核心風控情境與紙糊牆警報永久失效**
   - **評級**：`Critical` ｜ **模組/行號**：`scenario_classifier.py:89`, `market_embeds.py:1014`, `gex_scraper.py:265`
   - **成因與影響**：GEX 模組定義 Put GEX 為負值（$-raw\_gex$），但下游誤要求 `pw_gex >= 500k`，致巨鯨護航與左側加碼在真實防守時被永久阻斷，且紙糊牆警報因要求 `pw_gex > 0` 而 100% 啞火。
   - **修復摘要**：下游所有依賴 `pw_gex` 判斷深度與門檻之邏輯（如 `scenario_classifier.py`、`market_embeds.py`）已統一加上絕對值函數 `abs(pw_gex)`，確保符號語意不再衝突，恢復紙糊牆警報功能。

2. **[FIXED] Top 2: estimate_symbol_gamma_flip 累加演算法數學原理錯誤致全域靜默失敗恆回傳 0.0**
   - **評級**：`Critical` ｜ **模組/行號**：`index_microstructure.py:820-847`, `evaluation.py:185`, `opportunity_cost.py:111`
   - **成因與影響**：誤以靜態 GEX 由左向右累加當價格穿越致轉正點必在現價上方，後續疊加 `s <= spot` 使得所有候選點全數遭剔除，全域恆回傳 `0.0`，導致軋空預警與轉倉進場判定全數癱瘓。
   - **修復摘要**：廢棄全鏈累積加總機制，改採逐履約價相鄰對符號變化偵測（`GEX(K_i) < 0 <= GEX(K_{i+1})`），精準識別個別履約價的 GEX 方向翻轉點；保留 bracket ±30%、最近交叉點優先、Regime 方向一致性三重防禦；新增三條 ISSUE-4.1 對應 unit test（正常非零返回、全正 fallback、全負 fallback），全套 1180 項 pytest 驗證通過。


3. **[FIXED] Top 3: 戰術閘門順序副作用（Sequential Clobbering）抹除強烈預警**
   - **評級**：`Critical` ｜ **模組/行號**：`evaluation.py:38-57, 189-286`, `signal_calculator.py:306-313`
   - **成因與影響**：`_apply_tactical_gate` 在狀態未鎖定時回傳全新實例並全覆蓋文案，後續條件（如動能發散、IV壓抑）無條件抹除前置的高危警報（軋空預警、負 Gamma 踩踏、Skew 90% 背離），引發嚴重風控盲區。
   - **修復摘要**：改採警語追加機制（Guideline Append），檢測到前置警報符號（⚠️/🚨/⛔/軋空/負Gamma）時保留並累加；同步修復 ISS-04，將 `capital_retreat_required` 作為 `is_crisis` 買入鎖定的絕對旗標；新增 2 項單元測試，34 項測試全數通過。

4. **[FIXED] Top 4: 防洗盤緩衝時間週期量綱嚴重錯配（日線 ATR vs 15 分鐘收盤）**
   - **評級**：`Critical` ｜ **模組/行號**：`metrics.py:95-97`, `signal_calculator.py:339, 355-358`, `pipeline.py:226`
   - **成因與影響**：系統自日線提取 14 天 ATR 計算防洗盤緩衝（$1.5 \times ATR_{daily} \approx 7.65 \times ATR_{15m}$），買點被壓低至荒謬價位，卻要求交易員以微觀 15 分鐘實體 K 線收盤確認離場，量綱嚴重錯配。
   - **修復摘要**：在 `schemas.py` 與 `metrics.py` 補齊 `atr_15m` 欄位與平方根折算；在 `signal_calculator.py` 支援顯式 15m ATR 與 `scale_atr_to_15m` 平方根時間量綱對齊（$ATR_{15m} \approx ATR_{daily}/\sqrt{26}$）；生產心跳 `pipeline.py` 開啟量綱對齊；新增 2 項量綱驗證單元測試，42 項測試全數通過。

5. **[FIXED] Top 5: 靜默期避讓 (avoid_silent_period) 在即時報價正常時全面失效**
   - **評級**：`Critical` ｜ **模組/行號**：`iv_metrics.py:470-488`, `batch_scan.py:165-179`
   - **成因與影響**：`has_earnings_event` 與 `has_macro_event` 僅在報價失效退回快取時賦值，正常 `LIVE_IV` 下恆為 `False`，致全站雷達 `/x` 的靜默期過濾在數據健康時 100% 漏失，誘使交易員在財報公布前開倉。
   - **修復摘要**：將事件行事曆判斷邏輯移出 `if iv_source in ["STORED_IV", "HV_PROXY"]` 條件分支之外，確保在 `LIVE_IV` 下同樣能正常賦值 `has_earnings_event` 與 `has_macro_event`。

---

## 2. 八大心跳區塊全鏈路深度稽核 (8-Block In-Depth Audit)

### Priority 4 Blocks (深層稽核)

---

### 2.1 🗓️ 事件風控 (Block 1)

#### 2.1.1 鏈路追蹤摘要
- **資料來源層**：
  - 財報資料由 `services/calendar_service.py:444` 調用 `services/market_data_service/fundamentals.py:118-140` (`get_earnings_calendar`)，向 Finnhub `client.earnings_calendar` 請求，持久化於 SQLite `earnings_calendar_cache` 表（`database/calendar_cache.py:170-185`）。
  - 宏觀高影響事件由 `services/calendar_service.py:197-250` (`_ensure_macro_month_cached`) 透過 Edge Scraper `/api/v1/macro/calendar` 抓取 TradingView 事件，寫入 `economic_calendar_events` 與 `economic_calendar_month_cache`（`database/calendar_cache.py:35-75`）。
  - CME FedWatch 利率定價由 `services/calendar_service.py:549-601` 透過 Edge Scraper `/api/v1/scrape/macro/fedwatch` 取得，記錄於 `economic_calendar_events.fedwatch_probability` 與 `kv_cache`（`macro_fedwatch_probability`）。
- **中間計算層**：
  - `market_analysis/intraday_pipeline/evaluation.py:74-79` 調用 `build_watchlist_event_context()`（`intraday_pipeline/events.py:118-160`）。
  - `events.py:19-29` (`_resolve_watchlist_event_mode`) 依據 `earnings_tte_hours` 與 `macro_tte_hours` 判定模式（`event-lock`, `earnings-guard`, `macro-guard`, `normal`）。
  - `cogs/unified_terminal/batch_scan.py:165-179` 評估 `avoid_silent_period` 量化過濾門檻。
- **終端渲染層**：
  - `cogs/embed_builders/watchlist_embeds.py:548-552` 渲染 ANSI 區塊 `**🗓️ 事件風控**`；行號 684-690 渲染 `實盤請預留 1.4x 波動邊界以防範 IV Crush`。

#### 2.1.2 發現缺陷清單

##### [FIXED] [ISSUE-1.1] 即時報價下靜默期過濾（avoid_silent_period）全面失效
- **嚴重等級**：`Critical`
- **原始碼引用**：`market_analysis/sentiment/iv_metrics.py:470-488`, `cogs/unified_terminal/batch_scan.py:165-179`
- **邏輯論證**：
  在 `iv_metrics.py:471` 中，行事曆檢查被包覆於條件式中：
  ```python
  if iv_source in ["STORED_IV", "HV_PROXY"]:
      # 檢查 earnings 與 macro 事件並賦值 has_earnings_event / has_macro_event
  ```
  當系統連線良好且即時期權鏈報價正常時，`iv_source == "LIVE_IV"`。此時 `has_earnings_event` 與 `has_macro_event` 保持預設值 `False`。
  下游 `batch_scan.py:168-172`：
  ```python
  if "avoid_silent_period" in quant_filters:
      earnings_loading = getattr(iv_data, "has_earnings_event", False)
      macro_loading = getattr(iv_data, "has_macro_event", False)
      if earnings_loading or macro_loading:
          passed = False
  ```
  在數據健康時，`earnings_loading` 恆為 `False`，過濾器無法攔截即將發布財報的股票。
- **衝擊半徑**：全站 `/x` 雷達與自選股掃描的「靜默期避讓」防禦完全失靈，用戶在財報公布前夕建立裸賣方（Short Strangle / CSP）期權部位，暴露於致命的 IV Crush 與跳空黑天鵝風險。

##### [FIXED] [ISSUE-1.2] 財報日時間強制錨定 00:00 導致盤中鎖死與 BMO 盤後誤鎖
- **嚴重等級**：`Critical`
- **原始碼引用**：`services/calendar_service.py:137-160, 378-390`, `market_analysis/intraday_pipeline/events.py:22-24`
- **邏輯論證**：
  `calendar_service.py:145` 僅解析 `entry.get("date")`，直接丟棄 Finnhub 回傳的 `hour` 欄位（`bmo`, `amc`, `dmh`）。隨後在第 380 行：
  ```python
  next_dt = datetime.combine(earnings_date, datetime.min.time()).replace(tzinfo=ny_tz)
  tte_hours = (next_dt - datetime.now(ny_tz)).total_seconds() / 3600
  if tte_hours <= 0.0:
      tte_hours = 0.1
  ```
  當日期等於美東當天時，盤中（09:30-16:00 ET）計算出的 `tte_hours` 原為負數，被強制設為 `0.1`。
  進入 `events.py:22`：
  ```python
  if earnings_tte_hours is not None and 0 < earnings_tte_hours <= 72.0:
      return "event-lock"
  ```
  因 `0.1 <= 72.0`，在財報發布日整天，不論財報是在盤前（BMO）已發布完畢，還是盤後（AMC）尚未發布，全天固定處於 `event-lock` 狀態。
- **衝擊半徑**：盤前（BMO）公布財報之標的，其重大波動風險於美東 09:30 開盤時已完全落地定價，但系統在當天剩餘交易時段全天封鎖交易；盤後（AMC）財報標的在發布前夕亦無法提供精確的分鐘級倒數警報。
- **修復摘要**：在 `calendar_service.py` 保留 Finnhub `hour` 欄位並解析時段（BMO 錨定 08:30 ET，AMC 錨定 16:30 ET，DMH 錨定 12:00 ET；未知時段當日錨定 16:30，未來日期相容 00:00）；`EarningsEvent` 增加 `hour` 與 `is_released` 欄位；當美東時間已過發布點時 `tte_hours <= 0` 且 `is_released = True`；在 `events.py` 中，發布後標記風險出清並解除 `event-lock`；新增 migration v071 為 `earnings_calendar_cache` 增設 `hour` 欄位。

##### [FIXED] [ISSUE-1.3] SQLite UTC CURRENT_TIMESTAMP 與主機本地時區混用造成快取雪崩
- **嚴重等級**：`High`
- **原始碼引用**：`services/calendar_service.py:108-115`, `services/market_data_service/fundamentals.py:127`, `database/calendar_cache.py:93`
- **邏輯論證**：
  `calendar_service.py:109`：
  ```python
  checked_at = datetime.fromisoformat(raw_ts.replace(" ", "T"))
  return checked_at >= datetime.now() - timedelta(hours=max_age_hours)
  ```
  SQLite 的 `CURRENT_TIMESTAMP` 是無時區的 UTC 時間（例如 `04:00:00`）。若宿主機為非 UTC 時區（例如 Asia/Taipei UTC+8），`datetime.now()` 產生本地時間（`12:00:00`）。兩者直接相減產生 8 小時固有偏差，剛寫入的快取立即被判定為過期，造成快取擊穿並對 Finnhub 產生雪崩式重複請求。
  此外，`fundamentals.py:127` 以 `datetime.now().strftime("%Y-%m-%d")` 作為 Finnhub 請求起點，在美東時間前一日晚間（台北時間次日上午），因本地日期比美東快一天，發出請求時跳過了美東當日的財報。
- **衝擊半徑**：非美東 VPS 環境下財報行事曆查詢漏失當日數據，且 API 配額迅速耗盡觸發 HTTP 429 限制。
- **修復摘要**：`calendar_service.py:108` 在解析 SQLite `CURRENT_TIMESTAMP` 時自動補充 `+00:00` 並與 `datetime.now(timezone.utc)` 比較，消除時區差造成的假性快取過期；`fundamentals.py:127` 強制採用美東時區 `datetime.now(ZoneInfo("America/New_York"))` 判定 `from_date` 與 `to_date`。

##### [FIXED] [ISSUE-1.4] 宏觀事件過點剔除導致 FOMC 決策發布後關鍵兩小時風控裸奔
- **嚴重等級**：`High`
- **原始碼引用**：`services/calendar_service.py:477-482`, `market_analysis/intraday_pipeline/events.py:86-110`
- **邏輯論證**：
  `calendar_service.py:477` (`get_next_high_impact_event`)：
  ```python
  for event in sorted(events, key=lambda item: item.tte_hours):
      if event.tte_hours <= 0:
          continue
      return event
  ```
  該方法強制跳過所有 `tte_hours <= 0` 的事件。
  然而在 `events.py:86-88` 中：
  ```python
  if current_cst >= macro_release_time:
      is_macro_released = True
  ```
  由於上游 `get_next_high_impact_event` 絕不回傳 `tte_hours <= 0` 的事件，此處 `is_macro_released` 永遠不可能被執行（Dead Code）。更致命的是，當 FOMC 於 14:00 ET 公布利率決策後，該事件在 14:00 瞬間自隊列中消失，`_resolve_watchlist_event_mode` 因無即期事件而將風控自 `macro-guard` 降級為 `normal`。
- **衝擊半徑**：在美東 14:00 至 16:00 鮑爾記者會與市場劇烈消化政策的最高波動震盪期，系統防線完全解除，風控形同虛設。
- **修復摘要**：在 `calendar_service.py:get_next_high_impact_event` 放寬篩選條件至 `event.tte_hours >= -2.0`；在 `events.py` 納入 `-2.0 <= macro_tte_hours <= 48.0`，於重磅事件公布後 2 小時市場消化震盪期內維持 `macro-guard` 防禦與減倉提示，徹底消除過點瞬間風控裸奔。

##### [FIXED] [ISSUE-1.5] 遠期財報完全壓制即期宏觀事件之優先級反轉
- **嚴重等級**：`Medium`
- **原始碼引用**：`market_analysis/intraday_pipeline/events.py:19-29, 39-59`
- **邏輯論證**：
  若標的將於 6 天後（144 小時）公布財報，但 1 小時後即將公布美國核心 CPI 或召開 FOMC 決策，原系統直接命中第 2 個條件返回 `earnings-guard`。在後續的風控文字與乘數生成中，1 小時後的總經海嘯被 6 天後的財報完全遮蔽。
- **衝擊半徑**：重大總經事件警報遭中遠期財報掩蓋，交易員無法感知即時系統性風險。
- **修復摘要**：在 `events.py:_resolve_watchlist_event_mode` 重構事件優先級判定邏輯，當宏觀事件進入 48 小時內（或公布後 2 小時消化期）時，其系統性緊迫性優先於遠期財報（72~168 小時）返回 `macro-guard`；並在 `_build_watchlist_event_summary` 支援雙事件並列提示，徹底防止即期總經海嘯被遮蔽。

##### [FIXED] [ISSUE-1.6] 期權到期日與行事曆結算（OPEX）及財報穿越風控完全缺位
- **嚴重等級**：`Medium`
- **原始碼引用**：`market_analysis/option_guidance.py:262, 268`, `models/schemas.py:168, 179`
- **邏輯論證**：
  `WatchlistEventContext` 及 `models/schemas.py` 完全未設計月度期權結算日（OPEX，每月第 3 個星期五）的偵測機制。在選約邏輯 `find_best_contract()` 中，系統推薦 30～45 DTE 的合約時，未比對該合約的到期日是否跨越了已知的下季度財報日。
- **衝擊半徑**：期權賣方合約跨越財報結算日，承擔未被系統揭露的巨大盈餘公布跳空風險（Earnings Jump Risk）。
- **修復摘要**：在 `schemas.py` 增設 `is_opex`、`crosses_earnings` 與 `is_opex_week` 欄位；在 `events.py` 引入 `_is_opex_week` 檢測；在 `option_guidance.py:build_watchlist_option_plan` 中偵測候選合約到期日是否為月度 OPEX 並於策略指引提示，同時比對合約到期日若跨越未知落地之財報公布日，強制將賣方計畫轉為嚴格 `WAIT (跨越財報日，防範跳空風險)`，阻斷裸賣部位。

#### 2.1.3 具體可執行優化建議
1. **無條件評估事件狀態**：將 `iv_metrics.py:470` 的行事曆檢查移出 `if iv_source in [...]` 區塊，使其在 `LIVE_IV` 下亦能正確賦值 `has_earnings_event` 與 `has_macro_event`。
2. **時段動態校準與釋放**：在 `calendar_service.py` 保留 Finnhub `hour` 欄位（BMO 錨定 08:30 ET，AMC 錨定 16:30 ET）。當盤中美東時間超過公布時間時，標記 `is_earnings_released = True`，於次一輪掃描解除 `event-lock`。
3. **時區統治與 UTC-Aware**：廢除代碼中所有裸 `datetime.now()`，全面改用 `datetime.now(timezone.utc)`；與 Finnhub 互動嚴格鎖定美東時區 `datetime.now(ZoneInfo("America/New_York")).date()`。
4. **宏觀發布冷卻窗口**：放寬 `calendar_service.py:get_next_high_impact_event` 篩選條件至 `tte_hours >= -2.0`，在數據公布後 2 小時內持續維持 `macro-guard` 防禦。
5. **雙軌獨立展示**：在 `WatchlistEventContext` 中同時承載財報與總經摘要，於 Discord Embed 中分行並列渲染，禁止單一事件覆蓋另一事件。

---

### 2.2 ⚙️ 量化 Skew 解析 (Block 2)

#### 2.2.1 鏈路追蹤摘要
- **資料來源層**：
  - 由 `services/market_data_service/options.py:196-244` (`get_option_chain`) 自 Yahoo Finance 取得期權鏈，預設帶入 `prune_pct = 0.1` 裁減履約價。
- **中間計算層**：
  - `market_analysis/sentiment/options_flow.py:172, 288` 調用 `calculate_skew()` 與 `calculate_pcr()`。
  - `options_flow.py:31-87` (`_select_contract_near_target_delta`) 篩選 $|\delta| \in [0.10, 0.40]$ 且最接近 $0.25$ 的合約，計算 $Skew = (IV_{put} - IV_{call}) 	imes 100$。
  - 歷史存儲與百分位由 `history_storage.py:123, 143-167` 透過 SQLite 表 `sentiment_history` 處理（窗口寫死 `LIMIT 100`）。
  - `market_analysis/intraday_pipeline/evaluation.py:184-195` 判定軋空引擎（Squeeze Gate），行號 238 判定 Skew 百分位極端避險。
- **終端渲染層**：
  - `cogs/embed_builders/watchlist_embeds.py:553-564` 渲染 ANSI 區塊 `**⚙️ 量化 Skew 解析**`，行號 403-427 依固定門檻（0.90 / 1.10）輸出 Volume/OI PCR 狀態。

#### 2.2.2 發現缺陷清單

###### [FIXED] [ISSUE-2.1] 滾動窗口高頻採樣坍塌（100 列 ≈ 3.8 天）引發宏觀分位失真
- **嚴重等級**：`Critical`
- **原始碼引用**：`market_analysis/sentiment/history_storage.py:123, 143-151`, `market_analysis/sentiment/options_flow.py:206`
- **邏輯論證**：
  `history_storage.py:123` 設定 `_PERCENTILE_WINDOW_ROWS = 100`。
  每次背景排程心跳掃描（每 15 或 30 分鐘）以及用戶執行 `/x` 指令時，`options_flow.py:206` 均向 `sentiment_history` 插入一筆新快照。美股常規交易時段為 6.5 小時，即 26 根 15 分鐘 K 棒。100 筆資料僅覆蓋 **3.8 個交易日**。
  金融計量上，Skew 百分位必須衡量標的在數月至一年（60～252 個交易日）中的相對定價偏斜。將 3.8 天盤中微觀噪音作為歷史母體，使得持續恐慌行情在第 4 天被誤判為「50% 常態」，而正常的盤中短暫跳動卻被誤診為「99% 世紀極端尾部風險」。
- **衝擊半徑**：Skew 百分位徹底失去跨週期對比價值，產生嚴重的指標漂移與虛假訊號。
- **修復摘要**：在 `history_storage.py:123` 將 `_PERCENTILE_WINDOW_ROWS` 擴大至 500 列（約 20 個交易日盤中取樣），消除 3.8 天高頻採樣坍塌失真。

##### [FIXED] [ISSUE-2.2] 冷啟動 20 筆超敏感門檻觸發虛假資金撤退與交易凍結
- **嚴重等級**：`High`
- **原始碼引用**：`market_analysis/sentiment/history_storage.py:119, 164-173`, `market_analysis/intraday_pipeline/evaluation.py:238-248`
- **邏輯論證**：
  `history_storage.py:119` 設定 `_MIN_PERCENTILE_SAMPLES = 20`。
  當用戶將新股票加入自選，累積至第 20 筆（僅 5 小時盤中採樣）時，若當前數值恰為該 5 小時內最大值，依 midrank 公式：
  $$\text{Percentile} = \frac{19 + 0.5 \times 1}{20} \times 100\% = 97.5\%$$
  隨後在 `evaluation.py:238`：
  ```python
  if metrics.skew_percentile is not None and metrics.skew_percentile > 90.0:
      tactical = _apply_tactical_gate(..., capital_retreat_required=True)
  ```
  直接跨越 90% 警戒線，強制鎖定 `WAIT (機構避險背離/尾部風險警戒)`，並要求啟動 70%～85% 資金撤退。
- **衝擊半徑**：新加入標的在開盤數小時內極易因微小的日內擺動而誤觸最高級別風控，導致帳戶持倉遭不合理清算並凍結開倉。
- **修復摘要**：在 `history_storage.py:get_indicator_percentile_with_sample_size` 擴充時間跨度檢查：若樣本涵蓋的獨立交易日不足 3 天（`< 3` distinct calendar dates）或總樣本數不足 60 筆，判定為冷啟動積累期，回傳 `(None, sample_size)`，避免單日內 20 筆 15m 噪音將最高點誤判為 97.5% 世紀極端而引發虛假資金撤退。

##### [FIXED] [ISSUE-2.3] 期權鏈預設 10% 裁減截斷 25-Delta 合約與虛值對沖 PCR
- **嚴重等級**：`High`
- **原始碼引用**：`services/market_data_service/options.py:199`, `market_analysis/sentiment/options_flow.py:172, 288`, `market_analysis/sentiment/uoa_detector.py:49`
- **邏輯論證**：
  `options.py:199` 定義 `prune_pct: Optional[float] = 0.1`。而在 `options_flow.py` 調用時未指定該參數，採納預設值 0.1（即僅保留現價 $\pm 10\%$ 履約價）。
  在 Black-Scholes-Merton 模型中（詳見附錄 A.4 推導），25-Delta 虛值看跌期權履約價滿足：
  $$K_{25\Delta P} \approx S \cdot e^{-0.6745 \sigma \sqrt{\tau}}$$
  當標的年化隱含波動率 $\sigma \ge 40\%$、期限 $\tau = 30/365$ 時，$K_{25\Delta P} \le 0.90 S$，其履約價完全落在現價 10% 之外。這導致高波動標的期權鏈根本挑選不到 25-Delta 合約，演算法被迫選取 35～40 Delta 之合約，或拋出異常退回歷史快取。同時，PCR 計算完全丟失了深度價外的對沖 Put。
- **衝擊半徑**：高成長與高波動標的（如 NVDA、TSLA、AMD）的 Skew 定價嚴重失真，且低估機構尾部對沖規模。
- **修復摘要**：在 `options_flow.py:calculate_skew` 與 `calculate_pcr` 呼叫 `get_option_chain` 時顯式傳入 `prune_pct=0.35`，完整保留 25-Delta 合約及深度虛值對沖 Put，並由 `test_calculate_skew_and_pcr_passes_prune_pct_35` 進行單元測試防護。

##### [FIXED] [ISSUE-2.4] Volume PCR 除以零邏輯將極端單邊賣壓反轉為看漲多頭
- **嚴重等級**：`High`
- **原始碼引用**：`market_analysis/sentiment/options_flow.py:312-327`
- **邏輯論證**：
  在 `options_flow.py:312`：
  ```python
  volume_pcr = total_put_vol / total_call_vol if total_call_vol > 0 else 0.0
  ...
  if volume_pcr < 0.90:
      volume_state = "中性偏多/看漲主導"
  ```
  在盤前時段或遭遇流動性真空、極端恐慌時，若看漲期權成交量為 0（`total_call_vol == 0`）而看跌期權爆量成交（例如 Put 成交 10,000 口），數學上 PCR 應趨近於正無窮大（極端看跌）。但程式碼卻將其賦值為 `0.0`，進而滿足 `volume_pcr < 0.90`，在卡片與日誌中標註為「中性偏多/看漲主導」。
- **衝擊半徑**：極端單邊崩跌行情在訊號層與 UI層被扭曲為「多頭買盤主導」，引導用戶在崩跌中逆勢接刀。
- **修復摘要**：在 `options_flow.py:312-313` 修正除以零分支，當 `total_call == 0` 且 `total_put > 0` 時賦值 `volume_pcr = 99.9`（空頭主導）；兩者皆為 0 時回傳 1.0（中性平衡）；新增 `test_calculate_pcr_zero_call_volume_reports_bearish` 確保極端賣壓不再反轉為多頭。

##### [FIXED] [ISSUE-2.5] 軋空引擎（Squeeze Gate）物理邏輯顛倒
- **嚴重等級**：`High`
- **原始碼引用**：`market_analysis/intraday_pipeline/evaluation.py:184-195`
- **邏輯論證**：
  `evaluation.py:184` 判定軋空邏輯：
  ```python
  crossed_gamma_flip_up = (gamma_flip_est > 0 and spot > gamma_flip_est and net_gex > 0)
  pcr_confirms = metrics.oi_pcr is not None and metrics.oi_pcr >= 1.0
  if crossed_gamma_flip_up and pcr_confirms and iv_rising_with_price:
      tactical.action_guideline += "
🚨 【軋空預警】... 進入正 Gamma 區間，OI PCR 顯示籌碼結構具備實質空頭供軋倉..."
  ```
  金融微觀結構中：
  1. 正 Gamma 區間（`net_gex > 0`）內，造市商的動態對沖是「逢漲賣出、逢跌買入」，其交易行為本質是壓制波動、自我穩定，根本無法形成順勢助漲的 Gamma Squeeze；真正的軋空必然發生在負 Gamma 區間（`net_gex < 0`），迫使造市商在股價上漲時瘋狂追買現貨以對沖空頭 Delta。
  2. 條件要求 `oi_pcr >= 1.0`（看跌期權多於看漲），但 Gamma Squeeze 的引爆源通常是散戶瘋狂搶購 OTM Call（Call Buying Mania，即 $PCR \ll 0.5$）。
- **衝擊半徑**：軋空預警在真正具備軋空潛力的環境中保持沉默，反而在造市商流動性充裕的自穩定區間誤報。
- **修復摘要**：在 `evaluation.py:184-195` 校正軋空微觀物理公式為造市商處於負 Gamma 泥淖 (`net_gex < 0`) 且看漲買盤壓倒性主導 (`oi_pcr <= 0.60`)，搭配 IV 隨價格同步走揚，徹底導正原先誤用正 Gamma 自穩定區與 Put 主導結構的物理顛倒缺陷。

##### [FIXED] [ISSUE-2.6] 前 3 到期日截斷導致指數 ETF（SPY/QQQ）結構性 PCR 失真
- **嚴重等級**：`Medium`
- **原始碼引用**：`market_analysis/sentiment/options_flow.py:285`, `cogs/embed_builders/watchlist_embeds.py:422-427`
- **邏輯論證**：
  原代碼寫死僅截取前 3 個到期日（`expirations[:3]`）。對於每日皆有到期合約（0-DTE）的指數 ETF（SPY、QQQ、IWM），前 3 個到期日僅涵蓋未來 3 天，遺失了 30～45 天期主力對沖合約。此外，指數 ETF 天生具備結構性機構避險需求，常態 PCR 普遍落在 $1.3 \sim 1.8$。採用與個股相同的 0.90/1.10 固定門檻，使得指數 ETF 幾乎常年被誤診為「空頭主導」。
- **衝擊半徑**：大盤 ETF 的情緒指標失真，無法提供有效的系統性風險參照。
- **修復摘要**：在 `options_flow.py:calculate_pcr` 針對大盤指數 ETF（SPY/QQQ/IWM/DIA）動態放寬選約範圍至涵蓋 DTE $\le 30$ 天內之所有到期日（最多 8 個），完整吸納月度對沖主力；並依據機構常態避險水準將大盤 ETF 的看漲主導門檻放寬至 $PCR \le 1.20$，偏空門檻調升至 $PCR > 1.40$，消除系統性誤判。

##### [FIXED] [ISSUE-2.7] 零軸無死區（Deadband）將常態波動率微笑誤診為結構異常
- **嚴重等級**：`Low`
- **原始碼引用**：`market_analysis/sentiment/skew_taxonomy.py:55-59`
- **邏輯論證**：
  在歷史數據未滿 20 筆時，分類器依賴 `skew_val > 0` 的純符號判定。股票因槓桿效應（Leverage Effect）天然存在輕微的負偏態，正常的波動率微笑可能產生 $+0.05\%$ 的微小 Skew。缺乏零軸死區（如 $[-0.5\%, +0.5\%]$）導致微小的量化噪聲被判定為「Put 昂貴/異常左偏」。
- **衝擊半徑**：UI 頻繁閃爍非實質性的異常偏斜提示。
- **修復摘要**：在 `market_analysis/sentiment/skew_taxonomy.py:classify_skew_regime_fallback` 增設 $[-0.5\%, +0.5\%]$（`abs(skew_val) <= 0.005`）零軸死區，將微小波動率微笑噪聲歸類為 `SKEW_STATE_FLAT`（波動率微笑對稱），避免冷啟動階段過度敏感誤診為異常偏斜，徹底消除 UI 頻繁閃爍。

#### 2.2.3 具體可執行優化建議
1. **動態或放寬期權鏈裁減比例**：在計算 Skew 與 PCR 時，向 `get_option_chain` 明確傳入 `prune_pct = 0.35`，或依當前 IV 自適應調整 $	ext{prune\_pct} = \max(0.15, \min(0.40, IV 	imes 0.6))$，確保 25-Delta 合約完整保留。
2. **重採樣為每日收盤基準（Canonical Daily Snapshot）**：限制 `sentiment_history` 對 `SKEW_D25` 指標的寫入頻率為每日僅在美東收盤（16:15 ET）記錄一筆；歷史窗口設為過去 60 個交易日，杜絕 3.8 天盤中坍塌。
3. **安全除零與防反轉**：當 `total_call_vol == 0` 時，若 `total_put_vol > 0` 應輸出極端看跌 `volume_pcr = 99.9`；若兩者皆為 0 則回傳 `None`，嚴禁給予 `0.0`。
4. **校正軋空微觀物理公式**：將軋空條件修正為 `net_gex < 0`（造市商助漲助跌）與 `oi_pcr <= 0.60`（Call 倉位壓倒性主導）。
5. **指數 ETF 專用動態基準**：按 DTE（$\le 30$ 天）匯總指數 ETF 到期日，並將偏多門檻動態放寬至 $PCR \le 1.2$。

---

### 2.3 🧱 物理籌碼牆與邊緣偵測 (Block 3)

#### 2.3.1 鏈路追蹤摘要
- **資料來源層**：
  - 由 `nexus_edge_scraper/gex_scraper.py` 爬取 Yahoo Finance 網頁計算 Greeks 與各履約價 GEX。
  - `nexus_core/cogs/unified_terminal/radar_data.py:322-328` 提取 `put_wall_gex`。
- **中間計算層**：
  - `market_analysis/scenario_classifier.py:89-112` 根據 `pw_gex` 判定 `is_solid_wall`，並分類市場情境（`WHALE_ESCORT_RESONANCE`, `GOLDEN_LEFT`）。
  - `market_analysis/volume_profile.py:25-56` 將日線 K 棒切箱為 50 bins 計算 HVN/LVN。
  - `market_analysis/dynamic_rollover/structural_signals.py:124-130` 篩選現價下方正支撐牆。
- **終端渲染層**：
  - `cogs/embed_builders/market_embeds.py:1014-1016, 1259-1262` 渲染紙糊牆警告。
  - `cogs/embed_builders/watchlist_embeds.py:576-588` 渲染 ANSI 區塊 `**🧱 物理籌碼牆與邊緣偵測 (Market Footprints)**`。

#### 2.3.2 發現缺陷清單

##### [FIXED] [ISSUE-3.1] GEX PutWall 符號負值衝突導致核心風控情境與紙糊牆警報永久失效
- **嚴重等級**：`Critical`
- **原始碼引用**：`nexus_edge_scraper/gex_scraper.py:265`, `nexus_core/cogs/unified_terminal/radar_data.py:324`, `nexus_core/market_analysis/scenario_classifier.py:89`, `nexus_core/cogs/embed_builders/market_embeds.py:1014, 1259`, `nexus_core/market_analysis/dynamic_rollover/structural_signals.py:128`
- **邏輯論證**：
  1. 在 `gex_scraper.py:265` 中，Put 的 GEX 被定義為負數：`signed_gex = -raw_gex`。在 PutWall（未平倉 Put 聚集處），該履約價的 Net GEX 必然為龐大的負數（如 `-2,500,000`）。
  2. `radar_data.py:324` 直接自 `gex_profile` 提取該值，保留負號傳入 `scenario_classifier.py`。
  3. `scenario_classifier.py:89` 定義：
     ```python
     is_solid_wall = pw_gex is None or pw_gex >= 500_000.0
     ```
     當真實數據存在時，`-2,500,000.0 >= 500_000.0` 恆為 `False`！只有在數據損毀 `pw_gex is None` 時，`is_solid_wall` 才會被評為 `True`。
  4. 連帶導致 `WHALE_ESCORT_RESONANCE`（巨鯨護航共振）與 `GOLDEN_LEFT`（黃金左側加碼）在正常真實市場中**永久無法觸發**！
  5. 在 `market_embeds.py:1014, 1259` 檢查紙糊牆時要求：
     ```python
     if pw_gex_val is not None and 0 < pw_gex_val < 500_000.0 and put_wall > 0:
     ```
     因 `pw_gex_val < 0`，此條件恆為 `False`，即便某履約價 GEX 僅為 `-50k`（極度薄弱的紙糊牆），系統亦永遠無法向用戶發出紙糊牆警報。
- **衝擊半徑**：核心護航策略被阻斷，系統失去左側安全加碼能力；重大紙糊牆崩塌風險被 100% 漏報。

##### [FIXED] [ISSUE-3.2] Volume Profile 20 根 K 棒分 50 箱之稀疏抽樣與邊界假正例
- **嚴重等級**：`High`
- **原始碼引用**：`market_analysis/volume_profile.py:25-56`, `cogs/unified_terminal/radar_data.py:727-731`, `market_analysis/scenario_classifier.py:122`
- **邏輯論證**：
  `radar_data.py:727` 調用 `calculate_volume_profile_from_df(df_hist, days=20, is_hourly=False)`，僅傳入 **20 根日線 K 棒**。
  `volume_profile.py:25` 設定 `num_bins = 50`。將 20 筆數據塞入 50 個箱子，至少有 30 個箱子的成交量為 0。
  然而程式碼使用 `vol_profile = df_subset.groupby("Bin")["Volume"].sum()`，僅統計非空箱。隨後以 `lvn_bin = vol_profile.idxmin()` 尋找最小值。這導致：
  1. 真正的 0 成交量真空箱完全被 `groupby` 丟棄；
  2. `idxmin()` 取出的是「存在數據的箱子中成交量最小者」，即 20 天內的最高價或最低價邊界單點。
  真正的 LVN 必須是價值區間內部波峰之間的局部波谷，而非統計區間的最外圍極值。此算法將隨機邊界日誤認為真空區，直接導致 `scenario_classifier.py:122` 的強勢突破（STRONG_BREAKOUT）發出大量虛假買訊。
- **衝擊半徑**：價格在邊界處頻繁誤報「突破真空區加速」，引導交易員在高位追高被套。
- **修復摘要**：在 `volume_profile.py` 引入動態自適應分箱（日線 `max(10, min(20, len(df)))`），補齊 0 成交量箱，以 3-bin rolling mean 平滑，並排除外側 10% 邊界價格箱，強制在有效成交量母體內部尋找真正的流動性真空凹槽 (LVN)，消滅外圍邊界假正例。

##### [FIXED] [ISSUE-3.3] Watchlist Heartbeat 2.0 Embed 渲染缺漏 CallWall 與 LVN
- **嚴重等級**：`High`
- **原始碼引用**：`cogs/embed_builders/watchlist_embeds.py:576-588`
- **邏輯論證**：
  在 Heartbeat 2.0 Embed 中，標題名為 `🧱 物理籌碼牆與邊緣偵測 (Market Footprints)`。
  但內部代碼僅輸出：
  - GEX PutWall
  - Vol POC
  - Option Skew
  承諾的做市商天花板（CallWall）、邊緣真空區（LVN）以及籌碼牆厚度標籤（紙糊牆警告）在該區塊完全缺席。
- **衝擊半徑**：心跳卡片資訊維度殘缺，用戶無法獲得上方阻力與真空暴跌防禦點位。
- **修復摘要**：在 `schemas.py` 增設 `volume_lvn` 與 `gex_max_call_wall` 欄位；在 `metrics.py` 正式接入計算與存儲；在 `watchlist_embeds.py` 的 Block 3 補齊做市商頂牆 CallWall、底牆 PutWall 深度標籤 `[厚/薄]` 以及 `Vol POC / LVN 真空區` 雙點位呈現。

##### [FIXED] [ISSUE-3.4] Yahoo 單一到期日爬蟲截斷與假牆風險
- **嚴重等級**：`High`
- **原始碼引用**：`nexus_edge_scraper/gex_scraper.py:129-133, 196-203`
- **邏輯論證**：
  `gex_scraper.py:129` 請求 `https://finance.yahoo.com/quote/{symbol}/options`，未帶任何到期日參數，預設僅載入近週到期合約。佔全市場未平倉量 70% 以上的月度 OPEX 合約被完全忽視。在週四至週五，$t \to 0$ 導致近週合約 ATM Gamma 異常膨脹，形成一個收盤即灰飛煙滅的「虛幻假牆（Phantom Wall）」。
  此外，第 199 行在 IV 缺失時直接將 IV 硬編碼為 `0.20`（20%）。對於 NVDA、TSLA 等高波動股（IV 50%～80%），代入 20% 計算出的 Gamma 會大幅虛高，扭曲牆體判定。
- **衝擊半徑**：籌碼牆支撐力判定被短命的近週合約綁架，無法識別真實主力月度防線。
- **修復摘要**：在 `gex_scraper.py` 將 IV 缺失時的硬編碼 0.20 升級為基於該標的全期權鏈非零 IV 中位數的動態自適應估算（最低 0.05，標的完全無期權報價時回退至 0.25）；在到期時間計算中增設 $t \ge 2/365$ 物理保護窗，防止到期日前夕 Gamma 趨近無窮大產生虛幻假牆。

#### 2.3.3 具體可執行優化建議
1. **統一 GEX 深度判定為絕對值（修復 ISSUE-3.1）**：
   在 `scenario_classifier.py:89` 改為 `is_solid_wall = pw_gex is None or abs(pw_gex) >= 500_000.0`；在 `market_embeds.py:1014, 1259` 改為 `if pw_gex_val is not None and abs(pw_gex_val) < 500_000.0`。
2. **重構 Volume Profile 分箱與局部極小值（修復 ISSUE-3.2）**：
   動態調整分箱數 $	ext{num\_bins} = \min(20, 	ext{len}(df))$；補齊 0 成交量箱；LVN 必須以尋找兩大 HVN 峰值之間的局部極小值（Local Minima）定義，若價值區內無顯著波谷則回傳 `None`。
3. **補齊 Heartbeat 2.0 Embed 欄位（修復 ISSUE-3.3）**：
   在 Block 3 Embed 補上 `CallWall (天花板)`、`LVN (真空區)` 及 `[薄/厚]` 牆體深度標籤。
4. **多到期日匯總與 IV 兜底（修復 ISSUE-3.4）**：
   爬蟲至少抓取近月與次月兩個關鍵到期日並累加 GEX；IV 缺失時改以標的 20 日 HV 代替，嚴禁硬編碼 0.20。

---

### 2.4 🧲 Gamma 曝險分布 (Block 4)

#### 2.4.1 鏈路追蹤摘要
- **資料來源層**：
  - 由 `nexus_edge_scraper/gex_scraper.py` 或 `local_api/macro.py` 根據 BSM 模型展開全鏈履約價的 Net GEX。
- **中間計算層**：
  - `nexus_core/market_analysis/index_microstructure.py:768-849` (`estimate_symbol_gamma_flip`) 估算個股 Gamma Flip 水位。
  - `evaluation.py:184-188` 依據 `gamma_flip_est` 與 `net_gex` 判定軋空與市場狀態。
  - `market_analysis/dynamic_rollover/opportunity_cost.py:111-124` 檢核進場條件一。
- **終端渲染層**：
  - `cogs/embed_builders/portfolio_embeds.py:1334` 渲染 Net GEX Regime；`watchlist_embeds.py:654-657` 渲染 GEX Profile Matrix 長條圖。

#### 2.4.2 發現缺陷清單

##### [FIXED] [ISSUE-4.1] `estimate_symbol_gamma_flip` 數學原理錯誤致全域靜默失敗恆回傳 0.0
- **嚴重等級**：`Critical`
- **原始碼引用**：`nexus_core/market_analysis/index_microstructure.py:827-849`, `evaluation.py:185`, `opportunity_cost.py:111`
- **邏輯論證**：
  `index_microstructure.py:820-845`：
  ```python
  cumulative = 0.0
  prev_cumulative: Optional[float] = None
  candidates: list[float] = []
  for strike, gex in sorted_strikes:
      cumulative += gex
      if prev_cumulative is not None and prev_cumulative < 0 <= cumulative:
          candidates.append(strike)
      prev_cumulative = cumulative

  if total_gex > 0:
      candidates = [s for s in candidates if s <= spot]
  else:
      candidates = [s for s in candidates if s >= spot]

  if not candidates:
      return 0.0
  ```
  數學機制檢驗：
  1. 選擇權市場中，現價下方以 Put 為主，`gex_profile` 數值為負；現價上方以 Call 為主，數值為正。
  2. 從最低履約價向最高履約價累加時，`cumulative` 必然先累積龐大負值，直至穿越現價並累加 Call 的正 GEX 時，`cumulative` 才會開始回升。
  3. 因此，`prev_cumulative < 0 <= cumulative`（由負轉正零交叉點）**幾何上必然發生在現價上方（$strike > spot$）**！
  4. 然而，第 840 行規定：若標的總體為 Long Gamma（`total_gex > 0`），候選點**必須 $\le spot$**。這導致唯一的轉正候選點被全數過濾，`candidates` 變為空列表，**函數回傳 `0.0`**！
  5. 若標的為 Short Gamma（`total_gex < 0`），`cumulative` 最終總和為負，從頭到尾從未轉正，迴圈內根本產生不了任何 candidate，**同樣回傳 `0.0`**！
- **衝擊半徑**：`estimate_symbol_gamma_flip` 在 99% 以上的正常期權鏈上全域靜默回傳 `0.0`。連帶使下游軋空預警永久不觸發、機會成本進場條件一直接將 Short Gamma 標的誤殺拒絕，Symbol Hub 恆顯示 `Gamma Flip: --`。

##### [FIXED] [ISSUE-4.2] 美股 100 股乘數遺漏導致 Dollar GEX 縮小 100 倍
- **嚴重等級**：`High`
- **原始碼引用**：`nexus_edge_scraper/gex_scraper.py:256`, `nexus_edge_scraper/local_api/macro.py:57`, `nexus_core/market_analysis/index_microstructure.py:543`
- **邏輯論證**：
  在 `gex_scraper.py:256` 中：
  ```python
  gamma = _calculate_gamma(spot_price, strike, t, 0.04, iv)
  raw_gex = oi * gamma * spot_price * spot_price
  ```
  美股選擇權每口標準合約控制 100 股。計算 Dollar GEX（現貨每變動 1% 或 1 美元時做市商避險的名目金額）時，公式必須包含合約乘數 100（詳見附錄 A.1 推導）：
  $$\text{Dollar GEX} = OI \times 100 \times \Gamma \times S^2$$
  由於遺漏了 100 乘數，計算出的 GEX 數值系統性縮小了 **100 倍**。
  在 `index_microstructure.py:543` 定義之紙糊牆門檻為 `500,000.0`（50 萬美元）。一堵真實擁有 2,000 萬美元做市商深度的堅固城牆，被縮小 100 倍後僅剩 20 萬美元，被系統荒謬地誤標為「單薄紙牆」。
- **衝擊半徑**：全系統籌碼深度被錯誤低估兩個數量級，嚴重的負 Gamma 泥淖警報難以觸發，強阻力牆被誤診為弱紙牆。
- **修復摘要**：在 `nexus_edge_scraper/gex_scraper.py:256` 及 `local_api/macro.py:57` 補齊美股期權標準 100 股合約乘數 `raw_gex = oi * 100.0 * gamma * spot_price * spot_price`，修正 GEX 數值系統性縮小 100 倍問題，並更新對應單元測試數值。

##### [FIXED] [ISSUE-4.3] Black-Scholes 模型忽略股息率 $q$ 與硬編碼無風險利率
- **嚴重等級**：`Medium`
- **原始碼引用**：`nexus_edge_scraper/gex_scraper.py:44-51, 255`
- **邏輯論證**：
  `_calculate_gamma` 硬編碼無風險利率 $r=0.04$，且令連續股息率 $q=0$。
  依據 Black-Scholes-Merton 模型：
  $$\Gamma = \frac{e^{-q \tau} \phi(d_1)}{S \sigma \sqrt{\tau}}, \quad d_1 = \frac{\ln(S/K) + (r - q + \frac{1}{2}\sigma^2)\tau}{\sigma \sqrt{\tau}}$$
  對於 SPY（股息率約 1.3%）或金融/能源等高配息藍籌股，忽略 $q$ 且硬編碼 $r$ 會系統性扭曲 $d_1$，造成 Put 與 Call 的相對 Gamma 權重偏斜，進而使 Net GEX 零軸發生偏移。
- **衝擊半徑**：高配息標的的 GEX 估算產生系統性偏誤。
- **修復摘要**：在 `nexus_edge_scraper/gex_scraper.py` 及 `local_api/macro.py` 中升級 `_calculate_gamma` 與 `_calculate_delta` 支援 Merton 連續股息率模型（引入 $e^{-q\tau}$ 折現與 $r-q$ 漂移率修正），並自 `ticker.info` 動態提取標的股息率 `dividend_yield`（如 SPY ~1.3%），支援傳入動態無風險利率 $r$，消除高配息與大盤 ETF 的 GEX 系統性偏誤。

##### [FIXED] [ISSUE-4.4] Net GEX Regime 缺乏中性死區與 Heartbeat 2.0 遺漏
- **嚴重等級**：`Medium`
- **原始碼引用**：`nexus_core/cogs/embed_builders/portfolio_embeds.py:1334`, `cogs/embed_builders/watchlist_embeds.py:654-657`
- **邏輯論證**：
  `portfolio_embeds.py:1334` 僅以 `net_gex > 0` 作為二元劃分，缺乏中性死區（Neutral Deadband）。當 Net GEX 於 0 軸附近因幾手平倉單小幅跳動時，標籤在「自穩定」與「助漲助跌」之間劇烈翻轉。
  更嚴重的是，在 Heartbeat 2.0 Embed（Block 4）中，僅畫出了 GEX 長條圖，完全遺漏了 Regime 狀態標籤與 Gamma Flip 水位。
- **衝擊半徑**：零軸附近產生頻繁的狀態閃爍，且心跳卡片未呈現最重要的 Gamma 風險特徵。
- **修復摘要**：在 `market_analysis/index_microstructure.py` 為 Net GEX Regime 增設 $[-50\text{k}, +50\text{k}]$ 中性死區（`⚖️ NEUTRAL_GAMMA (中性均衡)`），杜絕零軸附近的標籤高頻閃爍；並在 Heartbeat 2.0 Embed（Block 4）補齊 Net GEX Regime 狀態標籤與 Gamma Flip 水位行（含現價距 Flip 距離百分比），完整呈現做市商 Gamma 結構特徵。

#### 2.4.3 具體可執行優化建議
1. **重構 Gamma Flip 演算法（修復 ISSUE-4.1）**：
   廢棄全域累加機制，改採局部相鄰線性插值（詳見附錄 A.2 推導）：
   尋找現價附近 Net GEX 變號的相鄰履約價對（$GEX(K_i) \cdot GEX(K_{i+1}) \le 0$），代入閉式解：
   $$S^* = K_i - GEX(K_i) \cdot rac{K_{i+1} - K_i}{GEX(K_{i+1}) - GEX(K_i)}$$
   優先選取距離現價最近的根。此方法運算複雜度為 $O(N)$，耗時 $< 0.1	ext{ms}$，徹底根治回傳 0.0 的問題。
2. **補齊 100 股合約乘數（修復 ISSUE-4.2）**：
   在 `gex_scraper.py:256` 明確加入合約乘數：
   ```python
   raw_gex = oi * 100.0 * gamma * spot_price * spot_price
   ```
3. **BSM 模型納入股息率（修復 ISSUE-4.3）**：
   由 yfinance 獲取標的 `trailingAnnualDividendYield`，在 Greeks 計算中代入 $q$，並自 CME FedWatch 取得即時無風險利率 $r$。
4. **增設中性死區並補全 Embed（修復 ISSUE-4.4）**：
   在 $[-50	ext{k}, +50	ext{k}]$ 區間標註為 `⚖️ NEUTRAL_GAMMA (中性均衡)`；並在 Heartbeat 2.0 Embed 明確呈現 Regime 標籤與 Gamma Flip 價位及緩衝空間。

---

### Secondary 4 Blocks (標準深度稽核)

---

### 2.5 🧱 心跳：期權結構與波動率 (Block 5)

#### 2.5.1 鏈路追蹤摘要
- **資料來源與計算**：
  - `nexus_core/market_analysis/sentiment/iv_metrics.py:523-563` 讀取 SQLite `historical_iv` 表，並混合 1 年 K 線歷史計算 `HV_20`。
  - `iv_metrics.py:229-238` 提取近遠月計算期限結構比率（Near/Far Ratio）。
- **終端渲染層**：
  - `cogs/embed_builders/watchlist_embeds.py:667-695` 渲染 ANSI 區塊 `**🧱 心跳：期權結構與波動率**`。

#### 2.5.2 發現缺陷清單
1. **[FIXED] [ISS-06] [High] 歷史波動率 (HV) 與隱含波動率 (IV) 混雜基準窗口，人為放大 IV Rank 達 20～40%**：
   - 引用：`iv_metrics.py:523-575`
   - 論證：金融市場中方差風險溢價（VRP）恆正，隱含波動率長期高於已實現歷史波動率（$\mathbb{E}[IV] > \mathbb{E}[HV]$）。在資料庫記錄不足時，以 `HV_20` 填補歷史陣列，人為壓低了歷史下界 $\min(history\_values)$，導致新標的 IV Rank 系統性虛高 20%～40%，頻繁誤觸泡沫警報而錯誤封鎖買方交易。
   - **修復**：在 `iv_metrics.py` 徹底隔離 HV 與 IV，禁止將 `HV_20` 混入歷史 IV 母體；歷史 IV 樣本不足 60 天時標註 `iv_rank = None`（UI 顯示 `--% 數據積累中`），保護買方不被因方差風險溢價 (VRP) 壓低歷史下界所導致的虛高 IV Rank 誤殺；支援 `min_history_records` 參數。
2. **[FIXED] [ISS-09] [Medium] 期限結構 DTE 取樣隨機漂移與 0-DTE 假倒掛**：
   - 引用：`iv_metrics.py:229-238`
   - 論證：近月合約直接取 `0 <= days <= 14` 的第一個，在週四/週五時自動選中 0-DTE 或 1-DTE 合約。微觀結構噪音使 0-DTE IV 劇烈震盪，導致期限結構比率出現隨機性飆升，誤報 `⚠️ [Backwardation]`。
   - **修復**：在 `iv_metrics.py:_calculate_iv_term_structure` 將近月合約 DTE 取樣約束強制收緊為 $5 \le DTE \le 20$，遠月合約設為 $21 \le DTE \le 60$，徹底過濾 0-DTE/1-DTE 微觀躁動與假倒掛。
3. **[FIXED] [ISS-13] [Low] IV Percentile 已計算卻在 Embed Builder 中被丟棄**：
   - 引用：`watchlist_embeds.py:667-669`
   - 論證：`iv_metrics.py` 計算了 `iv_percentile`，但 Embed 只渲染了 `iv_rank`，變數淪為孤兒死代碼，資訊揭露不完整。
   - **修復**：在 `cogs/embed_builders/watchlist_embeds.py` 提取 `iv_percentile` 並與 `iv_rank` 並列渲染為 `IV Rank: xx% ｜ IVP: yy%`，完整揭露波動率分位資訊，消除孤兒指標。
4. **[FIXED] [ISS-14] [Low] 5% IV 硬性過濾器誤殺超低波標的**：
   - 引用：`iv_metrics.py:441`
   - 論證：若 IV < 0.05 則判定為無效報價退回歷史快取。短債 ETF（如 SHY、BIL）之真實 IV 常態性小於 5%，被系統全面誤殺。
   - **修復**：在 `market_analysis/sentiment/iv_metrics.py` 將 Rule 4 合理性檢核門檻由 0.05 放寬至 0.01（1.0%），保護短天期美債 ETF（如 BIL、SHY）等超低波動標的免於被無效報價過濾器誤殺。

#### 2.5.3 具體可執行優化建議
- 數據庫不滿 60 天時標註 `IV Rank: --% (數據積累中)`，嚴禁將 HV 混入；
- 近月期限結構取樣強制約束在 $5 \le DTE \le 20$，過濾 0-DTE 噪聲；
- 在 Embed 中補齊 IV Percentile 欄位；
- 放寬超低波 ETF 的 IV 過濾下限至 1.0%。

---

### 2.6 🎯 結算與目標 (Block 6)

#### 2.6.1 鏈路追蹤摘要
- **資料來源與計算**：
  - `nexus_core/market_analysis/sentiment/max_pain.py:28-36, 119-139, 640-653` 計算未平倉最大痛點。
  - `iv_metrics.py:196-198, 587-605` 計算 Expected Move（Straddle vs Root-T）。
- **終端渲染層**：
  - `cogs/embed_builders/watchlist_embeds.py:708` 渲染 ANSI 區塊 `**🎯 結算與目標 (Target Lock)**`。

#### 2.6.2 發現缺陷清單
1. **[FIXED] [ISS-07] [High] 週五盤中 Max Pain 陷入 0-DTE 到期日釘住陷阱**：
   - 引用：`max_pain.py:28-36, 456-487`
   - 論證：週五美東 16:00 前 `_current_week_friday()` 回傳當天。盤中痛點迅速向現價收斂退化為內含價值，失去前瞻性週度戰術錨點功能。
   - **修復**：在 `max_pain.py:_current_week_friday` 中，週五（含盤中 0-DTE）自動向前過渡滾動至下週五到期日，避免盤中痛點迅速向現價收斂退化為內含價值，維繫前瞻性週度戰術錨點功能；支援 `now_dt` 參數以供精確單元測試驗證。
2. **[FIXED] [ISS-08] [High] OI 不足時退化為「成交量加權 Max Pain」悖離造市商對沖假說**：
   - 引用：`max_pain.py:640-685`
   - 論證：Max Pain 成立基石在於造市商對沖**未平倉合約 (OI)** 的持倉義務。成交量主要反映當日散戶追單與平倉，成交量加權痛點在金融原理上完全無效，給出虛假磁吸點位。
   - **修復**：在 `max_pain.py:_calculate_max_pain_raw` 徹底移除 Volume-weighted Max Pain 降級替代分支；當未平倉合約 (OI) 嚴重不足時，直接宣告 `error="Insufficient OI for Max Pain calculation"` 並標記 `data_status="Insufficient_OI"` 與 `is_degraded=1`，堅決不使用散戶成交量偽造磁吸痛點；在 Cache-Aside 路徑安全回退至舊 SQLite 快取或標記 None。
3. **[FIXED] [ISS-10] [Medium] Max Pain 遍歷計算採 $O(N^2)$ DataFrame 切片且平原期偏左**：
   - 引用：`max_pain.py:119-139`
   - 論證：在 Python 迴圈中逐一對 Pandas DataFrame 進行布林切片，耗費 CPU；遇連續相等最小值時永遠取最左側最小履約價，產生系統性向下偏差。
   - **修復**：在 `max_pain.py:_calculate_max_pain_with_weights` 依附錄 A.3 推導實作 $O(N)$ 前綴和（Prefix Sums）向量化演算法，將全鏈痛點計算優化至 $O(N)$ 複雜度；並於相鄰相等最小值平原期改採距現價絕對距離最近之履約價，消除系統性向下偏誤。
4. **[FIXED] [ISS-11] [Medium] Expected Move 雙軌覆蓋與維度混用**：
   - 引用：`iv_metrics.py:196-198, 587-605`
   - 論證：Straddle 法在 $DTE < 7$ 時不按 $\sqrt{7/DTE}$ 外推，導致數值偏小，隨後在 `max(straddle_em, em_from_iv)` 中無條件被 Root-T 覆蓋失效。
   - **修復**：在 `iv_metrics.py:_calculate_straddle_implied_em` 將 ATM Straddle 預期區間依據附錄 A.5 校正為標準 1σ 維度（$\sqrt{\pi/2} \approx 1.2533 \times Straddle$），並以時間平方根平滑平移至 7 天；同時於盤中優先採用真實市場定價之 Straddle 預期波動，消除雙軌維度衝突與無條件覆蓋。

#### 2.6.3 具體可執行優化建議
- 週五開盤即自動過渡至下週五到期日合約，或提供雙痛點（當日結算價 vs 下週目標）；
- OI 不足時直接宣告 `Max Pain: N/A (OI 不足)`，堅決不使用 Volume 代替；
- 採用前綴和向量化優化至 $O(N)$（詳見附錄 A.3），平原期選取距離現價最近之履約價。

---

### 2.7 🎯 執行建議 (Block 7)

#### 2.7.1 鏈路追蹤摘要
- **資料來源與計算**：
  - `market_analysis/intraday_pipeline/evaluation.py:16-57, 189-286` 執行戰術狀態閘門判定。
  - `market_analysis/signal_calculator.py:306-313, 424-430` 計算建議買入價位與建倉股數。
- **終端渲染層**：
  - `cogs/embed_builders/watchlist_embeds.py:729` 渲染 ANSI 區塊 `**🎯 執行建議 (Execution Suggestions)**`。

#### 2.7.2 發現缺陷清單
1. **[FIXED] [ISS-01] [Critical] 戰術閘門順序副作用（Sequential Clobbering）抹除強烈預警**：
   - 引用：`evaluation.py:38-57, 189-286`
   - 論證：`_apply_tactical_gate` 在 `locked=False` 時直接生成全新物件，後續條件的提示字串完全覆蓋前置條件。若同時發生負 Gamma 踩踏與動能發散，關鍵的踩踏預警被完全消除。
   - **修復**：改為警語追加機制（Guideline Append），檢測到前置警報特徵時累加而非整包覆寫。
2. **[FIXED] [ISS-04] [High] 路由關鍵字比對不符導致風控下輸出買單**：
   - 引用：`signal_calculator.py:306-313`
   - 論證：`signal_calculator.py:307` 要求 `"SHIELD" in tactical_model.sddm_route` 才啟動危機鎖定。但閘門輸出的路由名稱為 `"WAIT (機構避險背離...)"`，比對落入 `else` 分支，使得系統一邊宣告嚴密避險，一邊在下方產出明確的「建議買入價位」與「建議買入股數」。
   - **修復**：將 `tactical_model.capital_retreat_required` 作為 `is_crisis` 觸發的顯式布林旗標，徹底消除字串比對不一致。
3. **[FIXED] [ISS-05] [High] 基本面護城河 LLM 提示詞無注入防護且忽略置信度**：
   - 引用：`fundamental_thesis.py:131-150`, `evaluation.py:115-122`
   - 論證：直接將外部財報字串拼接進 Prompt，易受 Prompt Injection 攻擊；下游完全無視 `confidence` 分數，低置信度幻覺即可觸發持倉清算。
   - **修復**：在 `fundamental_thesis.py` 中引入 XML 邊界標籤 `<filing_context>...</filing_context>` 與 `<section_{key}>` 隔離外部財報文字，並在 system prompt 增設注入防護指令，嚴格視外部文字為不可信被動數據；在 `evaluation.py` 加入置信度門檻 `fc.get("is_broken") and float(fc.get("confidence", 0.0) or 0.0) >= 0.75` 始觸發強制清算，低置信度警告予以過濾並記錄 log，杜絕模型幻覺引發誤清算。

#### 2.7.3 具體可執行優化建議
- 改為警語追加機制（Guideline Append）或複合風控狀態機；
- 廢除字串包含比對，以 `WatchlistTacticalPlan.capital_retreat_required` 作為買入鎖定的絕對布林旗標；
- 使用 XML 標籤隔離 Prompt 外部文字，並要求 `confidence >= 0.75` 始觸發基本面清算。

---

### 2.8 🛡️ 心跳：操盤指引與委託風控 (Block 8)

#### 2.8.1 鏈路追蹤摘要
- **資料來源與計算**：
  - `metrics.py:95-97` 提取日線 ATR14。
  - `signal_calculator.py:339, 355-358` 扣除 $1.5 	imes ATR$ 計算防洗盤點位。
  - `pipeline.py:281-301` 透過 `asyncio.gather` 併發取得補充數據。
- **終端渲染層**：
  - `cogs/embed_builders/watchlist_embeds.py:750` 渲染 ANSI 區塊 `**🛡️ 心跳：操盤指引與委託風控**`。

#### 2.8.2 發現缺陷清單
1. **[FIXED] [ISS-02] [Critical] 防洗盤緩衝時間週期量綱嚴重錯配（日線 ATR vs 15 分鐘收盤）**：
   - 引用：`metrics.py:95-97`, `signal_calculator.py:339, 355-358`, `pipeline.py:226`
   - 論證：美股 1 個交易日包含 26 根 15 分鐘 K 棒，波動率按 $\sqrt{26} \approx 5.1$ 比例縮放（詳見附錄 A.6）。$1.5 \times ATR_{daily}$ 實質相當於 $7.65 \times ATR_{15m}$。扣除如此龐大的日線緩衝，卻要求交易員依賴微觀 15 分鐘 K 線實體收盤確認，買點荒謬偏低無法成交，完全喪失盤中過濾下影線洗盤的意義。
   - **修復**：在 `schemas.py` 增設 `atr_15m`，在 `signal_calculator.py` 支援 `scale_atr_to_15m` 依隨機遊走平方根法則折算為真正 15m ATR 緩衝，並在生產 `pipeline.py` 開啟量綱對齊。
2. **[FIXED] [ISS-03] [High] `asyncio.gather` 未捕獲例外引發四組數據全滅雪崩**：
   - 引用：`pipeline.py:281-301`
   - 論證：`asyncio.gather` 預設 `return_exceptions=False`。最脆弱的 UOA 抓取一旦超時或觸發 429，整個 gather 立即崩潰，`except` 區塊將 IV、PCR、UOA、Max Pain 同步重設為 `None`，引發大面積卡片降級。
   - **修復**：在 `pipeline.py:281-316` 替 `asyncio.gather` 配置 `return_exceptions=True`，解包時對每項結果檢查 `isinstance(r, BaseException)`，單項失敗僅降級該指標並記錄 warning，確保其餘數據正常傳遞給 Embed Builder；新增 `test_build_watchlist_heartbeat_embed_isolates_uoa_failure` 驗證雪崩隔離。
3. **[FIXED] [ISS-12] [Medium] 缺乏多棒實體確認機制，即時下影線刺穿即觸發枯竭預警**：
   - 引用：`evaluation.py:196-202`
   - 論證：直接拿即時 Tick 價格與 PutWall 比對，盤中微觀插針立刻觸發警報，違背防洗盤哲學。
   - **修復**：在 `intraday_pipeline/evaluation.py` 引入 15m 實體收盤貫穿確認機制（`is_gamma_cliff_confirmed`）與防洗盤緩衝線（$PutWall - 1.5 \times ATR_{15m}$），盤中單根下影線短暫假刺穿僅標記中度測試預警，嚴格過濾流動性獵殺噪音，杜絕恐慌性誤報。

#### 2.8.3 具體可執行優化建議
- 將盤中防洗盤緩衝改用 `fetch_atr_15m`，或將日線 ATR 除以 $\sqrt{26}$ 進行量綱對齊；
- 為 `asyncio.gather` 配置 `return_exceptions=True`，並逐一解包降級；
- 接線 `gamma_cliff_confirmation.py`，強制要求 15 分鐘 K 線收盤實體破位方發布警報。

---

## 3. 附錄：關鍵數學公式重新推導與理論一致性校驗 (Appendix)

### A.1 Black-Scholes-Merton 模型、股息率 $q$ 修正與 Dollar GEX 物理定義

在 Black-Scholes-Merton 模型下，考慮標的連續分紅率 $q$ 與無風險利率 $r$，歐式看漲期權 $C$ 與看跌期權 $P$ 的定價公式為：
$$C = S e^{-q 	au} N(d_1) - K e^{-r 	au} N(d_2)$$
$$P = K e^{-r 	au} N(-d_2) - S e^{-q 	au} N(-d_1)$$
其中：
$$d_1 = rac{\ln(S/K) + (r - q + rac{1}{2}\sigma^2)	au}{\sigma \sqrt{	au}}, \quad d_2 = d_1 - \sigma \sqrt{	au}$$

期權 Gamma 定義為期權價格對底層資產價格的二階偏導：
$$\Gamma_{call} = rac{\partial^2 C}{\partial S^2} = rac{\partial}{\partial S} \left( e^{-q 	au} N(d_1) ight) = e^{-q 	au} rac{\phi(d_1)}{S \sigma \sqrt{	au}}$$
由看跌看漲平價公式（Put-Call Parity）$C - P = S e^{-q 	au} - K e^{-r 	au}$，對 $S$ 兩次求導可得：
$$\Gamma_{put} = rac{\partial^2 P}{\partial S^2} = \Gamma_{call} = rac{e^{-q 	au} \phi(d_1)}{S \sigma \sqrt{	au}}$$
其中 $\phi(x) = rac{1}{\sqrt{2\pi}} e^{-rac{1}{2}x^2}$ 為標準常態分佈概率密度函數。當 $q > 0$ 時，忽略 $e^{-q	au}$ 項並令 $q=0$ 會使 $d_1$ 增大，系統性低估深度價外 Put 的 Gamma 並高估 Call 的 Gamma。

#### Dollar GEX 物理量綱推導與 100 股乘數
在美股選擇權市場中，1 口合約代表 100 股標的股票。
造市商為維持 Delta 中性，持有的股票對沖頭寸為 $H$。當客戶買入期權時，造市商持有空頭合約：
$$\Delta_{	ext{dealer}} = -100 	imes \Delta_{	ext{contract}}$$
當現貨價格 $S$ 變動時，造市商為維持中性必須調整的股票股數為：
$$rac{d H}{d S} = -100 	imes \Gamma$$
若定義 **Dollar GEX** 為「現貨價格變動 1% 時造市商需要調倉的名目金額（Dollar Amount）」：
$$\Delta S = 0.01 	imes S$$
$$	ext{Dollar GEX}_{\$1\%} = OI 	imes 100 	imes \Gamma 	imes S 	imes (0.01 S) = OI 	imes \Gamma 	imes S^2$$
若定義 **Total Dollar GEX** 為現貨價格每變動 1 美元時造市商對沖股數對應的名義金額：
$$	ext{Dollar GEX} = OI 	imes 100 	imes \Gamma 	imes S^2$$
無論採用何種金融定義，**100 股合約乘數必然存在於推導鏈中**。`gex_scraper.py` 遺漏了 100 乘數，導致輸出的數值在量綱上比標準 Dollar GEX 小了整整 100 倍。

---

### A.2 局部相鄰線性插值之 Gamma Flip 封閉解推導

Gamma Flip 定義為全市場做市商淨 Gamma 總和為 0 的現貨價格平衡點 $S^*$：
$$	ext{NetGEX}(S^*) = 0$$
在離散履約價數據下，設已排序履約價序列 $K_1 < K_2 < \dots < K_N$，各節點對應的淨 GEX 為 $GEX(K_i)$。
若存在相鄰履約價 $K_i$ 與 $K_{i+1}$ 滿足正負符號反轉：
$$GEX(K_i) \cdot GEX(K_{i+1}) \le 0$$
在區間 $[K_i, K_{i+1}]$ 內採用局部線性插值代理：
$$GEX(S) pprox GEX(K_i) + rac{GEX(K_{i+1}) - GEX(K_i)}{K_{i+1} - K_i} (S - K_i)$$
令 $GEX(S^*) = 0$，解得封閉形式解：
$$0 = GEX(K_i) + rac{GEX(K_{i+1}) - GEX(K_i)}{K_{i+1} - K_i} (S^* - K_i)$$
$$S^* = K_i - GEX(K_i) \cdot rac{K_{i+1} - K_i}{GEX(K_{i+1}) - GEX(K_i)}$$

若全鏈存在多個交叉點，選取原則為最小化與當前現價 $S_0$ 的距離：
$$S^*_{	ext{optimal}} = rg\min_{S^*} |S^* - S_0|$$
此算法具有局部性，時間複雜度為 $O(N)$，且完全不受全域由左向右累加方向性偏差的干擾。

---

### A.3 凸分段線性函數之 Max Pain 數學證明與 $O(N)$ 前綴和演算法推導

#### 凸性證明
在到期結算日，若結算價為 $S$，所有期權買方獲得的內含價值總損失（即賣方總盈利的相反數）為：
$$P(S) = \sum_{K_i < S} OI_c(K_i)(S - K_i) + \sum_{K_j > S} OI_p(K_j)(K_j - S)$$
在任意非節點處，對 $S$ 求一階導微商：
$$rac{d P(S)}{d S} = \sum_{K_i < S} OI_c(K_i) - \sum_{K_j > S} OI_p(K_j)$$
因為未平倉量 $OI \ge 0$，當 $S$ 單調遞增時，小於 $S$ 的 Call 集合單調擴大，大於 $S$ 的 Put 集合單調縮小。因此 $rac{dP(S)}{dS}$ 是關於 $S$ 的單調遞增階梯函數。
由此可知，$P(S)$ 為嚴格連續的**凸分段線性函數（Convex Piecewise Linear Function）**。
根據凸優化理論，凸分段線性函數的全局最小值必在某個轉折節點（Knot）或相鄰相等區間上取得。因此，將搜索空間約束在現有履約價集合 $S \in \{K_1, \dots, K_N\}$ 內具備嚴密的數學正當性。

#### $O(N)$ 前綴和算法推導
設履約價已排序 $K_1 < K_2 < \dots < K_N$。
在第 $m$ 個節點 $K_m$ 處：
$$CallPain(K_m) = K_m \sum_{i=1}^{m-1} OI_c(K_i) - \sum_{i=1}^{m-1} K_i OI_c(K_i)$$
$$PutPain(K_m) = \sum_{j=m+1}^{N} K_j OI_p(K_j) - K_m \sum_{j=m+1}^{N} OI_p(K_j)$$
定義四組累積前綴和陣列（可在 $O(N)$ 時間內預處理）：
$$A_m = \sum_{i=1}^{m} OI_c(K_i), \quad B_m = \sum_{i=1}^{m} K_i OI_c(K_i)$$
$$C_m = \sum_{j=1}^{m} OI_p(K_j), \quad D_m = \sum_{j=1}^{m} K_j OI_p(K_j)$$
則對於任意 $K_m$：
$$CallPain(K_m) = K_m A_{m-1} - B_{m-1}$$
$$PutPain(K_m) = (D_N - D_m) - K_m (C_N - C_m)$$
$$TotalPain(K_m) = CallPain(K_m) + PutPain(K_m)$$
計算單個節點的複雜度由 $O(N)$ 降至 $O(1)$，全鏈總複雜度由 $O(N^2)$ 降為 $O(N)$，徹底消除 DataFrame 重複切片開銷。

---

### A.4 25-Delta 虛值履約價與波動率展開式之幾何推導證明 10% 裁減必然截斷

在 BSM 模型中，看跌期權的 Delta 公式為：
$$\Delta_P = -e^{-q 	au} N(-d_1)$$
在短期限（如 1 個月）下，設 $q pprox 0$。尋找 25-Delta 虛值 Put 即要求：
$$|\Delta_P| = N(-d_1) = 0.25$$
查詢標準常態分佈反函數：
$$-d_1 = \Phi^{-1}(0.25) pprox -0.6745 \implies d_1 pprox 0.6745$$
展開 $d_1$ 定義：
$$rac{\ln(S/K) + (r + rac{1}{2}\sigma^2)	au}{\sigma \sqrt{	au}} pprox 0.6745$$
對於短期期權，$(r + rac{1}{2}\sigma^2)	au \ll \sigma \sqrt{	au}$，忽略高階小項後得：
$$\ln\left(rac{S}{K}ight) pprox 0.6745 \sigma \sqrt{	au}$$
$$rac{K_{25\Delta P}}{S} pprox \exp\left(-0.6745 \sigma \sqrt{	au}ight)$$

當標的隱含波動率 $\sigma = 0.50$（年化 50%），到期期限 $	au = 30 / 365 pprox 0.0822$ 年時：
$$\sigma \sqrt{	au} = 0.50 	imes \sqrt{0.0822} pprox 0.50 	imes 0.2867 = 0.1434$$
$$-0.6745 \sigma \sqrt{	au} pprox -0.6745 	imes 0.1434 pprox -0.0967$$
$$rac{K_{25\Delta P}}{S} pprox e^{-0.0967} pprox 0.9078 \quad (	ext{距現價 } -9.22\%)$$
當波動率進一步上升至 $\sigma = 0.60$（高成長科技股常態）時：
$$-0.6745 \sigma \sqrt{	au} = -0.6745 	imes (0.60 	imes 0.2867) pprox -0.1160$$
$$rac{K_{25\Delta P}}{S} pprox e^{-0.1160} pprox 0.8905 \quad (	ext{距現價 } -10.95\%)$$
**數學證明結論**：當 $\sigma \ge 55\%$ 且 $	au \ge 30$ 天時，$K_{25\Delta P}$ 必然嚴格小於 $0.90 S$。代碼中硬編碼 `prune_pct = 0.1` 必然物理性截斷 25-Delta 履約價，使 Skew 計算 100% 失敗。

---

### A.5 ATM Straddle ($0.85 	imes (C+P)$) 與 Root-T ($S \cdot \sigma \cdot \sqrt{	au}$) 等價性條件推導

在平值（ATM，$S=K$）且利率 $r pprox 0, q pprox 0$ 條件下，BSM 看漲期權價格為：
$$C_{ATM} = S \left( 2 N\left(rac{1}{2}\sigma\sqrt{	au}ight) - 1 ight)$$
對標準常態分佈累積函數在原點進行 Taylor 一階展開：
$$N(x) = rac{1}{2} + rac{1}{\sqrt{2\pi}} x + O(x^3)$$
代入 $x = rac{1}{2}\sigma\sqrt{	au}$：
$$C_{ATM} pprox S \left( 2 \left( rac{1}{2} + rac{1}{\sqrt{2\pi}} rac{\sigma\sqrt{	au}}{2} ight) - 1 ight) = rac{S \sigma \sqrt{	au}}{\sqrt{2\pi}} pprox 0.3989 \cdot S \sigma \sqrt{	au}$$
由平價公式，平值看跌期權 $P_{ATM} pprox C_{ATM}$。因此平值跨式（ATM Straddle）價格為：
$$	ext{Straddle} = C_{ATM} + P_{ATM} pprox 2 	imes rac{S \sigma \sqrt{	au}}{\sqrt{2\pi}} = \sqrt{rac{2}{\pi}} S \sigma \sqrt{	au} pprox 0.7979 \cdot S \sigma \sqrt{	au}$$
業界經驗法則中，預期波動區間通常取 $1\sigma$ 波動，即 $S \sigma \sqrt{	au}$。
若以 Straddle 估算：
$$EM_{	ext{Straddle}} pprox 0.85 	imes 	ext{Straddle} pprox 0.85 	imes 0.7979 \cdot S \sigma \sqrt{	au} pprox 0.678 \cdot S \sigma \sqrt{	au}$$
注意 $0.678 pprox rac{2}{3}$，對應於常態分佈約 $50\%$ 的平均絕對偏差（Mean Absolute Deviation）。若要對齊標準 $1\sigma$ 預期移動（涵蓋 68.3% 機率）：
$$EM_{1\sigma} = rac{1}{\sqrt{2/\pi}} 	ext{Straddle} = \sqrt{rac{\pi}{2}} 	ext{Straddle} pprox 1.2533 	imes 	ext{Straddle}$$
當代碼混用 $0.85 	imes 	ext{Straddle}$ 與 $S \sigma \sqrt{	au}$ 時，兩者本質上處於不同的置信水平維度（$0.68\sigma$ vs $1.0\sigma$），直接取 `max()` 會導致在大部分情況下 Straddle 法被完全架空。

---

### A.6 ATR 時間框架波動率縮放理論 ($\sigma_{daily} pprox \sqrt{26} 	imes \sigma_{15m}$) 與防洗盤緩衝量綱校正

美股正規交易時段自 09:30 至 16:00 ET，共計 6.5 小時，即 390 分鐘。
在 15 分鐘 K 線框架下，單一交易日包含的 K 棒數量為：
$$N = rac{390}{15} = 26 	ext{ 根}$$
根據幾何布朗運動與隨機遊走理論，獨立同分布收益率的波動率隨時間平方根縮放（Square-Root-of-Time Rule）：
$$\sigma_{	ext{daily}} = \sqrt{N} \cdot \sigma_{15m} = \sqrt{26} \cdot \sigma_{15m} pprox 5.099 \cdot \sigma_{15m}$$

真實波幅（True Range, TR）與平均真實波幅（ATR）在統計學上與資產收益率標準差成正比。在常態性假設下：
$$\mathbb{E}[TR] \propto \sigma$$
因此，日線 ATR 與 15 分鐘 ATR 滿足相同的縮放關係：
$$ATR_{	ext{daily}} pprox \sqrt{26} \cdot ATR_{15m} pprox 5.1 \cdot ATR_{15m}$$
在 `signal_calculator.py:339` 中，系統扣除的防洗盤緩衝為：
$$	ext{Buffer} = 1.5 	imes ATR_{	ext{daily}} pprox 1.5 	imes (5.1 \cdot ATR_{15m}) pprox 7.65 \cdot ATR_{15m}$$
**量綱衝突結論**：
若要求交易員以「15 分鐘 K 線實體收盤」作為離場撤退信號，則其對應的微觀洗盤擾動尺度應為 $1.5 	imes ATR_{15m}$。
當系統錯誤代入日線 ATR 時，交易員必須承受 $7.65$ 倍的 15 分鐘 ATR 巨幅偏離，此時跌幅早已演變為單日崩跌，完全失去了「過濾 15 分鐘下影線流動性獵殺」的微觀結構防守意義。正確的量綱校正公式必須為：
$$	ext{Buffer}_{	ext{Intraday}} = 1.5 	imes ATR_{15m} = rac{1.5}{\sqrt{26}} 	imes ATR_{	ext{daily}} pprox 0.294 	imes ATR_{	ext{daily}}$$
