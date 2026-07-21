#!/bin/bash
# Nexus Seeker Docker-based Pre-commit Hook

# 確保從項目根目錄執行
ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR/nexus_core"

echo "🔍 [Docker] 正在啟動容器化測試環境..."

# 使用 Docker Compose 執行測試
# --rm 確保測試完後刪除暫時容器
docker compose run --rm nexus-seeker python -m pytest tests

EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo "❌ 測試失敗！提交已攔截。"
    echo "請修正 Docker 環境中的錯誤後再嘗試提交。"
    exit $EXIT_CODE
fi

echo ""
echo "✅ Docker 測試全數通過，允許提交。"
exit 0
