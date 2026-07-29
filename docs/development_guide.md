# 開發者指南 (Development Guide)

歡迎參與 Nexus Seeker 專案開發！本專案實施嚴格的品質控制與架構規範。

## 核心規範 (Conventions)

### 型別安全 (Type Safety)
專案全面啟用 `mypy` 嚴格模式 (Strict Mode)：
- **Union 安全性**：存取 nullable 物件屬性前（例如 `interaction.message`）必須進行顯式檢查（`if obj is not None:`）。
- **參數宣告**：所有函式與變數皆需加上 Type Hints，優先使用 Pydantic Models 而非零散的 dicts。

### Embed 集中化渲染 (Output Centralization)
為了確保全站視覺風格一致與防止 Discord API 長度錯誤：
- 所有 UI (Cogs, Views, Modals) **禁止直接實例化 `discord.Embed`**。
- Embed 的建構必須透過 `cogs/embed_builders/` 模組，並回傳封裝好的 `NexusEmbed` 類別。
- `NexusEmbed` 內建顏色調色盤（資訊藍、警報紅、收租綠等）與標準化頁尾。

### 量化控制台排版 (ANSI Grid)
- 實時行情、量化數據與持倉等面板，必須使用 ````ansi` 程式碼區塊包裹以進行等寬字體渲染。
- 數據表格的每一列需動態計算最大字元寬度以對齊網格。

## 本地開發與測試

測試環境必須透過 Docker Compose 執行，以確保依賴與作業系統環境一致。

```bash
cd nexus_core

# 執行全域型別檢查
docker compose run --rm nexus-seeker python -m mypy --config-file pyproject.toml .

# 執行所有單元測試
docker compose run --rm nexus-seeker python -m pytest tests

# 執行特定測試檔案
docker compose run --rm nexus-seeker python -m pytest tests/unit/test_intraday_pipeline.py
```

## 資料庫變更
- 所有的 SQLite 資料表變更都必須透過 Migration 腳本處理，請勿手動修改 Schema。
- 新增的 Migration 檔案應放置於 `nexus_core/database/migrations/` 目錄。
