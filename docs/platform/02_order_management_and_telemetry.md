# 委託單管理與遙測定價對齊引擎

## 1. 功能總覽

為支援動態戰術委託調整與現貨資產的「布陷阱位」需求，系統提供一套專用的 SQLite 狀態引擎，搭配動態 Discord Modal 設定管線與量化價格對齊邏輯。三層架構：

1. **資料庫層**（`database/orders.py`）：`active_orders` 表儲存所有待成交委託。
2. **互動層**（`cogs/order_ui.py`）：委託設定、調整、取消的 Discord UI 入口。
3. **遙測定價引擎**（`services/telemetry_pricing_engine.py`）：依市場微觀結構動態計算委託價格與部位規模的對齊建議。

## 2. 資料庫結構（`database/orders.py`）

`active_orders` 表追蹤所有待成交委託：

- `user_id`（INTEGER）、`symbol`（TEXT）
- `quantity`（REAL）、`order_type`（TEXT：`MARKET`、`LIMIT`、`STOP`、`STOP_LIMIT`、`TRAILING_STOP_USD`、`TRAILING_STOP_PCT`）
- `validity`（TEXT：`DAY`、`EXT_DAY`、`NIGHT`、`GTC_90`）
- `limit_price`（REAL）、`stop_price`（REAL）、`trailing_value`（REAL）
- SQLite schema 遷移依時序管理於 `database/migrations/` 下

## 3. 互動與介面層（`cogs/order_ui.py`）

使用者透過互動式 Discord 介面直接管理委託單的設定、調整與取消：

- **委託設定面板（`/order_panel`）**：呈現動態下拉選單。選擇委託類型後觸發客製化的 `DynamicOrderModal`，內含基礎欄位（標的、數量、效期）與條件式價格欄位（限價、停損、移動停損值）。
- **待成交委託清單（`/list_orders`）**：以詳細的繁體中文 embed 顯示目前所有待成交委託，並附：
  - `❌ 取消委託 (Cancel Order)` 按鈕：觸發 `CancelOrderModal` 進行低延遲取消。
  - `✏️ 編輯委託單 (Edit Order)` 按鈕：觸發 `EditOrderModal` 編輯標的、數量、方向、價格等欄位。（注意：直接使用 `/edit_order` slash command 額外支援更新 `order_type` 與 `validity`。）
- **遙測價格與規模對齊（`/telemetry_alert`）**：實作動態遙測價格與規模對齊警示，提供：
  - `⚡ 一鍵套用遙測建議價 (Apply Telemetry Price)` 按鈕：**只**套用使用者在快照中看到的建議（不會改動快照以外的委託單，也不再有快取或即時重算的後備），**同時**更新 SQLite 中委託單的價格與數量／股數至更安全的對齊值；STOP_LIMIT 的停損價會平移與限價相同的差額，保留原本的停損／限價價差。結果以 `create_telemetry_apply_result_embed()` 分頁呈現。若觸發規模下修，會內建 `[⚠️ Tail Risk Mitigation]` 提示。

## 4. 遙測定價引擎（`services/telemetry_pricing_engine.py`）

引擎沿著三條操作向量計算建議限價／停損價偏移：

1. **期權流與重力**：
   - **Max Pain 遷移**：依期權 Max Pain 遷移動態偏移重力指數。
   - **極端 Skew 尾部風險連動**：當期權 Skew 百分位觸及極端尾部（`skew_percentile_pct < 5.0` 或 `> 95.0`，全系統統一的 0~100 量綱；入口以 `ensure_percentile_pct()` 攔截越界值並視為中性 50）時，引擎將待成交委託的價格向現價**收斂 1.5%**（截斷恐慌／軋空的陰影），並對數量／股數動態套用 **`0.75` 防禦性乘數**（`[⚠️ Tail Risk Mitigation]`），保護資金流動性、防止水位枯竭。
2. **統計波動率邊界**：由短期 IV 飆升（3% 價格緩衝回撤）或 IV 崩塌（下限收斂至預期波幅下緣）驅動的回調，依預期波幅（EM）上下限縮放。
3. **技術面與流動性錨點**：支撐區偏移對齊前收盤跳空回補位與心理整數關卡（例如以 `Round Level - 0.75` 偏移避開整數關卡）。

### 4.1 Skew 輸入：真實分位優先
新增委託的遙測定價與 `/telemetry_alert` 對齊警報，兩條路徑一律經 `services/order_telemetry_service.py::resolve_skew_percentile_pct()` 取得 Skew 百分位：

1. **真實分位** `calculate_skew()["skew_percentile"]`：標的相對於自己歷史的位置（日級規範母體，樣本不足時為高頻回退池，見 [`../valuation_pricing/03_skew_pcr_divergence_confluence.md`](../valuation_pricing/03_skew_pcr_divergence_confluence.md) §5.4）。
2. **分位缺失時退回絕對值換算**：原始 Skew $> 5$ 個百分點視為 98、$< -2$ 視為 2。這是改版前的判定方式，只作為後備，確保資料缺失時不會比改版前更寬鬆。
3. **兩者皆無**：中性 50，不觸發尾端防禦。

改版前新增委託路徑直接使用絕對值換算，一鍵套用的後備路徑則把 Skew 寫死為 98（每次都觸發尾端防禦）。絕對值換算無法跨標的比較：高波動小型股的 Skew 常態就在 5 個百分點以上，幾乎每次都被收斂價格、砍股數；低波動大型股則即使處在自身歷史高點也從不觸發。

**後續觀察**：改用真實分位後，尾端防禦的觸發頻率會重新分布（高波動小型股大幅減少、大型股偶爾觸發）。上線後留意 `[⚠️ 尾端風險防禦]` 日誌的標的分布；若某些標的長期停在高頻回退池（`skew_percentile_source = INTRADAY_FALLBACK`），其分位語意與日級母體不同，觸發頻率會偏高。

### 4.2 對齊決策的市場輸入與快照欄位來源
`/telemetry_alert`（與盤中推播）經 `_fetch_alignment_market_inputs()` 並行抓取下列資料再呼叫 `generate_alignment_decision()`。`asyncio.gather` 使用 `return_exceptions=True`：任何單一輸入失敗只視為該項缺值，不會讓整個指令失敗。

| 參數 | 來源 | 缺值時 |
|---|---|---|
| `iv` | `fetch_and_calculate_iv_metrics().current_iv` | 0.35 |
| `hist_iv` | 見下方「歷史 IV 基準」 | 等於 `iv`（中性） |
| `iv_rank` | `iv_rank / 100`（0~1） | `None`（引擎視為 1.0，保守抑制上調） |
| `max_pain_price` / `prev_max_pain` | `calculate_max_pain()`；過期、斷路或缺值時歸零 | 0（不參與定價） |
| `skew_percentile_pct` | `resolve_skew_percentile_pct()`（§4.1） | 50 |
| `put_call_ratio` | `calculate_pcr()["volume_pcr"]` | 1.0（不觸發 Skew/PCR 恐慌折價） |
| `uoa_array` | `detect_uoa()` | 空清單 |
| `macro_event_dates` | 14 天內高影響宏觀事件 + 該標的財報日 | 空集合 |

**歷史 IV 基準（`hist_iv`）**：僅在 `iv_source == "LIVE_IV"`、未套用事件加載（`event_loading_applied` 不為 True）且 `IVMetrics.iv_hist_mean_20d`（近 20 筆已儲存日 IV 的均值）為正數時採用該均值；其餘情況一律 `hist_iv = iv`，讓 vol-spike／vol-crush 都不觸發。原因：`HV_PROXY` 的「IV」其實是已實現波動率（禁止與 IV 混比，見 `iv_metrics.py` ISS-06），`STORED_IV`／`HV_PROXY` 遇事件時 `current_iv` 會被乘以 1.4，拿來和基準比較都會誤觸發假訊號。新增委託路徑（`resolve_telemetry_pricing`）套用相同規則，IV 取不到時沿用 `iv = 0.35`、`hist_iv = iv`。

**快照欄位的資料來源**：

- **現價與前收盤**：`fetch_cache_and_live_price()` 先讀快取報價，只作廢「該標的」的報價快取後重新呼叫 `get_quote()`（沿用 Alpaca 串流分鐘價 → Finnhub → yfinance 後備的優先序）；不再用日線收盤覆蓋、也不再清空全站報價快取。`prev_close` 取報價的 `pc`（前一個正規盤收盤）。
- **沒有即時現價**：該委託單仍會出現在快照中，標示為 `NO_LIVE_PRICE`／`DATA DEGRADED`，維持原價原量，不執行決策；`live_price`、距離成交差、損益與牆距離一律為缺值（`--`），絕不以委託價冒充現價。
- **GEX Put Wall 與 Max Pain 是不同的量**：卡片上的「GEX PutWall／距離硬支撐」取自 `fetch_symbol_gex_metrics(symbol)["put_wall"]`（預設 `force_live=False`，走快取 → edge 快照；服務過期快取時 `put_wall_is_stale=True`）；Max Pain 只作為定價輸入（`max_pain_price`）。GEX 取不到時 `put_wall=None`、牆狀態「待確認」。
- **損益**：`gain_loss_pct` 對多頭為 `(現價 − 均價) / 均價`，空頭持倉（數量為負）為 `(均價 − 現價) / 均價`；沒有均價或持股為 0 時為缺值而非 `+0.00%`。持倉狀態分為「持倉中／空頭持倉／空倉待命」；持倉型態標籤區分純現貨、現貨＋選擇權、保證金槓桿（推定）與無持倉紀錄。
- **IV 狀態**：IV 不可用顯示 `UNAVAILABLE`；有 IV 但 IV Rank 尚無（樣本累積中）顯示 `樣本累積 n/m`，不再沿用模型預設的 `Normal`。`is_premarket` 在正規盤以外（含盤後）皆為 True。
- **同一標的**在一次快照中只抓一次報價與市場輸入；快照上限為 `MAX_ALIGNMENT_ITEMS = 20` 筆，且在抓取下一筆資料「之前」就檢查，超過時 `truncated=True`（Embed 會自行分頁）。

**快照不丟單**：快照以 `emit_suppressed_decision=True` 呼叫引擎，IV Rank 保險絲與「提價被 Max Pain／預期波動邊界截斷後不高於原價」兩條路徑也會回傳決策（`SUPPRESSED`／`NO_ALIGNMENT_NEEDED`，建議價量等於原價原量），委託單不會從快照中消失；非 emit 模式仍回傳 `None`。

**賣出委託不調整**：對齊引擎的定價（左側捕獸夾）只為買單設計，`SELL` 委託一律回傳 `SUPPRESSED`／`BUY-ONLY ENGINE`，維持原價原量。

一鍵套用只處理快照（`suggestions`）內的委託單，移動停損單（`TRAILING_STOP_USD`／`TRAILING_STOP_PCT`）不適用價格對齊，與 `/telemetry_alert` 的排除規則一致。STOP_LIMIT 套用時，停損價平移與限價相同的差額（例：限價 100／停損 98 → 建議價 101，則停損變 99）。

**後續觀察**：PCR 為真實值後，Pillar 3（Skew $> 90$ 且 PCR $> 1.5$ 時建議價打 9 折）開始可能觸發。上線後留意 `Skew/PCR 極端恐慌` 理由出現的頻率與標的；`iv_hist_mean_20d` 在樣本不足 10 筆時為 None，期間 `hist_iv` 維持中性。

## 5. 核心程式碼檔案路徑關聯

- `nexus_core/database/orders.py`：`active_orders` 表 CRUD 操作
- `nexus_core/cogs/order_ui.py`：`/order_panel`、`/list_orders`、`/telemetry_alert` 入口
- `nexus_core/cogs/order_views.py`：互動式清單視圖與遙測對齊按鈕
- `nexus_core/cogs/order_modals.py`：取消／調整 Modal
- `nexus_core/services/telemetry_pricing_engine.py`：三向量定價對齊與門控邏輯（stale-lock、深海跳空限制、純現貨閘門、UOA 軋空分類）
- `nexus_core/services/order_telemetry_service.py`：委託遙測掃描服務
- `nexus_core/database/migrations/v038_add_active_orders.py`：`active_orders` 表註冊遷移
- `nexus_core/tests/unit/test_order_ui.py`：委託 UI、資料庫、遙測定價對齊的單元測試
