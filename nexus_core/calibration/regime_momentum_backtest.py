"""大盤三態切換 + 動能輪動（日線）回測：取代動態轉倉引擎的候選策略。

規格（使用者 2026-09-25 確定，見 docs/strategies/08_regime_momentum_rotation.md）：

1. 大盤三態（以 SPY 日線判定）：
   - 好 (GOOD)：收盤 > 200 日均線，且 50 日均線 > 200 日均線
   - 轉弱 (WEAK)：兩條件只成立一個
   - 很差 (BAD)：兩條件都不成立
   狀態切換需連續 `confirm_days` 個交易日成立才生效。
2. GOOD：核心 VOO 0%，資金等權投入選股池中 12-1 動能前 `top_n` 名。
3. WEAK：XLP／XLV／XLU／GLD 中 12-1 動能最強的 `defensive_top_n` 檔 + VOO，等權。
4. BAD：全部轉入 BOXX（現金型部位）。
5. 不設停利；個股從持有期間最高收盤價回落 `trailing_stop` 時出場，資金留在 BOXX，
   等下一次調整再投入。
6. 每月第一個交易日調整；狀態切換時於次一交易日立即調整。

時序（無前視）：第 t 日的一切決策（狀態、動能排名、回落出場）只用到第 t−1 日收盤為止
的資料，於第 t 日開盤執行，並以第 t 日收盤計價。

本模組是**離線回測**，不修改任何 production 程式碼或參數；指標一律呼叫
`market_analysis/downside_risk.py`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional

import numpy as np
import pandas as pd

from market_analysis.downside_risk import (
    annualized_downside_deviation,
    historical_var_cvar,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
)

Regime = Literal["GOOD", "WEAK", "BAD"]

# 選股池：大型科技與成長股。刻意包含當年強勢、後來崩跌或長期落後的標的
# （INTC、CSCO、IBM、ORCL、QCOM、TXN、MRNA、COIN），以降低事後挑選偏差。
# Yahoo 沒有已下市公司的資料，存活者偏差無法完全消除（見報告限制段落）。
DEFAULT_UNIVERSE: tuple[str, ...] = (
    "AAPL",
    "MSFT",
    "AMZN",
    "GOOGL",
    "META",
    "NVDA",
    "AMD",
    "INTC",
    "CSCO",
    "ORCL",
    "IBM",
    "QCOM",
    "TXN",
    "ADBE",
    "CRM",
    "NFLX",
    "TSLA",
    "MU",
    "AVGO",
    "PLTR",
    "MRNA",
    "COIN",
)
DEFENSIVE_ETFS: tuple[str, ...] = ("XLP", "XLV", "XLU", "GLD")
MARKET_SYMBOL = "SPY"  # 大盤訊號（資料比 VOO 早）
CORE_SYMBOL = "VOO"  # 持倉；上市前以 SPY 報酬代理
CORE_PROXY = "SPY"
CASH_SYMBOL = "BOXX"  # 上市前以 BIL 報酬代理，再之前以固定無風險利率計息
CASH_PROXY = "BIL"

TRADING_DAYS = 252


@dataclass(frozen=True)
class RegimeMomentumParams:
    """所有規則參數集中於此；預設值即使用者規格。"""

    top_n: int = 5
    defensive_top_n: int = 2
    trailing_stop: float = 0.25
    confirm_days: int = 3
    momentum_lookback: int = 252  # 12 個月
    momentum_skip: int = 21  # 扣除最近 1 個月
    sma_fast: int = 50
    sma_slow: int = 200
    cost_rate: float = 0.0015  # 單邊交易成本（比照既有回測）
    # BOXX／BIL 皆無資料時的現金年化利率（只影響 2007-01～2007-05 這一小段）
    fallback_cash_rate: float = 0.045
    universe: tuple[str, ...] = DEFAULT_UNIVERSE
    defensive: tuple[str, ...] = DEFENSIVE_ETFS
    # 回落出場只套用於「個別持股」（選股池成員）；防禦 ETF 與 VOO 由狀態切換保護
    stop_applies_to_universe_only: bool = True


# ---------------------------------------------------------------------------
# 價格序列
# ---------------------------------------------------------------------------


def chain_returns(primary: pd.Series, proxy: pd.Series) -> pd.Series:
    """以 proxy 的報酬補齊 primary 上市前的期間，輸出與 primary 上市後比例一致的價格序列。

    primary 上市後的價格原封不動；上市前以 proxy 的逐日報酬往回推。兩者都缺的日期為 NaN。
    """
    primary = primary.astype(float)
    proxy = proxy.astype(float)
    first = primary.first_valid_index()
    if first is None:
        return proxy.copy()
    out = primary.copy()
    before = proxy.loc[:first].dropna()
    if len(before) >= 2:
        # 以 first 當天的比例把 proxy 縮放到 primary 的價位
        scale = float(primary.loc[first]) / float(before.iloc[-1])
        out.loc[before.index[:-1]] = before.iloc[:-1] * scale
    return out


def build_cash_index(
    calendar: pd.DatetimeIndex,
    boxx: Optional[pd.Series],
    bil: Optional[pd.Series],
    fallback_rate: float,
) -> pd.Series:
    """現金型部位（BOXX）的總報酬指數。

    銜接：BOXX 上市後用 BOXX 日報酬；BOXX 上市前、BIL 上市後用 BIL 日報酬；兩者皆無時
    以 `fallback_rate` 年化利率逐日計息。第一天為 1.0。
    """
    rets = pd.Series(fallback_rate / TRADING_DAYS, index=calendar, dtype=float)
    for series in (bil, boxx):  # 後者覆蓋前者
        if series is None:
            continue
        s = series.astype(float).reindex(calendar)
        r = s.pct_change()
        valid = r.notna() & s.shift(1).notna()
        rets.loc[valid] = r.loc[valid]
    rets.iloc[0] = 0.0
    return (1.0 + rets).cumprod()


# ---------------------------------------------------------------------------
# 大盤狀態與動能
# ---------------------------------------------------------------------------


def classify_regime_raw(close: pd.Series, fast: int, slow: int) -> pd.Series:
    """逐日原始狀態（尚未經連續確認）。均線資料不足的日期為 NaN。"""
    c = close.astype(float)
    sma_f = c.rolling(fast, min_periods=fast).mean()
    sma_s = c.rolling(slow, min_periods=slow).mean()
    cond1 = c > sma_s
    cond2 = sma_f > sma_s
    n = cond1.astype(int) + cond2.astype(int)
    raw = pd.Series(
        np.where(n == 2, "GOOD", np.where(n == 1, "WEAK", "BAD")), index=c.index
    )
    raw[sma_s.isna()] = np.nan
    return raw


def confirm_regime(raw: pd.Series, confirm_days: int) -> pd.Series:
    """狀態切換需原始狀態連續 `confirm_days` 日一致才生效；第一個有效狀態直接採用。"""
    out: list[Optional[str]] = []
    current: Optional[str] = None
    candidate: Optional[str] = None
    streak = 0
    for value in raw.tolist():
        if not isinstance(value, str):
            out.append(current)
            continue
        if current is None:
            current = value
            candidate, streak = None, 0
        elif value == current:
            candidate, streak = None, 0
        else:
            if value == candidate:
                streak += 1
            else:
                candidate, streak = value, 1
            if streak >= max(1, confirm_days):
                current = value
                candidate, streak = None, 0
        out.append(current)
    return pd.Series(out, index=raw.index, dtype=object)


def momentum_scores(close: pd.DataFrame, lookback: int, skip: int) -> pd.DataFrame:
    """P(t−skip) / P(t−lookback) − 1。歷史不足 lookback+1 筆有效收盤（未上市或剛上市）者為 NaN。"""
    c = close.astype(float)
    score = c.shift(skip) / c.shift(lookback) - 1.0
    enough = c.notna().rolling(lookback + 1, min_periods=1).sum() >= lookback + 1
    return score.where(enough)


def rank_top(scores: pd.Series, n: int) -> list[str]:
    """依分數由高到低取前 n 名；NaN 不入選；同分依代號排序以確保決定性。"""
    s = scores.dropna()
    if s.empty or n <= 0:
        return []
    ordered = sorted(s.items(), key=lambda kv: (-kv[1], kv[0]))
    return [sym for sym, _ in ordered[:n]]


# ---------------------------------------------------------------------------
# 模擬
# ---------------------------------------------------------------------------


@dataclass
class TradeRecord:
    day: date
    symbol: str
    notional: float  # 正 = 買進，負 = 賣出
    reason: str


@dataclass
class StopEvent:
    day: date
    symbol: str
    peak: float
    price: float


@dataclass
class SimulationResult:
    nav: pd.Series
    regime: pd.Series  # 每日生效的狀態（決策用，= 前一日收盤確認的狀態）
    trades: list[TradeRecord]
    stops: list[StopEvent]
    holding_periods: list[int]  # 個股持有天數（交易日）
    weights: pd.DataFrame  # 每日收盤後權重（含 CASH）
    params: RegimeMomentumParams = field(default_factory=RegimeMomentumParams)


def _first_trading_day_of_month(index: pd.DatetimeIndex) -> np.ndarray:
    months = np.array([(d.year, d.month) for d in index])
    flags = np.ones(len(index), dtype=bool)
    flags[1:] = (months[1:] != months[:-1]).any(axis=1)
    return flags


def simulate(
    opens: pd.DataFrame,
    closes: pd.DataFrame,
    cash_index: pd.Series,
    market_close: pd.Series,
    start: str,
    end: str,
    params: RegimeMomentumParams = RegimeMomentumParams(),
    initial_capital: float = 100_000.0,
) -> SimulationResult:
    """逐日模擬。`opens`／`closes` 需含選股池、防禦 ETF 與 CORE_SYMBOL（已串接代理）。"""
    idx = closes.index
    raw = classify_regime_raw(
        market_close.reindex(idx), params.sma_fast, params.sma_slow
    )
    confirmed = confirm_regime(raw, params.confirm_days)
    mom_uni = momentum_scores(
        closes[list(params.universe)], params.momentum_lookback, params.momentum_skip
    )
    mom_def = momentum_scores(
        closes[list(params.defensive)], params.momentum_lookback, params.momentum_skip
    )
    month_start = pd.Series(_first_trading_day_of_month(idx), index=idx)

    days = idx[
        (idx >= pd.Timestamp(start, tz=idx.tz)) & (idx <= pd.Timestamp(end, tz=idx.tz))
    ]
    if len(days) < 2:
        raise ValueError("回測期間的交易日不足")
    pos_of = {d: i for i, d in enumerate(idx)}

    shares: dict[str, float] = {}
    cash = float(initial_capital)  # 以 BOXX 指數計息的現金型部位（金額）
    peak_close: dict[str, float] = {}
    entry_pos: dict[str, int] = {}
    pending_stop: set[str] = set()
    last_state: Optional[str] = None

    nav_out: list[float] = []
    state_out: list[Optional[str]] = []
    weight_rows: list[dict[str, float]] = []
    trades: list[TradeRecord] = []
    stops: list[StopEvent] = []
    holding_periods: list[int] = []
    universe_set = set(params.universe)

    def price(frame: pd.DataFrame, sym: str, i: int) -> float:
        v = frame.iat[i, frame.columns.get_loc(sym)]
        if v is None or not np.isfinite(v):
            # 缺價（停牌等）時沿用前一有效收盤
            col = closes[sym].iloc[: i + 1].dropna()
            return float(col.iloc[-1]) if len(col) else float("nan")
        return float(v)

    for k, day in enumerate(days):
        i = pos_of[day]
        # --- 現金部位計息（前一日收盤 → 今日收盤；以指數比值計）
        if k > 0:
            prev = pos_of[days[k - 1]]
            cash *= float(cash_index.iat[i]) / float(cash_index.iat[prev])

        # --- 決策（只用 i−1 收盤為止的資料）
        state = confirmed.iat[i - 1] if i >= 1 else None
        target: Optional[list[str]] = None
        reason = ""
        if isinstance(state, str):
            if state != last_state:
                reason = f"狀態切換 {last_state}→{state}"
            elif k == 0 or bool(month_start.iat[i]):
                reason = "每月調整"
            if reason:
                if state == "GOOD":
                    target = rank_top(mom_uni.iloc[i - 1], params.top_n)
                elif state == "WEAK":
                    target = rank_top(mom_def.iloc[i - 1], params.defensive_top_n) + [
                        CORE_SYMBOL
                    ]
                else:
                    target = []
        # 開盤價計算的淨值（執行價）
        nav_open = cash + sum(q * price(opens, s, i) for s, q in shares.items())

        if target is not None:
            # 調整到目標：等權；可交易標的在今日必須有開盤價
            tradable = [s for s in target if np.isfinite(price(opens, s, i))]
            w = 1.0 / len(tradable) if tradable else 0.0
            desired = {s: w * nav_open for s in tradable}
            # 先賣後買
            for s in sorted(set(shares) | set(desired)):
                px = price(opens, s, i)
                cur_val = shares.get(s, 0.0) * px
                delta = desired.get(s, 0.0) - cur_val
                if abs(delta) < 1e-6:
                    continue
                if delta < 0:
                    cost = abs(delta) * params.cost_rate
                    cash += abs(delta) - cost
                    new_q = shares.get(s, 0.0) + delta / px
                    trades.append(TradeRecord(day.date(), s, delta, reason))
                    if new_q <= 1e-9:
                        shares.pop(s, None)
                        if s in entry_pos:
                            holding_periods.append(i - entry_pos.pop(s))
                        peak_close.pop(s, None)
                    else:
                        shares[s] = new_q
            for s in sorted(desired):
                px = price(opens, s, i)
                cur_val = shares.get(s, 0.0) * px
                delta = desired[s] - cur_val
                if delta <= 1e-6:
                    continue
                cost = delta * params.cost_rate
                spend = min(delta, max(cash - cost, 0.0))
                if spend <= 0:
                    continue
                cash -= spend + spend * params.cost_rate
                if s not in shares:
                    entry_pos[s] = i
                    peak_close[s] = float("nan")
                shares[s] = shares.get(s, 0.0) + spend / px
                trades.append(TradeRecord(day.date(), s, spend, reason))
            pending_stop.clear()
            last_state = state

        # --- 前一日收盤觸發的回落出場（今日開盤執行；若今日已調整則已處理）
        for s in sorted(pending_stop):
            if s not in shares:
                continue
            px = price(opens, s, i)
            value = shares.pop(s) * px
            cash += value - value * params.cost_rate
            trades.append(TradeRecord(day.date(), s, -value, "回落停損"))
            if s in entry_pos:
                holding_periods.append(i - entry_pos.pop(s))
            peak_close.pop(s, None)
        pending_stop.clear()

        # --- 收盤計價、更新持有期間高點、檢查回落
        values: dict[str, float] = {}
        for s, q in shares.items():
            c = price(closes, s, i)
            values[s] = q * c
            applies = (not params.stop_applies_to_universe_only) or s in universe_set
            if not applies:
                continue
            pk = peak_close.get(s, float("nan"))
            peak_close[s] = c if not math.isfinite(pk) else max(pk, c)
            if c <= peak_close[s] * (1.0 - params.trailing_stop):
                pending_stop.add(s)
                stops.append(StopEvent(day.date(), s, peak_close[s], c))
        nav = cash + sum(values.values())
        nav_out.append(nav)
        state_out.append(state if isinstance(state, str) else None)
        row = {s: v / nav for s, v in values.items()} if nav > 0 else {}
        row["CASH"] = cash / nav if nav > 0 else 1.0
        weight_rows.append(row)

    nav_series = pd.Series(nav_out, index=days, name="nav")
    return SimulationResult(
        nav=nav_series,
        regime=pd.Series(state_out, index=days, dtype=object),
        trades=trades,
        stops=stops,
        holding_periods=holding_periods,
        weights=pd.DataFrame(weight_rows, index=days).fillna(0.0),
        params=params,
    )


# ---------------------------------------------------------------------------
# 對照組
# ---------------------------------------------------------------------------


def buy_and_hold(
    close: pd.Series, start: str, end: str, initial: float = 100_000.0
) -> pd.Series:
    """單一標的買進持有（已串接代理的價格序列）。"""
    c = close.astype(float)
    tz = c.index.tz
    c = c[
        (c.index >= pd.Timestamp(start, tz=tz)) & (c.index <= pd.Timestamp(end, tz=tz))
    ]
    c = c.ffill()
    return initial * c / float(c.dropna().iloc[0])


def equal_weight_pit(
    opens: pd.DataFrame,
    closes: pd.DataFrame,
    universe: tuple[str, ...],
    start: str,
    end: str,
    cost_rate: float,
    initial: float = 100_000.0,
) -> pd.Series:
    """等權持有選股池（point-in-time）：每年第一個交易日開盤再平衡，只納入當時已上市
    （前一日有收盤價）的標的；上市後要等到下一次年度再平衡才加入。"""
    idx = closes.index
    tz = idx.tz
    days = idx[(idx >= pd.Timestamp(start, tz=tz)) & (idx <= pd.Timestamp(end, tz=tz))]
    pos_of = {d: i for i, d in enumerate(idx)}
    shares: dict[str, float] = {}
    cash = float(initial)
    out: list[float] = []
    last_year: Optional[int] = None
    for day in days:
        i = pos_of[day]
        if last_year != day.year:
            members = [
                s
                for s in universe
                if i >= 1
                and np.isfinite(closes[s].iat[i - 1])
                and np.isfinite(opens[s].iat[i])
            ]
            nav_open = cash + sum(q * float(opens[s].iat[i]) for s, q in shares.items())
            w = 1.0 / len(members) if members else 0.0
            turnover = 0.0
            new_shares: dict[str, float] = {}
            for s in members:
                px = float(opens[s].iat[i])
                new_shares[s] = w * nav_open / px
            for s in set(shares) | set(new_shares):
                px = float(opens[s].iat[i])
                turnover += abs(new_shares.get(s, 0.0) - shares.get(s, 0.0)) * px
            cost = turnover * cost_rate
            # 成本自淨值扣除，按比例縮減新部位
            scale = (nav_open - cost) / nav_open if nav_open > 0 else 1.0
            shares = {s: q * scale for s, q in new_shares.items()}
            cash = 0.0 if members else nav_open - cost
            last_year = day.year
        nav = cash
        for s, q in shares.items():
            c = closes[s].iat[i]
            if not np.isfinite(c):
                c = closes[s].iloc[: i + 1].dropna().iloc[-1]
            nav += q * float(c)
        out.append(nav)
    return pd.Series(out, index=days, name="ew_pit")


# ---------------------------------------------------------------------------
# 指標
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Metrics:
    total_return: float
    cagr: float
    sortino: float
    downside_dev: float
    max_drawdown: float
    var_95: float
    cvar_95: float
    sharpe: float  # 描述性，不作判讀


def compute_metrics(nav: pd.Series, mar_annual: float) -> Metrics:
    nav = nav.dropna()
    rets = nav.pct_change().dropna().to_numpy()
    years = max(len(nav) / TRADING_DAYS, 1e-9)
    total = float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    cagr = float((nav.iloc[-1] / nav.iloc[0]) ** (1.0 / years) - 1.0)
    tail = historical_var_cvar(rets)
    return Metrics(
        total_return=total,
        cagr=cagr,
        sortino=sortino_ratio(rets, mar_annual, annual_return=cagr),
        downside_dev=annualized_downside_deviation(rets, mar_annual),
        max_drawdown=max_drawdown(nav.to_numpy()).max_drawdown,
        var_95=tail.var if tail else float("nan"),
        cvar_95=tail.cvar if tail else float("nan"),
        sharpe=sharpe_ratio(rets, mar_annual, annual_return=cagr),
    )


def scaled_benchmark(
    strategy_nav: pd.Series,
    bench_nav: pd.Series,
    cash_index: pd.Series,
    mar_annual: float,
) -> tuple[float, pd.Series]:
    """減碼 B&H：w × 對照組 + (1−w) × BOXX 現金，逐日再平衡；w 以下行差對齊
    （w = DD_策略 / DD_對照，夾在 [0, 1]）。回傳 (w, 淨值序列)。"""
    s_ret = strategy_nav.pct_change().dropna()
    b_ret = bench_nav.reindex(strategy_nav.index).pct_change().dropna()
    c_ret = cash_index.reindex(strategy_nav.index).pct_change().dropna()
    dd_s = annualized_downside_deviation(s_ret.to_numpy(), mar_annual)
    dd_b = annualized_downside_deviation(b_ret.to_numpy(), mar_annual)
    w = min(1.0, max(0.0, dd_s / dd_b)) if dd_b > 0 else 0.0
    mixed = w * b_ret + (1.0 - w) * c_ret.reindex(b_ret.index).fillna(0.0)
    nav = float(strategy_nav.iloc[0]) * (1.0 + mixed).cumprod()
    nav = pd.concat(
        [pd.Series([float(strategy_nav.iloc[0])], index=[strategy_nav.index[0]]), nav]
    )
    return w, nav


def realized_cash_rate(cash_index: pd.Series, start: str, end: str) -> float:
    """期間內現金型部位（BOXX／BIL 代理）的實際年化報酬，作為 Sortino 的 MAR。"""
    tz = cash_index.index.tz
    c = cash_index[
        (cash_index.index >= pd.Timestamp(start, tz=tz))
        & (cash_index.index <= pd.Timestamp(end, tz=tz))
    ]
    years = max(len(c) / TRADING_DAYS, 1e-9)
    return float((c.iloc[-1] / c.iloc[0]) ** (1.0 / years) - 1.0)


def slice_nav(nav: pd.Series, start: str, end: str) -> pd.Series:
    tz = nav.index.tz
    return nav[
        (nav.index >= pd.Timestamp(start, tz=tz))
        & (nav.index <= pd.Timestamp(end, tz=tz))
    ]


def window_drawdown(nav: pd.Series, start: str, end: str) -> float:
    """區間內的最大回撤（以區間內自身高點計）。"""
    s = slice_nav(nav, start, end)
    return max_drawdown(s.to_numpy()).max_drawdown if len(s) else float("nan")


def regime_switch_stats(regime: pd.Series, whipsaw_days: int = 20) -> dict[str, float]:
    """狀態切換次數與 whipsaw（切換後 `whipsaw_days` 個交易日內又切回原狀態）次數。"""
    states = regime.dropna().tolist()
    switches: list[tuple[int, str, str]] = []
    for j in range(1, len(states)):
        if states[j] != states[j - 1]:
            switches.append((j, states[j - 1], states[j]))
    whipsaw = 0
    for a, b in zip(switches, switches[1:]):
        if b[0] - a[0] <= whipsaw_days and b[2] == a[1]:
            whipsaw += 1
    days_in = {s: states.count(s) for s in ("GOOD", "WEAK", "BAD")}
    return {
        "switches": float(len(switches)),
        "whipsaws": float(whipsaw),
        **{f"days_{k.lower()}": float(v) for k, v in days_in.items()},
    }


def annual_turnover(result: SimulationResult) -> float:
    """單邊年換手率 = Σ|成交金額| / 2 / 平均淨值 / 年數。"""
    traded = sum(abs(t.notional) for t in result.trades)
    years = max(len(result.nav) / TRADING_DAYS, 1e-9)
    return traded / 2.0 / float(result.nav.mean()) / years
