# 個股 15 分鐘價量突破警報系統

## 1. 功能總覽

不同於 WTI 監控（單一固定標的 `CL=F`，見 [`../macro_sentiment/03_wti_crude_oil_monitor.md`](../macro_sentiment/03_wti_crude_oil_monitor.md)），這是一套**每使用者、多標的**的自選警戒清單：每位使用者最多可註冊 15 組獨立的 `(symbol, target_price, direction, volume_multiplier)` 監控，排程器在盤中每 15 分鐘評估所有已註冊監控。

## 2. K 棒完整性防呆（`market_analysis/price_volume_alert.py::get_confirmed_15m_bar`）

觸發條件是**15 分鐘實體 K 棒收盤價**相對於目標價，結合放量門檻：

- 透過 `services.market_data_service.get_history_df(symbol, period="5d", interval="15m", force_refresh=True)` 取得真實 `interval="15m"` OHLCV。
- **`force_refresh=True` 為必要條件**：`get_history_df` 預設快取結果 6 小時（`_HISTORY_CACHE_TTL`），若不強制刷新，15 分鐘節奏的呼叫方可能拿到過期／不完整的 K 棒。這是刻意與 `market_analysis/dynamic_rollover/opportunity_cost.py::_confirm_entry_signal` 分歧的設計——後者同樣使用 `interval="15m"`，但**不**繞過快取、也**不**檢查 K 棒是否已收盤，新的盤中 K 棒邏輯不應照抄該模式。
- **已收盤 K 棒偵測**：yfinance 的 15m K 棒索引是該 K 棒的*起始*時間。唯有 `bar_start + 15min <= now (ET)` 時才視為「已收盤」，否則引擎退回倒數第二根 K 棒，避免仍在形成中的 K 棒即時價觸發誤判警報。這與 `market_analysis/gamma_cliff_confirmation.py::is_gamma_cliff_confirmed` 不同——後者儘管命名為「15分鐘」，實際操作的是 1 分鐘 K 棒，並非真正的 15m K 棒。
- **`trim_to_confirmed_15m_bars(df)` 是唯一的截斷定義來源**，抽取出來讓 `get_confirmed_15m_bar` 與 `dynamic_rollover` 左側進場閘門／盤勢分類器無法互相漂移。它回傳截斷至最後一根已收盤 K 棒的資料框（若剩餘 K 棒數不足以計算 20 根均量基準，或索引非 datetime 型別，則回傳 `None`——寧可失效也不假設「已收盤」）。對於*縮量*類條件（如左側進場閘門的縮量窒息 `volume <= 0.7× avg`）尤其危險：一根才進行 2 分鐘的 K 棒只累積了一小部分成交量，若誤用未收盤 K 棒，該子條件在大多數時間都會被誤判為真。
- **放量門檻**：比較已收盤 K 棒的成交量與前 20 根 K 棒（`_VOLUME_LOOKBACK_BARS`）的均量，呼應 `opportunity_cost.py` 進場確認邏輯已使用的 20 根回看窗（該路徑使用 1.2 倍門檻，本功能預設使用者可調整的 1.5 倍門檻）。

## 3. 門檻比對與純價格警報支援（`evaluate_watch_trigger`）

刻意與 K 棒抓取邏輯分離，讓多位監控同一標的的使用者共用同一次 yfinance 呼叫：

- `direction = "above"` → `Close >= target_price`（突破）
- `direction = "below"` → `Close <= target_price`（跌破）
- **雙模支援**：
  - **價量突破模式（`volume_multiplier > 0`，預設 1.5x）**：要求當根 15 分鐘收盤價達標且成交量大於前 20 根均量的倍數，防止假突破／雜訊。
  - **純價格警報模式（`volume_multiplier = 0`）**：若將 `volume_multiplier` 設為 `0`，量能檢查條件將無條件通過（`volume_condition = True`），轉為單純的實體 K 線價格觸及／破位警報。

## 4. 每使用者監控設定（`database/price_volume_watch.py`，遷移 `v063`）

使用專用 SQLite 表（`price_volume_watches`，主鍵 `(user_id, symbol)`）而非 `kv_cache`，因為排程器需要跨使用者、跨標的的批次查詢（`get_all_watches()`），單鍵 KV 儲存無法高效支援。`upsert_watch()` 強制 `_MAX_WATCHES_PER_USER = 15` 上限（VPS 記憶體／API 呼叫保護），此上限只套用於*新增*標的——更新既有監控的價格／方向／倍數不計入上限。

## 5. 排程器（`cogs/trading/price_volume_alert_monitor.py`）

- `@tasks.loop(minutes=15)`，受 `market_time.is_market_open()` 閘門控制（與 WTI 的 24/7 節奏不同，因為股票盤中 K 棒在非交易時段沒有意義）。
- 先依標的分組所有已註冊監控，每個唯一標的每輪只抓取一次已收盤 K 棒，即使多位使用者監控同一標的。
- **KV Cache 防重複發送**：`price_volume_alert_{user_id}_{symbol}_{YYYYMMDD}` —— 每位使用者、每個標的、每天最多一則警報（與 WTI 的每日防重複鍵值相同模式）。

## 6. 互動指令與 `/notif_settings`

- `/price_alert_set <symbol> <target_price> <direction> [volume_multiplier=1.5]`：新增／更新監控（採參數化指令而非 Modal，因所有欄位皆為簡單純量；可將 `volume_multiplier: 0` 開啟純價格警報）。
- `/price_alert_list`：列出呼叫者目前的活躍監控。
- `/price_alert_remove <symbol>`：移除一組監控。
- `/notif_settings`：受 **🎯 Alpha 策略與情報** 模組下的正式頻道 `alpha_price_volume_watch` 管控。

## 7. 核心程式碼檔案路徑關聯

- `nexus_core/market_analysis/price_volume_alert.py`：`get_confirmed_15m_bar()`, `trim_to_confirmed_15m_bars()`, `evaluate_watch_trigger()`
- `nexus_core/database/price_volume_watch.py`：`price_volume_watches` 表 CRUD，`upsert_watch()`, `get_all_watches()`
- `nexus_core/cogs/trading/price_volume_alert_monitor.py`：15 分鐘排程器，KV Cache 防重複發送
- `nexus_core/database/migrations/v063_add_price_volume_watches.py`：`price_volume_watches` 表註冊遷移
