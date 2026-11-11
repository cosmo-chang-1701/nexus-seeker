"""「總經量化選擇權顧問」(Macro Quant Options Advisor, MQA) 機械化日線回測。

把顧問的左側逆勢（RSI 超賣分兩批進場，IVP 高時改賣 CSP）、右側趨勢確認（站上 EMA20／SMA50
補到目標部位、連兩日有效跌破才出場）、單腳選擇權（CSP／CC、停利、轉倉、被指派與被買走）、
FOMC 與財報事件封鎖，以及保護性 Put 避險，機械化成可逐日重播的規則。完整規格見
docs/architecture/05_calibration_harness_and_forward_collection.md §5.20。

時序（無前視）：第 D 日收盤的所有訊號只用到 D 日（含）以前的資料，掛單後於 D+1 日開盤
執行；選擇權的停利、轉倉、到期與財報出場則在當日收盤處理，並以當日收盤逐日計價。

本模組是**離線回測**，不修改任何 production 程式碼或參數，也不寫任何資料庫。選擇權以
Black-Scholes（無股息、固定利率）定價，IV 為「已實現波動 + 大盤 VIX 溢價」的代理值，
不是真實的隱含波動率（見報告的限制段落）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Optional

import numpy as np
import pandas as pd

from calibration.regime_momentum_backtest import (
    DEFAULT_UNIVERSE,
    MARKET_SYMBOL,
    TRADING_DAYS,
)

InitialState = Literal["CASH", "INVESTED"]

# 2026 年 FOMC 決議日（美東）。
FOMC_DATES_2026: tuple[date, ...] = (
    date(2026, 1, 28),
    date(2026, 3, 18),
    date(2026, 4, 29),
    date(2026, 6, 17),
    date(2026, 7, 29),
    date(2026, 9, 16),
    date(2026, 10, 28),
    date(2026, 12, 9),
)

_EPS = 1e-9

COUNTER_KEYS: tuple[str, ...] = (
    "tier1",
    "tier2",
    "right_entries",
    "right_topups",
    "right_exits",
    "loss_stops",
    "time_stop_pauses",
    "earnings_exits",
    "fomc_blocked",
    "skipped_left_cap",
    "skipped_no_buying_power",
    "roll_skipped",
    "csp_opened",
    "csp_assigned",
    "csp_tp",
    "cc_opened",
    "cc_called_away",
    "cc_tp",
    "rolls",
    "hedges",
    "shorts",
)


@dataclass(frozen=True)
class AdvisorParams:
    """所有規則參數集中於此；預設值即規格。"""

    initial_state: InitialState = "CASH"
    options_enabled: bool = True
    hedge_enabled: bool = True
    cc_on_right: bool = True
    earnings_exit_spot: bool = True
    spot_cost_rate: float = 0.0015  # 現貨市價成交的單邊成本（指派與被買走不計）
    earnings_exit_days_before: int = 2
    earnings_reentry_days_after: int = 1
    # 左側（逆勢）
    left_tier1_pct: float = 0.15
    left_tier2_pct: float = 0.25
    rsi_entry: float = 30.0
    tier2_drop: float = 0.05
    csp_min_ivp: float = 50.0
    left_symbol_cap: float = 0.50
    left_account_cap: float = 0.40
    time_stop_days: int = 15
    max_loss_nav_pct: float = 0.01
    # 右側（趨勢確認）
    right_topup_threshold: float = 0.90
    right_exit_confirm_days: int = 2
    # 選擇權
    option_rate: float = 0.04
    option_spread: float = 0.03
    option_dte: int = 35
    csp_delta: float = 0.15
    cc_delta: float = 0.25
    option_take_profit: float = 0.50
    roll_dte: int = 21
    # 保護性 Put
    hedge_min_equity_pct: float = 0.50
    hedge_delta: float = 0.25
    hedge_budget_pct: float = 0.004
    universe: tuple[str, ...] = DEFAULT_UNIVERSE


# ---------------------------------------------------------------------------
# Black-Scholes（只用 math.erf，不引入 scipy）
# ---------------------------------------------------------------------------


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1(spot: float, strike: float, t: float, rate: float, sigma: float) -> float:
    return (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / (
        sigma * math.sqrt(t)
    )


def bs_price(
    spot: float, strike: float, t: float, rate: float, sigma: float, kind: str
) -> float:
    """歐式選擇權理論價；`kind` 為 "C" 或 "P"。T≤0 或 σ≤0 時回傳內含價值。"""
    if t <= 0 or sigma <= 0:
        intrinsic = spot - strike if kind == "C" else strike - spot
        return max(intrinsic, 0.0)
    d1 = _d1(spot, strike, t, rate, sigma)
    d2 = d1 - sigma * math.sqrt(t)
    disc = math.exp(-rate * t)
    if kind == "C":
        return spot * norm_cdf(d1) - strike * disc * norm_cdf(d2)
    return strike * disc * norm_cdf(-d2) - spot * norm_cdf(-d1)


def bs_delta(
    spot: float, strike: float, t: float, rate: float, sigma: float, kind: str
) -> float:
    """Call 的 Delta ∈ (0,1)，Put 的 Delta ∈ (−1,0)。"""
    if t <= 0 or sigma <= 0:
        if kind == "C":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    d1 = _d1(spot, strike, t, rate, sigma)
    return norm_cdf(d1) if kind == "C" else norm_cdf(d1) - 1.0


def strike_for_delta(
    spot: float, t: float, rate: float, sigma: float, delta: float, kind: str
) -> float:
    """以二分法解出使選擇權 Delta 等於 `delta` 的連續履約價（不取整）。

    Call 與 Put 的 Delta 對履約價都單調遞減，因此在 [0.01·S, 100·S] 的對數區間內二分
    即可收斂。"""
    if t <= 0 or sigma <= 0 or spot <= 0:
        raise ValueError("t、sigma、spot 必須為正")
    lo, hi = math.log(spot * 0.01), math.log(spot * 100.0)
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        k = math.exp(mid)
        d = bs_delta(spot, k, t, rate, sigma, kind)
        if d > delta:
            lo = mid
        else:
            hi = mid
    return math.exp(0.5 * (lo + hi))


# ---------------------------------------------------------------------------
# 指標（只用收盤價，因果）
# ---------------------------------------------------------------------------


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI：前 `period` 個漲跌幅的簡單平均作種子，之後以 (n−1)/n 平滑。"""
    values = close.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    finite = np.nonzero(np.isfinite(values))[0]
    if len(finite) <= period:
        return pd.Series(out, index=close.index)
    first = int(finite[0])
    seg = values[first:]
    delta = np.diff(seg)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())

    def _rsi(g: float, loss: float) -> float:
        if loss <= 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / loss)

    out[first + period] = _rsi(avg_gain, avg_loss)
    for j in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gains[j]) / period
        avg_loss = (avg_loss * (period - 1) + losses[j]) / period
        out[first + j + 1] = _rsi(avg_gain, avg_loss)
    return pd.Series(out, index=close.index)


def ema(close: pd.Series, span: int = 20) -> pd.Series:
    return close.ewm(span=span, adjust=False, min_periods=span).mean()


def sma(close: pd.Series, window: int = 50) -> pd.Series:
    return close.rolling(window, min_periods=window).mean()


def realized_vol(close: pd.Series, window: int = 20) -> pd.Series:
    """日對數報酬的 `window` 日標準差（ddof=1）年化。"""
    rets = np.log(close.astype(float)).diff()
    return rets.rolling(window, min_periods=window).std() * math.sqrt(TRADING_DAYS)


def iv_proxy(rv: pd.Series, vix: pd.Series, rv_market: pd.Series) -> pd.Series:
    """IV 代理 = max(RV20 + max(VIX/100 − RV20_SPY, 0), 0.05)。"""
    premium = (vix / 100.0 - rv_market).clip(lower=0.0)
    return (rv + premium).clip(lower=0.05)


def iv_percentile(iv: pd.Series, window: int = 252, min_obs: int = 126) -> pd.Series:
    """今日 iv 在過去 `window` 個交易日（含今日）的百分位（0–100，≤ 今日者的比例）；
    觀測數不足 `min_obs` 時為 NaN。"""
    arr = iv.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    for t in range(len(arr)):
        if not np.isfinite(arr[t]):
            continue
        win = arr[max(0, t - window + 1) : t + 1]
        win = win[np.isfinite(win)]
        if len(win) < min_obs:
            continue
        out[t] = float((win <= arr[t]).mean() * 100.0)
    return pd.Series(out, index=iv.index)


# ---------------------------------------------------------------------------
# 模擬
# ---------------------------------------------------------------------------


@dataclass
class _Opt:
    oid: int
    sym: str
    kind: str  # "P" | "C"
    role: str  # "csp" | "cc" | "hedge"
    strike: float
    qty: float  # 股數
    expiry: pd.Timestamp
    premium_ps: float  # 每股收取（空方，已扣價差）或支付（避險，已含價差）的權利金


@dataclass
class _SymState:
    spot: float = 0.0
    left_shares: float = 0.0
    left_cost: float = 0.0
    confirmed: bool = False
    tier1_done: bool = False
    tier2_done: bool = False
    paused: bool = False
    tier1_ref: float = float("nan")
    tier1_pos: int = -1
    below: int = 0

    def reset(self, keep_spot_status: bool = False) -> None:
        self.tier1_done = False
        self.tier2_done = False
        self.paused = False
        self.tier1_ref = float("nan")
        self.tier1_pos = -1
        self.below = 0
        if not keep_spot_status:
            self.confirmed = False
            self.left_shares = 0.0
            self.left_cost = 0.0


@dataclass
class AdvisorResult:
    nav: pd.Series
    equity_exposure: pd.Series  # 每日股票市值 / 淨值
    left_exposure: pd.Series  # 每日左側曝險（成本 + CSP 擔保）/ 淨值
    option_pnl: float
    option_pnl_parts: dict[str, float]
    spot_costs: float
    counters: dict[str, int]
    min_cash: float
    trades: list[dict[str, Any]] = field(default_factory=list)


_PRIORITY = {
    "right_exit": 0,
    "loss_stop": 1,
    "hedge": 2,
    "right_entry": 3,
    "tier1": 4,
    "tier2": 5,
    "cc": 6,
}


def earnings_positions(
    index: pd.DatetimeIndex,
    dates: Sequence[date],
    days_before: int,
    days_after: int,
) -> list[tuple[int, int, int]]:
    """把財報日轉成 (出場日位置, 財報日位置, 封鎖截止位置)；位置為 `index` 的整數位置。

    出場＝財報日前 `days_before` 個交易日的收盤；封鎖區間為 [出場日, 財報日] 的收盤
    （區間內不得新增訊號），財報日後 `days_after` 個交易日的收盤起才可再產生訊號。
    財報日落在資料之後時以工作日數外推。"""
    out: list[tuple[int, int, int]] = []
    last = index[-1]
    for d in dates:
        ts = pd.Timestamp(d)
        if index.tz is not None:
            ts = ts.tz_localize(index.tz)
        if ts <= last:
            epos = int(index.searchsorted(ts, side="left"))
        else:
            epos = len(index) - 1 + int(np.busday_count(last.date(), d))
        out.append((epos - days_before, epos, epos + days_after - 1))
    return out


class _Sim:
    def __init__(
        self,
        opens: pd.DataFrame,
        closes: pd.DataFrame,
        cash_index: pd.Series,
        vix: pd.Series,
        start: str,
        end: str,
        params: AdvisorParams,
        earnings: dict[str, list[date]],
        fomc_dates: Sequence[date],
        capital: float,
    ) -> None:
        if MARKET_SYMBOL not in closes.columns:
            raise ValueError(f"缺少大盤標的 {MARKET_SYMBOL}")
        self.p = params
        self.index = closes.index
        idx = self.index
        tz = idx.tz
        mask = (idx >= pd.Timestamp(start, tz=tz)) & (idx <= pd.Timestamp(end, tz=tz))
        self.days = idx[mask]
        if len(self.days) < 2:
            raise ValueError("回測期間的交易日不足")
        self.i0 = int(idx.get_loc(self.days[0]))
        if self.i0 < 1:
            raise ValueError("起始日前需至少一個交易日的資料")
        i0 = self.i0
        self.syms = [
            s
            for s in params.universe
            if s in closes.columns
            and np.isfinite(closes[s].iat[i0 - 1])
            and s in opens.columns
            and np.isfinite(opens[s].iat[i0])
        ]
        self.n_members = len(self.syms)
        self.col = {s: j for j, s in enumerate(self.syms)}
        cl = closes.ffill()
        self.C = closes[self.syms].to_numpy(dtype=float)
        self.Cf = cl[self.syms].to_numpy(dtype=float)
        self.O = opens[self.syms].to_numpy(dtype=float)
        self.spy_c = cl[MARKET_SYMBOL].to_numpy(dtype=float)
        self.spy_o = opens[MARKET_SYMBOL].to_numpy(dtype=float)
        spy_close = cl[MARKET_SYMBOL]
        self.spy_sma = sma(spy_close, 50).to_numpy(dtype=float)
        vix_a = vix.reindex(idx).ffill()
        rv_spy = realized_vol(spy_close, 20)
        self.vix_iv = (vix_a / 100.0).to_numpy(dtype=float)
        self.rsi: dict[str, np.ndarray] = {}
        self.ema20: dict[str, np.ndarray] = {}
        self.sma50: dict[str, np.ndarray] = {}
        self.iv: dict[str, np.ndarray] = {}
        self.ivf: dict[str, np.ndarray] = {}
        self.ivp: dict[str, np.ndarray] = {}
        for s in self.syms:
            c = cl[s]
            self.rsi[s] = rsi_wilder(c, 14).to_numpy(dtype=float)
            self.ema20[s] = ema(c, 20).to_numpy(dtype=float)
            self.sma50[s] = sma(c, 50).to_numpy(dtype=float)
            iv_s = iv_proxy(realized_vol(c, 20), vix_a, rv_spy)
            self.iv[s] = iv_s.to_numpy(dtype=float)
            self.ivf[s] = iv_s.ffill().to_numpy(dtype=float)
            self.ivp[s] = iv_percentile(iv_s).to_numpy(dtype=float)
        self.cash_idx = cash_index.reindex(idx).ffill().to_numpy(dtype=float)
        self.fomc = {pd.Timestamp(d) for d in fomc_dates}
        self.earn_exit: dict[str, dict[int, int]] = {}
        self.earn_block: dict[str, list[tuple[int, int]]] = {}
        for s in self.syms:
            ex: dict[int, int] = {}
            blocks: list[tuple[int, int]] = []
            for exit_pos, _epos, reentry in earnings_positions(
                idx,
                earnings.get(s, []),
                params.earnings_exit_days_before,
                params.earnings_reentry_days_after,
            ):
                ex[exit_pos] = reentry
                blocks.append((exit_pos - 1, reentry))
            self.earn_exit[s] = ex
            self.earn_block[s] = blocks

        self.cash = float(capital)
        self.capital = float(capital)
        self.st = {s: _SymState() for s in self.syms}
        self.opts: dict[int, _Opt] = {}
        self._oid = 0
        self.counters = {k: 0 for k in COUNTER_KEYS}
        self.pnl = {
            "premium": 0.0,
            "buyback": 0.0,
            "settle": 0.0,
            "hedge": 0.0,
            "open_mtm": 0.0,
        }
        self.spot_costs = 0.0
        self._roll_skipped: set[int] = set()
        self.trades: list[dict[str, Any]] = []
        self.min_cash = float(capital)
        self._i = self.i0

    # -- 基本工具 ----------------------------------------------------------

    def _log(
        self, day: pd.Timestamp, phase: str, kind: str, sym: str, **kw: Any
    ) -> None:
        self.trades.append(
            {"day": day, "phase": phase, "kind": kind, "symbol": sym, **kw}
        )

    def _blocked(self, sym: str, i: int) -> bool:
        return any(a <= i <= b for a, b in self.earn_block[sym])

    def _locked(self) -> float:
        return sum(o.strike * o.qty for o in self.opts.values() if o.role == "csp")

    def _free_cash(self) -> float:
        return self.cash - self._locked()

    def _csp_collateral(self, sym: str) -> float:
        return sum(
            o.strike * o.qty
            for o in self.opts.values()
            if o.role == "csp" and o.sym == sym
        )

    def _sym_opts(self, sym: str, role: Optional[str] = None) -> list[_Opt]:
        return [
            o
            for o in self.opts.values()
            if o.sym == sym and (role is None or o.role == role)
        ]

    def _expiry_for(self, day: pd.Timestamp) -> pd.Timestamp:
        target = day + pd.Timedelta(days=self.p.option_dte)
        if target > self.index[-1]:
            return target
        pos = int(self.index.searchsorted(target, side="right")) - 1
        return pd.Timestamp(self.index[pos])

    def _t(self, expiry: pd.Timestamp, day: pd.Timestamp) -> float:
        return float(max((expiry - day).days, 0)) / 365.0

    def _model(self, o: _Opt, spot: float, iv: float, day: pd.Timestamp) -> float:
        return bs_price(
            spot, o.strike, self._t(o.expiry, day), self.p.option_rate, iv, o.kind
        )

    def _has_position(self, sym: str) -> bool:
        return self.st[sym].spot > _EPS or bool(self._sym_opts(sym))

    def _left_exposure(self) -> float:
        return sum(self.st[s].left_cost + self._csp_collateral(s) for s in self.syms)

    def _remove_spot(self, sym: str, qty: float) -> None:
        st = self.st[sym]
        take = min(st.left_shares, qty)
        if st.left_shares > _EPS and take > 0:
            st.left_cost -= st.left_cost * take / st.left_shares
            st.left_shares -= take
        if st.left_shares <= _EPS:
            st.left_shares = 0.0
            st.left_cost = 0.0
        st.spot -= qty
        if st.spot < -_EPS:
            self.counters["shorts"] += 1
        if st.spot <= _EPS:
            st.spot = 0.0

    def _charge(self, notional: float) -> None:
        """現貨市價成交的單邊成本，自現金扣除並累計。"""
        c = notional * self.p.spot_cost_rate
        self.cash -= c
        self.spot_costs += c

    def _sell_spot_all(
        self, day: pd.Timestamp, phase: str, sym: str, px: float
    ) -> None:
        st = self.st[sym]
        if st.spot <= _EPS:
            return
        self.cash += st.spot * px
        self._charge(st.spot * px)
        self._log(day, phase, "sell_spot", sym, qty=st.spot, px=px)
        st.spot = 0.0
        st.left_shares = 0.0
        st.left_cost = 0.0

    def _open_option(
        self,
        day: pd.Timestamp,
        phase: str,
        sym: str,
        kind: str,
        role: str,
        strike: float,
        qty: float,
        expiry: pd.Timestamp,
        price: float,
    ) -> None:
        self._oid += 1
        sold = price * (1.0 - self.p.option_spread)
        self.opts[self._oid] = _Opt(
            self._oid, sym, kind, role, strike, qty, expiry, sold
        )
        self.cash += sold * qty
        self.pnl["premium"] += sold * qty
        self.counters["csp_opened" if role == "csp" else "cc_opened"] += 1
        self._log(day, phase, f"open_{role}", sym, qty=qty, strike=strike, px=sold)

    def _close_option(
        self,
        day: pd.Timestamp,
        phase: str,
        o: _Opt,
        model: float,
        forced: bool,
        px: float,
    ) -> bool:
        """以模型價 ×(1+價差) 買回空方選擇權。非強制時現金不足即不執行（回傳 False）；
        強制時不足的部分以該標的現貨賣出補足。"""
        cost = model * (1.0 + self.p.option_spread) * o.qty
        release = o.strike * o.qty if o.role == "csp" else 0.0
        locked_after = self._locked() - release
        shortfall = cost - (self.cash - locked_after)
        if shortfall > _EPS:
            if not forced:
                return False
            st = self.st[o.sym]
            net = px * (1.0 - self.p.spot_cost_rate)
            sell = min(st.spot, shortfall / net) if net > 0 else 0.0
            if sell > 0:
                self.cash += sell * px
                self._charge(sell * px)
                self._remove_spot(o.sym, sell)
                self._log(day, phase, "sell_spot_cover", o.sym, qty=sell, px=px)
        self.cash -= cost
        self.pnl["buyback"] -= cost
        del self.opts[o.oid]
        self._log(day, phase, f"buyback_{o.role}", o.sym, qty=o.qty, strike=o.strike)
        return True

    # -- 主迴圈 ------------------------------------------------------------

    def run(self) -> AdvisorResult:
        p = self.p
        pending: list[tuple[str, str, float, int]] = []
        nav_prev = self.capital
        navs: list[float] = []
        eq_exp: list[float] = []
        left_exp: list[float] = []
        for n, day in enumerate(self.days):
            i = self.i0 + n
            self._i = i
            if n == 0 and p.initial_state == "INVESTED":
                self._initial_invest(day, i)
            else:
                self._execute_open(day, i, nav_prev, pending)
            # 現金（含 CSP 擔保）依 BOXX 指數由前一日收盤計息到今日收盤
            if n > 0:
                self.cash *= float(self.cash_idx[i] / self.cash_idx[i - 1])
            self._close_phase(day, i)
            nav = self._nav(day, i)
            navs.append(nav)
            spot_val = sum(self.st[s].spot * self.Cf[i, self.col[s]] for s in self.syms)
            eq_exp.append(spot_val / nav if nav > 0 else 0.0)
            left_exp.append(self._left_exposure() / nav if nav > 0 else 0.0)
            self._check_shorts()
            self.min_cash = min(self.min_cash, self.cash)
            pending = self._signals(day, i, nav)
            nav_prev = nav
        names = pd.Index(self.days)
        self.pnl["open_mtm"] = self._open_option_value(
            self.days[-1], self.i0 + len(self.days) - 1
        )
        return AdvisorResult(
            nav=pd.Series(navs, index=names, name="nav"),
            equity_exposure=pd.Series(eq_exp, index=names),
            left_exposure=pd.Series(left_exp, index=names),
            option_pnl=sum(self.pnl.values()),
            option_pnl_parts=dict(self.pnl),
            spot_costs=self.spot_costs,
            counters=dict(self.counters),
            min_cash=self.min_cash,
            trades=self.trades,
        )

    def _check_shorts(self) -> None:
        for s in self.syms:
            covered = sum(o.qty for o in self._sym_opts(s, "cc"))
            if self.st[s].spot < -_EPS or covered > self.st[s].spot + 1e-6:
                self.counters["shorts"] += 1

    def _nav(self, day: pd.Timestamp, i: int) -> float:
        total = self.cash
        for s in self.syms:
            total += self.st[s].spot * self.Cf[i, self.col[s]]
        return float(total + self._open_option_value(day, i))

    def _open_option_value(self, day: pd.Timestamp, i: int) -> float:
        """未平倉選擇權的淨市值＝避險 Put 模型價值 − 空方選擇權模型價值（與 `_nav` 同定價）。"""
        total = 0.0
        for o in self.opts.values():
            if o.role == "hedge":
                spot = float(self.spy_c[i])
                iv = float(self.vix_iv[i])
            else:
                spot = float(self.Cf[i, self.col[o.sym]])
                iv = float(self.ivf[o.sym][i])
            if not np.isfinite(iv):
                iv = 0.3
            val = self._model(o, spot, iv, day) * o.qty
            total += val if o.role == "hedge" else -val
        return total

    # -- 開盤執行 ----------------------------------------------------------

    def _initial_invest(self, day: pd.Timestamp, i: int) -> None:
        per = (
            self.capital / self.n_members / (1.0 + self.p.spot_cost_rate)
            if self.n_members
            else 0.0
        )
        for s in self.syms:
            px = float(self.O[i, self.col[s]])
            qty = per / px
            self.st[s].spot = qty
            self.st[s].confirmed = True
            self.cash -= per
            self._charge(per)
            self._log(day, "open", "initial_buy", s, qty=qty, px=px)

    def _execute_open(
        self,
        day: pd.Timestamp,
        i: int,
        nav_prev: float,
        pending: list[tuple[str, str, float, int]],
    ) -> None:
        is_fomc = pd.Timestamp(day).normalize() in self.fomc
        budget_b = nav_prev / self.n_members if self.n_members else 0.0
        order = {s: k for k, s in enumerate(self.syms)}
        ordered = sorted(
            pending,
            key=lambda x: (_PRIORITY[x[0]], order.get(x[1], -1)),
        )
        for kind, sym, ref, sig_pos in ordered:
            if kind == "hedge":
                self._open_hedge(day, i, nav_prev)
                continue
            j = self.col[sym]
            px = float(self.O[i, j])
            if not np.isfinite(px):
                continue
            st = self.st[sym]
            iv_m = float(self.ivf[sym][i - 1])
            if kind == "right_exit":
                if not st.confirmed:
                    continue
                self._sell_spot_all(day, "open", sym, px)
                for o in self._sym_opts(sym, "cc"):
                    self._close_option(
                        day,
                        "open",
                        o,
                        self._model(o, px, iv_m, day),
                        forced=True,
                        px=px,
                    )
                self.counters["right_exits"] += 1
                st.reset()
            elif kind == "loss_stop":
                if st.left_shares <= _EPS:
                    continue
                qty = st.left_shares
                self.cash += qty * px
                self._charge(qty * px)
                self._log(day, "open", "sell_spot", sym, qty=qty, px=px)
                self._remove_spot(sym, qty)
                for o in self._sym_opts(sym, "cc"):
                    self._close_option(
                        day,
                        "open",
                        o,
                        self._model(o, px, iv_m, day),
                        forced=True,
                        px=px,
                    )
                self.counters["loss_stops"] += 1
                st.reset(keep_spot_status=st.spot > _EPS)
            elif kind == "right_entry":
                self._right_entry(day, i, sym, px, budget_b, is_fomc)
            elif kind in ("tier1", "tier2"):
                self._left_entry(
                    day, i, sym, px, kind, ref, sig_pos, budget_b, nav_prev, is_fomc
                )
            elif kind == "cc":
                self._write_cc(day, i, sym, px, is_fomc)

    def _right_entry(
        self,
        day: pd.Timestamp,
        i: int,
        sym: str,
        px: float,
        budget_b: float,
        is_fomc: bool,
    ) -> None:
        st = self.st[sym]
        target = budget_b - self._csp_collateral(sym)
        if target <= 0:
            return
        spot_val = st.spot * px
        if spot_val < self.p.right_topup_threshold * target:
            need = target - spot_val
            if is_fomc:
                self.counters["fomc_blocked"] += 1
                return
            if self._free_cash() + _EPS < need:
                self.counters["skipped_no_buying_power"] += 1
                return
            # 成本須由同一筆現金支付：目標吃滿現金時，買進金額縮減為「現金 / (1+成本率)」
            need = min(need, self._free_cash() / (1.0 + self.p.spot_cost_rate))
            self.cash -= need
            self._charge(need)
            st.spot += need / px
            self._log(day, "open", "buy_spot_right", sym, qty=need / px, px=px)
            self.counters["right_topups" if st.confirmed else "right_entries"] += 1
        elif st.spot > _EPS and not st.confirmed:
            self.counters["right_entries"] += 1
        else:
            return
        st.confirmed = True
        st.left_shares = 0.0
        st.left_cost = 0.0

    def _left_entry(
        self,
        day: pd.Timestamp,
        i: int,
        sym: str,
        px: float,
        kind: str,
        ref: float,
        sig_pos: int,
        budget_b: float,
        nav_prev: float,
        is_fomc: bool,
    ) -> None:
        p = self.p
        st = self.st[sym]
        if kind == "tier1":
            if st.tier1_done or self._has_position(sym):
                return
            pct = p.left_tier1_pct
        else:
            if (not st.tier1_done) or st.tier2_done or st.paused or st.confirmed:
                return
            pct = p.left_tier2_pct
        notional = pct * budget_b
        if is_fomc:
            self.counters["fomc_blocked"] += 1
            return
        sym_exp = st.left_cost + self._csp_collateral(sym)
        if (
            sym_exp + notional > p.left_symbol_cap * budget_b + _EPS
            or self._left_exposure() + notional > p.left_account_cap * nav_prev + _EPS
        ):
            self.counters["skipped_left_cap"] += 1
            return
        if self._free_cash() + _EPS < notional:
            self.counters["skipped_no_buying_power"] += 1
            return
        iv = float(self.iv[sym][i - 1])
        ivp = float(self.ivp[sym][i - 1])
        use_csp = (
            p.options_enabled
            and np.isfinite(iv)
            and np.isfinite(ivp)
            and ivp >= p.csp_min_ivp
        )
        if use_csp:
            expiry = self._expiry_for(day)
            t = self._t(expiry, day)
            k = strike_for_delta(px, t, p.option_rate, iv, -p.csp_delta, "P")
            qty = notional / k
            price = bs_price(px, k, t, p.option_rate, iv, "P")
            self._open_option(day, "open", sym, "P", "csp", k, qty, expiry, price)
        else:
            notional = min(notional, self._free_cash() / (1.0 + p.spot_cost_rate))
            self.cash -= notional
            self._charge(notional)
            st.spot += notional / px
            st.left_shares += notional / px
            st.left_cost += notional
            self._log(day, "open", "buy_spot_left", sym, qty=notional / px, px=px)
        if kind == "tier1":
            st.tier1_done = True
            st.tier1_ref = ref
            st.tier1_pos = sig_pos
            self.counters["tier1"] += 1
        else:
            st.tier2_done = True
            self.counters["tier2"] += 1
        self._log(
            day,
            "open",
            "left_exposure",
            sym,
            exposure=self._left_exposure(),
            nav_ref=nav_prev,
        )

    def _write_cc(
        self, day: pd.Timestamp, i: int, sym: str, px: float, is_fomc: bool
    ) -> None:
        p = self.p
        st = self.st[sym]
        if not (p.options_enabled and p.cc_on_right and st.confirmed):
            return
        uncovered = st.spot - sum(o.qty for o in self._sym_opts(sym, "cc"))
        if uncovered <= _EPS:
            return
        iv = float(self.iv[sym][i - 1])
        if not np.isfinite(iv):
            return
        if is_fomc:
            self.counters["fomc_blocked"] += 1
            return
        expiry = self._expiry_for(day)
        t = self._t(expiry, day)
        k = strike_for_delta(px, t, p.option_rate, iv, p.cc_delta, "C")
        price = bs_price(px, k, t, p.option_rate, iv, "C")
        self._open_option(day, "open", sym, "C", "cc", k, uncovered, expiry, price)

    def _open_hedge(self, day: pd.Timestamp, i: int, nav_prev: float) -> None:
        p = self.p
        if any(o.role == "hedge" for o in self.opts.values()):
            return
        iv = float(self.vix_iv[i - 1])
        px = float(self.spy_o[i])
        if not (np.isfinite(iv) and np.isfinite(px) and iv > 0):
            return
        expiry = self._expiry_for(day)
        t = self._t(expiry, day)
        k = strike_for_delta(px, t, p.option_rate, iv, -p.hedge_delta, "P")
        price = bs_price(px, k, t, p.option_rate, iv, "P") * (1.0 + p.option_spread)
        budget = p.hedge_budget_pct * nav_prev
        if price <= 0:
            return
        if self._free_cash() + _EPS < budget:
            self.counters["skipped_no_buying_power"] += 1
            return
        qty = budget / price
        self._oid += 1
        self.opts[self._oid] = _Opt(
            self._oid, MARKET_SYMBOL, "P", "hedge", k, qty, expiry, price
        )
        self.cash -= budget
        self.pnl["hedge"] -= budget
        self.counters["hedges"] += 1
        self._log(day, "open", "open_hedge", MARKET_SYMBOL, qty=qty, strike=k, px=price)

    # -- 收盤處理 ----------------------------------------------------------

    def _close_phase(self, day: pd.Timestamp, i: int) -> None:
        p = self.p
        # 1. 到期結算
        for o in sorted(self.opts.values(), key=lambda x: x.oid):
            if o.expiry <= day:
                self._settle(day, i, o)
        # 2. 財報出場（收盤）
        for s in self.syms:
            if i in self.earn_exit[s]:
                self._earnings_exit(day, i, s)
        # 3. 停利與轉倉
        for o in sorted(self.opts.values(), key=lambda x: x.oid):
            if o.role == "hedge" or o.oid not in self.opts:
                continue
            spot = float(self.Cf[i, self.col[o.sym]])
            iv = float(self.ivf[o.sym][i])
            if not np.isfinite(iv):
                continue
            model = self._model(o, spot, iv, day)
            if model <= (1.0 - p.option_take_profit) * o.premium_ps:
                if self._close_option(day, "close", o, model, forced=False, px=spot):
                    self.counters["csp_tp" if o.role == "csp" else "cc_tp"] += 1
                continue
            dte = (o.expiry - day).days
            tested = spot < o.strike if o.role == "csp" else spot > o.strike
            if dte <= p.roll_dte and tested and not self._blocked(o.sym, i):
                self._roll(day, i, o, spot, iv, model)
        # 4. 重置、時間停損
        for s in self.syms:
            st = self.st[s]
            if (st.tier1_done or st.confirmed or st.paused) and not self._has_position(
                s
            ):
                st.reset()
            c = self.Cf[i, self.col[s]]
            if (
                st.tier1_done
                and not st.tier2_done
                and not st.paused
                and st.tier1_pos >= 0
                and i - st.tier1_pos >= p.time_stop_days
                and c < st.tier1_ref
            ):
                st.paused = True
                self.counters["time_stop_pauses"] += 1

    def _settle(self, day: pd.Timestamp, i: int, o: _Opt) -> None:
        del self.opts[o.oid]
        if o.role == "hedge":
            spot = float(self.spy_c[i])
            val = max(o.strike - spot, 0.0) * o.qty
            self.cash += val
            self.pnl["hedge"] += val
            self._log(day, "close", "settle_hedge", o.sym, qty=o.qty, value=val)
            return
        j = self.col[o.sym]
        spot = float(self.Cf[i, j])
        st = self.st[o.sym]
        if o.role == "csp":
            if spot < o.strike:
                self.cash -= o.strike * o.qty
                self.pnl["settle"] -= (o.strike - spot) * o.qty
                st.spot += o.qty
                if not st.confirmed:
                    st.left_shares += o.qty
                    st.left_cost += o.strike * o.qty
                self.counters["csp_assigned"] += 1
                self._log(
                    day, "close", "csp_assigned", o.sym, qty=o.qty, strike=o.strike
                )
                self._cover_assigned(day, i, o.sym, o.qty, spot)
            else:
                self._log(day, "close", "csp_expired", o.sym, qty=o.qty)
        else:
            if spot > o.strike:
                qty = min(o.qty, st.spot)
                if o.qty > st.spot + 1e-6:
                    self.counters["shorts"] += 1
                self.cash += o.strike * qty
                self.pnl["settle"] -= (spot - o.strike) * o.qty
                self._remove_spot(o.sym, qty)
                self.counters["cc_called_away"] += 1
                self._log(
                    day, "close", "cc_called_away", o.sym, qty=qty, strike=o.strike
                )
            else:
                self._log(day, "close", "cc_expired", o.sym, qty=o.qty)

    def _cover_assigned(
        self, day: pd.Timestamp, i: int, sym: str, qty: float, spot: float
    ) -> None:
        p = self.p
        iv = float(self.ivf[sym][i])
        if not (p.options_enabled and np.isfinite(iv)) or self._blocked(sym, i):
            return
        expiry = self._expiry_for(day)
        t = self._t(expiry, day)
        k = strike_for_delta(spot, t, p.option_rate, iv, p.cc_delta, "C")
        price = bs_price(spot, k, t, p.option_rate, iv, "C")
        self._open_option(day, "close", sym, "C", "cc", k, qty, expiry, price)

    def _earnings_exit(self, day: pd.Timestamp, i: int, sym: str) -> None:
        st = self.st[sym]
        acted = False
        cpx = float(self.Cf[i, self.col[sym]])
        for o in self._sym_opts(sym):
            iv = float(self.ivf[sym][i])
            if not np.isfinite(iv):
                iv = 0.3
            model = self._model(o, cpx, iv, day)
            self._close_option(day, "close", o, model, forced=True, px=cpx)
            acted = True
        if self.p.earnings_exit_spot and st.spot > _EPS:
            self._sell_spot_all(day, "close", sym, float(self.Cf[i, self.col[sym]]))
            acted = True
        if acted:
            self.counters["earnings_exits"] += 1
        st.reset(keep_spot_status=(not self.p.earnings_exit_spot and st.spot > _EPS))

    def _skip_roll(self, oid: int) -> None:
        """每張選擇權只計一次「轉倉買不起」，避免被測試的部位每天重複累計。"""
        if oid not in self._roll_skipped:
            self._roll_skipped.add(oid)
            self.counters["roll_skipped"] += 1

    def _roll(
        self, day: pd.Timestamp, i: int, o: _Opt, spot: float, iv: float, model: float
    ) -> None:
        p = self.p
        if not p.options_enabled:
            return
        expiry = self._expiry_for(day)
        t = self._t(expiry, day)
        delta = -p.csp_delta if o.role == "csp" else p.cc_delta
        k = strike_for_delta(spot, t, p.option_rate, iv, delta, o.kind)
        cost = model * (1.0 + p.option_spread) * o.qty
        release = o.strike * o.qty if o.role == "csp" else 0.0
        need = k * o.qty if o.role == "csp" else 0.0
        if self.cash - cost - (self._locked() - release) + _EPS < need:
            self._skip_roll(o.oid)
            return
        qty, sym, role, kind = o.qty, o.sym, o.role, o.kind
        if not self._close_option(day, "close", o, model, forced=False, px=spot):
            self._skip_roll(o.oid)
            return
        price = bs_price(spot, k, t, p.option_rate, iv, kind)
        self._open_option(day, "close", sym, kind, role, k, qty, expiry, price)
        self.counters["rolls"] += 1

    # -- 訊號（收盤後，次日開盤執行）-----------------------------------------

    def _signals(
        self, day: pd.Timestamp, i: int, nav: float
    ) -> list[tuple[str, str, float, int]]:
        p = self.p
        out: list[tuple[str, str, float, int]] = []
        for s in self.syms:
            st = self.st[s]
            j = self.col[s]
            c = float(self.Cf[i, j])
            e20 = float(self.ema20[s][i])
            s50 = float(self.sma50[s][i])
            rsi = float(self.rsi[s][i])
            if not (np.isfinite(c) and np.isfinite(e20) and np.isfinite(s50)):
                continue
            above = c > e20 and c > s50
            below = c < e20 and c < s50
            st.below = st.below + 1 if below else 0
            if st.confirmed and st.below >= p.right_exit_confirm_days:
                out.append(("right_exit", s, c, i))
                continue
            if (
                st.left_shares > _EPS
                and st.left_cost - st.left_shares * c >= p.max_loss_nav_pct * nav
            ):
                out.append(("loss_stop", s, c, i))
                continue
            if self._blocked(s, i):
                continue
            if above:
                out.append(("right_entry", s, c, i))
            elif np.isfinite(rsi):
                if (
                    rsi < p.rsi_entry
                    and not st.tier1_done
                    and not self._has_position(s)
                ):
                    out.append(("tier1", s, c, i))
            if (
                st.tier1_done
                and not st.tier2_done
                and not st.paused
                and not st.confirmed
                and c <= st.tier1_ref * (1.0 - p.tier2_drop)
            ):
                out.append(("tier2", s, c, i))
            if p.options_enabled and p.cc_on_right and st.confirmed:
                uncovered = st.spot - sum(o.qty for o in self._sym_opts(s, "cc"))
                if uncovered > _EPS:
                    out.append(("cc", s, c, i))
        if p.hedge_enabled and p.options_enabled:
            spot_val = sum(self.st[s].spot * self.Cf[i, self.col[s]] for s in self.syms)
            sma_spy = float(self.spy_sma[i])
            if (
                not any(o.role == "hedge" for o in self.opts.values())
                and nav > 0
                and spot_val >= p.hedge_min_equity_pct * nav
                and np.isfinite(sma_spy)
                and float(self.spy_c[i]) < sma_spy
            ):
                out.append(("hedge", MARKET_SYMBOL, float(self.spy_c[i]), i))
        return out


def simulate_advisor(
    opens: pd.DataFrame,
    closes: pd.DataFrame,
    cash_index: pd.Series,
    vix: pd.Series,
    start: str,
    end: str,
    params: AdvisorParams = AdvisorParams(),
    earnings: Optional[dict[str, list[date]]] = None,
    fomc_dates: Sequence[date] = FOMC_DATES_2026,
    initial_capital: float = 100_000.0,
) -> AdvisorResult:
    """逐日模擬 MQA 機械化策略。`opens`／`closes` 需含選股池與 SPY（日曆對齊、已 ffill
    以外的缺值保留 NaN）；`cash_index` 為 BOXX 現金指數；`vix` 為 ^VIX 收盤（點數）。
    `earnings` 為各標的財報日清單（缺項＝無財報封鎖）。"""
    sim = _Sim(
        opens,
        closes,
        cash_index,
        vix,
        start,
        end,
        params,
        earnings or {},
        fomc_dates,
        initial_capital,
    )
    return sim.run()


def iv_proxy_frame(
    closes: pd.DataFrame, vix: pd.Series, symbols: Sequence[str]
) -> pd.DataFrame:
    """各標的每日 IV 代理值（供與真實 IV 比對）；SPY 使用 VIX/100。"""
    cl = closes.ffill()
    vix_a = vix.reindex(closes.index).ffill()
    rv_spy = realized_vol(cl[MARKET_SYMBOL], 20)
    cols: dict[str, pd.Series] = {}
    for s in symbols:
        if s == MARKET_SYMBOL:
            cols[s] = vix_a / 100.0
        elif s in cl.columns:
            cols[s] = iv_proxy(realized_vol(cl[s], 20), vix_a, rv_spy)
    return pd.DataFrame(cols, index=closes.index)
