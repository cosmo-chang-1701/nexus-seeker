"""實體替代數據之純函式計算層（期別對齊、SEC XBRL 單季推導、TSA / FRED / 台股月營收解析）。

本模組不做任何 I/O，所有網路抓取在 `services/alt_data_service.py`。

期別契約（`as_of_period`）：
- 一律為曆年季度 `YYYY-Qn`（例如 `2026-Q2`），由 `validate_period` 驗證。
- 各資料源觀測值都帶有「觀測期間」，與目標期別不一致者不得納入計算。

前視防護：
- SEC XBRL 事實以 `filed <= as_of` 過濾，同一期間多次申報取 `filed` 最新者。
- TSA 只採用 `<= as_of` 的日資料；FRED 由呼叫端以 `usable()` 依可用日過濾。
- 台股月營收快照以 `出表日期 <= as_of` 與 `資料年月` 屬於目標期別雙重檢查。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

# ============================================================================
# 期別（曆年季度）工具
# ============================================================================

_PERIOD_RE = re.compile(r"^(\d{4})-Q([1-4])$")


def validate_period(period: str) -> str:
    """驗證並回傳標準期別字串 `YYYY-Qn`；格式不符拋出 ValueError。"""
    if not isinstance(period, str) or _PERIOD_RE.match(period) is None:
        raise ValueError(
            f"as_of_period 必須為 YYYY-Qn 格式（例如 2026-Q2），收到: {period!r}"
        )
    return period


def _parse_period(period: str) -> tuple[int, int]:
    m = _PERIOD_RE.match(validate_period(period))
    assert m is not None
    return int(m.group(1)), int(m.group(2))


def period_of_date(d: date) -> str:
    """日期所屬曆年季度。"""
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def shift_period(period: str, quarters: int) -> str:
    """期別平移（負數往前）。"""
    year, q = _parse_period(period)
    idx = year * 4 + (q - 1) + quarters
    return f"{idx // 4}-Q{idx % 4 + 1}"


def period_bounds(period: str) -> tuple[date, date]:
    """期別首日與末日。"""
    year, q = _parse_period(period)
    start = date(year, 3 * (q - 1) + 1, 1)
    next_start = date(year + 1, 1, 1) if q == 4 else date(year, 3 * q + 1, 1)
    return start, next_start - timedelta(days=1)


def completed_periods(today: date, count: int = 2) -> list[str]:
    """`today` 之前已結束的最近 `count` 個曆年季度（新到舊）。"""
    current = period_of_date(today)
    return [shift_period(current, -(i + 1)) for i in range(count)]


# ============================================================================
# SEC XBRL companyconcept 事實與單季推導
# ============================================================================

# 單季期間長度容許範圍（天）：涵蓋 12 週（84 天）、13 週、16 週（112 天，例如 COST Q4）
QUARTER_MIN_DAYS = 70
QUARTER_MAX_DAYS = 120
# 時點值（存貨、RPO）以「期末日 − 45 天」對應曆季，與單季期間中點對應法一致
INSTANT_PERIOD_OFFSET_DAYS = 45


@dataclass(frozen=True)
class XbrlFact:
    """精簡後的 XBRL 事實（只保留計算所需欄位，避免快取整份 JSON）。"""

    start: date | None
    end: date
    val: float
    filed: date


@dataclass(frozen=True)
class QuarterValue:
    """單一離散季度的數值。"""

    start: date
    end: date
    val: float
    derived: bool  # True = 由累計期間相減推導（例如 Q4 = FY − 9M）


def _to_date(raw: Any) -> date | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def parse_companyconcept_facts(
    payload: Mapping[str, Any], unit: str = "USD"
) -> list[XbrlFact]:
    """解析 SEC companyconcept JSON 的 `units[unit]` 陣列為精簡事實清單。"""
    units = payload.get("units")
    if not isinstance(units, Mapping):
        return []
    rows = units.get(unit)
    if not isinstance(rows, list):
        return []
    facts: list[XbrlFact] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        end = _to_date(row.get("end"))
        filed = _to_date(row.get("filed"))
        val = row.get("val")
        if end is None or filed is None or not isinstance(val, (int, float)):
            continue
        facts.append(
            XbrlFact(
                start=_to_date(row.get("start")), end=end, val=float(val), filed=filed
            )
        )
    return facts


def _latest_filed(
    facts: Iterable[XbrlFact], as_of: date
) -> dict[tuple[date | None, date], XbrlFact]:
    """以 `filed <= as_of` 過濾，同一 (start, end) 期間取最新申報（含後續重編）。"""
    best: dict[tuple[date | None, date], XbrlFact] = {}
    for f in facts:
        if f.filed > as_of:
            continue
        key = (f.start, f.end)
        prev = best.get(key)
        if prev is None or f.filed > prev.filed:
            best[key] = f
    return best


def _is_quarter_length(start: date, end: date) -> bool:
    days = (end - start).days + 1
    return QUARTER_MIN_DAYS <= days <= QUARTER_MAX_DAYS


def duration_period(start: date, end: date) -> str:
    """期間型數值對應曆季：以期間中點所屬季度（與 SEC frames 對非曆年制公司的歸屬一致）。"""
    mid = start + (end - start) / 2
    return period_of_date(mid)


def instant_period(end: date) -> str:
    """時點型數值對應曆季：期末日往前 45 天所屬季度。"""
    return period_of_date(end - timedelta(days=INSTANT_PERIOD_OFFSET_DAYS))


def discrete_quarters(
    facts: Iterable[XbrlFact], as_of: date
) -> dict[date, QuarterValue]:
    """由期間型事實推導離散單季數值，以季末日為鍵。

    - 直接申報的單季（70–120 天）優先。
    - 否則以「同起始日的兩個累計期間相減」推導最後一季（例如 9M − 6M、FY − 9M；
      現金流量表在 10-Q 多為年初至今累計，Q4 只能由 10-K 全年減 9M 求得）。
    """
    latest = _latest_filed((f for f in facts if f.start is not None), as_of)
    quarters: dict[date, QuarterValue] = {}
    by_start: dict[date, list[XbrlFact]] = {}
    for (start, end), f in latest.items():
        assert start is not None
        if _is_quarter_length(start, end):
            quarters[end] = QuarterValue(start=start, end=end, val=f.val, derived=False)
        by_start.setdefault(start, []).append(f)

    for start, group in by_start.items():
        group_sorted = sorted(group, key=lambda x: x.end)
        for i, longer in enumerate(group_sorted):
            if longer.end in quarters:
                continue
            for shorter in group_sorted[:i]:
                q_start = shorter.end + timedelta(days=1)
                if _is_quarter_length(q_start, longer.end):
                    quarters[longer.end] = QuarterValue(
                        start=q_start,
                        end=longer.end,
                        val=longer.val - shorter.val,
                        derived=True,
                    )
                    break
    return quarters


def quarters_by_period(
    quarters: Mapping[date, QuarterValue],
) -> dict[str, QuarterValue]:
    """離散季度對應曆季；52/53 週或 12/16 週制造成碰撞時，取季末較晚者。"""
    out: dict[str, QuarterValue] = {}
    for q in sorted(quarters.values(), key=lambda x: x.end):
        out[duration_period(q.start, q.end)] = q
    return out


def instants_by_end(facts: Iterable[XbrlFact], as_of: date) -> dict[date, float]:
    """時點型事實（存貨、RPO）以期末日為鍵，`filed <= as_of` 取最新申報。"""
    latest = _latest_filed(facts, as_of)
    out: dict[date, float] = {}
    for (_start, end), f in latest.items():
        out[end] = f.val
    return out


def instants_by_period(
    facts: Iterable[XbrlFact], as_of: date
) -> dict[str, tuple[date, float]]:
    """時點型事實對應曆季（同季多個期末日取最晚者）。"""
    out: dict[str, tuple[date, float]] = {}
    for end, val in sorted(instants_by_end(facts, as_of).items()):
        out[instant_period(end)] = (end, val)
    return out


def yoy_pct(current: float, prior: float) -> float | None:
    """年增率（%）；基期 <= 0 時無意義回傳 None。"""
    if prior <= 0:
        return None
    return round((current - prior) / prior * 100.0, 2)


@dataclass(frozen=True)
class MetricYoY:
    """單一公司、單一指標的同口徑年增率與其觀測期間。"""

    period: str  # 目標曆季
    yoy_pct: float
    current_end: date
    prior_end: date
    detail: str  # 繁中口徑說明（例如「單季 vs 去年同季」）


def flow_quarter_yoy(
    facts: Iterable[XbrlFact], period: str, as_of: date
) -> MetricYoY | None:
    """期間型指標（capex、營收、銷貨成本）單季 vs 去年同季年增率。"""
    by_period = quarters_by_period(discrete_quarters(list(facts), as_of))
    cur = by_period.get(period)
    prior = by_period.get(shift_period(period, -4))
    if cur is None or prior is None:
        return None
    g = yoy_pct(cur.val, prior.val)
    if g is None:
        return None
    return MetricYoY(
        period=period,
        yoy_pct=g,
        current_end=cur.end,
        prior_end=prior.end,
        detail="單季 vs 去年同季",
    )


def instant_yoy(
    facts: Iterable[XbrlFact], period: str, as_of: date
) -> MetricYoY | None:
    """時點型指標（RPO）季末值 vs 去年同季季末值年增率。"""
    by_period = instants_by_period(list(facts), as_of)
    cur = by_period.get(period)
    prior = by_period.get(shift_period(period, -4))
    if cur is None or prior is None:
        return None
    g = yoy_pct(cur[1], prior[1])
    if g is None:
        return None
    return MetricYoY(
        period=period,
        yoy_pct=g,
        current_end=cur[0],
        prior_end=prior[0],
        detail="季末時點值 vs 去年同季季末",
    )


def dio_by_period(
    inventory_facts: Iterable[XbrlFact], cogs_facts: Iterable[XbrlFact], as_of: date
) -> dict[str, tuple[date, float]]:
    """DIO = 季末存貨 / 單季銷貨成本 × 該季天數（存貨取與銷貨成本季末同日之時點值）。"""
    inventory = instants_by_end(list(inventory_facts), as_of)
    quarters = discrete_quarters(list(cogs_facts), as_of)
    out: dict[str, tuple[date, float]] = {}
    for q in sorted(quarters.values(), key=lambda x: x.end):
        inv = inventory.get(q.end)
        if inv is None or q.val <= 0:
            continue
        days = (q.end - q.start).days + 1
        out[duration_period(q.start, q.end)] = (q.end, inv / q.val * days)
    return out


def dio_yoy(
    inventory_facts: Iterable[XbrlFact],
    cogs_facts: Iterable[XbrlFact],
    period: str,
    as_of: date,
) -> MetricYoY | None:
    """DIO 單季 vs 去年同季年增率（%）。"""
    by_period = dio_by_period(inventory_facts, cogs_facts, as_of)
    cur = by_period.get(period)
    prior = by_period.get(shift_period(period, -4))
    if cur is None or prior is None:
        return None
    g = yoy_pct(cur[1], prior[1])
    if g is None:
        return None
    return MetricYoY(
        period=period,
        yoy_pct=g,
        current_end=cur[0],
        prior_end=prior[0],
        detail=f"DIO {cur[1]:.1f} 天 vs 去年同季 {prior[1]:.1f} 天",
    )


# ============================================================================
# TSA 每日安檢客流
# ============================================================================

# 真實頁面為兩欄表格：Date (M/D/YYYY) | Numbers (千分位整數)；
# 當年度頁 `/travel/passenger-volumes` 由新到舊，歷年頁 `/travel/passenger-volumes/YYYY` 由舊到新。
_TSA_ROW_RE = re.compile(
    r"<tr[^>]*>\s*<td[^>]*>\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*</td>\s*"
    r"<td[^>]*>\s*([\d,]+)\s*</td>\s*</tr>",
    re.IGNORECASE,
)
TSA_YOY_ALIGN_DAYS = 364  # 52 週前同一星期幾，避免星期效應
TSA_MIN_MATCHED_DAYS = 28


def parse_tsa_table(html_text: str) -> dict[date, int]:
    """解析 TSA 兩欄表格為 {日期: 客流}。"""
    out: dict[date, int] = {}
    for m, d, y, num in _TSA_ROW_RE.findall(html_text):
        try:
            out[date(int(y), int(m), int(d))] = int(num.replace(",", ""))
        except ValueError:
            continue
    return out


@dataclass(frozen=True)
class WindowYoY:
    """高頻資料在目標期別內之同期比較結果。"""

    period: str
    yoy_pct: float
    window_start: date
    window_end: date
    observations: int
    detail: str


def tsa_period_yoy(
    daily: Mapping[date, int], period: str, as_of: date
) -> WindowYoY | None:
    """TSA 目標曆季內（截至 as_of）日均客流 vs 52 週前同星期幾之日均，需 >= 28 個配對日。"""
    start, end = period_bounds(period)
    cur_vals: list[int] = []
    prior_vals: list[int] = []
    matched: list[date] = []
    d = start
    while d <= min(end, as_of):
        cur = daily.get(d)
        prior = daily.get(d - timedelta(days=TSA_YOY_ALIGN_DAYS))
        if cur is not None and prior is not None and cur > 0 and prior > 0:
            cur_vals.append(cur)
            prior_vals.append(prior)
            matched.append(d)
        d += timedelta(days=1)
    if len(matched) < TSA_MIN_MATCHED_DAYS:
        return None
    g = yoy_pct(sum(cur_vals) / len(cur_vals), sum(prior_vals) / len(prior_vals))
    if g is None:
        return None
    return WindowYoY(
        period=period,
        yoy_pct=g,
        window_start=matched[0],
        window_end=matched[-1],
        observations=len(matched),
        detail=f"季內 {len(matched)} 日日均 vs 52 週前同星期幾",
    )


# ============================================================================
# FRED 月度序列
# ============================================================================


def monthly_period_yoy(
    observations: Sequence[tuple[date, float]], period: str
) -> WindowYoY | None:
    """月度序列目標曆季內已公布月份均值 vs 去年同月份均值（同月份配對；不足時回 None，不改用月增率）。"""
    start, end = period_bounds(period)
    by_month = {(d.year, d.month): v for d, v in observations}
    cur_vals: list[float] = []
    prior_vals: list[float] = []
    months: list[date] = []
    for d, v in sorted(observations):
        if start <= d <= end:
            prior = by_month.get((d.year - 1, d.month))
            if prior is not None and prior > 0 and v > 0:
                cur_vals.append(v)
                prior_vals.append(prior)
                months.append(d)
    if not months:
        return None
    g = yoy_pct(sum(cur_vals) / len(cur_vals), sum(prior_vals) / len(prior_vals))
    if g is None:
        return None
    return WindowYoY(
        period=period,
        yoy_pct=g,
        window_start=months[0],
        window_end=months[-1],
        observations=len(months),
        detail=f"季內 {len(months)} 個月均值 vs 去年同月份",
    )


# ============================================================================
# 台股月營收（TWSE / TPEx OpenAPI，民國年）
# ============================================================================


def parse_roc_year_month(raw: Any) -> date | None:
    """`資料年月` 民國年月（`11508` 或 `115/08`）轉為該月 1 日。"""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) < 4:
        return None
    try:
        year = int(digits[:-2]) + 1911
        month = int(digits[-2:])
        return date(year, month, 1)
    except ValueError:
        return None


def parse_roc_date(raw: Any) -> date | None:
    """`出表日期` 民國日期（`1150917` 或 `115/09/17`）轉為西元日期。"""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) < 6:
        return None
    try:
        return date(int(digits[:-4]) + 1911, int(digits[-4:-2]), int(digits[-2:]))
    except ValueError:
        return None
