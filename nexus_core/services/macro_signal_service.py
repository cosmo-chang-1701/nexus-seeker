"""總經訊號乾跑記錄：抓取 → 計算 → 寫入資料庫。**只記錄、不推播、不影響任何建議。**

每個交易日 16:15 ET 由 `cogs/trading/after_market.py` 呼叫 `run_macro_signal_job()`：
1. 從 FRED 抓取候選總經序列，以 INSERT OR IGNORE 寫入 `macro_series_observation`
   （事後修正時保留首次所見值）；
2. 以「當天已公布」的觀測（`available_date <= 今天`）計算各指標
   （`market_analysis/macro_signals.py`）；
3. 取 VIX／VIX3M 與固定科技池、VOO 的日線，計算 VIX 期限結構與科技池相對強弱；
4. 判定原始三態、以最近 5 個交易日確認，寫入 `macro_signal_log` 與 `macro_regime_log`。

任何一步失敗只記 log，不拋出、不影響同一排程的其他任務。
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import date, timedelta
from typing import Any, Optional

import config
from database.macro_signal_log import (
    load_observations,
    load_recent_regimes,
    store_observations,
    write_daily_log,
)
from market_analysis.macro_signals import (
    CONFIRM_DAYS,
    FED_HIKE_JUMP_PP,
    FIN_STRESS_THRESHOLD,
    FRED_SERIES,
    RATE_LOOKBACK_OBS,
    REAL_YIELD_JUMP_PP,
    SAHM_THRESHOLD,
    TWO_YEAR_JUMP_PP,
    Indicator,
    IndicatorReading,
    MacroState,
    Observation,
    available_date_for,
    change_over,
    claims_surge,
    classify_raw,
    confirm_state,
    credit_spread_widen,
    level_at_least,
    tech_relative_weak,
    usable,
    vix_term_inversion,
)
from services.single_flight import SingleFlightManager

logger = logging.getLogger(__name__)

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
# 每次只抓最近 3 年：足以涵蓋 126 個交易日變化、52 週低點與 Sahm；已存的觀測不會被覆寫
FRED_LOOKBACK_DAYS = 3 * 365
FRED_TIMEOUT_SECONDS = 20.0
# 科技池相對強弱需要 63 個交易日，取 6 個月日線即可
TECH_HISTORY_PERIOD = "6mo"
# 與既有 1GB VPS 慣例一致的歷史 K 線並行上限
_HISTORY_CONCURRENCY = 3


def parse_fred_csv(text: str) -> list[tuple[date, float]]:
    """解析 fredgraph.csv；缺值（"."、空字串）略過。欄名兼容 `DATE` 與 `observation_date`。"""
    out: list[tuple[date, float]] = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None:
        return out
    for row in reader:
        if len(row) < 2:
            continue
        raw_date, raw_value = row[0].strip(), row[1].strip()
        if not raw_value or raw_value == ".":
            continue
        try:
            out.append((date.fromisoformat(raw_date), float(raw_value)))
        except ValueError:
            continue
    return out


async def _download_fred(series_id: str, start: date) -> str:
    import httpx

    async with httpx.AsyncClient(timeout=FRED_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            FRED_CSV_URL, params={"id": series_id, "cosd": start.isoformat()}
        )
        resp.raise_for_status()
        return str(resp.text)


async def fetch_fred_series(series_id: str, today: date) -> list[Observation]:
    """抓取 FRED 序列並附上各觀測的可用日（前視防護）。"""
    start = today - timedelta(days=FRED_LOOKBACK_DAYS)
    text = await SingleFlightManager.run(
        f"fred_csv:{series_id}:{start.isoformat()}", _download_fred, series_id, start
    )
    kind = FRED_SERIES[series_id]
    return [
        Observation(d, v, available_date_for(kind, d))
        for d, v in parse_fred_csv(str(text))
    ]


async def refresh_fred_observations(today: date) -> dict[str, int]:
    """抓取全部 FRED 序列並寫入（INSERT OR IGNORE）；單一序列失敗不影響其他序列。"""
    inserted: dict[str, int] = {}
    for series_id in FRED_SERIES:
        try:
            obs = await fetch_fred_series(series_id, today)
            inserted[series_id] = await store_observations(series_id, obs, today)
        except Exception as e:
            logger.warning(f"[MacroSignal] FRED {series_id} 抓取或寫入失敗: {e}")
    return inserted


def _closes(df: Any, today: date) -> list[float]:
    """日線收盤（只取 `today` 當天及以前；`get_history_df` 為 tz-naive US/Eastern）。"""
    if df is None or getattr(df, "empty", True) or "Close" not in df:
        return []
    closes = df["Close"]
    try:
        closes = closes[[idx.date() <= today for idx in closes.index]]
    except Exception:
        pass
    return [float(v) for v in closes.dropna().tolist()]


async def _market_readings(today: date) -> list[IndicatorReading]:
    """VIX 期限結構與科技池相對 VOO 強弱（市場即時定價，當天收盤即可用）。"""
    from services.market_data_service import get_history_df
    from services.market_data_service.fundamentals import get_vix_term_structure

    readings: list[IndicatorReading] = []
    try:
        vts = await get_vix_term_structure()
        if vts.get("is_valid"):
            readings.append(
                vix_term_inversion(vts.get("vix_front"), vts.get("vix_back"), today)
            )
        else:
            readings.append(vix_term_inversion(None, None, today))
    except Exception as e:
        logger.warning(f"[MacroSignal] VIX 期限結構取得失敗: {e}")
        readings.append(vix_term_inversion(None, None, today))

    sem = asyncio.Semaphore(_HISTORY_CONCURRENCY)

    async def _load(symbol: str) -> tuple[str, list[float]]:
        async with sem:
            try:
                df = await get_history_df(symbol, period=TECH_HISTORY_PERIOD)
                return symbol, _closes(df, today)
            except Exception as e:
                logger.warning(f"[MacroSignal] {symbol} 日線取得失敗: {e}")
                return symbol, []

    results = await asyncio.gather(
        *(_load(s) for s in (*config.MACRO_TECH_POOL, "VOO"))
    )
    closes = dict(results)
    voo = closes.pop("VOO", [])
    tech = {s: c for s, c in closes.items() if c}
    readings.append(tech_relative_weak(tech, voo, today))
    return readings


def fred_readings(
    observations: dict[str, list[Observation]], today: date
) -> list[IndicatorReading]:
    """以 `today` 當天已公布的觀測計算全部 FRED 指標（純計算，無 I/O）。"""

    def u(series_id: str) -> list[Observation]:
        return usable(observations.get(series_id, []), today)

    return [
        change_over(
            Indicator.REAL_YIELD_JUMP,
            u("DFII10"),
            RATE_LOOKBACK_OBS,
            REAL_YIELD_JUMP_PP,
        ),
        change_over(
            Indicator.TWO_YEAR_JUMP, u("DGS2"), RATE_LOOKBACK_OBS, TWO_YEAR_JUMP_PP
        ),
        level_at_least(Indicator.FIN_STRESS, u("STLFSI4"), FIN_STRESS_THRESHOLD),
        claims_surge(u("ICSA")),
        credit_spread_widen(u("BAA10Y")),
        level_at_least(Indicator.SAHM_RULE, u("SAHMREALTIME"), SAHM_THRESHOLD),
        change_over(
            Indicator.FED_HIKE_CYCLE, u("DFF"), RATE_LOOKBACK_OBS, FED_HIKE_JUMP_PP
        ),
    ]


def resolve_states(
    readings: list[IndicatorReading],
    today: date,
) -> tuple[MacroState, Optional[MacroState]]:
    """原始三態 + 以最近 CONFIRM_DAYS 個交易日確認後的狀態。"""
    raw = classify_raw(readings)
    history = load_recent_regimes(today, CONFIRM_DAYS - 1)
    recent_raw: list[MacroState] = [h[1] for h in history] + [raw]
    previous_confirmed = history[-1][2] if history else None
    return raw, confirm_state(recent_raw, previous_confirmed)


def _job_allowed(bot: Any) -> bool:
    if not config.ENABLE_MACRO_SIGNAL_LOG:
        return False
    if getattr(bot, "_is_leader_instance", True) is not True:
        return False
    from services.llm_service import is_memory_safe

    if not is_memory_safe():
        logger.warning("[MacroSignal] 記憶體水位過高，略過本日總經訊號記錄")
        return False
    return True


async def run_macro_signal_job(bot: Any, trading_date: date) -> Optional[MacroState]:
    """16:15 ET 每日任務：抓取、計算並寫入。回傳確認後狀態（供 log）；失敗回 None。"""
    if not _job_allowed(bot):
        return None
    try:
        await refresh_fred_observations(trading_date)
        observations = {
            sid: await asyncio.to_thread(load_observations, sid) for sid in FRED_SERIES
        }
        readings = fred_readings(observations, trading_date)
        readings.extend(await _market_readings(trading_date))
        raw, confirmed = await asyncio.to_thread(resolve_states, readings, trading_date)
        await write_daily_log(trading_date, readings, raw, confirmed)
        lit = sorted(r.indicator.value for r in readings if r.flag)
        logger.info(
            f"[MacroSignal] {trading_date} raw={raw} confirmed={confirmed} "
            f"lit={lit or '無'}（乾跑：只記錄、不推播）"
        )
        return confirmed
    except Exception as e:
        logger.error(f"[MacroSignal] 總經訊號記錄失敗: {e}", exc_info=True)
        return None
