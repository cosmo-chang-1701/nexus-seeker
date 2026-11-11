"""固定比例配置偏離的純邏輯葉模組（docs/strategies/09_static_allocation_rebalance.md §7）。

規則（2026-11-11 決議）：固定比例、不擇時。
- 股票占比 = 多頭現股市值 / 可投資額；BOXX 與現金屬於防守桶，空頭不計入任何一桶；選擇權由輸入契約排除
  （stock_shares 只放現股，本模組不判斷選擇權）。
  此處刻意只有「多頭現股」才算股票（qty > 0），不可把 qty > 0 當成全部持倉。
- 提領前一個月起，下一次提領金額視為預留，從可投資額扣除，不算偏離。
- 偏離 = 股票占比 − 目標占比（百分點）；|偏離| ≥ 門檻即為超標。
- 提醒時機：第一天超標推播一次，之後仍超標則每個日曆月的第一次執行再推一次；
  目標改變視為新的一次超標。狀態只在推播送達後才推進，漏跑或失敗時下次補送。

只用標準庫、不做 I/O，所有輸入由呼叫端提供。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Mapping, Optional

__all__ = [
    "BOXX_SYMBOL",
    "DRIFT_THRESHOLD_PP",
    "AllocationBreakdown",
    "compute_breakdown",
    "reserve_amount",
    "is_outside",
    "DriftState",
    "parse_state",
    "state_to_json",
    "ReminderReason",
    "reminder_due",
    "next_state",
]

BOXX_SYMBOL = "BOXX"
DRIFT_THRESHOLD_PP = 5.0


@dataclass(frozen=True)
class AllocationBreakdown:
    equity_value: float  # 多頭現股市值（不含 BOXX）
    boxx_value: float
    cash: float  # max(cash, 0)
    reserve: float  # 實際扣除的提領預留（已夾在 [0, boxx_value + cash]）
    investable: float  # equity_value + boxx_value + cash − reserve
    equity_pct: float  # equity_value / investable × 100
    target_pct: float  # 夾在 [0, 100]
    drift_pp: float  # equity_pct − target_pct
    move_amount: float  # >0 移入股票、<0 移到 BOXX
    missing_prices: tuple[str, ...]  # qty > 0 但缺收盤價的標的，含 BOXX，排序


def _valid_price(price: Optional[float]) -> bool:
    return price is not None and math.isfinite(price) and price > 0


def compute_breakdown(
    stock_shares: Mapping[str, float],
    closes: Mapping[str, float],
    cash: float,
    target_pct: float,
    reserve: float = 0.0,
) -> Optional[AllocationBreakdown]:
    """計算股票 / BOXX+現金的配置；可投資額 ≤ 0，或現金／預留為非有限值（NaN、inf）時回傳 None。"""
    equity = 0.0
    boxx = 0.0
    missing: list[str] = []
    for symbol, qty in stock_shares.items():
        if not qty > 0:  # 空頭、數量為 0 與 NaN 皆不計入
            continue
        price = closes.get(symbol)
        if not _valid_price(price):
            missing.append(symbol)
            continue
        assert price is not None
        value = qty * price
        if symbol == BOXX_SYMBOL:
            boxx += value
        else:
            equity += value
    if not (math.isfinite(cash) and math.isfinite(reserve)):
        return None  # fail closed：NaN 會讓 max() 回傳 NaN 而通過 investable 檢查
    cash_v = max(cash, 0.0)
    reserve_v = min(max(reserve, 0.0), boxx + cash_v)
    investable = equity + boxx + cash_v - reserve_v
    if investable <= 0:
        return None
    target = min(max(target_pct, 0.0), 100.0)
    equity_pct = equity / investable * 100.0
    return AllocationBreakdown(
        equity_value=equity,
        boxx_value=boxx,
        cash=cash_v,
        reserve=reserve_v,
        investable=investable,
        equity_pct=equity_pct,
        target_pct=target,
        drift_pp=equity_pct - target,
        move_amount=target / 100.0 * investable - equity,
        missing_prices=tuple(sorted(missing)),
    )


def reserve_amount(today: date, next_date: Optional[date], next_amount: float) -> float:
    """提領所在月份的前一個月 1 日起（含）到提領日前一天，預留下次提領金額。"""
    if next_date is None or next_amount <= 0:
        return 0.0
    if next_date.month == 1:
        window_start = date(next_date.year - 1, 12, 1)
    else:
        window_start = date(next_date.year, next_date.month - 1, 1)
    if window_start <= today < next_date:
        return next_amount
    return 0.0


def is_outside(
    b: AllocationBreakdown, threshold_pp: float = DRIFT_THRESHOLD_PP
) -> bool:
    """|偏離| ≥ 門檻（邊界含等號）。"""
    return abs(b.drift_pp) >= threshold_pp


@dataclass(frozen=True)
class DriftState:
    outside: bool
    last_sent: Optional[str]  # 最近一次送達的日期 ISO（YYYY-MM-DD）
    target_pct: Optional[float]  # 上次評估時的目標；目標改變視為新的一次超標


def parse_state(raw: Any) -> Optional[DriftState]:
    """kv_cache 讀回的 dict 轉 DriftState；壞值回傳 None。"""
    if not isinstance(raw, dict):
        return None
    outside = raw.get("outside")
    if not isinstance(outside, bool):
        return None
    last_sent = raw.get("last_sent")
    if last_sent is not None:
        if not isinstance(last_sent, str):
            return None
        try:
            # 3.11+ 的 fromisoformat 也收 "20261111"；只接受 YYYY-MM-DD
            if date.fromisoformat(last_sent).isoformat() != last_sent:
                return None
        except ValueError:
            return None
    target = raw.get("target_pct")
    if target is not None:
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            return None
        target = float(target)
    return DriftState(outside=outside, last_sent=last_sent, target_pct=target)


def state_to_json(state: DriftState) -> dict[str, Any]:
    """parse_state 的反函數。"""
    return {
        "outside": state.outside,
        "last_sent": state.last_sent,
        "target_pct": state.target_pct,
    }


ReminderReason = Literal["CROSSING", "MONTHLY"]


def reminder_due(
    today: date, outside: bool, state: Optional[DriftState], target_pct: float
) -> Optional[ReminderReason]:
    """是否該提醒：首次超標 / 目標改變為 CROSSING，仍超標且跨月為 MONTHLY。"""
    if not outside:
        return None
    if state is None or not state.outside or state.target_pct != target_pct:
        return "CROSSING"
    if state.last_sent is None or state.last_sent[:7] != today.isoformat()[:7]:
        return "MONTHLY"
    return None


def next_state(
    today: date,
    outside: bool,
    state: Optional[DriftState],
    sent: bool,
    target_pct: float,
) -> Optional[DriftState]:
    """狀態只在回到門檻內或推播送達後推進；未送達維持原狀以便下次重試。"""
    if not outside:
        return DriftState(False, state.last_sent if state else None, target_pct)
    if sent:
        return DriftState(True, today.isoformat(), target_pct)
    return state
