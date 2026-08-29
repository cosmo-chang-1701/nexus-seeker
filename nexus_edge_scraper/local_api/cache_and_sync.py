"""local_api：watchlist 同步、背景排程快取讀取端點、以及系統健康檢查。"""

from datetime import datetime, timezone
from typing import Any
import asyncio
import logging
import os

import psutil
from fastapi import APIRouter
from pydantic import BaseModel

import database

logger = logging.getLogger(__name__)

router = APIRouter()


class WatchlistSyncRequest(BaseModel):
    symbols: list[str]
    priority_symbols: list[str] = []


def _row_age_seconds(updated_at: str | None) -> float | None:
    if not updated_at:
        return None
    try:
        updated_dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        return (datetime.now(timezone.utc) - updated_dt).total_seconds()
    except Exception:
        return None


@router.post("/api/v1/watchlist/sync")
async def sync_watchlist_symbols(payload: WatchlistSyncRequest) -> dict[str, Any]:
    """nexus_core 於每次心跳前 best-effort 呼叫，同步目前全體使用者去重後的
    自選標的清單，以及應優先每輪必抓的持倉標的（priority_symbols），讓背景
    排程 (scheduler.py) 知道該輪詢哪些標的、以及哪些標的不受批次輪替影響。"""
    try:
        await asyncio.to_thread(
            database.upsert_tracked_symbols,
            payload.symbols,
            payload.priority_symbols,
        )
        return {
            "status": "success",
            "data": {
                "synced": len(payload.symbols),
                "priority_synced": len(payload.priority_symbols),
            },
        }
    except Exception as e:
        logger.warning(f"同步 watchlist 標的清單失敗: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/api/v1/cache/gex/{symbol}")
async def get_cached_gex(symbol: str) -> dict[str, Any]:
    """讀取背景排程寫入的 GEX 快照(毫秒級 SQLite 讀取，不觸發即時抓取)。"""
    try:
        row = await asyncio.to_thread(database.get_gex_snapshot, symbol)
        if not row:
            return {"status": "error", "message": "not_found"}
        return {
            "status": "success",
            "data": {
                "spot": row.get("spot", 0.0),
                "net_gex": row.get("net_gex", 0.0),
                "call_wall": row.get("call_wall", 0.0),
                "put_wall": row.get("put_wall", 0.0),
                "gex_profile": row.get("gex_profile", {}),
            },
            "age_seconds": _row_age_seconds(row.get("updated_at")),
        }
    except Exception as e:
        logger.warning(f"[{symbol}] 讀取 GEX 快取失敗: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/api/v1/cache/options/{symbol}/chain")
async def get_cached_option_chain(
    symbol: str, expiry: str | None = None
) -> dict[str, Any]:
    """讀取背景排程寫入的 Option Chain 快照(毫秒級 SQLite 讀取)。"""
    try:
        row = await asyncio.to_thread(
            database.get_option_chain_snapshot, symbol, expiry
        )
        if not row:
            return {"status": "error", "message": "not_found"}
        return {
            "status": "success",
            "data": {
                "expiry": row.get("expiry"),
                "calls": row.get("calls", []),
                "puts": row.get("puts", []),
            },
            "age_seconds": _row_age_seconds(row.get("updated_at")),
        }
    except Exception as e:
        logger.warning(f"[{symbol}] 讀取 Option Chain 快取失敗: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/api/v1/health/sys")
async def sys_health() -> dict[str, Any]:
    """Return OS-level resource usage of the Edge Node"""
    import platform

    mem = psutil.virtual_memory()
    cpu_load = psutil.cpu_percent()
    disk = psutil.disk_usage("/")
    process = psutil.Process(os.getpid())
    proc_mem = process.memory_info().rss / (1024 * 1024)
    swap = psutil.swap_memory()

    battery = psutil.sensors_battery()
    battery_data = None
    if battery is not None:
        battery_data = {
            "percent": round(battery.percent, 1),
            "power_plugged": battery.power_plugged,
            "secsleft": battery.secsleft,
        }

    return {
        "os_system": platform.system(),
        "os_release": platform.release(),
        "memory_percent": mem.percent,
        "memory_available_mb": mem.available / (1024**2),
        "cpu_percent": cpu_load,
        "process_memory_mb": proc_mem,
        "disk_percent": disk.percent,
        "disk_free_gb": disk.free / (1024**3),
        "swap_percent": swap.percent,
        "battery": battery_data,
    }
