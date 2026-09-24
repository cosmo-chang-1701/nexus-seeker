#!/bin/bash
# Nexus Seeker pre-push hook（core-test）：nexus_core 的 Docker 型別檢查 + 快速測試子集
#
# 預設：core 全量 Mypy → 快速子集 Pytest（-m "not slow and not integration"，xdist 平行）
# NEXUS_FULL_TESTS=1 git push：改跑不帶 -m 篩選的全量測試（與 CI 相同的測試集合）
#
# nexus_edge_scraper 不在此腳本內：edge 測試由 `scraper-test` hook（路徑觸發）與 CI 負責，
# edge 全量 Mypy 由 `edge-mypy` hook（路徑觸發）負責。

# xdist worker 數受 docker-compose.yml 的 mem_limit: 850m 限制，勿改成 -n auto。
# 實測（2026-09）：每個 worker 約 330MB anon；-n 2 峰值約 655~715MB，-n 3 起即觸頂 850m 開始換頁。
XDIST_ARGS="-n 2 --dist loadfile"
FAST_FILTER="not slow and not integration"

ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR/nexus_core" || exit 1

echo "🔍 [Docker: nexus_core] 正在執行 nexus_core Mypy 靜態型別檢查 (全量檔案掃描)..."
docker compose run --rm nexus-seeker bash -c 'mypy $(find . -maxdepth 3 -name "*.py" ! -path "*/build/*" ! -path "*/.venv/*" ! -path "*/.*")'
CORE_MYPY_EXIT_CODE=$?

if [ $CORE_MYPY_EXIT_CODE -ne 0 ]; then
    echo ""
    echo "❌ [nexus_core] Mypy 靜態型別檢查失敗！"
    exit $CORE_MYPY_EXIT_CODE
fi

if [ "${NEXUS_FULL_TESTS:-0}" = "1" ]; then
    echo ""
    echo "🔍 [Docker: nexus_core] NEXUS_FULL_TESTS=1：正在執行全量容器化測試 (Pytest, ${XDIST_ARGS})..."
    MARK_ARGS=()
else
    echo ""
    echo "🔍 [Docker: nexus_core] 正在執行快速測試子集 (-m \"${FAST_FILTER}\", ${XDIST_ARGS})..."
    echo "   全量測試由 CI 把關；如需在本機跑全量：NEXUS_FULL_TESTS=1 git push"
    MARK_ARGS=(-m "$FAST_FILTER")
fi

# 映像若是在加入 pytest-xdist 之前建置的，`-n` 會以難懂的 usage error 失敗；先給出明確提示。
# shellcheck disable=SC2086
docker compose run --rm nexus-seeker bash -c '
if ! python -c "import xdist" 2>/dev/null; then
    echo "❌ 映像內缺少 pytest-xdist，請先在 nexus_core/ 執行：docker compose build"
    exit 3
fi
exec python -m pytest tests "$@"
' pytest "${MARK_ARGS[@]}" $XDIST_ARGS -p no:cacheprovider
CORE_EXIT_CODE=$?

if [ $CORE_EXIT_CODE -ne 0 ]; then
    echo ""
    echo "❌ [nexus_core] Pytest 測試失敗！"
    exit $CORE_EXIT_CODE
fi

echo ""
if [ "${NEXUS_FULL_TESTS:-0}" = "1" ]; then
    echo "✅ Docker nexus_core 型別檢查與全量測試全數通過！"
else
    echo "✅ Docker nexus_core 型別檢查與快速測試子集全數通過！（slow / integration 由 CI 全量把關）"
fi
exit 0
