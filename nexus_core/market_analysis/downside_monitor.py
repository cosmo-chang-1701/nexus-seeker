"""投組下行風險監控的純邏輯（無 I/O）：指標快照、回撤階梯、CVaR 預算與已實現報酬。

所有指標定義一律呼叫 `market_analysis/downside_risk.py`（單一權威），本模組只負責
「何時該推播」的判定與 NAV 快照的報酬還原。I/O（持倉讀取、歷史價格、推播、寫入）
在 `services/downside_risk_service.py`。

評估優先序（`docs/risk_portfolio/07_downside_risk_sortino_var_cvar.md`）：Sortino 為主，
MDD 與 VaR / CVaR 為輔。**Sortino 不推播**——它是慢變數、用於評估；推播只由回撤
階梯與 CVaR 預算觸發，兩者都是會在數日內惡化到需要行動的左尾訊號。

所有閾值皆為校準前的保守值（PRE_CALIBRATION），調整走 calibration 報告 → 人工審核 → PR。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import numpy as np

from market_analysis.downside_risk import (
    MIN_VAR_SAMPLES,
    current_drawdown,
    historical_var_cvar,
    max_drawdown,
    nav_from_returns,
    sortino_ratio,
)

# ---------------------------------------------------------------------------
# 具名常數（PRE_CALIBRATION）
# ---------------------------------------------------------------------------

# 模擬報酬序列的回看長度（交易日）
LOOKBACK_DAYS = 252

# Sortino 顯示視窗（交易日）：一季與一年
SORTINO_WINDOWS: tuple[int, int] = (63, 252)

# 距 1 年高點的回撤階梯。B&H 投資人被迫認賠出場多半發生在 -15%～-20% 區間
# （2025 回測 B&H MDD 16.8%），-10% 作為第一道提醒。PRE_CALIBRATION。
DRAWDOWN_TIERS: tuple[float, ...] = (0.10, 0.15, 0.20)

# 回撤回升超過「已觸發階梯 − 緩衝」才重新武裝，避免在階梯線附近來回震盪時每天
# 重複推播同一階。PRE_CALIBRATION。
DRAWDOWN_REARM_BUFFER = 0.025

# 1 日 CVaR95 預算 = risk_limit (%) × 此係數。risk_limit 預設 15% → 3.0%，約等於
# 2025 年 SPY/NVDA/GLD 混合 B&H 的 1 日 CVaR95 (2.94%)。PRE_CALIBRATION。
CVAR_BUDGET_PER_RISK_LIMIT = 0.20

# 尾部體制轉換：近一季 CVaR95 較過去 60 個交易日的滾動中位數擴張倍數。PRE_CALIBRATION。
CVAR_SHORT_WINDOW = 63
CVAR_BASELINE_DAYS = 60
CVAR_EXPANSION_RATIO = 1.5


@dataclass(frozen=True)
class DownsideSnapshot:
    """投組下行風險快照（比例皆為正值損失；Sortino 不足樣本時為 None）。"""

    n_obs: int
    sortino_63: Optional[float]
    sortino_252: Optional[float]
    max_drawdown: float
    current_drawdown: float
    var_95: Optional[float]
    cvar_95: Optional[float]
    cvar_short: Optional[float]
    cvar_short_baseline: Optional[float]


def _sortino_window(
    returns: np.ndarray, window: int, mar_annual: float
) -> Optional[float]:
    if returns.size < window:
        return None
    return sortino_ratio(returns[-window:], mar_annual)


def rolling_short_cvar_baseline(returns: np.ndarray) -> Optional[float]:
    """過去 `CVAR_BASELINE_DAYS` 個交易日，每日以前 `CVAR_SHORT_WINDOW` 日計算的
    CVaR95 之中位數（不含今日視窗，避免當前尾部事件墊高自己的基準）。"""
    needed = CVAR_SHORT_WINDOW + CVAR_BASELINE_DAYS
    if returns.size < needed:
        return None
    values: list[float] = []
    for end in range(returns.size - CVAR_BASELINE_DAYS, returns.size):
        res = historical_var_cvar(
            returns[end - CVAR_SHORT_WINDOW : end], min_samples=MIN_VAR_SAMPLES
        )
        if res is not None:
            values.append(res.cvar)
    if not values:
        return None
    return float(np.median(values))


def compute_snapshot(
    returns: Sequence[float] | np.ndarray,
    mar_annual: float,
    intraday_return: Optional[float] = None,
) -> Optional[DownsideSnapshot]:
    """由日報酬序列（可附加今日盤中報酬）計算下行風險快照；樣本不足回 None。"""
    arr = np.asarray(returns, dtype=float)
    arr = arr[np.isfinite(arr)][-LOOKBACK_DAYS:]
    if arr.size < MIN_VAR_SAMPLES:
        return None
    path = arr if intraday_return is None else np.append(arr, intraday_return)
    nav = nav_from_returns(path)
    tail = historical_var_cvar(arr)
    short_tail = historical_var_cvar(arr[-CVAR_SHORT_WINDOW:])
    return DownsideSnapshot(
        n_obs=int(arr.size),
        sortino_63=_sortino_window(arr, SORTINO_WINDOWS[0], mar_annual),
        sortino_252=_sortino_window(arr, SORTINO_WINDOWS[1], mar_annual),
        max_drawdown=max_drawdown(nav).max_drawdown,
        current_drawdown=current_drawdown(nav),
        var_95=tail.var if tail else None,
        cvar_95=tail.cvar if tail else None,
        cvar_short=short_tail.cvar if short_tail else None,
        cvar_short_baseline=rolling_short_cvar_baseline(arr),
    )


# ---------------------------------------------------------------------------
# 回撤階梯與重新武裝
# ---------------------------------------------------------------------------


def evaluate_drawdown_tier(
    drawdown: float, armed_level: float
) -> tuple[Optional[float], float]:
    """回撤階梯判定。

    `armed_level` 為目前已觸發（尚未重新武裝）的最深階梯，0.0 代表全部武裝。
    回傳 `(要推播的階梯或 None, 新的 armed_level)`：
    - 回撤跨越比 `armed_level` 更深的階梯 → 推播最深的那一階，armed_level 前進；
    - 回撤回升到 `armed_level − DRAWDOWN_REARM_BUFFER` 之上 → 該階重新武裝，armed_level
      退回到目前回撤仍然超過的最深階梯（可能是 0.0）。
    """
    crossed = [t for t in DRAWDOWN_TIERS if drawdown >= t]
    deepest = max(crossed) if crossed else 0.0
    if deepest > armed_level:
        return deepest, deepest
    if armed_level > 0.0 and drawdown < armed_level - DRAWDOWN_REARM_BUFFER:
        return None, deepest
    return None, armed_level


# ---------------------------------------------------------------------------
# CVaR 預算
# ---------------------------------------------------------------------------


def cvar_budget(risk_limit_pct: float) -> float:
    """1 日 CVaR95 預算（比例）。`risk_limit_pct` 為 `user_settings.risk_limit`（%）。"""
    return max(0.0, risk_limit_pct) / 100.0 * CVAR_BUDGET_PER_RISK_LIMIT


def evaluate_cvar_breaches(
    snapshot: DownsideSnapshot, risk_limit_pct: float
) -> list[str]:
    """回傳觸發的 CVaR 條件代碼：`BUDGET`（超過預算）、`EXPANSION`（尾部體制轉換）。"""
    reasons: list[str] = []
    if snapshot.cvar_95 is not None and snapshot.cvar_95 > cvar_budget(risk_limit_pct):
        reasons.append("BUDGET")
    if (
        snapshot.cvar_short is not None
        and snapshot.cvar_short_baseline is not None
        and snapshot.cvar_short_baseline > 0.0
        and snapshot.cvar_short >= CVAR_EXPANSION_RATIO * snapshot.cvar_short_baseline
    ):
        reasons.append("EXPANSION")
    return reasons


# ---------------------------------------------------------------------------
# NAV 快照 → 已實現日報酬
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NavSnapshot:
    """單日淨值快照：`shares` 為各標的持股（期權以 Delta 等值股數計、帶號），
    `closes` 為當日收盤價。"""

    date: str
    nav: float
    shares: Mapping[str, float]
    closes: Mapping[str, float]


def realized_returns_from_snapshots(snapshots: Sequence[NavSnapshot]) -> list[float]:
    """日報酬 = Σ 前一日持股 × (今日收盤 − 前一日收盤) ÷ 前一日 NAV。

    以前一日的持股計算，加碼 / 減碼 / 入金只會改變「下一天的持股」，不會被算成
    當天的報酬。前一日 NAV ≤ 0 或缺少任一方收盤價的標的不計入。
    """
    ordered = sorted(snapshots, key=lambda s: s.date)
    out: list[float] = []
    for prev, cur in zip(ordered, ordered[1:]):
        if prev.nav <= 0.0:
            continue
        pnl = 0.0
        for sym, qty in prev.shares.items():
            p0 = prev.closes.get(sym)
            p1 = cur.closes.get(sym)
            if p0 is None or p1 is None or p0 <= 0.0 or p1 <= 0.0:
                continue
            pnl += qty * (p1 - p0)
        out.append(pnl / prev.nav)
    return out
