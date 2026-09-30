"""提領跑道的 I/O 層：讀 NAV 快照／CPI／Beta，呼叫純邏輯模組，寫入跑道快照並推播。

計算邏輯在 `market_analysis/withdrawal_runway.py`（單一權威）；本模組只負責讀資料、
組裝輸入、寫快照、推播。16:15 ET 由 `cogs/trading/after_market.py` 於 NAV 快照與 FRED
觀測更新之後呼叫；快照寫入成功後，經 `risk_withdrawal_runway` 頻道推播壓力跑道警示
（§2.5）與提領提醒（§3）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Literal, Mapping, Optional, Sequence

import numpy as np

import database
import market_time
from database.macro_signal_log import load_observations
from database.user_settings import UserContext, get_full_user_context
from database.withdrawal_runway import (
    RunwaySnapshot,
    load_snapshot,
    upsert_snapshots,
)
from market_analysis.downside_monitor import NavSnapshot
from market_analysis.macro_signals import Observation, usable
from market_analysis.withdrawal_runway import (
    RUNWAY_WARN_TIERS_YEARS,
    STRESS_BETA_FALLBACK,
    adjust_withdrawal,
    boxx_payments,
    clamp_beta,
    evaluate_tiers,
    plan_withdrawal,
    stress_runway,
    zero_return_runway_years,
)
from services.downside_risk_service import (
    build_portfolio_return_series,
    get_users_with_positions,
    load_nav_snapshots,
    load_portfolio_exposure,
)
from services.llm_service import is_memory_safe
from services.notification_dispatcher import notify

logger = logging.getLogger(__name__)

CPI_SERIES_ID = "CPIAUCSL"
BOXX_SYMBOL = "BOXX"
# NAV 快照超過此交易日數未更新 → 面板標示「資料過期」，不視為當日資料
NAV_STALE_TRADING_DAYS = 5
_MIN_BETA_OBS = 60
# 前置提醒日：提領月份前一個月的這一天（遇假日由「視窗內首次執行」自然順延）
PRE_REMINDER_DAY = 15
# 當日提醒最晚在提領日後幾個日曆天內補發（任務當天被跳過時），逾期不再提醒
_DAY_REMINDER_GRACE_DAYS = 7

ReminderKind = Literal["PRE", "DAY"]


def parse_months(text: str) -> list[int]:
    """`withdrawal_months`（逗號分隔）→ 排序月份；壞值回預設 [1, 7]。"""
    try:
        ms = sorted({int(x) for x in str(text).split(",") if x.strip()})
    except ValueError:
        return [1, 7]
    return ms if ms and all(1 <= m <= 12 for m in ms) else [1, 7]


def first_trading_day(year: int, month: int) -> Optional[date]:
    """該月的首個 NYSE 交易日；行事曆無資料 → None。"""
    first = date(year, month, 1)
    sched = market_time.nyse_calendar.schedule(
        start_date=first, end_date=first + timedelta(days=10)
    )
    return None if sched.empty else sched.index[0].date()


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
        target = first_trading_day(year, month)
        if target is None or target <= today:
            continue
        span = market_time.nyse_calendar.schedule(start_date=today, end_date=target)
        days = int(sum(1 for d in span.index if d.date() > today))
        return target, month, days
    raise ValueError(f"無法由 months={list(months)} 推算下一次提領日")


def reminder_due(
    today: date, months: Sequence[int], last_sent: Optional[str]
) -> Optional[tuple[ReminderKind, date]]:
    """今天是否該推播提領提醒；回傳（種類, 提領日），已送過或不在視窗 → None。

    - `DAY`：今天落在提領月份首個交易日起 `_DAY_REMINDER_GRACE_DAYS` 天內；
    - `PRE`：今天落在「前一個月 15 日 ≤ 今天 < 提領日」。
    以視窗內第一次成功執行判定（而非恰好某一天），任務當天被跳過時隔天仍會補發。
    `last_sent` 為上次送達的狀態 id（`f"{kind}_{提領日}"`）。
    """
    ms = sorted(set(months))
    due: Optional[tuple[ReminderKind, date]] = None
    if today.month in ms:
        target = first_trading_day(today.year, today.month)
        if (
            target is not None
            and 0 <= (today - target).days <= _DAY_REMINDER_GRACE_DAYS
        ):
            due = ("DAY", target)
    if due is None:
        target, _, _ = next_withdrawal_date(today, ms)
        pre_year, pre_month = (
            (target.year, target.month - 1)
            if target.month > 1
            else (target.year - 1, 12)
        )
        if date(pre_year, pre_month, PRE_REMINDER_DAY) <= today < target:
            due = ("PRE", target)
    if due is None or last_sent == reminder_state_id(*due):
        return None
    return due


def reminder_state_id(kind: ReminderKind, target: date) -> str:
    return f"{kind}_{target.isoformat()}"


def parse_target_weights(text: Optional[str]) -> Optional[dict[str, float]]:
    """`withdrawal_target_weights`（JSON {symbol: weight}）；空、壞值或無正權重 → None（等權）。"""
    if not text:
        return None
    try:
        raw = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for k, v in raw.items():
        if isinstance(v, (int, float)) and math.isfinite(v) and v > 0:
            out[str(k).upper()] = float(v)
    return out or None


def sellable_holdings(
    stock_shares: Mapping[str, float], closes: Mapping[str, float]
) -> dict[str, float]:
    """賣出清單候選：現股多頭市值（排除 BOXX、空頭與無收盤價者）。"""
    out: dict[str, float] = {}
    for sym, qty in stock_shares.items():
        price = float(closes.get(sym, 0.0))
        if sym == BOXX_SYMBOL or qty <= 0 or price <= 0:
            continue
        out[sym] = float(qty) * price
    return out


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
) -> Optional[tuple[RunwaySnapshot, NavSnapshot]]:
    """回傳（跑道快照, 使用的 NAV 快照）；NAV 快照供提領提醒的賣出清單取收盤價。"""
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
    snap = compute_user_runway(
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
    return None if snap is None else (snap, latest)


# ---------------------------------------------------------------------------
# 推播（§2.5 警示分級、§3 提領提醒）
# ---------------------------------------------------------------------------


def _tier_state_key(user_id: int) -> str:
    # runway_state_ 前綴刻意不列入每日去重白名單：武裝狀態必須跨日保留
    return f"runway_state_tiers_{user_id}"


def _remind_state_key(user_id: int) -> str:
    return f"runway_state_remind_{user_id}"


async def _notify_warnings(
    bot: Any, user_id: int, snap: RunwaySnapshot, *, stale: bool, today_str: str
) -> None:
    """壓力跑道跌破已武裝門檻 → 以最嚴重一級推播；送達才前進武裝狀態。"""
    from cogs.embed_builders.alert_embeds.withdrawal_alerts import (
        create_runway_warning_embed,
    )

    if stale:  # §5.1：NAV 快照過期只在面板標註，不推播警示
        return
    raw = await asyncio.to_thread(database.get_kv_cache, _tier_state_key(user_id))
    # 無狀態 = 全部武裝：上線當下已低於門檻者會立即收到第一則警示（規格刻意如此）
    armed = (
        [int(t) for t in raw]
        if isinstance(raw, list)
        else list(RUNWAY_WARN_TIERS_YEARS)
    )
    fired, new_armed = evaluate_tiers(snap.stress_years, armed)
    if fired:
        tier = min(fired)
        sent = await notify(
            bot,
            user_id,
            "risk_withdrawal_runway",
            embed=create_runway_warning_embed(snap, tier),
            dedup_key=f"runway_warn_{user_id}_{tier}_{today_str}",
        )
        # 頻道關閉期間不前進，使用者重新開啟後仍會收到
        if sent:
            await database.save_kv_cache(_tier_state_key(user_id), new_armed)
    elif sorted(new_armed) != sorted(armed) or not isinstance(raw, list):
        await database.save_kv_cache(_tier_state_key(user_id), new_armed)


async def _notify_reminder(
    bot: Any,
    user_id: int,
    snap: RunwaySnapshot,
    ctx: UserContext,
    nav_snapshot: NavSnapshot,
    *,
    today: date,
    today_str: str,
) -> None:
    """前置提醒日／提領日 → 推播提領額與賣出清單；送達才記錄已送。"""
    from cogs.embed_builders.alert_embeds.withdrawal_alerts import (
        create_withdrawal_reminder_embed,
    )

    months = parse_months(ctx.withdrawal_months)
    raw = await asyncio.to_thread(database.get_kv_cache, _remind_state_key(user_id))
    due = reminder_due(today, months, raw if isinstance(raw, str) else None)
    if due is None:
        return
    kind, target = due
    exposure = await asyncio.to_thread(load_portfolio_exposure, user_id)
    plan = plan_withdrawal(
        snap.next_withdrawal,
        snap.boxx_value,
        sellable_holdings(exposure.stock_shares, nav_snapshot.closes),
        parse_target_weights(ctx.withdrawal_target_weights),
    )
    sent = await notify(
        bot,
        user_id,
        "risk_withdrawal_runway",
        embed=create_withdrawal_reminder_embed(
            kind, target, plan, snap, rebalance=target.month == months[0]
        ),
        dedup_key=f"runway_remind_{user_id}_{kind}_{today_str}",
    )
    if sent:
        await database.save_kv_cache(
            _remind_state_key(user_id), reminder_state_id(kind, target)
        )


async def run_withdrawal_runway_job(bot: Any, trading_date: date) -> int:
    """16:15 ET：為已設定提領的使用者計算並寫入跑道快照，再推播警示與提醒。

    回傳寫入筆數。快照寫入失敗時不推播，避免推播內容與面板不一致。
    """
    if not _job_allowed(bot):
        return 0
    uids = await asyncio.to_thread(get_users_with_positions)
    cpi_obs = await asyncio.to_thread(load_observations, CPI_SERIES_ID)
    spy: Optional[dict[str, float]] = None
    out: list[RunwaySnapshot] = []
    pending: list[tuple[RunwaySnapshot, UserContext, NavSnapshot]] = []
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
            result = await _snapshot_for_user(uid, ctx, trading_date, spy, cpi_obs)
            if result is not None:
                out.append(result[0])
                pending.append((result[0], ctx, result[1]))
        except Exception as e:
            logger.error(f"[WithdrawalRunway] uid={uid} 計算失敗: {e}")
    try:
        await upsert_snapshots(out)
    except Exception as e:
        logger.error(f"[WithdrawalRunway] 快照寫入失敗: {e}")
        return 0
    today_str = trading_date.strftime("%Y%m%d")
    for snap, ctx, nav_snapshot in pending:
        try:
            stale = await asyncio.to_thread(is_snapshot_stale, snap, trading_date)
            await _notify_warnings(
                bot, snap.user_id, snap, stale=stale, today_str=today_str
            )
        except Exception as e:
            logger.error(f"[WithdrawalRunway] uid={snap.user_id} 跑道警示失敗: {e}")
        try:
            await _notify_reminder(
                bot,
                snap.user_id,
                snap,
                ctx,
                nav_snapshot,
                today=trading_date,
                today_str=today_str,
            )
        except Exception as e:
            logger.error(f"[WithdrawalRunway] uid={snap.user_id} 提領提醒失敗: {e}")
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
    "first_trading_day",
    "get_runway_display",
    "get_runway_snapshot",
    "is_snapshot_stale",
    "next_withdrawal_date",
    "parse_target_weights",
    "reminder_due",
    "run_withdrawal_runway_job",
    "sellable_holdings",
]
