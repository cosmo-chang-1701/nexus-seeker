"""回測結果分析：逐年分列、逐檔貢獻、BOXX 退場統計（純函式，無 I/O）。

判讀指標一律呼叫 `market_analysis/downside_risk.py`（Sortino 為主，MDD 與 VaR／CVaR
為輔）。逐年 Sortino 以該年的日報酬計算，分子為該年的幾何年化報酬、MAR 為無風險利率。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from market_analysis.downside_risk import (
    annualized_return,
    historical_var_cvar,
    max_drawdown,
    nav_from_returns,
    sortino_ratio,
)

RISK_FREE_RATE = 0.045


@dataclass(frozen=True)
class PeriodMetrics:
    label: str
    days: int
    total_return: float
    sortino: float
    max_drawdown: float
    cvar_95: Optional[float]


def _period(label: str, returns: Sequence[float]) -> PeriodMetrics:
    arr = np.asarray(list(returns), dtype=float)
    nav = nav_from_returns(arr)
    tail = historical_var_cvar(arr)
    return PeriodMetrics(
        label=label,
        days=int(arr.size),
        total_return=float(nav[-1] - 1.0) if arr.size else 0.0,
        sortino=sortino_ratio(
            arr, RISK_FREE_RATE, annual_return=annualized_return(arr)
        ),
        max_drawdown=max_drawdown(nav).max_drawdown,
        cvar_95=tail.cvar if tail else None,
    )


def yearly_metrics(
    daily_history: Sequence[Any],
) -> list[tuple[PeriodMetrics, PeriodMetrics]]:
    """逐年 (策略, B&H) 指標。每年的第一個交易日報酬以前一年最後一天收盤為基準。"""
    by_year: dict[str, tuple[list[float], list[float]]] = {}
    for i, rec in enumerate(daily_history):
        if i == 0:
            continue  # 第一天是建倉日，報酬以期初本金計，與 calculate_metrics 一致
        year = str(rec.date)[:4]
        strat, bench = by_year.setdefault(year, ([], []))
        strat.append(float(rec.daily_return))
        bench.append(float(rec.benchmark_daily_return))
    return [(_period(y, s), _period(y, b)) for y, (s, b) in sorted(by_year.items())]


def symbol_contribution(
    trades: Sequence[Any], positions: dict[str, Any]
) -> dict[str, dict[str, float]]:
    """每檔標的的損益貢獻：已實現（含手續費、Covered Call 收益）+ 期末未實現。

    空頭部位以 `<SYM>_SHORT` 記帳，這裡併回同一檔標的。
    """
    out: dict[str, dict[str, float]] = {}

    def _row(sym: str) -> dict[str, float]:
        return out.setdefault(
            sym.replace("_SHORT", ""),
            {"realized": 0.0, "unrealized": 0.0, "sells": 0.0, "stop_exits": 0.0},
        )

    for t in trades:
        if t.scenario == "INIT":
            continue
        row = _row(t.symbol)
        row["realized"] += float(t.realized_pnl)
        if t.action in ("SELL", "COVER"):
            row["sells"] += 1
            if "SL1" in t.reason or "SL2" in t.reason or "結構停損" in t.reason:
                row["stop_exits"] += 1
    for sym, pos in positions.items():
        _row(sym)["unrealized"] += float(pos.unrealized_pnl)
    for row in out.values():
        row["total"] = row["realized"] + row["unrealized"]
    return out


def retreat_summary(
    episodes: Sequence[dict[str, Any]],
    last_core_px: float,
    last_boxx_px: float,
    total_days: int,
) -> dict[str, Any]:
    """BOXX 退場統計：次數、天數、whipsaw 成本。

    whipsaw 成本（每次退場）= 回場時核心價 / 退場時核心價 − 1：為正代表退場期間核心
    其實上漲，回場時以更高的價格買回（錯過的反彈）；為負代表成功避開的跌幅。尚未
    回場的最後一次以回測最後一天的價格計算。BOXX 報酬 = 同期間停泊資金的收益。
    """
    rows: list[dict[str, Any]] = []
    for ep in episodes:
        enter_core = ep["enter_core_px"] if ep["enter_core_px"] else last_core_px
        enter_boxx = ep["enter_boxx_px"] if ep["enter_boxx_px"] else last_boxx_px
        days = ep.get("days")
        if days is None:
            days = total_days - int(ep["exit_idx"])
        rows.append(
            {
                "exit_date": ep["exit_date"],
                "enter_date": ep["enter_date"],
                "days": int(days),
                "core_change": enter_core / ep["exit_core_px"] - 1.0,
                "boxx_return": enter_boxx / ep["exit_boxx_px"] - 1.0,
            }
        )
    return {
        "exits": len(episodes),
        "entries": sum(1 for ep in episodes if ep["enter_date"]),
        "days_in_retreat": sum(r["days"] for r in rows),
        "episodes": rows,
        "missed_rebounds": [r for r in rows if r["core_change"] > 0],
    }
