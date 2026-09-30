"""總經訊號乾跑：候選指標計算、資料可用日規則與三態判定（純函式葉模組）。

用途：以 LLM 時代的真實資料做**前向驗證**。每天計算一組候選總經指標與「好／轉差／最差」
三態判定，只寫入資料庫（`services/macro_signal_service.py`），**不推播、不影響任何建議**。
歷史回測（PR #19）顯示先前的指標（信用利差、Sahm、升息）在崩跌開始後才亮；使用者也擔心
LLM 出現後歷史關係已改變，因此改以前向資料判讀哪些指標真的領先。

三態（使用者目標：平時 100% 科技股）：
- 第二層（系統性危機）任一亮 → ``WORST``（建議 BOXX）
- 否則第一層（利率衝擊）任一亮 → ``CAUTION``（建議 VOO）
- 否則 → ``GOOD``（100% 科技股）
對照組指標只記錄，不參與判定。

門檻皆事先依經濟邏輯訂定，標為 PRE_CALIBRATION；**不得**依歷史回測回頭調整，
否則前向驗證就失去意義。

本模組只依賴 stdlib，可被 services 與 calibration 共用。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Literal, Mapping, Optional, Sequence

# ---------------------------------------------------------------------------
# 狀態與指標
# ---------------------------------------------------------------------------

MacroState = Literal["GOOD", "CAUTION", "WORST"]

STATE_GOOD: MacroState = "GOOD"
STATE_CAUTION: MacroState = "CAUTION"
STATE_WORST: MacroState = "WORST"

# 狀態需連續 N 個交易日成立才切換（減少洗盤）
CONFIRM_DAYS = 5  # PRE_CALIBRATION


class Indicator(str, Enum):
    # 第一層：利率衝擊（科技股 → VOO）
    REAL_YIELD_JUMP = "real_yield_jump"  # FRED DFII10
    TWO_YEAR_JUMP = "two_year_jump"  # FRED DGS2
    TECH_RELATIVE_WEAK = "tech_relative_weak"  # 科技池 vs VOO 3 個月相對報酬
    # 第二層：系統性危機（→ BOXX）
    VIX_TERM_INVERSION = "vix_term_inversion"  # ^VIX / ^VIX3M
    FIN_STRESS = "fin_stress"  # FRED STLFSI4
    CLAIMS_SURGE = "claims_surge"  # FRED ICSA
    # 對照組（先前回測過，只記錄）
    CREDIT_SPREAD_WIDEN = "credit_spread_widen"  # FRED BAA10Y
    SAHM_RULE = "sahm_rule"  # FRED SAHMREALTIME
    FED_HIKE_CYCLE = "fed_hike_cycle"  # FRED DFF


TIER1: frozenset[Indicator] = frozenset(
    {Indicator.REAL_YIELD_JUMP, Indicator.TWO_YEAR_JUMP, Indicator.TECH_RELATIVE_WEAK}
)
TIER2: frozenset[Indicator] = frozenset(
    {Indicator.VIX_TERM_INVERSION, Indicator.FIN_STRESS, Indicator.CLAIMS_SURGE}
)
CONTROL: frozenset[Indicator] = frozenset(
    {Indicator.CREDIT_SPREAD_WIDEN, Indicator.SAHM_RULE, Indicator.FED_HIKE_CYCLE}
)

# ---------------------------------------------------------------------------
# 門檻（PRE_CALIBRATION：事先依經濟邏輯訂定，不依回測調整）
# ---------------------------------------------------------------------------

RATE_LOOKBACK_OBS = 126  # 約半年交易日
REAL_YIELD_JUMP_PP = 1.0
TWO_YEAR_JUMP_PP = 1.0
FED_HIKE_JUMP_PP = 1.0

TECH_RELATIVE_LOOKBACK = 63  # 約 3 個月交易日
TECH_RELATIVE_WEAK_PP = -5.0  # 科技池 3 個月報酬減 VOO ≤ −5 pp

VIX_TERM_INVERSION_RATIO = 1.0

FIN_STRESS_THRESHOLD = 0.0  # STLFSI4 ≥ 0 代表壓力高於長期平均

CLAIMS_AVG_WEEKS = 4
CLAIMS_LOW_LOOKBACK_WEEKS = 52
CLAIMS_SURGE_RATIO = 0.20  # 4 週平均較 52 週低點上升 ≥ 20%

CREDIT_SPREAD_MA_OBS = 126
CREDIT_SPREAD_MULT = 1.2

SAHM_THRESHOLD = 0.50

# ---------------------------------------------------------------------------
# 資料可用日（前視防護）
# ---------------------------------------------------------------------------

# FRED 序列 → 發布節奏
SeriesKind = Literal[
    "daily", "weekly_stlfsi", "weekly_claims", "monthly_sahm", "monthly_cpi"
]

FRED_SERIES: dict[str, SeriesKind] = {
    "DFII10": "daily",
    "DGS2": "daily",
    "BAA10Y": "daily",
    "DFF": "daily",
    "STLFSI4": "weekly_stlfsi",
    "ICSA": "weekly_claims",
    "SAHMREALTIME": "monthly_sahm",
    # 提領跑道的通膨調整用（docs/risk_portfolio/05），不參與任何總經指標判定
    "CPIAUCSL": "monthly_cpi",
}


def _next_weekday(d: date) -> date:
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def available_date_for(kind: SeriesKind, obs_date: date) -> date:
    """某筆觀測「最早可在哪一天被使用」（保守估計，寧晚勿早）。

    - daily：FRED 於次一營業日更新 → 觀測日的下一個平日。
    - weekly_stlfsi：觀測日為週五（週結束），隔週四公布 → +6 天。
    - weekly_claims：觀測日為週六（週結束），隔週四公布 → +5 天。
    - monthly_cpi：觀測日 + 45 天（提領跑道 CPI_RELEASE_LAG_DAYS）。
    - monthly_sahm：觀測日為當月 1 日，次月第一個週五的就業報告後才可計算；
      保守以**次月 10 日**為準（遇週末順延到下一個平日）。
    """
    if kind == "daily":
        return _next_weekday(obs_date)
    if kind == "weekly_stlfsi":
        return obs_date + timedelta(days=6)
    if kind == "weekly_claims":
        return obs_date + timedelta(days=5)
    if kind == "monthly_cpi":
        # 觀測日為當月 1 日；與提領跑道的公布延遲一致（45 天）
        return obs_date + timedelta(days=45)
    # monthly_sahm
    year = obs_date.year + (1 if obs_date.month == 12 else 0)
    month = 1 if obs_date.month == 12 else obs_date.month + 1
    day = min(10, calendar.monthrange(year, month)[1])
    avail = date(year, month, day)
    while avail.weekday() >= 5:
        avail += timedelta(days=1)
    return avail


@dataclass(frozen=True)
class Observation:
    obs_date: date
    value: float
    available_date: date


def usable(observations: Sequence[Observation], as_of: date) -> list[Observation]:
    """只保留在 `as_of` 當天已公布的觀測，依觀測日排序。"""
    return sorted(
        (o for o in observations if o.available_date <= as_of),
        key=lambda o: o.obs_date,
    )


# ---------------------------------------------------------------------------
# 指標計算
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndicatorReading:
    indicator: Indicator
    value: Optional[float]
    # None = 資料不足，無法判定（不視為亮起，但會記錄）
    flag: Optional[bool]
    # 使用到的最新一筆觀測的所屬日期與可用日
    as_of_date: Optional[date]
    available_date: Optional[date]


def _missing(ind: Indicator) -> IndicatorReading:
    return IndicatorReading(ind, None, None, None, None)


def change_over(
    ind: Indicator,
    obs: Sequence[Observation],
    lookback: int,
    threshold_pp: float,
) -> IndicatorReading:
    """最新值減 `lookback` 筆之前的值 ≥ threshold_pp 即亮。"""
    if len(obs) <= lookback:
        return _missing(ind)
    last = obs[-1]
    change = last.value - obs[-1 - lookback].value
    return IndicatorReading(
        ind,
        round(change, 4),
        change >= threshold_pp,
        last.obs_date,
        last.available_date,
    )


def credit_spread_widen(obs: Sequence[Observation]) -> IndicatorReading:
    ind = Indicator.CREDIT_SPREAD_WIDEN
    if len(obs) < CREDIT_SPREAD_MA_OBS:
        return _missing(ind)
    window = obs[-CREDIT_SPREAD_MA_OBS:]
    avg = sum(o.value for o in window) / len(window)
    last = obs[-1]
    ratio = last.value / avg if avg > 0 else 0.0
    return IndicatorReading(
        ind,
        round(ratio, 4),
        avg > 0 and ratio >= CREDIT_SPREAD_MULT,
        last.obs_date,
        last.available_date,
    )


def level_at_least(
    ind: Indicator, obs: Sequence[Observation], threshold: float
) -> IndicatorReading:
    if not obs:
        return _missing(ind)
    last = obs[-1]
    return IndicatorReading(
        ind,
        round(last.value, 4),
        last.value >= threshold,
        last.obs_date,
        last.available_date,
    )


def claims_surge(obs: Sequence[Observation]) -> IndicatorReading:
    """4 週平均相對「過去 52 週內 4 週平均的最低點」上升幅度。"""
    ind = Indicator.CLAIMS_SURGE
    need = CLAIMS_AVG_WEEKS + CLAIMS_LOW_LOOKBACK_WEEKS - 1
    if len(obs) < need:
        return _missing(ind)
    vals = [o.value for o in obs]
    avgs = [
        sum(vals[i - CLAIMS_AVG_WEEKS + 1 : i + 1]) / CLAIMS_AVG_WEEKS
        for i in range(CLAIMS_AVG_WEEKS - 1, len(vals))
    ]
    current = avgs[-1]
    low = min(avgs[-CLAIMS_LOW_LOOKBACK_WEEKS:])
    rise = current / low - 1.0 if low > 0 else 0.0
    last = obs[-1]
    return IndicatorReading(
        ind,
        round(rise, 4),
        low > 0 and rise >= CLAIMS_SURGE_RATIO,
        last.obs_date,
        last.available_date,
    )


def vix_term_inversion(
    vix: Optional[float], vix3m: Optional[float], as_of: date
) -> IndicatorReading:
    ind = Indicator.VIX_TERM_INVERSION
    if vix is None or vix3m is None or vix3m <= 0:
        return _missing(ind)
    ratio = vix / vix3m
    return IndicatorReading(
        ind, round(ratio, 4), ratio >= VIX_TERM_INVERSION_RATIO, as_of, as_of
    )


def period_return(closes: Sequence[float], lookback: int) -> Optional[float]:
    """最後一筆收盤相對 `lookback` 筆之前的報酬；資料不足回 None。"""
    if len(closes) <= lookback:
        return None
    base = closes[-1 - lookback]
    if base <= 0:
        return None
    return closes[-1] / base - 1.0


def tech_relative_weak(
    tech_closes: Mapping[str, Sequence[float]],
    voo_closes: Sequence[float],
    as_of: date,
) -> IndicatorReading:
    """科技池等權 3 個月報酬（各檔報酬的平均）減 VOO 3 個月報酬，單位 pp。"""
    ind = Indicator.TECH_RELATIVE_WEAK
    voo_ret = period_return(voo_closes, TECH_RELATIVE_LOOKBACK)
    tech_rets = [
        r
        for r in (
            period_return(c, TECH_RELATIVE_LOOKBACK) for c in tech_closes.values()
        )
        if r is not None
    ]
    if voo_ret is None or not tech_rets:
        return _missing(ind)
    diff_pp = (sum(tech_rets) / len(tech_rets) - voo_ret) * 100.0
    return IndicatorReading(
        ind, round(diff_pp, 4), diff_pp <= TECH_RELATIVE_WEAK_PP, as_of, as_of
    )


# ---------------------------------------------------------------------------
# 三態判定
# ---------------------------------------------------------------------------


def classify_raw(readings: Sequence[IndicatorReading]) -> MacroState:
    """未經確認的原始狀態。資料不足（flag=None）不視為亮起；對照組不參與。"""
    lit = {r.indicator for r in readings if r.flag}
    if lit & TIER2:
        return STATE_WORST
    if lit & TIER1:
        return STATE_CAUTION
    return STATE_GOOD


def confirm_state(
    recent_raw: Sequence[MacroState],
    previous_confirmed: Optional[MacroState],
    confirm_days: int = CONFIRM_DAYS,
) -> Optional[MacroState]:
    """以最近 `confirm_days` 個交易日（含今天，依時間排序）的原始狀態確認。

    - 最近 N 天原始狀態全相同 → 確認為該狀態；
    - 否則維持前一個已確認狀態；
    - 尚未累積 N 天且沒有前一個確認狀態 → None（上線初期的暖機期）。
    """
    window = list(recent_raw)[-confirm_days:]
    if len(window) == confirm_days and len(set(window)) == 1:
        return window[-1]
    return previous_confirmed
