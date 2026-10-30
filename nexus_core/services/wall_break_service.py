"""wall_break_service.py — GEX 牆突破／跌破管線服務（錨點、T+1 結算、盤中偵測）。

由 `IntradayScanPipeline` 每輪（:10/:40 ET）對每檔自選標的呼叫 `WallBreakMonitor`：
同一輪內同一標的只計算一次（供多位使用者共用結果），每位使用者再各自推播。
資料蒐集（錨點、事件、T+1）與推播開關無關，事件表是前向蒐集紀錄。

所有外送請求皆走既有服務函式（已經過 rate_gate），本模組不新增任何限流器。
"""

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

import pandas as pd

import market_time
from database.wall_break import (
    get_latest_wall_anchor_before,
    get_pending_t1_events,
    get_wall_break_event,
    insert_wall_break_event,
    save_wall_break_t1,
    upsert_wall_anchor,
)
from market_analysis.atr_utils import compute_atr_15m_from_df
from market_analysis.price_volume_alert import (
    compute_time_of_day_volume_stats,
    trim_to_confirmed_15m_bars,
)
from market_analysis.sentiment_engine import SentimentEngine
from market_analysis.wall_break import (
    BarSnapshot,
    OptionsSnapshot,
    T1EventInput,
    T1Result,
    T1Snapshot,
    WallAnchor,
    WallBreakResult,
    classify_wall_break,
    detect_wall_break_triggers,
    evaluate_t1,
    gates_to_json,
)
from models.schemas import WatchlistEvaluation
from services import market_data_service

logger = logging.getLogger(__name__)

_SKIP_SYMBOLS = frozenset({"BOXX", "BIL"})
_SETTLE_MIN_MINUTES = (
    10 * 60
)  # T+1 結算最早 10:00 ET，避開開盤初期仍是前一日的 edge GEX
# T+1 只能在事件日後的第一個 NYSE 交易日結算；更晚者標記 EXPIRED（只紀錄、不推播）
T1_MAX_SESSION_GAP = 1
T1_EXPIRED_VERDICT = "EXPIRED"
_OPEN_MINUTES = 9 * 60 + 30
_SESSION_MINUTES = 390.0
_BAR_MINUTES = 15
_EVAL_LAST_N_BARS = 2
_VOL_WINDOW = 20


def _now_et() -> datetime:
    return datetime.now(market_time.ny_tz)


@dataclass
class WallBreakOutcome:
    intraday: list[WallBreakResult] = field(default_factory=list)
    t1: list[tuple[dict[str, Any], T1EventInput, T1Result]] = field(
        default_factory=list
    )


def parse_gex_profile(raw: Any) -> dict[float, float]:
    """dict 或 JSON 字串 -> {履約價: 淨 GEX}；壞項目略過，整體失敗回傳 {}。"""
    try:
        data = raw
        if isinstance(data, (str, bytes)):
            data = json.loads(data)
        if not isinstance(data, dict):
            return {}
        out: dict[float, float] = {}
        for k, v in data.items():
            try:
                strike = float(k)
                gex = float(v)
            except (TypeError, ValueError):
                continue
            if math.isnan(strike) or math.isnan(gex):
                continue
            out[strike] = gex
        return out
    except Exception:
        return {}


def _wall_or_none(value: Any) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 and not math.isnan(v) else None


def _profile_get(profile: dict[float, float], strike: float) -> Optional[float]:
    for k, v in profile.items():
        if abs(k - strike) < 1e-6:
            return v
    return None


def _oi_at(chain: Any, direction: str, strike: float) -> Optional[float]:
    """BREAKOUT 取 calls、BREAKDOWN 取 puts 在 strike 的未平倉量；查無回傳 None。"""
    if chain is None:
        return None
    df = chain.calls if direction == "BREAKOUT" else chain.puts
    if df is None or getattr(df, "empty", True):
        return None
    for _, r in df.iterrows():
        try:
            if abs(float(r["strike"]) - strike) < 1e-6:
                oi = float(r["openInterest"])
                return None if math.isnan(oi) else oi
        except (KeyError, TypeError, ValueError):
            continue
    return None


class WallBreakMonitor:
    def __init__(self) -> None:
        self._cycle_cache: dict[str, WallBreakOutcome] = {}

    def begin_cycle(self) -> None:
        self._cycle_cache.clear()

    async def process_symbol(
        self, symbol: str, evaluation: Optional[WatchlistEvaluation]
    ) -> WallBreakOutcome:
        sym = symbol.upper()
        cached = self._cycle_cache.get(sym)
        if cached is not None:
            return cached
        outcome = WallBreakOutcome()
        if evaluation is None or sym in _SKIP_SYMBOLS:
            return outcome
        symbol_gex = getattr(evaluation, "symbol_gex", None)
        if not isinstance(symbol_gex, dict):
            return outcome
        self._cycle_cache[sym] = outcome
        try:
            await self._compute(sym, evaluation, symbol_gex, outcome)
        except Exception as e:
            logger.warning(f"[{sym}] 牆突破處理失敗: {e}")
        return outcome

    async def _compute(
        self,
        sym: str,
        evaluation: WatchlistEvaluation,
        symbol_gex: dict[str, Any],
        outcome: WallBreakOutcome,
    ) -> None:
        now_et = _now_et()
        today = now_et.strftime("%Y-%m-%d")
        profile = parse_gex_profile(symbol_gex.get("gex_profile"))
        call_wall = _wall_or_none(symbol_gex.get("call_wall"))
        put_wall = _wall_or_none(symbol_gex.get("put_wall"))
        gex_expiry_raw = symbol_gex.get("expiry")
        gex_expiry = str(gex_expiry_raw) if gex_expiry_raw else None
        gex_fresh = not symbol_gex.get("_is_stale_cache") and (
            call_wall is not None or put_wall is not None
        )
        settle_window = (
            gex_fresh and now_et.hour * 60 + now_et.minute >= _SETTLE_MIN_MINUTES
        )

        # 3. 錨點 upsert
        if gex_fresh:
            try:
                await upsert_wall_anchor(sym, today, call_wall, put_wall, gex_expiry)
            except Exception as e:
                logger.warning(f"[{sym}] 牆錨點寫入失敗: {e}")

        # 4. T+1 結算
        if settle_window:
            try:
                await self._settle_t1(
                    sym,
                    evaluation,
                    today,
                    call_wall,
                    put_wall,
                    gex_expiry,
                    profile,
                    outcome,
                )
            except Exception as e:
                logger.warning(f"[{sym}] 牆突破 T+1 結算失敗: {e}")

        # 5. 錨點讀取
        anchor_row = await asyncio.to_thread(get_latest_wall_anchor_before, sym, today)
        if anchor_row is None:
            return
        a_call = _wall_or_none(anchor_row.get("call_wall"))
        a_put = _wall_or_none(anchor_row.get("put_wall"))
        if a_call is None and a_put is None:
            return
        if anchor_row.get("trade_date") != market_time.get_last_completed_trading_date(
            now_et
        ):
            return

        # 6. 便宜的預檢
        try:
            q = await market_data_service.get_quote(sym)
        except Exception as e:
            logger.warning(f"[{sym}] 牆突破預檢報價失敗: {e}")
            q = None
        if isinstance(q, dict):
            try:
                qh = float(q.get("h") or 0.0)
                ql = float(q.get("l") or 0.0)
            except (TypeError, ValueError):
                qh = ql = 0.0
            if qh > 0 and ql > 0:
                can_up = a_call is not None and qh >= a_call
                can_down = a_put is not None and ql <= a_put
                if not can_up and not can_down:
                    return

        # 7. K 棒
        df = await market_data_service.get_history_df(
            sym, period="5d", interval="15m", force_refresh=True
        )
        dfc = trim_to_confirmed_15m_bars(df)
        if dfc is None:
            return
        today_pos = [
            i for i, ts in enumerate(dfc.index) if ts.strftime("%Y-%m-%d") == today
        ]
        positions = today_pos[-_EVAL_LAST_N_BARS:]
        if not positions:
            return

        daily_sma20 = await self._daily_volume_sma20(sym, today)
        anchor = WallAnchor(
            trade_date=str(anchor_row["trade_date"]),
            call_wall=a_call,
            put_wall=a_put,
            gex_expiry=anchor_row.get("gex_expiry"),
        )
        metrics = evaluation.metrics
        uoa: Optional[list[dict[str, Any]]] = None

        # 8. 偵測與分類
        for i in positions:
            bar = self._build_bar(dfc, i, today_pos, daily_sma20)
            if bar is None:
                continue
            for trig in detect_wall_break_triggers(anchor, bar):
                existing = await asyncio.to_thread(
                    get_wall_break_event,
                    sym,
                    today,
                    trig.direction,
                    trig.trigger,
                )
                if existing is not None:
                    continue
                if uoa is None:
                    try:
                        uoa = list(await SentimentEngine.detect_uoa(sym) or [])
                    except Exception as e:
                        logger.warning(f"[{sym}] 牆突破 UOA 取得失敗: {e}")
                        uoa = []
                options = OptionsSnapshot(
                    gex_profile=profile if gex_fresh else {},
                    skew_percentile=metrics.skew_percentile,
                    skew_is_fallback=bool(metrics.skew_is_fallback),
                    uoa=uoa,
                )
                result = classify_wall_break(trig, bar, options)

                old_wall_oi: Optional[float] = None
                oi_expiry: Optional[str] = None
                if trig.trigger == "CLOSE_BEYOND":
                    try:
                        oi_expiry = gex_expiry
                        if oi_expiry is None:
                            expiries = (
                                await market_data_service.get_all_option_expiries(sym)
                            )
                            oi_expiry = next(
                                (e for e in sorted(expiries or []) if e >= today),
                                None,
                            )
                        if oi_expiry is not None:
                            chain = await market_data_service.get_option_chain(
                                sym, oi_expiry, prune_pct=None
                            )
                            old_wall_oi = _oi_at(chain, trig.direction, trig.old_wall)
                    except Exception as e:
                        logger.warning(f"[{sym}] 牆突破舊牆 OI 取得失敗: {e}")
                        old_wall_oi = None
                old_wall_gex = (
                    _profile_get(profile, trig.old_wall) if gex_fresh else None
                )
                inserted = await insert_wall_break_event(
                    {
                        "symbol": sym,
                        "trade_date": today,
                        "direction": trig.direction,
                        "trigger_kind": trig.trigger,
                        "bar_time": trig.bar_time.strftime("%Y-%m-%d %H:%M"),
                        "anchor_trade_date": trig.anchor_trade_date,
                        "old_wall": trig.old_wall,
                        "close": trig.close,
                        "verdict": result.verdict,
                        "label": result.label,
                        "gates_json": gates_to_json(result.gates),
                        "old_wall_oi": old_wall_oi,
                        "oi_expiry": oi_expiry,
                        "gex_expiry": gex_expiry,
                        "old_wall_gex": old_wall_gex,
                    }
                )
                if inserted:
                    outcome.intraday.append(result)

    async def _settle_t1(
        self,
        sym: str,
        evaluation: WatchlistEvaluation,
        today: str,
        call_wall: Optional[float],
        put_wall: Optional[float],
        gex_expiry: Optional[str],
        profile: dict[float, float],
        outcome: WallBreakOutcome,
    ) -> None:
        pending = await asyncio.to_thread(get_pending_t1_events, sym, today)
        gap_cache: dict[str, Optional[int]] = {}
        for row in pending:
            try:
                trade_date = str(row["trade_date"])
                if trade_date not in gap_cache:
                    gap_cache[trade_date] = await self._sessions_elapsed(
                        sym, trade_date, today
                    )
                gap = gap_cache[trade_date]
                if gap is None:
                    continue
                if gap == 0:
                    logger.debug(
                        f"[{sym}] 牆突破 T+1：{today} 非交易日，事件 {trade_date} 維持待結算"
                    )
                    continue
                if gap > T1_MAX_SESSION_GAP:
                    saved_exp = await save_wall_break_t1(
                        int(row["id"]),
                        today,
                        T1_EXPIRED_VERDICT,
                        "逾期未結算（非 T+1）",
                        json.dumps({"sessions_elapsed": gap}),
                    )
                    if saved_exp:
                        logger.info(
                            f"[{sym}] 牆突破事件 {trade_date} 逾期未結算"
                            f"（已過 {gap} 個交易日），標記 EXPIRED"
                        )
                    continue
                oi_now: Optional[float] = None
                oi_expiry = row.get("oi_expiry")
                if oi_expiry and str(oi_expiry) >= today:
                    chain = await market_data_service.get_option_chain(
                        sym, str(oi_expiry), prune_pct=None
                    )
                    oi_now = _oi_at(chain, row["direction"], float(row["old_wall"]))
                ev = T1EventInput(
                    direction=row["direction"],
                    old_wall=float(row["old_wall"]),
                    intraday_verdict=row["verdict"],
                    old_wall_oi=row.get("old_wall_oi"),
                    gex_expiry=row.get("gex_expiry"),
                )
                snap = T1Snapshot(
                    trade_date=today,
                    spot=evaluation.metrics.current_price,
                    call_wall=call_wall,
                    put_wall=put_wall,
                    gex_expiry=gex_expiry,
                    gex_profile=profile,
                    old_wall_oi_now=oi_now,
                )
                res = evaluate_t1(ev, snap)
                saved = await save_wall_break_t1(
                    int(row["id"]),
                    today,
                    res.verdict,
                    res.label,
                    gates_to_json(res.checks),
                )
                if saved:
                    outcome.t1.append((row, ev, res))
            except Exception as e:
                logger.warning(f"[{sym}] 牆突破 T+1 單筆結算失敗: {e}")

    @staticmethod
    async def _sessions_elapsed(sym: str, trade_date: str, today: str) -> Optional[int]:
        """事件日（不含）到今日（含）的 NYSE 交易日數；日曆失敗回傳 None（不得過期）。"""
        try:
            return await asyncio.to_thread(
                market_time.count_nyse_sessions_after,
                date.fromisoformat(trade_date),
                date.fromisoformat(today),
            )
        except Exception as e:
            logger.warning(f"[{sym}] 牆突破 T+1 交易日曆計算失敗，維持待結算: {e}")
            return None

    async def _daily_volume_sma20(self, sym: str, today: str) -> Optional[float]:
        try:
            daily = await market_data_service.get_history_df(sym, period="1y")
            if daily is None or daily.empty:
                return None
            vols = [
                float(v)
                for ts, v in zip(daily.index, daily["Volume"])
                if ts.strftime("%Y-%m-%d") < today and not pd.isna(v)
            ]
            if len(vols) < _VOL_WINDOW:
                return None
            tail = vols[-_VOL_WINDOW:]
            return sum(tail) / _VOL_WINDOW
        except Exception as e:
            logger.warning(f"[{sym}] 牆突破日線均量取得失敗: {e}")
            return None

    @staticmethod
    def _build_bar(
        dfc: pd.DataFrame,
        i: int,
        today_pos: list[int],
        daily_sma20: Optional[float],
    ) -> Optional[BarSnapshot]:
        try:
            row = dfc.iloc[i]
            ts = dfc.index[i]
            bar_start: datetime = ts.to_pydatetime()
            avg20: Optional[float] = None
            if i >= _VOL_WINDOW:
                avg20 = float(dfc["Volume"].iloc[i - _VOL_WINDOW : i].mean())
            sub = dfc.iloc[: i + 1]
            tod_avg = compute_time_of_day_volume_stats(sub)[0]
            atr = compute_atr_15m_from_df(sub)
            end_minutes = (
                bar_start.hour * 60 + bar_start.minute + _BAR_MINUTES - _OPEN_MINUTES
            )
            f = min(max(end_minutes / _SESSION_MINUTES, 0.05), 1.0)
            day_sum = float(
                sum(float(dfc["Volume"].iloc[p]) for p in today_pos if p <= i)
            )
            return BarSnapshot(
                bar_time=bar_start,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row["Volume"]),
                avg_volume_20=avg20,
                tod_avg_volume=tod_avg,
                atr_15m=atr if atr > 0 else None,
                projected_day_volume=day_sum / f,
                daily_volume_sma20=daily_sma20,
            )
        except Exception as e:
            logger.warning(f"牆突破 K 棒快照建構失敗: {e}")
            return None
