#!/bin/bash
# Nexus Seeker pre-push hook（edge-mypy）：在 nexus_edge_scraper 容器內以完整依賴跑全量 Mypy。
# pre-commit 的 mypy-edge-scraper 只裝了部分 additional_dependencies，
# playwright / pandas / yfinance / pytest 等在那裡一律被當成 Any；此 hook 補上完整型別解析。

ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR/nexus_edge_scraper" || exit 1

echo "🔍 [Docker: nexus_edge_scraper] 正在執行 nexus_edge_scraper Mypy 靜態型別檢查 (全量檔案掃描)..."
docker compose run --rm nexus-edge-api bash -c 'mypy $(find . -maxdepth 3 -name "*.py" ! -path "*/build/*" ! -path "*/.venv/*" ! -path "*/.*")'
EDGE_MYPY_EXIT_CODE=$?

if [ $EDGE_MYPY_EXIT_CODE -ne 0 ]; then
    echo ""
    echo "❌ [nexus_edge_scraper] Mypy 靜態型別檢查失敗！"
    exit $EDGE_MYPY_EXIT_CODE
fi

echo "✅ [nexus_edge_scraper] Mypy 靜態型別檢查通過！"
exit 0
