# 事件日曆架構與宏觀事件翻譯引擎

## 1. 功能總覽

`services/calendar_service.py` 是共用的日曆閘道，設計原則：

- 宏觀事件依**月份**快取（動態從 `nexus_edge_scraper` 查詢 TradingView 取得）
- 財報事件依**標的**快取（透過 Finnhub API 取得）
- Watchlist 心跳、日曆視圖、盤前警示、Analyst Agent 流程全部共用同一套 SQLite 快取路徑

新增功能時，**不應**在既有日曆輔助函式已存在的情況下，繞過它直接呼叫原始市場日曆 API。

## 2. 宏觀經濟日曆翻譯與正規化引擎（`market_analysis/macro_calendar_translator.py`）

為徹底解決 TradingView 原始總經事件英文名稱繁雜、縮寫不一，以及聯準會官員演講解析困難的問題，系統建置了全域統一的中文化與正規化引擎：

1. **標準 150+ 總經事件中英對照庫（`_RAW_MACRO_EVENT_TRANSLATIONS`）**：
   - 涵蓋通膨物價（CPI、Core CPI、PCE、PPI 年增／月增率）、就業市場（Nonfarm Payrolls、Initial Jobless Claims、Unemployment Rate、JOLTs）、GDP 與經濟成長、房地產市場（Existing/New Home Sales、Building Permits）、國庫券與公債拍賣（4-Week ~ 30-Year Treasury Auction）、ISM／S&P PMI 採購經理人指數、密西根大學消費者信心指數等。
   - 支援不分大小寫與多種常見別名變體映射，確保事件名稱 100% 符合台灣與華語金融市場慣用翻譯。
2. **聯準會官員動態演講解析（`FED_OFFICIALS_MAP` & `translate_macro_event`）**：
   - 內建 30+ 位現任與歷任聯準會官員名冊（包括 Powell 鮑爾、Waller 華勒、Bowman 鮑曼、Williams 威廉斯、Brainard 布蘭納德、Yellen 葉倫等）。
   - 透過正規表示式自動擷取「Fed [Name] Speaks / Speech / Testifies」模式，動態組合為標準化中文，例如：`"Fed Waller Speech"` → `"聯準會華勒發表演說"`。
3. **優雅降級與保底機制**：若遭遇未在庫內之罕見事件，系統會保留原始英文名稱並自動清理冗餘後綴，確保永不拋出異常。

## 3. 核心程式碼檔案路徑關聯

- `nexus_core/services/calendar_service.py`：共用日曆閘道，月度宏觀快取與標的財報快取
- `nexus_core/market_analysis/macro_calendar_translator.py`：150+ 總經事件翻譯庫、`FED_OFFICIALS_MAP`、`translate_macro_event()`
- `nexus_core/cogs/calendar.py`：宏觀與財報日曆指令入口，`event_checker`（每 4 小時）排程
