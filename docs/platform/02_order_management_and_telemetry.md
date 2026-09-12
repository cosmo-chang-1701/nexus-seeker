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
  - `⚡ 一鍵套用遙測建議價 (Apply Telemetry Price)` 按鈕：依遙測定價引擎最新計算結果，**同時**更新 SQLite 中委託單的價格與數量／股數至更安全的對齊值。若觸發規模下修，會內建 `[⚠️ Tail Risk Mitigation]` 提示。

## 4. 遙測定價引擎（`services/telemetry_pricing_engine.py`）

引擎沿著三條操作向量計算建議限價／停損價偏移：

1. **期權流與重力**：
   - **Max Pain 遷移**：依期權 Max Pain 遷移動態偏移重力指數。
   - **極端 Skew 尾部風險連動**：當期權 Skew 百分位觸及極端尾部（`skew_percentile < 0.05` 或 `skew_percentile > 0.95`）時，引擎將待成交委託的價格向現價**收斂 1.5%**（截斷恐慌／軋空的陰影），並對數量／股數動態套用 **`0.75` 防禦性乘數**（`[⚠️ Tail Risk Mitigation]`），保護資金流動性、防止水位枯竭。
2. **統計波動率邊界**：由短期 IV 飆升（3% 價格緩衝回撤）或 IV 崩塌（下限收斂至預期波幅下緣）驅動的回調，依預期波幅（EM）上下限縮放。
3. **技術面與流動性錨點**：支撐區偏移對齊前收盤跳空回補位與心理整數關卡（例如以 `Round Level - 0.75` 偏移避開整數關卡）。

## 5. 核心程式碼檔案路徑關聯

- `nexus_core/database/orders.py`：`active_orders` 表 CRUD 操作
- `nexus_core/cogs/order_ui.py`：`/order_panel`、`/list_orders`、`/telemetry_alert` 入口
- `nexus_core/cogs/order_views.py`：互動式清單視圖與遙測對齊按鈕
- `nexus_core/cogs/order_modals.py`：取消／調整 Modal
- `nexus_core/services/telemetry_pricing_engine.py`：三向量定價對齊與門控邏輯（stale-lock、深海跳空限制、純現貨閘門、UOA 軋空分類）
- `nexus_core/services/order_telemetry_service.py`：委託遙測掃描服務
- `nexus_core/database/migrations/v038_add_active_orders.py`：`active_orders` 表註冊遷移
- `nexus_core/tests/unit/test_order_ui.py`：委託 UI、資料庫、遙測定價對齊的單元測試
