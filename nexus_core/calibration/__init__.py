"""calibration — 離線回測校準工具 (事件研究 + 前向蒐集報告)。

⚠️ 本套件**永不修改任何程式碼或常數**，也**永不寫入資料庫**：輸出只有
`{out}/calibration/{UTC 時間戳}/` 下的 report.md / results.json / manifest.json。
建議值須經人工審核後，另以 PR 修改對應常數 (見 parameter_registry.py 的 code_path)。

在開發機執行，不要在 1GB VPS 上跑：

    cd nexus_core
    docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker python -m calibration fetch --max-symbols 40
    docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker python -m calibration run --offline --seed 7

規格見 docs/architecture/05_calibration_harness_and_forward_collection.md。
"""
