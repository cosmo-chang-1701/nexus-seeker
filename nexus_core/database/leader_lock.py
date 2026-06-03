import sqlite3
import time
import logging

import config

logger = logging.getLogger(__name__)


LOCK_NAME_DISCORD_BOT = "discord_bot"


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
    conn = sqlite3.connect(config.DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute(
            "SELECT instance_id, heartbeat_ts FROM runtime_leader_lock WHERE name = ?",
            (name,),
        )
        row = cursor.fetchone()

        if row is None:
            cursor.execute(
                "INSERT INTO runtime_leader_lock (name, instance_id, heartbeat_ts) VALUES (?, ?, ?)",
                (name, instance_id, now),
            )
            conn.commit()
            return True

        current_holder = str(row["instance_id"])
        last_hb = int(row["heartbeat_ts"])
        expired = (now - last_hb) > ttl_seconds

        if current_holder == instance_id or expired:
            cursor.execute(
                "UPDATE runtime_leader_lock SET instance_id = ?, heartbeat_ts = ? WHERE name = ?",
                (instance_id, now, name),
            )
            conn.commit()
            return True

        return False
    except Exception as e:
        conn.rollback()
        logger.debug(f"Leader lock acquire failed: {e}")
        return False
    finally:
        conn.close()


def release_leader_lock(name: str, instance_id: str) -> None:
    """Release the lock if we currently hold it."""
    conn = sqlite3.connect(config.DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM runtime_leader_lock WHERE name = ? AND instance_id = ?",
            (name, instance_id),
        )
        conn.commit()
    finally:
        conn.close()
