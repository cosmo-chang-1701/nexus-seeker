"""宏觀預期差標準化計算服務 (Macro Surprise Service)。

職責：
1. 從 cached economic calendar (economic_calendar_events) 讀取已公布實際值與預測值的總經事件。
2. 匹配官方註冊表定義 (MACRO_EVENT_REGISTRY)。
3. 解析並標準化數值（處理百分比、K/M/B 量綱）。
4. 結合過去 12 期歷史樣本計算 Standardized Surprise Z-Score (若樣本 >= 6)。
5. 寫入 macro_release_surprise 資料表。
"""

from __future__ import annotations

import asyncio
import logging
import math

from database.connection import get_read_connection
from database.fundamental_pipeline import (
    get_macro_surprises_for_event,
    save_macro_surprise,
)
from market_analysis.fundamental_pipeline.macro_surprise import (
    calculate_standardized_surprise,
    match_macro_event,
)
from market_analysis.fundamental_pipeline.models import MacroSurpriseReading

logger = logging.getLogger(__name__)


def parse_calendar_metric_value(raw: str | None) -> float | None:
    """解析日曆中的字串數值為浮點數。相容百分比、K/M/B 等量綱。"""
    if not raw:
        return None
    cleaned = raw.strip().replace(",", "")
    if cleaned in ("", "-", "N/A", "null", "None", "."):
        return None

    # 處理開頭的貨幣符號
    if cleaned.startswith("$"):
        cleaned = cleaned[1:].strip()

    # 處理百分比 (如 "3.2%") -> 3.2
    if cleaned.endswith("%"):
        try:
            val = float(cleaned[:-1].strip())
            return None if math.isnan(val) or math.isinf(val) else val
        except ValueError:
            return None

    # 處理量綱後綴 K, M, B
    multiplier = 1.0
    if cleaned.endswith(("k", "K")):
        multiplier = 1e3
        cleaned = cleaned[:-1].strip()
    elif cleaned.endswith(("m", "M")):
        multiplier = 1e6
        cleaned = cleaned[:-1].strip()
    elif cleaned.endswith(("b", "B")):
        multiplier = 1e9
        cleaned = cleaned[:-1].strip()

    try:
        val = float(cleaned) * multiplier
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    except ValueError:
        return None


def _load_calendar_events_with_actuals() -> list[dict[str, str]]:
    """從 economic_calendar_events 讀取具備 actual_value 與 consensus_value 的事件。"""
    conn = get_read_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT event, event_time, consensus_value, actual_value, country
            FROM economic_calendar_events
            WHERE country = 'US'
              AND actual_value IS NOT NULL
              AND actual_value != ''
              AND consensus_value IS NOT NULL
              AND consensus_value != ''
            ORDER BY event_time ASC
            """
        )
        rows = cur.fetchall()
        return [
            {
                "event": str(r[0]),
                "event_time": str(r[1]),
                "consensus_value": str(r[2]),
                "actual_value": str(r[3]),
                "country": str(r[4]),
            }
            for r in rows
        ]
    finally:
        conn.close()


async def process_macro_surprises() -> list[MacroSurpriseReading]:
    """掃描已公布的總經事件並計算其標準化預期差。"""
    events = await asyncio.to_thread(_load_calendar_events_with_actuals)
    if not events:
        logger.debug("[MacroSurpriseService] 目前無具備實際值與共識值的總經事件")
        return []

    processed_readings: list[MacroSurpriseReading] = []

    for item in events:
        event_name = item["event"]
        defn = match_macro_event(event_name)
        if defn is None:
            continue

        actual = parse_calendar_metric_value(item["actual_value"])
        forecast = parse_calendar_metric_value(item["consensus_value"])
        if actual is None or forecast is None:
            continue

        release_time = item["event_time"]
        # 讀取該事件過往歷史預期差樣本（非同步委派避免阻塞 event loop）
        prior_surprises = await asyncio.to_thread(
            get_macro_surprises_for_event, defn.event_key, 12
        )
        # 排除相同發布時間的舊記錄避免自我干擾
        historical_diffs = [
            s.raw_diff for s in prior_surprises if s.release_time_utc != release_time
        ]

        raw_diff, z_score = calculate_standardized_surprise(
            actual=actual,
            forecast=forecast,
            historical_diffs=historical_diffs,
        )

        reading = MacroSurpriseReading(
            event_key=defn.event_key,
            release_time_utc=release_time,
            actual=round(actual, 4),
            forecast=round(forecast, 4),
            raw_diff=round(raw_diff, 4),
            z_score=round(z_score, 4) if z_score is not None else None,
            growth_sign=defn.growth_sign,
        )
        await save_macro_surprise(reading)
        processed_readings.append(reading)

    logger.info(
        f"[MacroSurpriseService] 成功處理並記錄 {len(processed_readings)} 筆總經發布預期差"
    )
    return processed_readings
