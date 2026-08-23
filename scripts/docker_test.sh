#!/bin/bash
# Nexus Seeker Docker-based Pre-commit Hook

# 確保從項目根目錄執行
ROOT_DIR="$(git rev-parse --show-toplevel)"

echo "🔍 [Docker: nexus_core] 正在執行 nexus_core Mypy 靜態型別檢查..."
cd "$ROOT_DIR/nexus_core"
docker compose run --rm nexus-seeker mypy .
CORE_MYPY_EXIT_CODE=$?

if [ $CORE_MYPY_EXIT_CODE -ne 0 ]; then
    echo ""
    echo "❌ [nexus_core] Mypy 靜態型別檢查失敗！"
    exit $CORE_MYPY_EXIT_CODE
fi

echo ""
echo "🔍 [Docker: nexus_core] 正在執行 nexus_core 容器化測試 (Pytest)..."
docker compose run --rm nexus-seeker python -m pytest tests -p no:cacheprovider
CORE_EXIT_CODE=$?

if [ $CORE_EXIT_CODE -ne 0 ]; then
    echo ""
    echo "❌ [nexus_core] Pytest 測試失敗！"
    exit $CORE_EXIT_CODE
fi

echo ""
echo "🔍 [Docker: nexus_edge_scraper] 正在執行 nexus_edge_scraper Mypy 靜態型別檢查..."
cd "$ROOT_DIR/nexus_edge_scraper"
docker compose run --rm nexus-edge-api mypy .
EDGE_MYPY_EXIT_CODE=$?

if [ $EDGE_MYPY_EXIT_CODE -ne 0 ]; then
    echo ""
    echo "❌ [nexus_edge_scraper] Mypy 靜態型別檢查失敗！"
    exit $EDGE_MYPY_EXIT_CODE
fi

echo ""
echo "🔍 [Docker: nexus_edge_scraper] 正在執行 nexus_edge_scraper 容器化測試 (Pytest)..."
docker compose run --rm nexus-edge-api python -m pytest tests -p no:cacheprovider -v
EDGE_EXIT_CODE=$?

if [ $EDGE_EXIT_CODE -ne 0 ]; then
    echo ""
    echo "❌ [nexus_edge_scraper] Pytest 測試失敗！"
    exit $EDGE_EXIT_CODE
fi

echo ""
echo "✅ Docker 完整測試 (nexus_core + nexus_edge_scraper) 全數通過！"
exit 0
