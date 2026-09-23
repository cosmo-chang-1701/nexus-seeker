"""
tests/unit/test_calibration_backtest_feature_flags.py

2025 回測複刻的階段 1A／1B／3 開關與出場分層事件 (handoff.md §9 待辦 3、4)。

不變式：
  1. PYRAMID_ADD 條件二 (停損 >= 成本) 不成立時絕不加碼——金字塔加碼與攤平的唯一分界。
  2. 逃頂分級各級獨立去重：同級 10 日內不重複，但升級不被較低級的冷卻擋住。
  3. WATCH 級只買 Put、不賣股；同時最多一筆對沖。
  4. 出場事件標註的方向語意與 production evaluation_recorder 一致。
  5. 全部開關打開時，整段模擬可在合成資料上跑完。

「開關全關時與改動前逐位元相同」需要真實 2025 快取與舊版引擎，無法在單元測試
重現；已於施工時以 HEAD 版引擎對照 aggressive／defensive 兩模式的逐日 NAV 與
全部交易紀錄驗證相同 (見 handoff.md §9)。
"""

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from calibration.backtest_engine_2025 import (
    HedgePut,
    Position,
    RolloverBacktestEngine2025,
    _bsm_put_price,
    _strike_for_put_delta,
)
from calibration.data_store import DataStore
from market_analysis.dynamic_rollover.constants import (
    _MACRO_TOP_ESCAPE_ELEVATED_TRIM_RATIO,
    _MACRO_TOP_ESCAPE_PUT_TARGET_DELTA,
    _MACRO_TOP_ESCAPE_TRIM_RATIO,
    _PYRAMID_MAX_ADDS,
)
from tests.unit.calibration_fixtures import synthetic_daily, synthetic_hourly

TODAY = date(2025, 6, 10)


def _engine(
    tmp_path: Path,
    enable_tp1_trend_exempt: bool = False,
    enable_pyramid_add: bool = False,
    enable_escape_tiers: bool = False,
) -> RolloverBacktestEngine2025:
    return RolloverBacktestEngine2025(
        cache_dir=tmp_path,
        enable_tp1_trend_exempt=enable_tp1_trend_exempt,
        enable_pyramid_add=enable_pyramid_add,
        enable_escape_tiers=enable_escape_tiers,
    )


def _daily_feat(high10: list[float], high60: float = 100.0) -> pd.DataFrame:
    n = len(high10)
    dates = [TODAY - timedelta(days=n - i) for i in range(n)] + [TODAY]
    return pd.DataFrame(
        {
            "date": dates,
            "close": [100.0] * (n + 1),
            "high10": high10 + [high10[-1]],
            "high60": [high60] * (n + 1),
        }
    )


def _bar(**overrides: Any) -> pd.Series:
    base: dict[str, Any] = {
        "session_vwap": 99.0,
        "atr14_prev": 2.0,
        "rsi": 60.0,
    }
    base.update(overrides)
    return pd.Series(base, name=pd.Timestamp("2025-06-10 15:30", tz="UTC"))


# ---------------------------------------------------------------- BSM 輔助
def test_strike_for_put_delta_round_trips() -> None:
    spot, t, sigma = 500.0, 45 / 365.0, 0.20
    strike = _strike_for_put_delta(spot, _MACRO_TOP_ESCAPE_PUT_TARGET_DELTA, t, sigma)
    assert strike < spot  # OTM Put
    h = 0.01
    delta = (
        _bsm_put_price(spot + h, strike, t, sigma)
        - _bsm_put_price(spot - h, strike, t, sigma)
    ) / (2 * h)
    assert delta == pytest.approx(_MACRO_TOP_ESCAPE_PUT_TARGET_DELTA, abs=1e-3)


def test_bsm_put_at_expiry_is_intrinsic() -> None:
    assert _bsm_put_price(90.0, 100.0, 0.0, 0.2) == 10.0
    assert _bsm_put_price(110.0, 100.0, 0.0, 0.2) == 0.0


# ---------------------------------------------------------------- 1A
class TestTp1TrendExempt:
    def _eng(self, tmp_path: Path, high10: list[float]) -> RolloverBacktestEngine2025:
        eng = _engine(tmp_path, enable_tp1_trend_exempt=True)
        eng.daily_feat["NVDA"] = _daily_feat(high10)
        return eng

    def test_wall_migrating_positive_gamma_above_vwap_is_exempt(
        self, tmp_path: Path
    ) -> None:
        eng = self._eng(tmp_path, [100.0, 102.0])
        assert eng._is_tp1_trend_exempt("NVDA", TODAY, _bar(), 101.0, 0.5)

    def test_flat_wall_is_not_exempt(self, tmp_path: Path) -> None:
        eng = self._eng(tmp_path, [100.0, 100.0])
        assert not eng._is_tp1_trend_exempt("NVDA", TODAY, _bar(), 101.0, 0.5)

    def test_negative_gamma_is_not_exempt(self, tmp_path: Path) -> None:
        eng = self._eng(tmp_path, [100.0, 102.0])
        assert not eng._is_tp1_trend_exempt("NVDA", TODAY, _bar(), 101.0, -0.1)

    def test_below_vwap_is_not_exempt(self, tmp_path: Path) -> None:
        eng = self._eng(tmp_path, [100.0, 102.0])
        assert not eng._is_tp1_trend_exempt(
            "NVDA", TODAY, _bar(session_vwap=102.0), 101.0, 0.5
        )

    def test_missing_history_fails_safe(self, tmp_path: Path) -> None:
        eng = self._eng(tmp_path, [102.0])
        assert not eng._is_tp1_trend_exempt("NVDA", TODAY, _bar(), 101.0, 0.5)


# ---------------------------------------------------------------- 1B
class TestPyramidAdd:
    def _setup(
        self, tmp_path: Path, stop_loss: float = 100.0
    ) -> tuple[RolloverBacktestEngine2025, Position]:
        eng = _engine(tmp_path, enable_pyramid_add=True)
        eng.daily_feat["NVDA"] = _daily_feat([100.0, 100.0], high60=108.0)
        eng.portfolio.cash = 50_000.0
        pos = Position(
            symbol="NVDA",
            asset_class="SATELLITE",
            shares=100.0,
            avg_cost=100.0,
            current_price=108.0,
            current_value=10_800.0,
            stop_loss=stop_loss,
        )
        eng.portfolio.positions["NVDA"] = pos
        eng._bar_counter = 100
        return eng, pos

    def _call(
        self,
        eng: RolloverBacktestEngine2025,
        pos: Position,
        macro_tier: str = "NORMAL",
        **overrides: Any,
    ) -> None:
        kwargs: dict[str, Any] = dict(
            symbol="NVDA",
            current_date=TODAY,
            pos=pos,
            bar=_bar(session_vwap=106.0),
            spot=108.0,
            call_wall=130.0,
            put_wall=103.0,
            gamma_flip=104.0,
            atr_15m=0.5,
            net_gex_proxy=1.0,
            nav=100_000.0,
            vix_prev=16.0,
            macro_tier=macro_tier,
            ts_str="2025-06-10 15:30",
            date_str="2025-06-10",
        )
        kwargs.update(overrides)
        eng._try_pyramid_add(**kwargs)

    def test_happy_path_adds_and_advances_state(self, tmp_path: Path) -> None:
        eng, pos = self._setup(tmp_path)
        self._call(eng, pos)
        assert pos.shares > 100.0
        assert pos.dynamic_state["pyramid_count"] == 1
        assert pos.dynamic_state["last_pyramid_bar"] == 100
        assert eng.portfolio.trades[-1].scenario == "PYRAMID_ADD"

    def test_stop_below_cost_never_adds(self, tmp_path: Path) -> None:
        """最高優先不變式：停損低於成本時任何其他條件都不得讓它加碼。"""
        eng, pos = self._setup(tmp_path, stop_loss=99.99)
        self._call(eng, pos)
        assert pos.shares == 100.0
        assert not eng.portfolio.trades

    def test_max_adds_is_enforced(self, tmp_path: Path) -> None:
        eng, pos = self._setup(tmp_path)
        pos.dynamic_state["pyramid_count"] = _PYRAMID_MAX_ADDS
        self._call(eng, pos)
        assert not eng.portfolio.trades

    def test_cooldown_blocks_next_bar(self, tmp_path: Path) -> None:
        eng, pos = self._setup(tmp_path)
        pos.dynamic_state["last_pyramid_bar"] = 99
        self._call(eng, pos)
        assert not eng.portfolio.trades

    def test_non_normal_macro_tier_blocks(self, tmp_path: Path) -> None:
        eng, pos = self._setup(tmp_path)
        self._call(eng, pos, macro_tier="WATCH")
        assert not eng.portfolio.trades

    def test_trend_structure_required(self, tmp_path: Path) -> None:
        eng, pos = self._setup(tmp_path)
        self._call(eng, pos, net_gex_proxy=-0.5)
        assert not eng.portfolio.trades

    def test_exposure_cap_downsizes(self, tmp_path: Path) -> None:
        eng, pos = self._setup(tmp_path)
        cap_usd = 100_000.0 * eng.max_satellite_budget_pct
        pos.current_value = cap_usd - 108.0 * 3  # 只剩 3 股額度
        self._call(eng, pos)
        added = pos.shares - 100.0
        assert 0 < added <= 3.0


# ---------------------------------------------------------------- 3
class TestEscapeTiers:
    def _setup(self, tmp_path: Path) -> RolloverBacktestEngine2025:
        eng = _engine(tmp_path, enable_escape_tiers=True)
        eng.portfolio.cash = 50_000.0
        for sym in ("NVDA", "GLD"):
            eng.portfolio.positions[sym] = Position(
                symbol=sym,
                asset_class="SATELLITE",
                shares=100.0,
                avg_cost=100.0,
                current_price=100.0,
                current_value=10_000.0,
            )
        eng.daily_feat["SPY"] = pd.DataFrame(
            {"date": [TODAY - timedelta(days=1)], "close": [500.0]}
        )
        eng.daily_feat["NVDA"] = eng.daily_feat["SPY"]
        eng.daily_feat["GLD"] = eng.daily_feat["SPY"]
        return eng

    _PRICES = {"SPY": 500.0, "NVDA": 100.0, "GLD": 100.0}

    @pytest.mark.parametrize(
        "tier, ratio",
        [
            ("ELEVATED", _MACRO_TOP_ESCAPE_ELEVATED_TRIM_RATIO),
            ("CRITICAL", _MACRO_TOP_ESCAPE_TRIM_RATIO),
        ],
    )
    def test_trim_ratio_per_tier(self, tmp_path: Path, tier: str, ratio: float) -> None:
        eng = self._setup(tmp_path)
        eng._apply_escape_tier(tier, TODAY, self._PRICES, 20.0, "2025-06-10")
        assert eng.portfolio.positions["NVDA"].shares == pytest.approx(
            100.0 * (1 - ratio)
        )

    def test_same_tier_cooldown_but_escalation_passes(self, tmp_path: Path) -> None:
        eng = self._setup(tmp_path)
        eng._apply_escape_tier("ELEVATED", TODAY, self._PRICES, 20.0, "2025-06-10")
        after_first = eng.portfolio.positions["NVDA"].shares
        later = TODAY + timedelta(days=3)
        eng._apply_escape_tier("ELEVATED", later, self._PRICES, 20.0, "2025-06-13")
        assert eng.portfolio.positions["NVDA"].shares == after_first
        eng._apply_escape_tier("CRITICAL", later, self._PRICES, 20.0, "2025-06-13")
        assert eng.portfolio.positions["NVDA"].shares < after_first

    def test_watch_buys_single_put_without_selling_stock(self, tmp_path: Path) -> None:
        eng = self._setup(tmp_path)
        cash_before = eng.portfolio.cash
        eng._apply_escape_tier("WATCH", TODAY, self._PRICES, 20.0, "2025-06-10")
        hedge = eng.active_hedge
        assert hedge is not None
        assert eng.portfolio.positions["NVDA"].shares == 100.0
        assert eng.portfolio.cash < cash_before
        assert hedge.strike < 500.0
        # 同時最多一筆：冷卻過後的第二次 WATCH 不得再開
        eng._apply_escape_tier(
            "WATCH", TODAY + timedelta(days=15), self._PRICES, 20.0, "2025-06-25"
        )
        assert sum(t.action == "BUY" for t in eng.portfolio.trades) == 1

    def test_hedge_rolls_out_before_theta_zone(self, tmp_path: Path) -> None:
        eng = _engine(tmp_path, enable_escape_tiers=True)
        eng.daily_feat["^VIX"] = pd.DataFrame({"date": [TODAY], "close": [20.0]})
        eng.active_hedge = HedgePut(
            strike=480.0,
            expiry=TODAY + timedelta(days=30),
            contracts=2,
            entry_premium=5.0,
            entry_date="2025-05-01",
        )
        assert eng._mark_hedge(TODAY, 470.0, "2025-06-10") > 0
        cash_before = eng.portfolio.cash
        assert eng._mark_hedge(TODAY + timedelta(days=10), 470.0, "2025-06-20") == 0.0
        assert eng.active_hedge is None
        assert eng.portfolio.cash > cash_before
        assert eng.portfolio.trades[-1].action == "SELL"


# ---------------------------------------------------------------- 出場事件標註
def _falling_bars(start: pd.Timestamp, sessions: int, drop: float) -> pd.DataFrame:
    idx: list[pd.Timestamp] = []
    day = start.normalize()
    while len(idx) < sessions * 7:
        if day.dayofweek < 5:
            idx.extend(
                day + pd.Timedelta(hours=13, minutes=30) + pd.Timedelta(hours=h)
                for h in range(7)
            )
        day += pd.Timedelta(days=1)
    n = len(idx)
    closes = [100.0 - drop * (i + 1) / n for i in range(n)]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 0.05 for c in closes],
            "Low": [c - 0.05 for c in closes],
            "Close": closes,
        },
        index=pd.DatetimeIndex(idx, tz="UTC"),
    )


def test_exit_event_direction_matches_production(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    start = pd.Timestamp("2025-06-02 13:30", tz="UTC")
    eng.hourly_data["NVDA"] = _falling_bars(start, sessions=8, drop=10.0)
    bar = _bar(atr14_prev=2.0)
    bar.name = start
    eng._log_exit_event("NVDA", "SL_STRUCTURAL", bar, 100.0)
    eng._log_exit_event("NVDA", "TP1_TREND_EXEMPT", bar, 100.0)
    labeled = {r["tier"]: r for r in eng.label_exit_events()}
    # 價格下跌：平倉訊號正確 (+1)，續抱訊號錯誤 (−1)
    assert labeled["SL_STRUCTURAL"]["direction"] == "SHORT"
    assert labeled["SL_STRUCTURAL"]["outcome"] == 1
    assert labeled["TP1_TREND_EXEMPT"]["direction"] == "LONG"
    assert labeled["TP1_TREND_EXEMPT"]["outcome"] == -1
    summary = {r["tier"]: r for r in eng.summarize_exit_events()}
    assert summary["SL_STRUCTURAL"]["correct_rate"] == 1.0


# ---------------------------------------------------------------- 整段模擬
def _synthetic_store(tmp_path: Path) -> tuple[str, str]:
    store = DataStore(tmp_path)
    for i, sym in enumerate(("SPY", "NVDA", "GLD")):
        d = synthetic_daily(n_days=500, seed=i + 20, start="2023-01-02", drift=0.001)
        store.save("1d", sym, d)
        store.save("1h", sym, synthetic_hourly(d, n_days=200, seed=i + 40))
    vix = synthetic_daily(n_days=500, seed=98, start="2023-01-02", drift=0.0)
    store.save("1d", "^VIX", vix.assign(Close=lambda d: 14.0 + (d["Close"] % 12.0)))
    store.save("1d", "^VIX3M", vix.assign(Close=lambda d: 18.0 + (d["Close"] % 6.0)))
    dates = synthetic_daily(n_days=500, start="2023-01-02").index
    return str(dates[-120].date()), str(dates[-10].date())


def test_full_simulation_runs_with_all_flags(tmp_path: Path) -> None:
    start, end = _synthetic_store(tmp_path)
    eng = RolloverBacktestEngine2025(
        cache_dir=tmp_path,
        start_date=start,
        end_date=end,
        enable_trend_continuation=True,
        enable_tp1_trend_exempt=True,
        enable_pyramid_add=True,
        enable_escape_tiers=True,
    )
    eng.run_simulation()
    metrics = eng.calculate_metrics()
    assert eng.portfolio.daily_history
    assert metrics.total_trades >= 0
    eng.summarize_exit_events()


def test_flags_off_simulation_is_deterministic(tmp_path: Path) -> None:
    start, end = _synthetic_store(tmp_path)
    runs = []
    for _ in range(2):
        eng = RolloverBacktestEngine2025(
            cache_dir=tmp_path, start_date=start, end_date=end
        )
        eng.run_simulation()
        runs.append([(r.date, r.nav) for r in eng.portfolio.daily_history])
        assert eng.active_hedge is None
        assert not eng.escape_tier_history
    assert runs[0] == runs[1]
