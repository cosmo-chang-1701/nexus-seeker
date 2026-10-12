#!/usr/bin/env bash
# Nexus Seeker: Automated Rollback Script from Bare Repo Migration
set -e

REPO_ROOT="$(pwd)"
LATEST_BACKUP=$(ls -td /tmp/nexus_seeker_migration_backup_* 2>/dev/null | head -n 1)

if [ -z "$LATEST_BACKUP" ] || [ ! -d "$LATEST_BACKUP" ]; then
    echo "❌ 找不到可用的備份快照目錄！"
    exit 1
fi

echo "⚠️ 正在從快照 $LATEST_BACKUP 執行回滾..."
cd "$REPO_ROOT"

rm -rf main test-lifecycle-check .bare .git wt nexus-seeker.code-workspace
cp -a "$LATEST_BACKUP/.git" .git
git reset --hard HEAD

[ -f "$LATEST_BACKUP/nexus_core.env" ] && cp -a "$LATEST_BACKUP/nexus_core.env" nexus_core/.env
[ -f "$LATEST_BACKUP/nexus_edge_scraper.env" ] && cp -a "$LATEST_BACKUP/nexus_edge_scraper.env" nexus_edge_scraper/.env
cp -a "$LATEST_BACKUP/nexus_main.db"* nexus_core/database/ 2>/dev/null || true
cp -a "$LATEST_BACKUP/nexus.db"* nexus_core/ 2>/dev/null || true

echo "✅ 回滾完成，專案已還原為原先標準目錄狀態。"
