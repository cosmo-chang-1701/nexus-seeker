"""固定比例配置 + 定期再平衡（日線）回測：選項 A。

動機（見 docs/strategies/09_static_allocation_rebalance.md）：先前五份回測顯示，沒有任何
擇時或進出場規則能比「固定較低的持股比例」更便宜地降低暴跌風險。本模組量出不同固定比例
下「暴跌風險 vs 報酬」的取捨，讓使用者依能承受的回撤挑選比例；它本身不做任何擇時。

三個資產桶：
- 科技池：選股池 point-in-time 等權（再平衡當日「前一日有收盤、當日有開盤」者才納入，
  與 `regime_momentum_backtest.equal_weight_pit` 相同規則）
- VOO（上市前以 SPY 串接，見 `daily_panel.prepare_panel`）
- BOXX 現金型部位（BOXX → BIL → 固定利率銜接的總報酬指數）

目標權重：股票總比例 E，其中科技池占 T；VOO = E × (1 − T)；BOXX = 1 − E。

再平衡方式：
- ANNUAL：每年第一個交易日
- QUARTERLY：每季（1／4／7／10 月）第一個交易日
- THRESHOLD：每月第一個交易日檢查，任一資產桶（以前一日收盤計）偏離目標 ≥
  `drift_threshold` 時才再平衡；科技池的成員名單也只在再平衡時更新
- 第一個交易日一律建倉。

時序（無前視）：再平衡的觸發與偏離判定只用前一日收盤；於當日開盤成交，當日收盤計價。
成本：股票（科技池與 VOO）成交金額 × `cost_rate`（單邊），BOXX 進出不計成本。

本模組是**離線回測**，不修改任何 production 程式碼或參數；指標一律呼叫
`market_analysis/downside_risk.py`（經 `regime_momentum_backtest.compute_metrics`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

import numpy as np
import pandas as pd

from calibration.regime_momentum_backtest import (
    CORE_SYMBOL,
    DEFAULT_UNIVERSE,
    TRADING_DAYS,
)

RebalanceMode = Literal["ANNUAL", "QUARTERLY", "THRESHOLD"]

BUCKET_TECH = "TECH"
BUCKET_CORE = "VOO"
BUCKET_CASH = "BOXX"

EQUITY_SHARES: tuple[float, ...] = (1.00, 0.80, 0.70, 0.60, 0.50, 0.40)
TECH_SHARES: tuple[float, ...] = (1.00, 0.50, 0.00)
REBALANCE_MODES: tuple[RebalanceMode, ...] = ("ANNUAL", "QUARTERLY", "THRESHOLD")


@dataclass(frozen=True)
class StaticAllocationParams:
    """所有規則參數集中於此。"""

    equity_share: float = 0.60  # E：股票總比例，其餘放 BOXX
    tech_share: float = 0.50  # T：股票內科技池的占比
    rebalance: RebalanceMode = "ANNUAL"
    drift_threshold: float = 0.05  # THRESHOLD 模式：任一資產桶偏離目標 ≥ 5 個百分點
    cost_rate: float = 0.0015  # 單邊交易成本（比照既有回測），BOXX 不計
    universe: tuple[str, ...] = DEFAULT_UNIVERSE

    def targets(self) -> dict[str, float]:
        e = min(max(self.equity_share, 0.0), 1.0)
        t = min(max(self.tech_share, 0.0), 1.0)
        return {
            BUCKET_TECH: e * t,
            BUCKET_CORE: e * (1.0 - t),
            BUCKET_CASH: 1.0 - e,
        }

    def label(self) -> str:
        mode = {"ANNUAL": "年", "QUARTERLY": "季", "THRESHOLD": "偏離"}[self.rebalance]
        return (
            f"股 {self.equity_share * 100:.0f}%／科技 {self.tech_share * 100:.0f}%"
            f"／{mode}"
        )


@dataclass
class StaticAllocationResult:
    nav: pd.Series
    rebalance_days: list[pd.Timestamp] = field(default_factory=list)
    traded_notional: float = 0.0  # 股票成交金額總和（買 + 賣），BOXX 不計
    costs: float = 0.0
    bucket_weights: pd.DataFrame = field(default_factory=pd.DataFrame)


def rebalance_calendar(index: pd.DatetimeIndex, mode: RebalanceMode) -> np.ndarray:
    """回傳每個交易日是否為「排程檢查日」：ANNUAL＝每年第一個交易日；QUARTERLY＝每季
    第一個交易日；THRESHOLD＝每月第一個交易日（當日才檢查偏離，未必成交）。"""
    years = np.array([d.year for d in index])
    months = np.array([d.month for d in index])
    new_month = np.ones(len(index), dtype=bool)
    new_month[1:] = (years[1:] != years[:-1]) | (months[1:] != months[:-1])
    if mode == "THRESHOLD":
        return new_month
    if mode == "QUARTERLY":
        return new_month & np.isin(months, (1, 4, 7, 10))
    new_year = np.ones(len(index), dtype=bool)
    new_year[1:] = years[1:] != years[:-1]
    return new_year


def _last_valid(frame: pd.DataFrame, sym: str, i: int) -> float:
    col = frame[sym]
    v = col.iat[i]
    if np.isfinite(v):
        return float(v)
    prior = col.iloc[: i + 1].dropna()
    return float(prior.iloc[-1]) if len(prior) else float("nan")


def bucket_weights_at(
    shares: dict[str, float],
    cash: float,
    prices: dict[str, float],
    tech_members: set[str],
) -> dict[str, float]:
    """以給定價格計算三個資產桶的權重。"""
    tech = sum(q * prices[s] for s, q in shares.items() if s in tech_members)
    core = sum(q * prices[s] for s, q in shares.items() if s not in tech_members)
    total = tech + core + cash
    if total <= 0:
        return {BUCKET_TECH: 0.0, BUCKET_CORE: 0.0, BUCKET_CASH: 1.0}
    return {
        BUCKET_TECH: tech / total,
        BUCKET_CORE: core / total,
        BUCKET_CASH: cash / total,
    }


def needs_rebalance(
    current: dict[str, float], targets: dict[str, float], threshold: float
) -> bool:
    """任一資產桶偏離目標 ≥ threshold（容許浮點誤差）時需要再平衡。"""
    return any(abs(current[k] - targets[k]) >= threshold - 1e-12 for k in targets)


def simulate_static(
    opens: pd.DataFrame,
    closes: pd.DataFrame,
    cash_index: pd.Series,
    start: str,
    end: str,
    params: StaticAllocationParams = StaticAllocationParams(),
    initial_capital: float = 100_000.0,
) -> StaticAllocationResult:
    """逐日模擬。`opens`／`closes` 需含選股池與 CORE_SYMBOL（已串接代理）；
    `cash_index` 為 BOXX 現金型部位的總報酬指數（與 closes 同日曆）。"""
    idx = closes.index
    tz = idx.tz
    days = idx[(idx >= pd.Timestamp(start, tz=tz)) & (idx <= pd.Timestamp(end, tz=tz))]
    if len(days) < 2:
        raise ValueError("回測期間的交易日不足")
    pos_of = {d: i for i, d in enumerate(idx)}
    scheduled = pd.Series(rebalance_calendar(idx, params.rebalance), index=idx)
    targets = params.targets()
    cash_idx = cash_index.reindex(idx).ffill()

    shares: dict[str, float] = {}
    tech_members: set[str] = set()
    cash = float(initial_capital)
    traded = 0.0
    costs = 0.0
    nav_out: list[float] = []
    w_rows: list[dict[str, float]] = []
    reb_days: list[pd.Timestamp] = []

    for n, day in enumerate(days):
        i = pos_of[day]
        # 現金型部位先依 BOXX 指數由前一日收盤計息到今日收盤（開盤成交視為以前一日
        # 收盤後的現金金額進行；當日利息對 1 天的誤差可忽略，且各配置一致）
        if n > 0:
            prev = pos_of[days[n - 1]]
            growth = float(cash_idx.iat[i] / cash_idx.iat[prev])
        else:
            growth = 1.0

        do_rebalance = n == 0
        if not do_rebalance and bool(scheduled.iat[i]):
            if params.rebalance == "THRESHOLD":
                prev_prices = {s: _last_valid(closes, s, i - 1) for s in shares}
                cur = bucket_weights_at(shares, cash, prev_prices, tech_members)
                do_rebalance = needs_rebalance(cur, targets, params.drift_threshold)
            else:
                do_rebalance = True

        if do_rebalance:
            open_px = {s: _last_valid(opens, s, i) for s in shares}
            nav_open = cash + sum(q * open_px[s] for s, q in shares.items())
            members = [
                s
                for s in params.universe
                if s in closes.columns
                and i >= 1
                and np.isfinite(closes[s].iat[i - 1])
                and np.isfinite(opens[s].iat[i])
            ]
            new_shares: dict[str, float] = {}
            tech_w = targets[BUCKET_TECH]
            if members and tech_w > 0:
                per = tech_w * nav_open / len(members)
                for s in members:
                    new_shares[s] = per / float(opens[s].iat[i])
            core_w = targets[BUCKET_CORE]
            # 科技池沒有任何可交易標的時，科技桶的資金自然留在 BOXX（下方現金 = 淨值 − 股票）
            if core_w > 0:
                new_shares[CORE_SYMBOL] = (
                    core_w * nav_open / float(opens[CORE_SYMBOL].iat[i])
                )
            turnover = 0.0
            for s in set(shares) | set(new_shares):
                px = (
                    float(opens[s].iat[i])
                    if np.isfinite(opens[s].iat[i])
                    else _last_valid(opens, s, i)
                )
                turnover += abs(new_shares.get(s, 0.0) - shares.get(s, 0.0)) * px
            cost = turnover * params.cost_rate
            # 成本自淨值扣除，所有部位（含 BOXX）按比例縮減，與 equal_weight_pit 相同作法；
            # 因此 E=100%／T=100%／ANNUAL 與等權對照組逐位元一致（有測試把關）
            scale = (nav_open - cost) / nav_open if nav_open > 0 else 1.0
            new_shares = {s: q * scale for s, q in new_shares.items()}
            stock_value = sum(q * float(opens[s].iat[i]) for s, q in new_shares.items())
            cash = nav_open - cost - stock_value
            shares = new_shares
            tech_members = set(members)
            traded += turnover
            costs += cost
            reb_days.append(day)
        cash *= growth
        close_px = {s: _last_valid(closes, s, i) for s in shares}
        nav = cash + sum(q * close_px[s] for s, q in shares.items())
        nav_out.append(nav)
        w_rows.append(bucket_weights_at(shares, cash, close_px, tech_members))

    return StaticAllocationResult(
        nav=pd.Series(nav_out, index=days, name=params.label()),
        rebalance_days=reb_days,
        traded_notional=traded,
        costs=costs,
        bucket_weights=pd.DataFrame(w_rows, index=days),
    )


# ---------------------------------------------------------------------------
# 額外指標（B&H 投資人關心的回撤體驗）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DrawdownEpisode:
    depth: float
    peak: pd.Timestamp
    trough: pd.Timestamp
    recovered: Optional[pd.Timestamp]  # None = 期末仍未收復高點
    recovery_days: int  # 高點 → 收復高點的交易日數；未收復時為高點 → 期末


def max_drawdown_episode(nav: pd.Series) -> DrawdownEpisode:
    """最大回撤的高點、低點與收復日（交易日數自高點起算）。"""
    s = nav.dropna()
    values = s.to_numpy(dtype=float)
    peaks = np.maximum.accumulate(values)
    dd = np.where(peaks > 0, (peaks - values) / peaks, 0.0)
    trough = int(np.argmax(dd))
    peak = int(np.argmax(values[: trough + 1])) if trough > 0 else 0
    peak_value = values[peak]
    after = np.nonzero(values[trough:] >= peak_value)[0]
    if len(after):
        rec = trough + int(after[0])
        return DrawdownEpisode(
            float(dd[trough]), s.index[peak], s.index[trough], s.index[rec], rec - peak
        )
    return DrawdownEpisode(
        float(dd[trough]), s.index[peak], s.index[trough], None, len(values) - 1 - peak
    )


def worst_rolling_return(nav: pd.Series, window: int = TRADING_DAYS) -> float:
    """最差的滾動 `window` 個交易日報酬（預設 12 個月）。"""
    s = nav.dropna()
    if len(s) <= window:
        return float(s.iloc[-1] / s.iloc[0] - 1.0)
    r = s / s.shift(window) - 1.0
    return float(r.min())


def annual_turnover_static(result: StaticAllocationResult) -> float:
    """單邊年換手率 = 股票成交金額 / 2 / 平均淨值 / 年數。"""
    years = max(len(result.nav) / TRADING_DAYS, 1e-9)
    return result.traded_notional / 2.0 / float(result.nav.mean()) / years
