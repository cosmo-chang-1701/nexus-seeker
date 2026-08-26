"""
scheduler.py

nexus_edge_scraper 原本是純請求驅動的服務(見 AGENTS.md 描述)，完全沒有背景
排程。本模組是第一個常駐背景輪詢器：對 nexus_core 同步過來的自選標的清單
(`database.tracked_symbols`)，於盤中每 5 分鐘（+隨機 1~5 秒緩衝）輪詢一小批
標的的 GEX 與最近到期日的 Option Chain，寫入本地 SQLite，讓 local_api.py 的
快取讀取端點可以毫秒級回應，取代過去 nexus_core 每次查詢都觸發一次即時
Playwright/yfinance 抓取的行為。

採分批輪詢（見 POLL_ROTATION_CYCLES 註解）：整份追蹤清單約每 30 分鐘輪完
一次，維持與單一時間點全量刷新相近的更新頻率，但改成分散在多輪、時間點
隨機化的小批次請求，降低對 Yahoo 的請求爆量與固定週期特徵。

刻意不使用完整的 NYSE 假日行事曆 —— 假日多跑幾輪只會產生稍舊但無害的
快取，不影響正確性，維持 edge 端一貫的輕量風格。
"""

from typing import Optional
import asyncio
import logging
import math
import random
from datetime import datetime

from playwright.async_api import Browser, async_playwright

import database
from gex_scraper import scrape_symbol_gex_core
from yf_api import fetch_option_chain_dict, fetch_option_expiries

logger = logging.getLogger(__name__)

POLL_BASE_INTERVAL_SECONDS = 5 * 60  # 基礎輪詢間隔：5 分鐘
POLL_JITTER_SECONDS = (
    1.0,
    5.0,
)  # 隨機緩衝秒數，打亂固定週期避免被 Yahoo 偵測為機器人模式
# 分批輪詢：Yahoo/yfinance 的期權鏈端點是逐一標的設計（見 yf_api.py 的
# v7/finance/options/{symbol}），沒有合併多標的的批次查詢端點，所以無法在單一
# HTTP 請求內拿多檔資料。改為在排程層面分批：每輪只處理整份追蹤清單的一小批，
# 批次大小依 POLL_ROTATION_CYCLES 動態計算，讓整份清單約每 POLL_ROTATION_CYCLES
# 輪（≈30 分鐘）輪完一次，維持與舊版「每 30 分鐘全量刷新」相近的單一標的更新頻率，
# 但把原本集中在單一時間點的請求爆量，分散成多個較小、間隔隨機化的批次。
POLL_ROTATION_CYCLES = 6
MAX_CONCURRENCY = 2
PRUNE_AFTER_HOURS = 48

_task: Optional["asyncio.Task[None]"] = None
_poll_cursor = 0


def _is_us_market_hours() -> bool:
    """簡化版美東交易時間判斷(週一至週五 9:30-16:00 ET)，不處理國定假日。"""
    try:
        from zoneinfo import ZoneInfo

        now_ny = datetime.now(ZoneInfo("America/New_York"))
    except Exception as e:
        logger.warning(f"無法取得美東時區時間，改用 UTC 粗略估算: {e}")
        now_ny = datetime.utcnow()

    if now_ny.weekday() >= 5:  # Saturday/Sunday
        return False
    minutes = now_ny.hour * 60 + now_ny.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


async def _poll_symbol(symbol: str, browser: Browser, sem: "asyncio.Semaphore") -> None:
    async with sem:
        await asyncio.sleep(random.uniform(0.5, 1.5))

        try:
            gex_data = await scrape_symbol_gex_core(symbol, browser)
            await asyncio.to_thread(
                database.save_gex_snapshot,
                symbol,
                float(gex_data.get("spot", 0.0)),
                float(gex_data.get("net_gex", 0.0)),
                float(gex_data.get("call_wall", 0.0)),
                float(gex_data.get("put_wall", 0.0)),
                gex_data.get("gex_profile", {}),
            )
        except Exception as e:
            logger.warning(f"[{symbol}] 排程 GEX 抓取失敗: {e}")

        try:
            expiries = await fetch_option_expiries(symbol)
            if expiries:
                expiry = expiries[0]
                chain = await fetch_option_chain_dict(symbol, expiry)
                if chain:
                    await asyncio.to_thread(
                        database.save_option_chain_snapshot,
                        symbol,
                        expiry,
                        chain.get("calls", []),
                        chain.get("puts", []),
                    )
        except Exception as e:
            logger.warning(f"[{symbol}] 排程 Option Chain 抓取失敗: {e}")


def _next_batch(symbols: list[str]) -> list[str]:
    """依模組層級游標 `_poll_cursor` 取出下一小批標的（round-robin），
    並前進游標供下一輪使用。批次大小動態依 POLL_ROTATION_CYCLES 計算。"""
    global _poll_cursor
    batch_size = max(1, math.ceil(len(symbols) / POLL_ROTATION_CYCLES))
    batch_size = min(batch_size, len(symbols))
    start = _poll_cursor % len(symbols)
    batch = [symbols[(start + i) % len(symbols)] for i in range(batch_size)]
    _poll_cursor = start + batch_size
    return batch


async def poll_once() -> None:
    """對目前 tracked_symbols 清單取出下一小批標的，執行一輪 GEX + Option Chain 輪詢
    （分批輪詢，見模組頂端 POLL_ROTATION_CYCLES 註解）。"""
    symbols = await asyncio.to_thread(database.get_tracked_symbols)
    if not symbols:
        return

    batch = _next_batch(symbols)

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        try:
            await asyncio.gather(*(_poll_symbol(sym, browser, sem) for sym in batch))
        finally:
            await browser.close()

    pruned = await asyncio.to_thread(database.prune_stale_symbols, PRUNE_AFTER_HOURS)
    if pruned:
        logger.info(f"已清除 {pruned} 個逾時未同步的追蹤標的")


async def _loop() -> None:
    await asyncio.to_thread(database.init_db)
    while True:
        try:
            if _is_us_market_hours():
                await poll_once()
        except Exception as e:
            logger.error(f"背景輪詢迴圈執行失敗: {e}", exc_info=True)
        await asyncio.sleep(
            POLL_BASE_INTERVAL_SECONDS + random.uniform(*POLL_JITTER_SECONDS)
        )


def start() -> None:
    """啟動背景輪詢任務(idempotent)。"""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def stop() -> None:
    """取消背景輪詢任務。"""
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
