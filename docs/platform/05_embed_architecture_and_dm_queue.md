# Embed 渲染架構與 DM 佇列投遞層

## 1. 功能總覽

本文涵蓋兩個緊密相關的展示與投遞層規範：所有生產環境 Discord Embed 建構須遵循的集中化與視覺一致性規則，以及 `bot.py` 所擁有的持久化 DM 佇列投遞機制。

## 2. Embed 輸出集中化

所有生產環境 Embed 建構應維持集中於：

- `nexus_core/cogs/embed_builders/`（`embed_builder.py` 作為向後相容 shim）

此規則由 `tests/unit/test_output_centralization.py` 強制執行。現行規範：

- cogs 不應直接建構 `discord.Embed`
- cogs 不應使用 `queue_dm(message=...)` 捷徑
- 推播／報告訊息優先使用**欄位式（field-based）embed**
- ANSI 表格應置於欄位內，避免直接塞進整個 description（除非別無選擇）

### 2.1 Discord Embed 排版最佳實踐
呈現多行統計數據或儀表板指標時（例如 `/sys_health`），優先使用**單一欄位區塊列表**（`name` 作為區段標題、`value` 承載含 `inline=False` 的多行 Markdown 統計），而非 3 欄 `inline=True` 網格。這能避免在行動裝置上斷版，也不需要空的 `​` 填充欄位。

### 2.2 視覺一致性與明確子類化（`NexusEmbed`）
- 為了在所有模組間維持絕對的視覺一致性與截斷保護，`cogs/embed_builders/` 內的**所有建構函式必須明確 import 並建構 `NexusEmbed`，而非 `discord.Embed`**（例如 `from cogs.embed_builders._core import NexusEmbed`）。Monkey patching 已淘汰。
- **精選色票**：所有標準顏色皆映射至一致、高質感的色票：
  - 主要系統／資訊：精選藍 `0x3498DB`
  - 危險／風險警示：精選紅 `0xE74C3C`
  - 結算／獲利：精選綠 `0x2ECC71`
  - 警告／觀察：精選橙 `0xF39C12`
  - 次要：精選 blurple `0x5865F2`
- **標準化 Footer 簽名**：每個 embed footer 動態格式化為 `"🌌 Nexus Seeker • [模組描述]"`，無重複前綴，並同步系統時間戳。
- **分頁相容性（`from_dict`）**：`.from_dict()` 類別方法經過覆寫，可將序列化字典 payload 無縫轉換回完整風格化的 `NexusEmbed` 實例。

### 2.3 量化控制台與多模組 Embed 排版原則
- **控制台美學（ANSI 包裹）**：所有含即時行情、量化數據、持倉或風險精算的內容，必須使用 ` ```ansi ` 程式碼區塊包裹以進行控制台渲染。
- **樹狀縮排與結構標籤**：子項目統一使用分叉角 ` ├─ ` 與結尾角 ` └─ ` 符號進行多級縮排，輔以長度適配的 `-` 分隔線，維護純文字網格的視覺層級。
- **欄位模組化與欄位分離**：除概覽摘要（Overview）或特定心跳大圖使用單一 description 整合外，任何包含多個邏輯子模組的 Embed 必須以多個 Discord Embed Fields 進行物理隔離。各欄位 Title 應搭配適當 Emoji 前綴，其 Value 則獨立包裹各自的 ` ```ansi ` 程式碼區塊，避免混雜。
- **數據狀態後綴與降級防禦**：
  - 非交易時段、市場封盤或網路／API 異常導致即時數據不可用時，Embed Title 必須追加狀態後綴（如 ` [盤前數據未更新/降級模式]`），受影響指標應自動降級顯示為公允的預設字元（如 `--%`、`--`、`N/A`、`封盤中`）。
  - 成功讀取本地 SQLite 快取或歷史代理數據（如歷史波動率 proxy）時，Title 應註明來源屬性（如 ` [盤前/前日收盤]` 或 ` [盤前/HV代理]`），數值旁應附加 `(前日收盤 / 歷史波動率代理)` 標記，確保數據透明度。
  - 核心比對數值偏離公允區間超出特定閾值（例如價格偏離痛點 >30% 觸發斷路器）時，下游執行或操作指南需自動顯示 `N/A (已觸發斷路器)` 或相關警告，暫停輸出特定交易建議。
- **啟發式代理數據揭露**：當某項使用者可見的判定／標籤是由**啟發式規則或代理指標**推算而來（而非真實的第一手數據源），必須在該欄位／圖例附近明確揭露，不能讓使用者誤以為是即時精確數據。現行案例：
  - UOA SWEEP/BLOCK/CROSS 分類（`_format_uoa_field()`、`watchlist_embeds.py` 心跳 UOA 表格）：由成交量整數手數形狀 + Bid/Ask 執行價位置兩套啟發式訊號組合而成，非真實 order-type tape 資料，表格下方固定附註揭露文字。
  - UOA ΔOI 欄位（`watchlist_embeds.py` 心跳 UOA 表格）：`uoa_detector` 在上游未提供實際未平倉變動時，以 `volume - open_interest` 代理推估，非真實 ΔOI；欄位標為 `ΔOI*` 並於表格下方附註揭露。
  - 🧲 共振磁吸／高階磁吸過濾（Radar Terminal `build_radar_scan_embed` 圖例、`magnetic_filters` 下拉選單描述）：`dp_poc` 是 Volume-POC/HVN 的代理指標，本平台無真實暗池數據源，固定附註揭露。
  - UOA Volume/OI 比例欄位：OI 是前一交易日收盤的未平倉量，非盤中即時數據（選擇權市場結構性限制，任何資料源皆同，非本平台獨有），比例欄位分母固定使用此值，表格下方固定附註揭露文字。
  - 新增此類代理判定時，比照上述既有案例的措辭與位置（表格／圖例附近、簡短一行）加入揭露，而非省略。
- **結構化網格與戰術意圖映射**：數據表格（如異常交易流、委託單列表、持倉明細）必須動態計算每列的最大字元寬度以對齊網格。底層原始數據流或交易類別應被映射轉換為直觀的戰術意圖描述，使終端使用者能迅速判讀意圖與支撐／阻力物理界線。
- **字數上限與分頁保護**：批次掃描或查詢的標的／項目數量過大時，為避免超出 Discord 的 4096（Description）與 6000（Total Size）字元上限而導致 `400 Bad Request`，單一頁面最多僅能承載 **10 個標的**；分頁後 Title 後方應標註 `(第 X/Y 頁)`。互動指令透過 Ephemeral 就地換頁，排程背景任務則經由 `queue_dm` 作為獨立訊息分開投遞，嚴禁在單一訊息中過度堆疊。此原則的完整分頁演算法與安全裕度精算，見 [`../architecture/04_engineering_standards.md`](../architecture/04_engineering_standards.md)。

## 3. Notification and Delivery Layer（DM 佇列投遞層）

`nexus_core/bot.py` 擁有持久化 DM 佇列。現行重要行為：

- 待發送通知會先入列儲存，才進行發送
- 啟動／關閉時會嘗試復原並清空佇列狀態
- 過長文字會自動切分
- 切分時會保留 fenced code block 的完整性
- 藉此防範 Discord `content <= 2000` 字元限制導致的失敗

撰寫或修改通知相關文件時，DM 佇列應被視為**持久化、具重試語意**的投遞機制，而非 fire-and-forget（發送後不管）。

## 4. 核心程式碼檔案路徑關聯

- `nexus_core/cogs/embed_builders/_core.py`：`NexusEmbed` 基底類別
- `nexus_core/cogs/embed_builder.py`：向後相容 shim
- `nexus_core/tests/unit/test_output_centralization.py`：Embed 集中化強制檢查
- `nexus_core/tests/unit/test_embed_builder.py`：Embed 契約測試
- `nexus_core/bot.py`：持久化 DM 佇列 worker、啟動／關閉復原邏輯
