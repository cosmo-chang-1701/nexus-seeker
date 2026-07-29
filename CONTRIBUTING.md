# Contributing to Nexus Seeker

感謝您對 Nexus Seeker 的興趣與貢獻！為維持專案的高品質與量化邏輯的嚴謹性，請遵循以下開發規範：

## 分支規範 (Branching)
- `main`：穩定的生產環境分支。
- `feature/*`：用於開發新功能。
- `bugfix/*`：用於修復已知錯誤。

## 本地開發與測試 (Local Development & Testing)
本專案嚴格依賴 Docker 容器環境以確保依賴的一致性。
在發起 Pull Request 前，**您必須在本地端完成以下檢查**：

1. **型別安全檢查 (Mypy)**
   ```bash
   cd nexus_core
   docker compose run --rm nexus-seeker python -m mypy --config-file pyproject.toml .
   ```
2. **單元與整合測試 (Pytest)**
   如果您新增了策略或功能，請確保為其補上對應的測試案例。
   ```bash
   cd nexus_core
   docker compose run --rm nexus-seeker python -m pytest tests
   ```

## Pull Request 流程 (PR Process)
1. Fork 本專案並建立您的特性分支。
2. 確保您的程式碼遵守 `docs/development_guide.md` 中的所有架構規範（如 Embed 集中化、不直接修改 Schema 等）。
3. 發起 PR，並清楚描述您的修改動機、受影響的模組以及測試結果。
4. 等待 GitHub Actions (CI) 通過並由維護者審核。

## 開發文件 (Documentation)
開始撰寫程式碼前，請務必先閱讀以下架構文件：
- [系統架構](docs/architecture.md)
- [量化策略與風控邏輯](docs/quant_strategy.md)
- [開發與排版規範](docs/development_guide.md)
