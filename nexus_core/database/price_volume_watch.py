"""個股 15 分鐘價量突破警報 — 每用戶監測設定 CRUD。

使用專屬 SQLite 表 `price_volume_watches`（而非 kv_cache），因為排程器需要
跨使用者、跨標的做批次查詢（`get_all_watches`），kv_cache 的單一 key 結構
無法有效支援此種查詢。
"""

import sqlite3
import logging
from enum import Enum
from typing import List

import config
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# 單一使用者可同時監測的標的數量上限，避免無限制增長拖慢排程器
# 掃描週期（VPS 記憶體與 API 呼叫量防護）。
_MAX_WATCHES_PER_USER = 15


class WatchDirection(str, Enum):
    """價量警報的方向：向上突破 (>=) 或向下跌破 (<=)。"""

    ABOVE = "above"
    BELOW = "below"


class PriceVolumeWatch(BaseModel):
    """個股價量突破警報的單筆監測設定模型。"""

    user_id: int
    symbol: str
    target_price: float = Field(gt=0)
    direction: WatchDirection = WatchDirection.ABOVE
    volume_multiplier: float = Field(default=1.5, ge=0.0, le=5.0)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, v: str) -> str:
        return str(v).strip().upper()

    @field_validator("target_price", mode="before")
    @classmethod
    def round_price(cls, v: float) -> float:
        return round(float(v), 2)

    @field_validator("volume_multiplier", mode="before")
    @classmethod
    def round_multiplier(cls, v: float) -> float:
        return round(float(v), 2)


class WatchLimitExceededError(Exception):
    """使用者監測數量超過 `_MAX_WATCHES_PER_USER` 上限時拋出。"""


async def upsert_watch(
    user_id: int,
    symbol: str,
    target_price: float,
    direction: WatchDirection = WatchDirection.ABOVE,
    volume_multiplier: float = 1.5,
) -> PriceVolumeWatch:
    """新增或更新一筆使用者的價量監測設定 (以 user_id + symbol 為主鍵 upsert)。"""
    watch = PriceVolumeWatch(
        user_id=user_id,
        symbol=symbol,
        target_price=target_price,
        direction=direction,
        volume_multiplier=volume_multiplier,
    )

    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT COUNT(*) FROM price_volume_watches WHERE user_id = ? AND symbol != ?",
            (user_id, watch.symbol),
        )
        existing_count = cursor.fetchone()[0]
        if existing_count >= _MAX_WATCHES_PER_USER:
            raise WatchLimitExceededError(
                f"監測標的數量已達上限 ({_MAX_WATCHES_PER_USER} 檔)，請先移除部分監測後再新增。"
            )

        cursor.execute(
            """
            INSERT INTO price_volume_watches
                (user_id, symbol, target_price, direction, volume_multiplier, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, symbol) DO UPDATE SET
                target_price = excluded.target_price,
                direction = excluded.direction,
                volume_multiplier = excluded.volume_multiplier,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                watch.symbol,
                watch.target_price,
                watch.direction.value,
                watch.volume_multiplier,
            ),
        )
        conn.commit()
    finally:
        if conn:
            conn.close()

    return watch


def get_user_watches(user_id: int) -> List[PriceVolumeWatch]:
    """取得指定使用者的所有價量監測設定。"""
    return _query_watches(
        """
        SELECT user_id, symbol, target_price, direction, volume_multiplier
        FROM price_volume_watches
        WHERE user_id = ?
        ORDER BY symbol ASC
        """,
        (user_id,),
    )


def get_all_watches() -> List[PriceVolumeWatch]:
    """取得所有使用者的價量監測設定 (供排程器批次掃描使用)。"""
    return _query_watches(
        """
        SELECT user_id, symbol, target_price, direction, volume_multiplier
        FROM price_volume_watches
        ORDER BY symbol ASC
        """,
        (),
    )


def _query_watches(query: str, params: tuple) -> List[PriceVolumeWatch]:
    conn = None
    results: List[PriceVolumeWatch] = []
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute(query, params)
        for row in cursor.fetchall():
            uid, symbol, target_price, direction, volume_multiplier = row
            try:
                results.append(
                    PriceVolumeWatch(
                        user_id=uid,
                        symbol=symbol,
                        target_price=target_price,
                        direction=direction,
                        volume_multiplier=volume_multiplier,
                    )
                )
            except Exception as e:
                logger.warning(
                    f"價量監測設定解析失敗 (uid={uid}, symbol={symbol}): {e}"
                )
    except Exception as e:
        logger.error(f"讀取價量監測設定失敗: {e}")
    finally:
        if conn:
            conn.close()
    return results


async def delete_watch(user_id: int, symbol: str) -> bool:
    """移除一筆使用者的價量監測設定，回傳是否有實際刪除到資料列。"""
    normalized_symbol = symbol.strip().upper()
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM price_volume_watches WHERE user_id = ? AND symbol = ?",
            (user_id, normalized_symbol),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        logger.error(
            f"刪除價量監測設定失敗 (uid={user_id}, symbol={normalized_symbol}): {e}"
        )
        return False
    finally:
        if conn:
            conn.close()


__all__: list[str] = [
    "WatchDirection",
    "PriceVolumeWatch",
    "WatchLimitExceededError",
    "upsert_watch",
    "get_user_watches",
    "get_all_watches",
    "delete_watch",
]
