#!/usr/bin/env bash
# 在 Droplet 上每個交易日收盤後執行 `python -m calibration micro-snapshot`。
# 規格與判讀準則：docs/architecture/05_calibration_harness_and_forward_collection.md §5.13
#
# 安裝（在 Droplet 上）：
#   sudo mkdir -p /opt/nexus-calibration && sudo chown 1001:1001 /opt/nexus-calibration
#   sudo cp micro_snapshot_cron.sh /opt/nexus-calibration/ && sudo chmod +x /opt/nexus-calibration/micro_snapshot_cron.sh
#   crontab -e   # 主機時區為 UTC 時：週一到週五 22:00 UTC = 美東 18:00 (夏令) / 17:00 (冬令)
#   0 22 * * 1-5 /opt/nexus-calibration/micro_snapshot_cron.sh
#
# 設計：
# - 使用與 production 相同的映像檔，以獨立的 `docker run` 執行並限制記憶體。
#   刻意不用 `docker exec` 進 bot 容器：兩者共用記憶體上限時，OOM 可能殺掉 bot。
# - 抓取經 market_data_service 的 edge 代理 (TUNNEL_URL)，避開 Yahoo 對資料中心
#   IP 的封鎖。TUNNEL_URL 從執行中的 bot 容器讀取 swarm secret，不寫入任何檔案，
#   以 `-e TUNNEL_URL`（只給變數名）傳入，值不出現在命令列參數中。
# - 非交易日、尚未收盤、當天快照已存在時，CLI 會自行略過，cron 重跑是安全的。

set -uo pipefail

SERVICE="${SERVICE:-nexus-seeker-service}"
BASE="${BASE:-/opt/nexus-calibration}"
MIN_AVAIL_MB="${MIN_AVAIL_MB:-500}"
CONTAINER_MEMORY="${CONTAINER_MEMORY:-400m}"

exec >>"$BASE/cron.log" 2>&1
echo "=== $(date -u '+%F %T UTC') ==="

# 可用記憶體不足就跳過，避免擠壓 bot
avail_mb=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
if [ "$avail_mb" -lt "$MIN_AVAIL_MB" ]; then
  echo "略過：可用記憶體 ${avail_mb}MB < ${MIN_AVAIL_MB}MB"
  exit 0
fi

IMAGE=$(docker service inspect "$SERVICE" \
  --format '{{.Spec.TaskTemplate.ContainerSpec.Image}}' 2>/dev/null | cut -d@ -f1)
CID=$(docker ps -q --filter "label=com.docker.swarm.service.name=$SERVICE" | head -1)
if [ -z "$IMAGE" ] || [ -z "$CID" ]; then
  echo "錯誤：找不到執行中的服務 $SERVICE"
  exit 1
fi

TUNNEL_URL=$(docker exec "$CID" cat /run/secrets/TUNNEL_URL 2>/dev/null || true)
export TUNNEL_URL
if [ -z "$TUNNEL_URL" ]; then
  echo "警告：讀不到 TUNNEL_URL，將直連 Yahoo（資料中心 IP 可能被封鎖）"
fi

run() {
  docker run --rm \
    --memory "$CONTAINER_MEMORY" --cpus 0.5 \
    --user 1001:1001 \
    -e HOME=/tmp -e TZ=America/New_York -e TUNNEL_URL \
    -v nexus-data:/app/data \
    -v "$BASE":/app/.calibration_cache \
    "$IMAGE" "$@"
}

# 以唯讀方式備份 DB：標的池取 watchlist ∪ 固定流動性清單。備份失敗時只用固定清單。
run python -c "
import sqlite3
src = sqlite3.connect('file:/app/data/nexus_data.db?mode=ro', uri=True)
dst = sqlite3.connect('/app/.calibration_cache/snapshot.db')
src.backup(dst)
dst.close()
src.close()
" || echo "警告：DB 備份失敗，標的池只使用固定流動性清單"

run sh -c 'NEXUS_DB_NAME=/app/.calibration_cache/snapshot.db exec python -m calibration micro-snapshot --max-symbols 200 --force'
echo "exit=$?"
