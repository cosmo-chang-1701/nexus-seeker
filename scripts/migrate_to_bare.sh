#!/usr/bin/env bash
# Nexus Seeker: Automated Migration Script to Git Worktree Bare Repo
set -e

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

echo "=== 🌌 Nexus Seeker Bare Repo 架構遷移開始 ==="

# 檢查是否已經是 bare repo
if [ -d ".bare" ] && [ -f ".git" ]; then
    echo "ℹ️ 專案已處於 Bare Repo 架構中，無需重複遷移。"
    exit 0
fi

# 1. 確保 git 狀態無未提交的 tracked 變更
if [ -n "$(git status --porcelain)" ]; then
    echo "❌ 錯誤：工作目錄有未提交的變更，請先提交或暫存後再執行！"
    exit 1
fi

# 2. 建立不可逆防護快照備份至 /tmp
SNAPSHOT_DIR="/tmp/nexus_seeker_migration_backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SNAPSHOT_DIR"
echo "📦 [1/7] 建立快照備份至 $SNAPSHOT_DIR ..."
cp -a .git "$SNAPSHOT_DIR/.git"
[ -f "nexus_core/.env" ] && cp -a "nexus_core/.env" "$SNAPSHOT_DIR/nexus_core.env"
[ -f "nexus_edge_scraper/.env" ] && cp -a "nexus_edge_scraper/.env" "$SNAPSHOT_DIR/nexus_edge_scraper.env"
[ -f "nexus_core/database/nexus_main.db" ] && cp -a "nexus_core/database/nexus_main.db"* "$SNAPSHOT_DIR/" 2>/dev/null || true
[ -f "nexus_core/nexus.db" ] && cp -a "nexus_core/nexus.db"* "$SNAPSHOT_DIR/" 2>/dev/null || true

# 3. 轉換 .git 為 .bare
echo "🔄 [2/7] 轉換為 Bare Repository (.bare/) ..."
mv .git .bare
git --git-dir=.bare config core.bare true
git --git-dir=.bare config remote.origin.fetch "+refs/heads/*:refs/remotes/origin/*"

# 4. 建立根目錄 .git 指標
echo "📌 [3/7] 建立根目錄 .git 指標檔 ..."
printf "gitdir: ./.bare\n" > .git

# 5. 建立 main worktree
echo "🌿 [4/7] 建立 main 分支 Worktree ..."
git worktree add main main

# 6. 狀態搬移至 main/
echo "🚚 [5/7] 遷移機密與未版控狀態至 main/ ..."
[ -f "nexus_core/.env" ] && cp -a "nexus_core/.env" "main/nexus_core/.env"
[ -f "nexus_edge_scraper/.env" ] && cp -a "nexus_edge_scraper/.env" "main/nexus_edge_scraper/.env"
[ -f "nexus_core/database/nexus_main.db" ] && cp -a "nexus_core/database/nexus_main.db"* "main/nexus_core/database/" 2>/dev/null || true
[ -f "nexus_core/nexus.db" ] && cp -a "nexus_core/nexus.db"* "main/nexus_core/" 2>/dev/null || true
[ -d "nexus_core/.calibration_cache" ] && cp -a "nexus_core/.calibration_cache" "main/nexus_core/"
[ -d "nexus_core/reports" ] && cp -a "nexus_core/reports" "main/nexus_core/"
[ -d "nexus_core/data" ] && cp -a "nexus_core/data" "main/nexus_core/"
[ -d "nexus_core/.venv" ] && cp -a "nexus_core/.venv" "main/nexus_core/"
[ -d "nexus_edge_scraper/.venv" ] && cp -a "nexus_edge_scraper/.venv" "main/nexus_edge_scraper/"
[ -d ".agents" ] && cp -a .agents/* "main/.agents/" 2>/dev/null || true
[ -d ".vscode" ] && cp -a ".vscode" "main/"
[ -d ".antigravitycli" ] && cp -a ".antigravitycli" "main/"
[ -d ".claude" ] && cp -a ".claude" "main/"

# 7. 虛擬環境 shebang 修復
if [ -d "main/nexus_core/.venv/bin" ]; then
    echo "🔧 [6/7] 修復 Python 虛擬環境 shebang 路徑 ..."
    sed -i "s#$REPO_ROOT/nexus_core/.venv#$REPO_ROOT/main/nexus_core/.venv#g" "main/nexus_core/.venv/bin/"* 2>/dev/null || true
fi

# 8. 根目錄精確清理 (嚴禁 rm -rf *)
echo "🧹 [7/7] 依白名單清理容器根目錄舊檔案 ..."
rm -rf nexus_core nexus_edge_scraper docs scripts assets dist \
       .agents .vscode .antigravitycli .claude .github \
       .mypy_cache .pytest_cache .ruff_cache
rm -f AGENTS.md README.md CONTRIBUTING.md Dockerfile.test LICENSE \
      .pre-commit-config.yaml .gitignore

# 9. 重新安裝 hooks
if [ -x "main/nexus_core/.venv/bin/pre-commit" ]; then
    (cd main && ./nexus_core/.venv/bin/pre-commit install && ./nexus_core/.venv/bin/pre-commit install --hook-type pre-push)
fi

# 10. 建立根目錄輔助連結/檔案
cp main/scripts/wt.sh ./wt
chmod +x ./wt
cp main/nexus-seeker.code-workspace ./nexus-seeker.code-workspace

echo "🎉 === Nexus Seeker Bare Repo 架構遷移完成！==="
echo "所有開發工作請進入 main/ 目錄：cd main"
