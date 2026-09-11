import logging
import time

from database.connection import execute_write_rowcount

logger = logging.getLogger(__name__)


LOCK_NAME_DISCORD_BOT = "discord_bot"


# 讀後寫（SELECT 現況 → 決定是否接管）原本以 `BEGIN IMMEDIATE` 包成一個交易，
# 那是一條**自開連線、每 10 秒取一次獨占寫入鎖**的路徑，與寫入佇列直接互搶。
# 改寫成單一原子 UPSERT 後即可走佇列，語意由 WHERE 子句保證：
#   - 本實例已持有 → 續租
#   - 租約逾期 → 接管
#   - 他人持有且未逾期 → WHERE 不成立，rowcount 為 0，不搶
# 藍綠部署期間兩個容器並存時，這是跨程序寫入，互斥仍由 SQLite 的寫入鎖與此語句
# 本身的原子性保證——而這正是改成單一語句（而非讀後寫）的理由。
_ACQUIRE_SQL = """
INSERT INTO runtime_leader_lock (name, instance_id, heartbeat_ts)
VALUES (?, ?, ?)
ON CONFLICT(name) DO UPDATE SET
    instance_id = excluded.instance_id,
    heartbeat_ts = excluded.heartbeat_ts
WHERE runtime_leader_lock.instance_id = excluded.instance_id
   OR (excluded.heartbeat_ts - runtime_leader_lock.heartbeat_ts) > ?
"""


def try_acquire_leader_lock(
    name: str,
    instance_id: str,
    ttl_seconds: int = 30,
) -> bool:
    """Try to acquire/renew a leader lock.

    Uses a single-row lease model in SQLite:
    - If no row exists, insert and become leader.
    - If lease expired, take over.
    - If we already hold it, renew.
    """
    now = int(time.time())
    try:
        changed = execute_write_rowcount(
            _ACQUIRE_SQL, (name, instance_id, now, ttl_seconds)
        )
        return changed > 0
    except Exception as e:
        logger.debug(f"Leader lock acquire failed: {e}")
        return False


def release_leader_lock(name: str, instance_id: str) -> None:
    """Release the lock if we currently hold it."""
    try:
        execute_write_rowcount(
            "DELETE FROM runtime_leader_lock WHERE name = ? AND instance_id = ?",
            (name, instance_id),
        )
    except Exception as e:
        logger.debug(f"Leader lock release failed: {e}")
