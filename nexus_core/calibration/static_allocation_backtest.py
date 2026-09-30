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

總經切換（`tech_share_by_day`，選用；未提供時行為與固定比例逐位元相同）：逐日給定「當日開盤
採用」的科技池占比 T（由 `calibration/macro_regime.py` 以前一交易日已知資訊決定）。T 改變的
交易日於開盤做**股票部位內部換倉**：以當下股票市值（含漂移）依新的 T 重新分配到科技池與 VOO，
BOXX 金額不動——股票總比例與 BOXX 比例不因總經狀態改變，只在排程再平衡日回到目標。

三態配置（`targets_by_day`，選用，與 `tech_share_by_day` 互斥）：逐日給定「當日開盤採用」的
三個資產桶目標權重（TECH／VOO／BOXX 欄）。目標改變的交易日於開盤做**完整再平衡**到新目標
（例如好 → 轉差：科技池全數換成 VOO）；排程再平衡日依當時的目標再平衡（科技池成員更新）。

本模組是**離線回測**，不修改任何 production 程式碼或參數；指標一律呼叫
`market_analysis/downside_risk.py`（經 `regime_momentum_backtest.compute_metrics`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    switch_days: list[pd.Timestamp] = field(default_factory=list)  # 總經切換換倉日


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
    tech_share_by_day: Optional[pd.Series] = None,
    targets_by_day: Optional[pd.DataFrame] = None,
) -> StaticAllocationResult:
    """逐日模擬。`opens`／`closes` 需含選股池與 CORE_SYMBOL（已串接代理）；
    `cash_index` 為 BOXX 現金型部位的總報酬指數（與 closes 同日曆）。

    `tech_share_by_day`：總經切換用，逐日「當日開盤採用」的科技池占比；None = 固定
    `params.tech_share`（原行為）。
    `targets_by_day`：三態配置用，逐日「當日開盤採用」的資產桶目標權重（TECH／VOO／BOXX）；
    目標改變時完整再平衡。與 `tech_share_by_day` 互斥。"""
    if tech_share_by_day is not None and targets_by_day is not None:
        raise ValueError("tech_share_by_day 與 targets_by_day 不可同時提供")
    idx = closes.index
    tz = idx.tz
    days = idx[(idx >= pd.Timestamp(start, tz=tz)) & (idx <= pd.Timestamp(end, tz=tz))]
    if len(days) < 2:
        raise ValueError("回測期間的交易日不足")
    pos_of = {d: i for i, d in enumerate(idx)}
    scheduled = pd.Series(rebalance_calendar(idx, params.rebalance), index=idx)
    targets = params.targets()
    cash_idx = cash_index.reindex(idx).ffill()
    t_by_day: Optional[pd.Series] = None
    if tech_share_by_day is not None:
        t_by_day = (
            tech_share_by_day.reindex(idx)
            .ffill()
            .fillna(params.tech_share)
            .astype(float)
        )
    held_t = params.tech_share
    tgt_by_day: Optional[pd.DataFrame] = None
    if targets_by_day is not None:
        tgt_by_day = (
            targets_by_day[[BUCKET_TECH, BUCKET_CORE, BUCKET_CASH]]
            .reindex(idx)
            .ffill()
            .astype(float)
        )
    held_tgt: Optional[tuple[float, float, float]] = None
    switch_days: list[pd.Timestamp] = []

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

        if t_by_day is not None:
            t_today = float(t_by_day.iat[i])
            if n == 0 or t_today != held_t:
                targets = replace(params, tech_share=t_today).targets()

        state_change = False
        if tgt_by_day is not None:
            row = tgt_by_day.iloc[i]
            tgt_today = (
                float(row[BUCKET_TECH]),
                float(row[BUCKET_CORE]),
                float(row[BUCKET_CASH]),
            )
            if tgt_today != held_tgt:
                targets = {
                    BUCKET_TECH: tgt_today[0],
                    BUCKET_CORE: tgt_today[1],
                    BUCKET_CASH: tgt_today[2],
                }
                state_change = n > 0

        do_rebalance = n == 0 or state_change
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
            if t_by_day is not None:
                held_t = float(t_by_day.iat[i])
            if tgt_by_day is not None:
                held_tgt = (
                    targets[BUCKET_TECH],
                    targets[BUCKET_CORE],
                    targets[BUCKET_CASH],
                )
                if state_change:
                    switch_days.append(day)
        elif t_by_day is not None and float(t_by_day.iat[i]) != held_t:
            # 總經切換：股票部位內部換倉（BOXX 金額不動），成本比照再平衡按比例扣除
            t_new = float(t_by_day.iat[i])
            open_px = {s: _last_valid(opens, s, i) for s in shares}
            equity_open = sum(q * open_px[s] for s, q in shares.items())
            nav_open = cash + equity_open
            members = [
                s
                for s in params.universe
                if s in closes.columns
                and np.isfinite(closes[s].iat[i - 1])
                and np.isfinite(opens[s].iat[i])
            ]
            new_shares = {}
            if members and t_new > 0:
                per = t_new * equity_open / len(members)
                for s in members:
                    new_shares[s] = per / float(opens[s].iat[i])
            if t_new < 1.0:
                new_shares[CORE_SYMBOL] = (
                    (1.0 - t_new) * equity_open / float(opens[CORE_SYMBOL].iat[i])
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
            scale = (nav_open - cost) / nav_open if nav_open > 0 else 1.0
            new_shares = {s: q * scale for s, q in new_shares.items()}
            stock_value = sum(q * float(opens[s].iat[i]) for s, q in new_shares.items())
            # 科技池無可交易標的時（實際不會發生），未配置的股票金額留在 BOXX
            cash = nav_open - cost - stock_value
            shares = new_shares
            tech_members = set(members)
            traded += turnover
            costs += cost
            held_t = t_new
            switch_days.append(day)
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
        switch_days=switch_days,
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
