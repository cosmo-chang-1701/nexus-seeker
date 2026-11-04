"""信用利差（ICE BofA OAS）：FRED 抓取＋kv_cache 快取，僅供 /market 面板呈現。

另供擠壓進場「影子否決」前向紀錄讀取（market_analysis/squeeze_entry/shadow_vetoes.py，
僅紀錄、不參與判定）。不接任何閘門／決策；任何失敗皆吞下並回舊值，`get_credit_spreads()` 永不拋例外。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta
from typing import Any, Optional

from database.cache import get_kv_cache_with_age, save_kv_cache
from market_analysis.macro_signals import Observation
from services.macro_signal_service import fetch_fred_series
from services.single_flight import SingleFlightManager

logger = logging.getLogger(__name__)

HY_OAS_SERIES = "BAMLH0A0HYM2"
IG_OAS_SERIES = "BAMLC0A0CM"
CREDIT_SPREAD_KV_KEY = "macro_credit_spreads"
_REFRESH_SECONDS = 4 * 3600
_FETCH_TIMEOUT_SECONDS = 8.0
_FAILURE_BACKOFF_SECONDS = 900.0
_CHANGE_LOOKBACK_DAYS = 30

# 最近一次刷新失敗的 time.monotonic()；None 表示未失敗過
_last_failure_at: Optional[float] = None


def summarize_oas(obs: list[Observation]) -> Optional[dict[str, Any]]:
    """由觀測序列算出 bps、近 1 個月變動（bps）與觀測日；空序列回 None。"""
    if not obs:
        return None
    ordered = sorted(obs, key=lambda o: o.obs_date)
    last = ordered[-1]
    cutoff = last.obs_date - timedelta(days=_CHANGE_LOOKBACK_DAYS)
    ref: Optional[Observation] = None
    for o in ordered:
        if o.obs_date <= cutoff:
            ref = o
    change: Optional[int] = None
    if ref is not None:
        change = round((last.value - ref.value) * 100)
    return {
        "bps": round(last.value * 100),
        "change_1m_bps": change,
        "obs_date": last.obs_date.isoformat(),
    }


async def _fetch_one(series_id: str, today: date) -> Optional[dict[str, Any]]:
    obs = await fetch_fred_series(series_id, today, kind="daily")
    return summarize_oas(obs)


async def _refresh(today: date) -> dict[str, Any]:
    """兩個序列並行抓取；單側失敗該側為 None。兩側皆失敗則拋例外。"""
    results = await asyncio.gather(
        _fetch_one(HY_OAS_SERIES, today),
        _fetch_one(IG_OAS_SERIES, today),
        return_exceptions=True,
    )
    sides: list[Optional[dict[str, Any]]] = []
    errors: list[BaseException] = []
    for r in results:
        if isinstance(r, BaseException):
            errors.append(r)
            sides.append(None)
        else:
            sides.append(r)
    if errors:
        logger.warning(f"[CreditSpread] FRED 單側抓取失敗: {errors[0]!r}")
    if sides[0] is None and sides[1] is None:
        raise RuntimeError(
            f"HY／IG 皆無資料: {errors[0]!r}" if errors else "HY／IG 皆無資料"
        )
    return {"hy": sides[0], "ig": sides[1]}


async def get_credit_spreads() -> dict[str, Any]:
    """回傳 {"hy": {...}|None, "ig": {...}|None}；永不拋例外。"""
    global _last_failure_at
    old: Optional[dict[str, Any]] = None
    try:
        value, age = await asyncio.to_thread(
            get_kv_cache_with_age, CREDIT_SPREAD_KV_KEY
        )
        if isinstance(value, dict):
            old = value
            if age is not None and age < _REFRESH_SECONDS:
                return value
        if (
            _last_failure_at is not None
            and time.monotonic() - _last_failure_at < _FAILURE_BACKOFF_SECONDS
        ):
            return old if old is not None else {"hy": None, "ig": None}
        fresh = await asyncio.wait_for(
            SingleFlightManager.run("credit_spread_refresh", _refresh, date.today()),
            timeout=_FETCH_TIMEOUT_SECONDS,
        )
        await save_kv_cache(CREDIT_SPREAD_KV_KEY, fresh)
        _last_failure_at = None
        return dict(fresh)
    except Exception as e:
        _last_failure_at = time.monotonic()
        logger.warning(f"[CreditSpread] 信用利差刷新失敗，沿用舊值: {e!r}")
        return old if old is not None else {"hy": None, "ig": None}
