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
   # 全量（與 CI 相同）；xdist 平行，worker 數受 mem_limit 850m 限制，勿用 -n auto
   docker compose run --rm nexus-seeker python -m pytest tests -n 2 --dist loadfile
   # 快速子集（與 pre-push hook 相同）
   docker compose run --rm nexus-seeker python -m pytest tests -m "not slow and not integration" -n 2 --dist loadfile
   ```
   更新 `pyproject.toml` 的依賴（例如首次引入 `pytest-xdist`）後，需先 `docker compose build` 重建映像。

3. **pre-push hook 與逃生門**
   - `git push` 時的 `core-test` hook（`scripts/docker_test.sh`）只跑 core 全量 Mypy 與**快速子集**
     （`-m "not slow and not integration"`）；`slow` 與 `integration` 測試由 CI 全量把關。
   - 想在本機跑全量：`NEXUS_FULL_TESTS=1 git push`。
   - 只有在 CI 一定會把關的情況下（例如 rebase 後重新推送同一批已驗證的提交），才可略過 hook：
     `SKIP=core-test,scraper-test,edge-mypy git push` 或 `git push --no-verify`。
   - `tests/integration/` 下的測試由 `tests/conftest.py` 自動標記為 `integration`。
   - **新增的慢測試必須標記 `pytest.mark.slow`**：單一測試約 1 秒以上者逐一加 `@pytest.mark.slow`；
     成本分散在多個測試、整檔累計約 3 秒以上者整檔 `pytestmark = pytest.mark.slow`。
     pytest 以 `--strict-markers` 執行，拼錯的 marker 會直接報錯。
   - 四個 AST 不變式測試（`test_db_write_centralization.py`、`test_output_centralization.py`、
     `test_notification_dispatch_centralization.py`、`test_kv_cache_dedup_whitelist.py`）**不得**標記為 slow，
     必須留在快速子集。
   - `nexus_edge_scraper/` 有變更時才會觸發 `scraper-test`（edge pytest）與 `edge-mypy`
     （`scripts/docker_edge_mypy.sh`，在 edge 映像內以完整依賴跑全量 Mypy）。

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
