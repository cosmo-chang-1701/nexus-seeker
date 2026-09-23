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

其中被標記為 priority（實際持倉標的，見 database.upsert_tracked_symbols）
的標的不受批次輪替影響，每輪都會被抓取，將它們的延遲上限從 ~30 分鐘壓到
單一輪詢週期本身（~5 分鐘），其餘一般自選標的仍走原本的批次輪替。

刻意不使用完整的 NYSE 假日行事曆 —— 假日多跑幾輪只會產生稍舊但無害的
快取，不影響正確性，維持 edge 端一貫的輕量風格。
"""

from typing import Any, Optional
import asyncio
import logging
import math
import random
import time
from datetime import datetime

from playwright.async_api import Browser, async_playwright

import database
from gex_scraper import scrape_symbol_gex_core
from yf_api import (
    fetch_last_close,
    fetch_nearest_option_chain,
    fetch_option_chain_dict,
    fetch_option_expiries,
)

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

# 收盤後 EM 快照 (D-03 週預期波幅校準)：美東平日 16:20~20:00 之間每天一次。
# 16:20 讓收盤價與期權鏈定案；20:00 之前結束，避開 Yahoo 約在美東午夜到開盤前
# 重置期權鏈 (未平倉量歸 0、IV 1e-5) 的時段。不處理國定假日 (edge 維持輕量、
# 不引入 NYSE 行事曆)：假日記錄到的是前一交易日的資料，由 nexus_core 的離線
# 報告以 NYSE 行事曆濾除。
EM_SNAPSHOT_WINDOW_MINUTES = (16 * 60 + 20, 20 * 60)
EM_SNAPSHOT_MIN_DTE = 1
EM_SNAPSHOT_MAX_DTE = 14
EM_SNAPSHOT_MAX_EXPIRIES = 6

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


def _now_ny() -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/New_York"))


def _is_em_snapshot_window(now_ny: Optional[datetime] = None) -> bool:
    now = now_ny or _now_ny()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return EM_SNAPSHOT_WINDOW_MINUTES[0] <= minutes <= EM_SNAPSHOT_WINDOW_MINUTES[1]


def _mid(row: dict[str, Any]) -> float:
    """(bid+ask)/2；報價缺失時退回 lastPrice。與 nexus_core iv_metrics 的定義一致。"""

    def _num(key: str) -> float:
        try:
            value = float(row.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) else 0.0

    bid, ask = _num("bid"), _num("ask")
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return _num("lastPrice")


def compute_atm_straddle(
    calls: list[dict[str, Any]], puts: list[dict[str, Any]], spot: float
) -> Optional[tuple[float, float, float]]:
    """取最接近現價、Call 與 Put 都有報價的履約價，回傳 (strike, call_mid, put_mid)。"""
    if spot <= 0:
        return None
    call_by_strike: dict[float, float] = {}
    for row in calls:
        try:
            call_by_strike[float(row["strike"])] = _mid(row)
        except (KeyError, TypeError, ValueError):
            continue
    put_by_strike: dict[float, float] = {}
    for row in puts:
        try:
            put_by_strike[float(row["strike"])] = _mid(row)
        except (KeyError, TypeError, ValueError):
            continue
    common = [
        k
        for k in call_by_strike
        if k in put_by_strike and call_by_strike[k] > 0 and put_by_strike[k] > 0
    ]
    if not common:
        return None
    strike = min(common, key=lambda k: abs(k - spot))
    return strike, call_by_strike[strike], put_by_strike[strike]


async def _snapshot_symbol_em(
    symbol: str, trade_date: str, sem: "asyncio.Semaphore"
) -> list[dict[str, Any]]:
    from datetime import date as _date

    async with sem:
        try:
            spot = await fetch_last_close(symbol)
            if not spot or spot <= 0:
                return []
            expiries = await fetch_option_expiries(symbol)
            today = _date.fromisoformat(trade_date)
            targets: list[tuple[str, int]] = []
            for exp in sorted(expiries):
                try:
                    dte = (_date.fromisoformat(exp) - today).days
                except ValueError:
                    continue
                if EM_SNAPSHOT_MIN_DTE <= dte <= EM_SNAPSHOT_MAX_DTE:
                    targets.append((exp, dte))
            rows: list[dict[str, Any]] = []
            for exp, dte in targets[:EM_SNAPSHOT_MAX_EXPIRIES]:
                await asyncio.sleep(random.uniform(0.5, 1.5))
                chain = await fetch_option_chain_dict(symbol, exp)
                if not chain:
                    continue
                atm = compute_atm_straddle(
                    chain.get("calls", []), chain.get("puts", []), spot
                )
                if atm is None:
                    continue
                strike, call_mid, put_mid = atm
                rows.append(
                    {
                        "symbol": symbol,
                        "expiry": exp,
                        "dte": dte,
                        "spot": spot,
                        "strike": strike,
                        "call_mid": call_mid,
                        "put_mid": put_mid,
                    }
                )
            return rows
        except Exception as e:
            logger.warning(f"[{symbol}] 收盤後 EM 快照抓取失敗: {e}")
            return []


_last_em_snapshot_date: Optional[str] = None


async def record_em_snapshot_once(now_ny: Optional[datetime] = None) -> int:
    """收盤後 EM 快照：每個美東日期最多成功執行一次，回傳寫入列數。

    以 DB 檢查 (has_em_snapshot) 為準，服務重啟後不會重複抓取；模組層級旗標只是
    省去每輪都查一次 DB。沒有抓到任何資料時不設旗標，下一輪 (5 分鐘後) 會重試。
    """
    global _last_em_snapshot_date
    trade_date = (now_ny or _now_ny()).strftime("%Y-%m-%d")
    if _last_em_snapshot_date == trade_date:
        return 0
    if await asyncio.to_thread(database.has_em_snapshot, trade_date):
        _last_em_snapshot_date = trade_date
        return 0

    symbols = await asyncio.to_thread(database.get_tracked_symbols)
    if not symbols:
        return 0
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    results = await asyncio.gather(
        *(_snapshot_symbol_em(sym, trade_date, sem) for sym in symbols)
    )
    rows = [row for batch in results for row in batch]
    written = await asyncio.to_thread(database.save_em_snapshot, trade_date, rows)
    if written:
        _last_em_snapshot_date = trade_date
    logger.info(
        f"[em_snapshot] {trade_date}：{len(symbols)} 檔標的，寫入 {written} 筆到期日跨式"
    )
    return int(written)


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
            chain = await fetch_nearest_option_chain(symbol)
            if chain:
                await asyncio.to_thread(
                    database.save_option_chain_snapshot,
                    symbol,
                    chain["expiry"],
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


_last_history_prune_date: Optional[str] = None


async def _maybe_prune_gex_history() -> None:
    """GEX 快照歷史保留期清理，每個美東日期最多執行一次 (輪詢每 5 分鐘一輪，
    每輪都跑 DELETE 是純粹浪費)。"""
    global _last_history_prune_date
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    if _last_history_prune_date == today:
        return
    try:
        removed = await asyncio.to_thread(database.prune_gex_history)
        removed_em = await asyncio.to_thread(database.prune_em_history)
        _last_history_prune_date = today
        if removed or removed_em:
            logger.info(
                f"已清除超過保留期的歷史：GEX 快照 {removed} 筆、EM 快照 {removed_em} 筆"
            )
    except Exception as e:
        logger.warning(f"GEX 快照歷史保留期清理失敗: {e}")


async def poll_once() -> None:
    """執行一輪 GEX + Option Chain 輪詢：priority 標的（持倉）每輪必抓，
    其餘一般自選標的取出下一小批（分批輪詢，見模組頂端 POLL_ROTATION_CYCLES
    註解）。兩者為互斥分割（見 database.get_priority_symbols /
    get_non_priority_symbols），不會重複抓取同一標的。

    加入批次量與耗時觀測記錄：priority 標的自 commit 710cb59 起改為每輪必抓，
    會疊加在既有的非優先輪詢批次之上，MAX_CONCURRENCY 並未隨之調整。這裡記錄
    每輪的 priority/non-priority 批次量與總耗時，用以實際觀察是否真的出現
    單輪跑超過 POLL_BASE_INTERVAL_SECONDS 或排隊延遲的情況，作為是否需要調整
    併發/批次量的依據，而非憑空臆測。"""
    start_ts = time.monotonic()
    priority_symbols = await asyncio.to_thread(database.get_priority_symbols)
    non_priority_symbols = await asyncio.to_thread(database.get_non_priority_symbols)

    non_priority_batch = (
        _next_batch(non_priority_symbols) if non_priority_symbols else []
    )
    batch = list(priority_symbols) + non_priority_batch
    if not batch:
        return

    logger.info(
        f"[poll_once] 本輪批次量：priority={len(priority_symbols)}, "
        f"non_priority={len(non_priority_batch)}, total={len(batch)}, "
        f"MAX_CONCURRENCY={MAX_CONCURRENCY}"
    )

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

    await _maybe_prune_gex_history()

    elapsed = time.monotonic() - start_ts
    level = logger.warning if elapsed > POLL_BASE_INTERVAL_SECONDS else logger.info
    level(
        f"[poll_once] 本輪耗時 {elapsed:.1f}s（批次上限週期 "
        f"{POLL_BASE_INTERVAL_SECONDS}s）"
    )


async def _loop() -> None:
    await asyncio.to_thread(database.init_db)
    while True:
        try:
            if _is_us_market_hours():
                await poll_once()
            elif _is_em_snapshot_window():
                await record_em_snapshot_once()
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
