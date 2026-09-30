"""提領跑道的純邏輯葉模組（docs/risk_portfolio/05）。

只用標準庫、不做 I/O（唯一例外：讀取隨程式碼提交的 `data/stress_paths.csv`），
所有輸入由呼叫端（階段二的 service）提供，方便單元測試與離線重現。
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Optional, Sequence

WITHDRAWAL_MONTHS: tuple[int, ...] = (1, 7)
CPI_RELEASE_LAG_DAYS = 45
STRESS_BETA_CLAMP: tuple[float, float] = (0.5, 2.0)
STRESS_BETA_FALLBACK = 1.3
RUNWAY_WARN_TIERS_YEARS: tuple[int, ...] = (3, 2, 1)
RUNWAY_REARM_BUFFER_YEARS = 0.5
TRADING_DAYS_PER_YEAR = 252
WITHDRAWALS_PER_YEAR = 2
WITHDRAWAL_INTERVAL_DAYS = TRADING_DAYS_PER_YEAR // WITHDRAWALS_PER_YEAR
STRESS_HORIZON_YEARS = 10.0

STRESS_PATH_GFC = "GFC"
STRESS_PATH_DOTCOM = "DOTCOM"
_STRESS_CSV = Path(__file__).parent / "data" / "stress_paths.csv"


@dataclass(frozen=True)
class StressPath:
    returns: tuple[float, ...]
    cpi_growth: tuple[float, ...]


@lru_cache(maxsize=1)
def load_stress_paths() -> dict[str, StressPath]:
    """讀取靜態壓力路徑；欄位 path,k,ret,cpi_growth，k 必須自 0 連續。"""
    rets: dict[str, list[float]] = {}
    cpis: dict[str, list[float]] = {}
    with _STRESS_CSV.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            name = row["path"]
            if int(row["k"]) != len(rets.setdefault(name, [])):
                raise ValueError(f"{name}: k 不連續 (k={row['k']})")
            rets[name].append(float(row["ret"]))
            cpis.setdefault(name, []).append(float(row["cpi_growth"]))
    return {n: StressPath(tuple(rets[n]), tuple(cpis[n])) for n in rets}


def adjust_withdrawal(
    base: float, cpi_anchor: float, cpi_now: Optional[float]
) -> float:
    """第 n 次提領額 = 基準額 × CPI_now / CPI_anchor；CPI 缺漏時不調整（呼叫端須註明）。"""
    if base <= 0 or cpi_now is None or cpi_anchor <= 0 or cpi_now <= 0:
        return max(base, 0.0)
    return base * cpi_now / cpi_anchor


def zero_return_runway_years(nav: float, next_withdrawal: float) -> float:
    """零報酬對照：NAV ÷（每年 2 次 × 每次提領額）。提領額為 0 → inf。"""
    if next_withdrawal <= 0:
        return math.inf
    return max(nav, 0.0) / (WITHDRAWALS_PER_YEAR * next_withdrawal)


def boxx_payments(boxx_value: float, next_withdrawal: float) -> int:
    """BOXX 目前足以支付幾次完整提領。"""
    if next_withdrawal <= 0:
        return 0
    return int(max(boxx_value, 0.0) // next_withdrawal)


def clamp_beta(beta: Optional[float]) -> float:
    """None／非有限值 → 保守預設；其餘夾在 STRESS_BETA_CLAMP。"""
    if beta is None or not math.isfinite(beta):
        return STRESS_BETA_FALLBACK
    lo, hi = STRESS_BETA_CLAMP
    return min(max(beta, lo), hi)


def replay_years(
    nav: float,
    next_withdrawal: float,
    path: StressPath,
    scale: float,
    days_to_first: int = 0,
) -> float:
    """自今天起重演 `path`：NAV_k = NAV_{k-1} × (1 + scale × r_k) − 該日提領額。

    第一次提領在第 `days_to_first` 個交易日，之後每 WITHDRAWAL_INTERVAL_DAYS 一次；
    提領額以 `next_withdrawal` 為起點、依路徑同期 CPI 累積比值外推。回傳耗盡年數
    （第一個 NAV ≤ 0 的 k ÷ 252），重演 STRESS_HORIZON_YEARS 年仍存活則回傳該上限。
    """
    if nav <= 0:
        return 0.0
    if next_withdrawal <= 0:
        return STRESS_HORIZON_YEARS
    days = min(len(path.returns), int(STRESS_HORIZON_YEARS * TRADING_DAYS_PER_YEAR))
    next_due = max(days_to_first, 1)
    value = nav
    for k in range(1, days + 1):
        value *= 1.0 + scale * path.returns[k - 1]
        if k >= next_due:
            value -= next_withdrawal * path.cpi_growth[min(k, len(path.cpi_growth) - 1)]
            next_due += WITHDRAWAL_INTERVAL_DAYS
        if value <= 0:
            return k / TRADING_DAYS_PER_YEAR
    return STRESS_HORIZON_YEARS


@dataclass(frozen=True)
class StressRunway:
    gfc_years: float
    dotcom_years: float
    stress_years: float  # 兩者較差
    capped: bool  # True = 較差路徑重演滿 10 年仍未耗盡（顯示為「≥ 10 年」）


def stress_runway(
    nav: float,
    boxx_value: float,
    next_withdrawal: float,
    beta: Optional[float],
    days_to_first: int = 0,
    paths: Optional[Mapping[str, StressPath]] = None,
) -> StressRunway:
    """壓力跑道：2008 SPY × 投組 Beta 與 2000 QQQ × 股票占比，取較差（§2.3）。"""
    p = paths if paths is not None else load_stress_paths()
    s_gfc = clamp_beta(beta)
    equity_share = 1.0 - min(max(boxx_value / nav, 0.0), 1.0) if nav > 0 else 0.0
    gfc = replay_years(nav, next_withdrawal, p[STRESS_PATH_GFC], s_gfc, days_to_first)
    dot = replay_years(
        nav, next_withdrawal, p[STRESS_PATH_DOTCOM], equity_share, days_to_first
    )
    worst = min(gfc, dot)
    return StressRunway(gfc, dot, worst, worst >= STRESS_HORIZON_YEARS)


@dataclass(frozen=True)
class WithdrawalPlan:
    amount: float
    from_boxx: float
    sells: dict[str, float]  # symbol -> 賣出市值
    shortfall: float  # 全部可賣部位都賣了仍不足的差額


def plan_withdrawal(
    amount: float,
    boxx_value: float,
    holdings: Mapping[str, float],
    target_weights: Optional[Mapping[str, float]] = None,
) -> WithdrawalPlan:
    """先扣 BOXX；不足的 R 由超配最多的持股依序賣出，全部超配賣完仍不足則按目標權重
    比例賣出（§2.4）。只處理多頭現股（市值 ≤ 0 的項目忽略）；不產生負部位。"""
    amount = max(amount, 0.0)
    from_boxx = min(amount, max(boxx_value, 0.0))
    need = amount - from_boxx
    longs = {s: v for s, v in holdings.items() if v > 0}
    if need <= 0 or not longs:
        return WithdrawalPlan(amount, from_boxx, {}, need if not longs else 0.0)

    total = sum(longs.values())
    if target_weights:
        wsum = sum(target_weights.get(s, 0.0) for s in longs)
        weights = (
            {s: target_weights.get(s, 0.0) / wsum for s in longs} if wsum > 0 else None
        )
    else:
        weights = None
    if weights is None:
        weights = {s: 1.0 / len(longs) for s in longs}

    after = total - min(need, total)
    excess = {s: longs[s] - weights[s] * after for s in longs}
    sells: dict[str, float] = {}
    remaining = min(need, total)
    for s in sorted(excess, key=lambda k: excess[k], reverse=True):
        if remaining <= 1e-9 or excess[s] <= 0:
            break
        take = min(excess[s], longs[s], remaining)
        sells[s] = take
        remaining -= take
    if remaining > 1e-9:  # 超配不夠：其餘按目標權重、以剩餘市值為上限賣出
        headroom = {s: longs[s] - sells.get(s, 0.0) for s in longs}
        pool = sum(weights[s] for s in longs if headroom[s] > 0)
        for s in longs:
            if headroom[s] <= 0 or pool <= 0:
                continue
            take = min(headroom[s], remaining * weights[s] / pool)
            sells[s] = sells.get(s, 0.0) + take
    shortfall = max(need - total, 0.0)
    return WithdrawalPlan(
        amount, from_boxx, {s: v for s, v in sells.items() if v > 0}, shortfall
    )


def evaluate_tiers(
    stress_years: float, armed: Sequence[int]
) -> tuple[list[int], list[int]]:
    """警示分級與重新武裝（§2.5）。回傳（本次觸發的門檻, 新的武裝門檻）。

    已武裝且 `stress_years < T` → 觸發並解除武裝；已解除者要 `stress_years ≥ T + 緩衝`
    才重新武裝。多級同時跌破時全數列入 fired，呼叫端只需以最嚴重（最小 T）一級推播。
    """
    armed_set = set(armed)
    fired = sorted(t for t in armed_set if stress_years < t)
    armed_set -= set(fired)
    for t in RUNWAY_WARN_TIERS_YEARS:
        if t not in armed_set and stress_years >= t + RUNWAY_REARM_BUFFER_YEARS:
            armed_set.add(t)
    return fired, sorted(armed_set, reverse=True)
