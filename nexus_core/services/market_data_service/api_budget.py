"""API 配額觀測：統計 Finnhub／Yahoo 每端點的呼叫量，每小時輸出一行摘要。

只計數、不改任何呼叫行為；不開背景 task、不寫 DB。摘要於「窗口到期後的下一次
record_* 呼叫」時輸出並歸零，作為調整節流常數（limiter）的實測依據。
"""

import collections
import logging
import time
from typing import Literal

logger = logging.getLogger(__name__)

Source = Literal["finnhub", "yahoo"]

# 摘要窗口 1 小時：與 Finnhub 60 次/分的分鐘級配額相比，小時級足以看出
# 背景／互動的呼叫量級與尖峰端點，又不會讓 log 過於頻繁。
_WINDOW_SECONDS = 3600
# 摘要中列出的熱門端點數量
_TOP_ENDPOINTS = 5

_calls: collections.Counter[tuple[str, str, str]] = collections.Counter()
_rate_limited: collections.Counter[tuple[str, str]] = collections.Counter()
_window_started_at: float = time.time()


def _flush_if_due() -> None:
    """窗口滿 1 小時則輸出單行摘要並歸零。"""
    global _window_started_at
    now = time.time()
    if now - _window_started_at < _WINDOW_SECONDS:
        return

    parts: list[str] = []
    for src in ("finnhub", "yahoo"):
        total = sum(n for (s, _, _), n in _calls.items() if s == src)
        bg = sum(
            n for (s, _, mode), n in _calls.items() if s == src and mode == "background"
        )
        inter = total - bg
        limited = sum(n for (s, _), n in _rate_limited.items() if s == src)
        parts.append(f"{src}={total}（背景 {bg}／互動 {inter}，429={limited}）")

    by_endpoint: collections.Counter[str] = collections.Counter()
    for (s, ep, _), n in _calls.items():
        by_endpoint[f"{s}/{ep}"] += n
    top = "、".join(f"{k}={v}" for k, v in by_endpoint.most_common(_TOP_ENDPOINTS))

    logger.info(
        "📈 [API 配額] 過去 1 小時：%s；Top 端點：%s", "；".join(parts), top or "無"
    )
    _calls.clear()
    _rate_limited.clear()
    _window_started_at = now


def record_call(source: Source, endpoint: str, *, interactive: bool) -> None:
    """記錄一次實際送出的對外請求。"""
    _flush_if_due()
    mode = "interactive" if interactive else "background"
    _calls[(source, endpoint, mode)] += 1


def record_rate_limited(source: Source, endpoint: str) -> None:
    """記錄一次 429 限流回應。"""
    _flush_if_due()
    _rate_limited[(source, endpoint)] += 1


def snapshot() -> dict[str, int]:
    """回傳目前窗口計數（供測試）。key 為 `source/endpoint/mode` 與 `source/endpoint/429`。"""
    out: dict[str, int] = {}
    for (s, ep, mode), n in _calls.items():
        out[f"{s}/{ep}/{mode}"] = n
    for (s, ep), n in _rate_limited.items():
        out[f"{s}/{ep}/429"] = n
    return out


def reset_for_tests() -> None:
    """清空計數並重設窗口起點（供測試）。"""
    global _window_started_at
    _calls.clear()
    _rate_limited.clear()
    _window_started_at = time.time()
