# Alpaca 即時 1 分 K 串流

## 1. 功能總覽

`nexus_core/services/alpaca_stream_service.py` 連線 Alpaca 股票 WebSocket（`wss://stream.data.alpaca.markets/v2/{iex|sip}`），訂閱使用者實際關心的標的，在記憶體中維護每檔的分鐘 K 緩衝區、當日常規時段統計與 15 分 K 聚合。它有兩個下游：

1. **`get_quote` 的 Tier 0 現價**（`services/market_data_service/quote.py`）：條件完整時直接回傳串流的分鐘級現價，省下一次 Finnhub／yfinance 呼叫。
2. **15 分鐘價量警報**（`market_analysis/price_volume_alert.py`）：以串流聚合出的已收盤 15 分 K 取代 yfinance，預設為影子模式，見 [`06_price_volume_alert_system.md`](06_price_volume_alert_system.md) §2.1。

這是**分鐘級**、不是毫秒級的資料：服務只訂閱 1 分 K（`bars` + `updatedBars`），現價最多落後約 60–90 秒。加密貨幣串流（CryptoDataStream）目前沒有任何部位或消費端，因此不實作。

服務只在 leader 實例上連線（掛在 `bot.py` 的 `_start_leader_services()`／`_stop_leader_services()`），藍綠部署時另一個容器不會同時連線。預設關閉，需同時設定 `ENABLE_ALPACA_STREAM=true` 與金鑰。

## 2. 訂閱清單與排名（每 15 分鐘刷新）

候選來自持倉（`database.portfolio.get_all_portfolio_symbol_pairs()`，TRADE + HOLDING）、價量監測（`database.price_volume_watch.get_all_watches()`）與自選（`database.watchlist.get_all_watchlist()`），三次讀取合併在同一次 `asyncio.to_thread`（`collect_candidate_groups()`）。指數（`^VIX`）、期貨（`CL=F`）與 `VIX` 會被排除；`BRK-B` 轉成 Alpaca 的 `BRK.B`；同一標的不論出現在幾組、被幾位使用者關注，都只佔一個名額。

候選超過 `ALPACA_MAX_STREAM_SYMBOLS`（預設 30，對應免費 Basic 方案上限）時，`rank_symbols()` 依串流的**實際效益**排序後截斷：

| 順位 | 候選 | 理由 |
|---|---|---|
| 1 | 持倉 | 風控最需要新鮮報價；持倉超過上限時保留最活躍者 |
| 2 | 大型股白名單內的價量監測 | 價量警報 live 模式唯一會採用串流的標的 |
| 3 | 其餘所有候選 | 依前一交易日 **IEX 成交筆數**由高到低，同分依關注人數（跨價量監測與自選的不重複使用者數）、再依代號 |

第 3 順位不用字母排序，是因為串流的效益集中在 IEX 上活躍的標的：Tier 0 現價要求 90 秒內有真實 IEX 成交，冷門標的多數時間仍會退回 Finnhub，佔用名額卻很少用上。

活躍度以一次批次 REST 請求（`timeframe=1Day&feed=iex`，每批 100 檔）取得各標的最近一個已收盤交易日的成交筆數 `n`，**每個交易日只查一次**；同一天內只補查新出現的候選，查無資料記為 0，查詢失敗則不記錄、下次刷新重試。因此同一天內的排名只會因候選增減而變動，不會在標的之間反覆換檔（每次換檔都要退訂、重訂、回補）。

服務與目前訂閱比對後，只在同一條連線上送出差異的 `subscribe`／`unsubscribe`，被退訂的標的會釋放全部記憶體狀態。收到錯誤碼 405（訂閱數超限）時上限自動降 5 檔後重新訂閱。

**Tier 0 命中率**：服務記錄每檔「盤中查詢次數／Tier 0 命中次數」，換日後第一次刷新時以 `📈 Alpaca Tier 0 命中率` 輸出前一日摘要（由低到高），用來判斷哪些標的訂了卻用不上。

## 3. 分級（`StreamTier`）

| 分級 | 判定 | 影響 |
|---|---|---|
| `LARGE_CAP_US` | 列於 `config.ALPACA_LARGE_CAP_SYMBOLS` 白名單 | 價量警報 live 模式下可改用串流量能 |
| `SMALL_MID_CAP_US` | 其餘所有美股 | 價量警報一律維持 yfinance |

分級**不**決定是否 Forward Fill。IEX 約佔全市場成交量 2~3%，大型股偶爾也會缺分鐘；補齊規則對所有美股一律套用，無缺口時不會產生任何合成 K 棒。

## 4. K 棒處理（`market_analysis/stream_bars.py`，純 stdlib 葉模組）

- **只收常規時段**：K 棒時間（Alpaca 的 `t`，為 K 棒**起始**時間）必須落在該美東交易日的 `[開盤, 收盤)` 內（`market_time.get_session_bounds_utc()`，含半日市），盤前盤後一律丟棄。
- **Forward Fill**（`apply_forward_fill`）：
  - 同一分鐘 → 取代（`updatedBars` 遲到成交修正）；比最後一根舊 → 取代緩衝區內同一分鐘，否則丟棄。
  - 缺口 2–30 分鐘（`ALPACA_MAX_FFILL_MINUTES`）→ 以前收補齊中間每一分鐘，Volume=0，且合成 K 棒不得早於開盤。
  - 缺口超過 30 分鐘 → 不補（停牌、跨日）。
- **合成 K 棒只用於時間網格**：EMA(9/21)、SMA(20/50)、Wilder RSI(14) 與放量倍數以補齊後的序列計算；當日開盤價、高低點、**以開盤錨定的 VWAP** 與「最後成交時刻」只累計真實 K 棒（`SessionStats`，與 200 根滾動緩衝區分開維護，早盤 K 棒被擠出後仍然正確）。
- **15 分 K 聚合**（`aggregate_15m`）：時窗結束後再等 20 秒寬限期（`WINDOW_GRACE`）才收盤；時窗內沒有任何真實成交時，以前收產生 Volume=0 的平盤 K 棒。

每檔記憶體：`MinuteBar` 使用 `slots=True`，200 根約數十 KB；30 檔合計約數 MB。

## 5. 資料完整性不變式

這是本服務最重要的設計。兩個時間戳決定串流資料能否被下游採用：

- `coverage_since`：本次連線對該標的收到 `subscription` 回覆的時刻。
- `complete_since`：分鐘資料自此時刻起連續無缺。訂閱後以 REST（`https://data.alpaca.markets/v2/stocks/bars`，與串流同為 IEX）回補當日開盤起的 1 分 K 成功，才會前推到開盤；同時回補今日開盤前的 15 分 K 作為均量基準。回補失敗或記憶體水位過高（`is_memory_safe()`）時等於 `coverage_since`。

斷線時兩者一律清空、15 分 K 歷史也清空；重連後重新回補。任何 15 分鐘時窗只要不在完整區間內，15 分 K 歷史就會被清空重建，確保均量回看的 20 根一定是連續時窗。寧可暫時退回 Finnhub／yfinance，也不用中間缺了一段的資料。

昨收基準取自 `market_data_service.get_history_df(symbol, "5d", "1d")`（官方收盤價、6 小時快取），每個交易日每檔最多一次網路請求，在訂閱刷新時取得，不在報價熱路徑上。

## 6. `get_quote` Tier 0

位於 `_fetch_quote_uncached` 的 `_fetch()` 內、指數判斷之後、Finnhub 之前，因此結果仍會寫入既有的 15 秒 `_quote_cache` 並經過 single-flight 合併。以下條件**全部**成立才採用，否則行為與未啟用時完全相同：

- 服務已通過認證（單純連上 socket 不算）且該標的已訂閱
- 目前在今日常規時段內
- `complete_since <= 今日開盤`（開盤價與高低點完整）
- 最後一根真實 K 棒的**收盤時刻**（`t + 60s`）距今不超過 `ALPACA_TIER0_MAX_AGE_SECONDS`（90 秒）；成交稀疏的小型股會自然退回 Finnhub
- 已有今日的官方昨收基準

回傳欄位與 Finnhub `/quote` 相容：`c` 為最後真實收盤價、`pc` 為**官方昨收**（`intraday_pipeline/metrics.py`、`sentiment/iv_metrics.py`、`order_telemetry_service.py` 都把 `pc` 當昨收使用，絕不能是上一分鐘收盤）、`d`／`dp` 以 `pc` 計算、`o`／`h`／`l` 為今日 IEX 成交累計（極值可能略窄於全市場）、`t` 為最後真實 K 棒的收盤時刻。

**`/x` 標的分析中心的區間校正**：IEX 的 `h`／`l` 偏窄，而 Session VWAP 與 15m K 棒來自 yfinance 全市場 K 線，直接並列會出現「VWAP 高於當日最高價」「15m K 棒低點低於全日低點」。`cogs/unified_terminal/symbol_deep_dive.py` 以 `vwap_utils.fetch_session_stats()`（VWAP 與區間極值出自同一份 K 線）呼叫 `intraday_consistency.reconcile_daily_range()` 放寬 `h`／`l`（只放寬、不收窄，K 線不屬於今日時不合併）。放寬後 VWAP 仍在區間外即視為缺失；15m K 棒落後超過 30 分鐘、極值超出當日區間或疑似多根合併時，停用量比判定並揭露原因。這只影響 `/x` 的呈現，`get_quote` 回傳給其他消費端的 `h`／`l` 不變。

**後續觀察事項（上線後）**：
- **盤中實測**：開盤一小時後各執行一次 `/x SNDK` 與 `/x DRAM`，確認日高低點涵蓋 VWAP 與 15m K 棒極值、K 棒標示時間；IV 與預期區間數量級一致、EM 旁有跨式隱含 IV（見 [`../valuation_pricing/02_expected_move_and_max_pain.md`](../valuation_pricing/02_expected_move_and_max_pain.md) §5.4）；上方全為負 GEX 時顯示負 Gamma 真空與局部體制（見 [`../microstructure/03_gamma_flip_estimation.md`](../microstructure/03_gamma_flip_estimation.md) §5 第 4 點）。
- **校正頻率**：統計日誌 `報價日高低點以全市場 15m K 線校正` 的出現比例。若 Tier 0 命中的標的幾乎每次都要校正，代表 IEX 高低點不適合直接給 `/x` 使用，可評估讓 `/x` 的日高低點改以全市場 K 線為主。
- **異常 K 棒**：統計 `Session VWAP … 超出當日區間` 與 `15m K 棒一致性檢查未通過` 的頻率與標的分布。若集中在特定時段（例如開盤後第一小時），代表 yfinance 15m 資料延遲是系統性的，應評估改用串流的 `get_confirmed_15m_bar()`。

## 7. 連線與錯誤處理

- 收到 `authenticated` 才視為已連線並送出訂閱。
- 錯誤碼 402（認證失敗）、409（方案不支援，例如免費帳號設定 `sip`）→ 記 `logger.error` 並停止重試。
- 錯誤碼 406（連線數超限，通常是藍綠部署重疊）→ 至少 60 秒的退避後重連。
- 其他斷線 → 指數退避（2 秒起、上限 300 秒）。
- 訊息處理例外記為 `logger.warning`，不會中斷串流。

## 8. 設定（`nexus_core/config.py`）

| 環境變數／常數 | 預設 | 說明 |
|---|---|---|
| `ENABLE_ALPACA_STREAM` | `false` | 總開關 |
| `ALPACA_API_KEY`／`ALPACA_API_SECRET` | 空 | 缺任一者即不啟動 |
| `ALPACA_DATA_FEED` | `iex` | `iex` 或 `sip`（SIP 需付費方案） |
| `ALPACA_MAX_STREAM_SYMBOLS` | `30` | 訂閱上限 |
| `ALPACA_PV_ALERT_LIVE` | `false` | 價量警報是否改用串流（僅大型股） |
| `ALPACA_LARGE_CAP_SYMBOLS` | 15 檔 | 大型股白名單（常數） |
| `ALPACA_MAX_FFILL_MINUTES` | `30` | Forward Fill 最大缺口（常數） |
| `ALPACA_MAX_BUFFER_BARS` | `200` | 每檔 1 分 K 緩衝根數（常數） |
| `ALPACA_TIER0_MAX_AGE_SECONDS` | `90` | Tier 0 新鮮度門檻（常數） |

## 9. 核心程式碼檔案路徑

- `nexus_core/market_analysis/stream_bars.py`：`MinuteBar`、`apply_forward_fill()`、`SessionStats`、`aggregate_15m()`、`compute_technicals()`、`classify_symbol_tier()`
- `nexus_core/services/alpaca_stream_service.py`：`AlpacaStreamService`、`get_stream_service()`／`set_stream_service()` registry、`collect_candidate_groups()`／`rank_symbols()`
- `nexus_core/services/market_data_service/quote.py`：`_get_stream_quote()`（Tier 0）
- `nexus_core/market_analysis/price_volume_alert.py`：`get_confirmed_15m_bar_from_stream()` 與分派
- `nexus_core/tests/unit/test_stream_bars.py`、`nexus_core/tests/unit/test_alpaca_stream_service.py`
