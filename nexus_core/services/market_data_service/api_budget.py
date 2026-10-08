"""API 配額觀測：統計 Finnhub／Yahoo 每端點的呼叫量，每小時輸出一行摘要。

只計數、不改任何呼叫行為；不開背景 task、不寫 DB。摘要於「窗口到期後的下一次
record_* 呼叫」時輸出並歸零，作為調整節流常數（limiter）的實測依據。
"""

import collections
import logging
import threading
import time
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

Source = Literal["finnhub", "yahoo"]

# 摘要窗口 1 小時：與 Finnhub 60 次/分的分鐘級配額相比，小時級足以看出
# 背景／互動的呼叫量級與尖峰端點，又不會讓 log 過於頻繁。
# 注意：摘要僅在「窗口到期後的下一次 record_*」輸出，閒置後實際窗口可能遠長於此值，
# 因此摘要一律輸出實際經過時間與每小時平均速率。
_WINDOW_SECONDS = 3600
# 實際窗口超過此秒數時，額外記錄起訖時間（ET）
_LONG_WINDOW_SECONDS = 7200
# 摘要中列出的熱門端點數量
_TOP_ENDPOINTS = 5
_ET = ZoneInfo("US/Eastern")

# 模組層鎖：DB writer 等非 event loop 執行緒也可能經 asyncio.run 走到 call_yf，
# Counter 與窗口起點的所有讀寫、flush 都須在鎖內進行。
_lock = threading.Lock()
_calls: collections.Counter[tuple[str, str, str]] = collections.Counter()
_rate_limited: collections.Counter[tuple[str, str]] = collections.Counter()
_window_started_at: float = time.time()


def _format_elapsed(seconds: float) -> str:
    """把經過秒數格式化為「N 分鐘」或「N.N 小時」。"""
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分鐘"
    return f"{seconds / 3600:.1f} 小時"


def _build_summary(
    calls: collections.Counter[tuple[str, str, str]],
    limited_counter: collections.Counter[tuple[str, str]],
    started_at: float,
    now: float,
) -> str:
    """由（已複製的）計數組出單行摘要文字；純函式，不碰全域狀態。"""
    elapsed = max(now - started_at, 1.0)
    hours = elapsed / 3600
    parts: list[str] = []
    for src in ("finnhub", "yahoo"):
        total = sum(n for (s, _, _), n in calls.items() if s == src)
        bg = sum(
            n for (s, _, mode), n in calls.items() if s == src and mode == "background"
        )
        inter = total - bg
        limited = sum(n for (s, _), n in limited_counter.items() if s == src)
        rate = total / hours
        parts.append(
            f"{src}={total}（背景 {bg}／互動 {inter}，429={limited}，平均 {rate:.0f}/小時）"
        )

    by_endpoint: collections.Counter[str] = collections.Counter()
    for (s, ep, _), n in calls.items():
        by_endpoint[f"{s}/{ep}"] += n
    top = "、".join(f"{k}={v}" for k, v in by_endpoint.most_common(_TOP_ENDPOINTS))

    window = f"過去 {_format_elapsed(elapsed)}"
    if elapsed > _LONG_WINDOW_SECONDS:
        t0 = datetime.fromtimestamp(started_at, _ET).strftime("%m-%d %H:%M")
        t1 = datetime.fromtimestamp(now, _ET).strftime("%m-%d %H:%M")
        window += f"（{t0} ~ {t1} ET）"
    return f"📈 [API 配額] {window}：{'；'.join(parts)}；Top 端點：{top or '無'}"


def _flush_if_due() -> None:
    """窗口滿 1 小時則輸出單行摘要並歸零（鎖內複製清空，鎖外 log）。"""
    global _window_started_at
    now = time.time()
    with _lock:
        if now - _window_started_at < _WINDOW_SECONDS:
            return
        calls_copy = collections.Counter(_calls)
        limited_copy = collections.Counter(_rate_limited)
        started_at = _window_started_at
        _calls.clear()
        _rate_limited.clear()
        _window_started_at = now

    logger.info(_build_summary(calls_copy, limited_copy, started_at, now))


def record_call(source: Source, endpoint: str, *, interactive: bool) -> None:
    """記錄一次實際送出的對外請求。保證不外拋，絕不影響 API 呼叫主流程。"""
    try:
        _flush_if_due()
        mode = "interactive" if interactive else "background"
        with _lock:
            _calls[(source, endpoint, mode)] += 1
    except Exception as e:  # noqa: BLE001
        logger.debug("api_budget.record_call 失敗（已忽略）: %s", e)


def record_rate_limited(source: Source, endpoint: str) -> None:
    """記錄一次 429 限流回應。保證不外拋，絕不影響 API 呼叫主流程。"""
    try:
        _flush_if_due()
        with _lock:
            _rate_limited[(source, endpoint)] += 1
    except Exception as e:  # noqa: BLE001
        logger.debug("api_budget.record_rate_limited 失敗（已忽略）: %s", e)


def snapshot() -> dict[str, int]:
    """回傳目前窗口計數（供測試）。key 為 `source/endpoint/mode` 與 `source/endpoint/429`。"""
    out: dict[str, int] = {}
    with _lock:
        for (s, ep, mode), n in _calls.items():
            out[f"{s}/{ep}/{mode}"] = n
        for (s, ep), n in _rate_limited.items():
            out[f"{s}/{ep}/429"] = n
    return out


def reset_for_tests() -> None:
    """清空計數並重設窗口起點（供測試）。"""
    global _window_started_at
    with _lock:
        _calls.clear()
        _rate_limited.clear()
        _window_started_at = time.time()
