"""提領跑道的 I/O 層：讀 NAV 快照／CPI／Beta，呼叫純邏輯模組，寫入跑道快照。

計算邏輯在 `market_analysis/withdrawal_runway.py`（單一權威）；本模組只負責讀資料、
組裝輸入、寫快照。16:15 ET 由 `cogs/trading/after_market.py` 於 NAV 快照與 FRED 觀測
更新之後呼叫；階段二不推播（推播為階段三）。
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Optional, Sequence

import numpy as np

import market_time
from database.macro_signal_log import load_observations
from database.user_settings import UserContext, get_full_user_context
from database.withdrawal_runway import (
    RunwaySnapshot,
    load_snapshot,
    upsert_snapshots,
)
from market_analysis.macro_signals import Observation, usable
from market_analysis.withdrawal_runway import (
    STRESS_BETA_FALLBACK,
    adjust_withdrawal,
    boxx_payments,
    clamp_beta,
    stress_runway,
    zero_return_runway_years,
)
from services.downside_risk_service import (
    build_portfolio_return_series,
    get_users_with_positions,
    load_nav_snapshots,
)
from services.llm_service import is_memory_safe

logger = logging.getLogger(__name__)

CPI_SERIES_ID = "CPIAUCSL"
BOXX_SYMBOL = "BOXX"
# NAV 快照超過此交易日數未更新 → 面板標示「資料過期」，不視為當日資料
NAV_STALE_TRADING_DAYS = 5
_MIN_BETA_OBS = 60


def parse_months(text: str) -> list[int]:
    """`withdrawal_months`（逗號分隔）→ 排序月份；壞值回預設 [1, 7]。"""
    try:
        ms = sorted({int(x) for x in str(text).split(",") if x.strip()})
    except ValueError:
        return [1, 7]
    return ms if ms and all(1 <= m <= 12 for m in ms) else [1, 7]


def next_withdrawal_date(today: date, months: Sequence[int]) -> tuple[date, int, int]:
    """下一次提領日（提領月份的首個交易日，嚴格晚於 `today`）。

    回傳（提領日, 提領月份, 自 today 起的交易日數）；交易日數即重演路徑的 `days_to_first`。
    """
    ms = sorted(set(months))
    for offset in range(0, 25):
        year = today.year + (today.month - 1 + offset) // 12
        month = (today.month - 1 + offset) % 12 + 1
        if month not in ms:
            continue
        first = date(year, month, 1)
        sched = market_time.nyse_calendar.schedule(
            start_date=first, end_date=first + timedelta(days=10)
        )
        if sched.empty:
            continue
        target = sched.index[0].date()
        if target <= today:
            continue
        span = market_time.nyse_calendar.schedule(start_date=today, end_date=target)
        days = int(sum(1 for d in span.index if d.date() > today))
        return target, month, days
    raise ValueError(f"無法由 months={list(months)} 推算下一次提領日")


def cpi_at(observations: Sequence[Observation], as_of: date) -> Optional[float]:
    """`as_of` 當天已公布的最新 CPI；無資料 → None。"""
    seen = usable(observations, as_of)
    return seen[-1].value if seen else None


def cpi_for_anchor(observations: Sequence[Observation], anchor: str) -> Optional[float]:
    """基準月（YYYY-MM）的 CPI；不要求「當時已公布」（基準是使用者事後指定的歷史月份）。"""
    try:
        year, month = (int(x) for x in anchor.split("-"))
    except ValueError:
        return None
    for o in observations:
        if o.obs_date.year == year and o.obs_date.month == month:
            return o.value
    return None


def portfolio_beta(
    dates: Sequence[str], returns: Sequence[float], spy_returns: dict[str, float]
) -> Optional[float]:
    """模擬投組日報酬對 SPY 日報酬的 beta = cov / var；共同日不足或退化 → None。"""
    xs: list[float] = []
    ys: list[float] = []
    for d, r in zip(dates, returns):
        m = spy_returns.get(d)
        if m is not None:
            xs.append(float(r))
            ys.append(m)
    if len(xs) < _MIN_BETA_OBS:
        return None
    var = float(np.var(ys, ddof=1))
    if not math.isfinite(var) or var < 1e-12:
        return None
    beta = float(np.cov(xs, ys, ddof=1)[0, 1]) / var
    return beta if math.isfinite(beta) else None


def compute_user_runway(
    *,
    user_id: int,
    today: date,
    nav: float,
    nav_date: str,
    boxx_value: float,
    beta: Optional[float],
    withdrawal_amount: float,
    months: Sequence[int],
    cpi_anchor: Optional[float],
    cpi_now: Optional[float],
) -> Optional[RunwaySnapshot]:
    """純組裝（無 I/O）：nav ≤ 0 或未設定提領 → None。"""
    if nav <= 0 or withdrawal_amount <= 0:
        return None
    cpi_missing = cpi_anchor is None or cpi_now is None
    w_next = adjust_withdrawal(withdrawal_amount, cpi_anchor or 0.0, cpi_now)
    target, first_month, days = next_withdrawal_date(today, months)
    stress = stress_runway(
        nav,
        boxx_value,
        w_next,
        beta,
        days_to_first=days,
        months=months,
        first_month=first_month,
    )
    return RunwaySnapshot(
        user_id=user_id,
        as_of=today.isoformat(),
        nav=nav,
        nav_date=nav_date,
        zero_years=zero_return_runway_years(nav, w_next, months),
        gfc_years=stress.gfc_years,
        dotcom_years=stress.dotcom_years,
        stress_years=stress.stress_years,
        capped=stress.capped,
        next_withdrawal=w_next,
        boxx_value=boxx_value,
        boxx_payments=boxx_payments(boxx_value, w_next),
        beta=clamp_beta(beta),
        beta_is_fallback=beta is None or not math.isfinite(beta),
        cpi_missing=cpi_missing,
        next_date=target.isoformat(),
    )


async def _spy_returns() -> dict[str, float]:
    from services import market_data_service

    df = await market_data_service.get_history_df("SPY", "1y")
    if df is None or df.empty or "Close" not in df:
        return {}
    pct = df["Close"].astype(float).pct_change().dropna()
    return {i.strftime("%Y-%m-%d"): float(v) for i, v in pct.items()}


def _job_allowed(bot: Any) -> bool:
    if not getattr(bot, "_is_leader_instance", True):
        return False
    if not is_memory_safe():
        logger.warning("[WithdrawalRunway] 記憶體水位過高，跳過本輪跑道計算。")
        return False
    return True


async def _snapshot_for_user(
    uid: int,
    ctx: UserContext,
    today: date,
    spy: dict[str, float],
    cpi_obs: Sequence[Observation],
) -> Optional[RunwaySnapshot]:
    navs = await asyncio.to_thread(load_nav_snapshots, uid, 1)
    if not navs:
        return None
    latest = navs[-1]
    boxx = float(latest.shares.get(BOXX_SYMBOL, 0.0)) * float(
        latest.closes.get(BOXX_SYMBOL, 0.0)
    )
    beta: Optional[float] = None
    series = await build_portfolio_return_series(uid)
    if series is not None:
        beta = portfolio_beta(series.dates, series.returns.tolist(), spy)
    anchor = ctx.withdrawal_anchor_month
    return compute_user_runway(
        user_id=uid,
        today=today,
        nav=latest.nav,
        nav_date=latest.date,
        boxx_value=max(boxx, 0.0),
        beta=beta,
        withdrawal_amount=ctx.withdrawal_amount,
        months=parse_months(ctx.withdrawal_months),
        cpi_anchor=cpi_for_anchor(cpi_obs, anchor) if anchor else None,
        cpi_now=cpi_at(cpi_obs, today),
    )


async def run_withdrawal_runway_job(bot: Any, trading_date: date) -> int:
    """16:15 ET：為已設定提領的使用者計算並寫入跑道快照。回傳寫入筆數。"""
    if not _job_allowed(bot):
        return 0
    uids = await asyncio.to_thread(get_users_with_positions)
    cpi_obs = await asyncio.to_thread(load_observations, CPI_SERIES_ID)
    spy: Optional[dict[str, float]] = None
    out: list[RunwaySnapshot] = []
    for uid in uids:
        try:
            ctx = await asyncio.to_thread(get_full_user_context, uid)
            if ctx.withdrawal_amount <= 0:
                continue
            if spy is None:
                try:
                    spy = await _spy_returns()
                except Exception as e:
                    logger.warning(
                        f"[WithdrawalRunway] SPY 歷史抓取失敗，Beta 走預設: {e}"
                    )
                    spy = {}
            snap = await _snapshot_for_user(uid, ctx, trading_date, spy, cpi_obs)
            if snap is not None:
                out.append(snap)
        except Exception as e:
            logger.error(f"[WithdrawalRunway] uid={uid} 計算失敗: {e}")
    try:
        await upsert_snapshots(out)
    except Exception as e:
        logger.error(f"[WithdrawalRunway] 快照寫入失敗: {e}")
        return 0
    return len(out)


def is_snapshot_stale(snap: RunwaySnapshot, today: Optional[date] = None) -> bool:
    """NAV 快照日距今超過 NAV_STALE_TRADING_DAYS 個交易日 → 過期。"""
    today = today or datetime.now(market_time.ny_tz).date()
    try:
        nav_day = date.fromisoformat(snap.nav_date)
    except ValueError:
        return True
    if nav_day >= today:
        return False
    span = market_time.nyse_calendar.schedule(start_date=nav_day, end_date=today)
    return sum(1 for d in span.index if d.date() > nav_day) > NAV_STALE_TRADING_DAYS


async def get_runway_snapshot(user_id: int) -> Optional[RunwaySnapshot]:
    return await asyncio.to_thread(load_snapshot, user_id)


async def get_runway_display(user_id: int) -> tuple[Optional[RunwaySnapshot], bool]:
    """供顯示端使用：(最新快照, 是否過期)。無快照或未啟用提領 → (None, False)。"""
    snap = await get_runway_snapshot(user_id)
    if snap is None:
        return None, False
    # 設定變更時已刪除快照；此處再擋一次，避免任何殘留快照在停用後繼續顯示
    ctx = await asyncio.to_thread(get_full_user_context, user_id)
    if ctx.withdrawal_amount <= 0:
        return None, False
    return snap, await asyncio.to_thread(is_snapshot_stale, snap)


__all__ = [
    "STRESS_BETA_FALLBACK",
    "compute_user_runway",
    "get_runway_display",
    "get_runway_snapshot",
    "is_snapshot_stale",
    "next_withdrawal_date",
    "run_withdrawal_runway_job",
]
