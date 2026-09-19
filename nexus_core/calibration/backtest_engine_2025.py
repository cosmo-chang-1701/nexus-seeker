"""2025 年動態轉倉回測模擬引擎 (RolloverBacktestEngine2025).

涵蓋 9 大核心轉倉情境：
1. CORE_DEPLOYMENT (情境一: 核心資金超額再平衡與 Covered Call 收益增強)
2. OPPORTUNITY_COST (情境二: 衛星跨資產動能輪動 NVDA ↔ GLD)
3. SATELLITE_REBALANCE (情境三: 微觀結構雙軌防洗盤出場矩陣 SL1-4 / TP1-3)
4. MARGIN_DEFENSE (情境四: 大盤負 Gamma / VIX 飆升保證金防禦)
5. FUNDAMENTAL_BROKEN (情境五: 護城河破滅緊急撤離)
6. MACRO_TOP_ESCAPE_DEFENSE (情境六: 總經逃頂防禦減碼 25% 至 BOXX)
7. COVERED_CALL_PROFIT_LOCK (情境七: 賣方期權時間價值停利)
8. TRANSITION_ENGINE (情境八: 左側接刀向右側動能演化與 Pyramiding 加碼)
9. SHORT_ENTRY (情境九: Regime V 破位追空獨立做空進場訊號與凱利風控)

零前視偏差保證：所有日線特徵皆為 shift(1) 前一交易日收盤時已知數值；
小時線特徵與 Session VWAP 為當日累積計算。
"""

from dataclasses import dataclass, field
from datetime import date, datetime
import math
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from calibration.data_store import DataStore
from calibration.features import daily_features, hourly_features
from market_analysis.dynamic_rollover.constants import (
    _CORE_DEPLOYMENT_OPPORTUNITY_DEPLOY_RATIO,
    _CORE_EXCESS_MIN_TRADE_PCT,
    _ESTIMATED_ROUND_TRIP_COST_PCT,
    _EV_SPREAD_MIN_THRESHOLD,
    _MOMENTUM_DECAY_THRESHOLD,
    _BREAKOUT_READY_THRESHOLD,
    _PROFIT_LOCK_PROFIT_PCT_THRESHOLD,
    _ROLLOVER_RATIO_HIGH_PROFIT,
    _ROLLOVER_RATIO_STANDARD,
    _SHORT_ENTRY_ACCOUNT_RISK_PCT,
    _MICROSTRUCTURE_TP1_CALLWALL_PCT,
    _MICROSTRUCTURE_TP1_RATIO,
    _MICROSTRUCTURE_TP2_WALL_BREAK_PCT,
    _MICROSTRUCTURE_TP2_RATIO,
    _MICROSTRUCTURE_TP3_RATIO,
    _MICROSTRUCTURE_SL_TRAILING_CALLWALL_PROGRESS_PCT,
    _MICROSTRUCTURE_SL_NET_GEX_THRESHOLD,
    _MACRO_TOP_ESCAPE_TRIM_RATIO,
)
from market_analysis.dynamic_rollover.models import (
    RolloverScenario,
    ShortEntryEvaluation,
)
from market_analysis.dynamic_rollover.short_entry_sizing import (
    build_short_entry_levels,
    compute_short_entry_sizing,
)
from market_analysis.room_threshold import (
    compute_dynamic_room_threshold,
    compute_reference_stop,
    evaluate_wall_buffer,
)


# --- Regime III-B 趨勢延續態的 1h 代理常數 ---
# production 的判定是「近 6 根**已收盤 15m** K 棒至少 5 根站穩結構」(1.5 小時窗、
# 83% 容差)。本回測只有 1h K 線，粒度粗 4 倍，無法逐根複製；改以「近 4 根 1h K 棒
# 至少 3 根」(4 小時窗、75% 容差) 作為同形狀的代理。窗口在時間維度上比 production
# **更長更嚴**，方向是保守的。
#
# ⚠️ 本代理只能量測條件一放寬 (不再要求放量陽線) 的效果。條件四的 UOA 5 日回看窗
# **在本回測中完全未實作**——backtest_engine_2025.py 原本就沒有 UOA 條件
# (見 handoff.md §1.3 的門檻對照表)。因此 A/B 結果低估 III-B 的實際進場頻率，
# 判讀時必須把這點算進去：回測說「勉強打平」在 production 可能是明顯負向。
_BT_TREND_CONT_LOOKBACK_BARS_1H = 4
_BT_TREND_CONT_MIN_HELD_BARS_1H = 3


@dataclass
class Position:
    symbol: str
    asset_class: str  # "CORE", "SATELLITE", "DEFENSE"
    shares: float  # 正數為多頭，負數為空頭
    avg_cost: float
    side: str = "LONG"  # "LONG" 或 "SHORT"
    current_price: float = 0.0
    current_value: float = 0.0
    target_allocation_pct: float = 0.0
    stop_loss: float = 0.0
    anchor_base: float = 0.0
    target_wall: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    entry_date: str = ""
    tp1_triggered: bool = False
    tp2_triggered: bool = False
    tp3_triggered: bool = False
    is_pyramided: bool = False
    entry_regime: str = "REGIME_III_RIGHT_MOMENTUM"
    dynamic_state: dict[str, Any] = field(default_factory=lambda: dict())

    def update_price(self, price: float) -> None:
        self.current_price = price
        if self.side == "LONG":
            self.current_value = self.shares * price
            if price > self.highest_price or self.highest_price == 0.0:
                self.highest_price = price
            if price < self.lowest_price or self.lowest_price == 0.0:
                self.lowest_price = price
        else:
            # 空頭部位價值為名目負債價值
            self.current_value = -abs(self.shares) * price
            if price > self.highest_price or self.highest_price == 0.0:
                self.highest_price = price
            if price < self.lowest_price or self.lowest_price == 0.0:
                self.lowest_price = price

    @property
    def unrealized_pnl(self) -> float:
        if self.side == "LONG":
            return self.shares * (self.current_price - self.avg_cost)
        else:
            return abs(self.shares) * (self.avg_cost - self.current_price)

    @property
    def return_pct(self) -> float:
        if self.avg_cost <= 0:
            return 0.0
        if self.side == "LONG":
            return (self.current_price - self.avg_cost) / self.avg_cost
        else:
            return (self.avg_cost - self.current_price) / self.avg_cost


@dataclass
class TradeRecord:
    timestamp: str
    date: str
    symbol: str
    action: str  # "BUY", "SELL", "SHORT", "COVER", "INCOME"
    shares: float
    price: float
    notional: float
    fee: float
    scenario: str
    reason: str
    realized_pnl: float = 0.0


@dataclass
class DailyNavRecord:
    date: str
    nav: float
    cash: float
    positions_value: float
    benchmark_nav: float
    spy_weight: float
    nvda_weight: float
    gld_weight: float
    cash_weight: float
    vix: float
    market_regime: str
    daily_return: float
    benchmark_daily_return: float


@dataclass
class BacktestMetrics:
    total_return: float
    cagr: float
    benchmark_total_return: float
    benchmark_cagr: float
    annualized_volatility: float
    benchmark_volatility: float
    sharpe_ratio: float
    benchmark_sharpe: float
    sortino_ratio: float
    benchmark_sortino: float
    max_drawdown: float
    benchmark_max_drawdown: float
    calmar_ratio: float
    benchmark_calmar: float
    # 減碼 B&H 對照組 (docs/architecture/05 §6.1 指定的最重要 KPI)：以「與本策略
    # 同等年化波動的 B&H + 現金」為基準。回答的是「這套引擎創造 alpha，還是只是在
    # 降低曝險」——高勝率但年化波動只有 B&H 一半的策略，報酬本來就該低於 B&H，
    # 拿裸 B&H 比較會同時誤判它變好或變壞。
    scaled_benchmark_weight: float
    scaled_benchmark_total_return: float
    scaled_benchmark_max_drawdown: float
    excess_return_vs_scaled: float
    win_rate: float
    profit_factor: float
    total_trades: int
    scenario_stats: dict[str, dict[str, Any]]
    monthly_returns: dict[str, float]
    benchmark_monthly_returns: dict[str, float]


class Portfolio:
    def __init__(
        self, initial_cash: float = 100_000.0, fee_rate: float = 0.0015
    ) -> None:
        self.cash: float = initial_cash
        self.initial_cash: float = initial_cash
        self.fee_rate: float = fee_rate
        self.positions: dict[str, Position] = dict()
        self.trades: list[TradeRecord] = list()
        self.daily_history: list[DailyNavRecord] = list()

    def has_long(self, symbol: str) -> bool:
        pos = self.positions.get(symbol)
        return pos is not None and pos.side == "LONG" and pos.shares > 0

    def has_short(self, symbol: str) -> bool:
        key = symbol if symbol.endswith("_SHORT") else f"{symbol}_SHORT"
        pos = self.positions.get(key)
        return pos is not None and pos.side == "SHORT" and abs(pos.shares) > 0

    def is_active(self, symbol: str) -> bool:
        return self.has_long(symbol) or self.has_short(symbol)

    def get_locked_margin(self) -> float:
        """計算空頭現貨部位之 Reg-T 初始保證金 (50%) 佔用量。"""
        locked = 0.0
        for pos in self.positions.values():
            if pos.side == "SHORT":
                locked += abs(pos.shares) * pos.current_price * 0.50
        return float(locked)

    def get_total_nav(self, current_prices: dict[str, float]) -> float:
        total = self.cash
        for sym, pos in self.positions.items():
            base_sym = sym.replace("_SHORT", "")
            price = current_prices.get(
                sym, current_prices.get(base_sym, pos.current_price)
            )
            if price > 0:
                pos.update_price(price)
            if pos.side == "LONG":
                total += pos.current_value
            else:
                total += pos.unrealized_pnl
        return float(total)

    def buy(
        self,
        symbol: str,
        asset_class: str,
        price: float,
        notional: float,
        scenario: str,
        reason: str,
        timestamp: str,
        date_str: str,
        target_allocation_pct: float = 0.0,
        stop_loss: float = 0.0,
        anchor_base: float = 0.0,
        target_wall: float = 0.0,
        entry_regime: str = "REGIME_III_RIGHT_MOMENTUM",
    ) -> Optional[TradeRecord]:
        if price <= 0 or notional <= 0 or self.has_short(symbol):
            return None
        available_cash = max(0.0, self.cash - self.get_locked_margin())
        actual_notional = min(notional, available_cash)
        fee = actual_notional * self.fee_rate
        net_notional = actual_notional - fee
        if net_notional <= 0:
            return None
        shares_bought = net_notional / price
        self.cash -= actual_notional

        pos = self.positions.get(symbol)
        if pos is not None and pos.side == "LONG":
            total_shares = pos.shares + shares_bought
            new_cost = (
                pos.shares * pos.avg_cost + shares_bought * price
            ) / total_shares
            pos.shares = total_shares
            pos.avg_cost = new_cost
            pos.update_price(price)
            if stop_loss > 0:
                pos.stop_loss = max(pos.stop_loss, stop_loss)
            if anchor_base > 0:
                pos.anchor_base = anchor_base
            if target_wall > 0:
                pos.target_wall = max(pos.target_wall, target_wall)
        else:
            new_pos = Position(
                symbol=symbol,
                asset_class=asset_class,
                shares=shares_bought,
                avg_cost=price,
                side="LONG",
                current_price=price,
                current_value=shares_bought * price,
                target_allocation_pct=target_allocation_pct,
                stop_loss=stop_loss,
                anchor_base=anchor_base if anchor_base > 0 else price,
                target_wall=target_wall,
                highest_price=price,
                lowest_price=price,
                entry_date=date_str,
                entry_regime=entry_regime,
            )
            self.positions[symbol] = new_pos

        record = TradeRecord(
            timestamp=timestamp,
            date=date_str,
            symbol=symbol,
            action="BUY",
            shares=shares_bought,
            price=price,
            notional=actual_notional,
            fee=fee,
            scenario=scenario,
            reason=reason,
            realized_pnl=0.0,
        )
        self.trades.append(record)
        return record

    def sell(
        self,
        symbol: str,
        price: float,
        ratio: float,
        scenario: str,
        reason: str,
        timestamp: str,
        date_str: str,
    ) -> Optional[TradeRecord]:
        pos = self.positions.get(symbol)
        if pos is None or pos.side != "LONG" or pos.shares <= 0 or price <= 0:
            return None
        sell_ratio = max(0.0, min(1.0, ratio))
        shares_to_sell = pos.shares * sell_ratio
        remaining_shares = pos.shares - shares_to_sell
        # 若平倉後剩餘部位極小 (< $250 或 < 0.5 股)，執行 100% 清理，避免產生微型碎片 (dust)
        if 0.0 < (remaining_shares * price) < 250.0 or (0.0 < remaining_shares < 0.5):
            sell_ratio = 1.0
            shares_to_sell = pos.shares
        if shares_to_sell <= 1e-4:
            return None

        gross_proceeds = shares_to_sell * price
        fee = gross_proceeds * self.fee_rate
        net_proceeds = gross_proceeds - fee
        realized_pnl = (price - pos.avg_cost) * shares_to_sell - fee

        self.cash += net_proceeds
        pos.shares -= shares_to_sell
        pos.update_price(price)

        if pos.shares <= 1e-4:
            del self.positions[symbol]

        record = TradeRecord(
            timestamp=timestamp,
            date=date_str,
            symbol=symbol,
            action="SELL",
            shares=shares_to_sell,
            price=price,
            notional=gross_proceeds,
            fee=fee,
            scenario=scenario,
            reason=reason,
            realized_pnl=realized_pnl,
        )
        self.trades.append(record)
        return record

    def short(
        self,
        symbol: str,
        price: float,
        shares: float,
        stop_price: float,
        target_price: float,
        scenario: str,
        reason: str,
        timestamp: str,
        date_str: str,
    ) -> Optional[TradeRecord]:
        if price <= 0 or shares <= 0 or self.has_long(symbol):
            return None
        notional = shares * price
        fee = notional * self.fee_rate
        # 保證金抵押檢查：現金儲備需大於 notional * 0.5 (Reg-T)
        required_margin = notional * 0.5
        if self.cash < required_margin + fee:
            # 依可用保證金縮小口數
            available_for_margin = max(0.0, self.cash - fee)
            max_notional = available_for_margin / 0.5
            shares = max_notional / price
            notional = shares * price
            fee = notional * self.fee_rate
            if shares <= 1e-4:
                return None

        self.cash -= fee
        short_pos = Position(
            symbol=f"{symbol}_SHORT",
            asset_class="SATELLITE",
            shares=-shares,
            avg_cost=price,
            side="SHORT",
            current_price=price,
            current_value=-shares * price,
            target_allocation_pct=0.0,
            stop_loss=stop_price,
            anchor_base=target_price,
            highest_price=price,
            lowest_price=price,
            entry_date=date_str,
            entry_regime="REGIME_V_BREAKDOWN_CHASE",
        )
        self.positions[f"{symbol}_SHORT"] = short_pos

        record = TradeRecord(
            timestamp=timestamp,
            date=date_str,
            symbol=f"{symbol}_SHORT",
            action="SHORT",
            shares=shares,
            price=price,
            notional=notional,
            fee=fee,
            scenario=scenario,
            reason=reason,
            realized_pnl=0.0,
        )
        self.trades.append(record)
        return record

    def cover(
        self,
        symbol: str,
        price: float,
        ratio: float,
        scenario: str,
        reason: str,
        timestamp: str,
        date_str: str,
    ) -> Optional[TradeRecord]:
        key = symbol if symbol.endswith("_SHORT") else f"{symbol}_SHORT"
        pos = self.positions.get(key)
        if pos is None or pos.side != "SHORT" or abs(pos.shares) <= 0 or price <= 0:
            return None

        cover_ratio = max(0.0, min(1.0, ratio))
        shares_to_cover = abs(pos.shares) * cover_ratio
        remaining_shares = abs(pos.shares) - shares_to_cover
        if 0.0 < (remaining_shares * price) < 250.0 or (0.0 < remaining_shares < 0.5):
            cover_ratio = 1.0
            shares_to_cover = abs(pos.shares)
        if shares_to_cover <= 1e-4:
            return None

        cost_to_cover = shares_to_cover * price
        fee = cost_to_cover * self.fee_rate
        realized_pnl = (pos.avg_cost - price) * shares_to_cover - fee

        self.cash += realized_pnl
        pos.shares += shares_to_cover
        pos.update_price(price)

        if abs(pos.shares) <= 1e-4:
            del self.positions[key]

        record = TradeRecord(
            timestamp=timestamp,
            date=date_str,
            symbol=key,
            action="COVER",
            shares=shares_to_cover,
            price=price,
            notional=cost_to_cover,
            fee=fee,
            scenario=scenario,
            reason=reason,
            realized_pnl=realized_pnl,
        )
        self.trades.append(record)
        return record

    def add_income(
        self,
        symbol: str,
        amount: float,
        scenario: str,
        reason: str,
        timestamp: str,
        date_str: str,
    ) -> TradeRecord:
        self.cash += amount
        record = TradeRecord(
            timestamp=timestamp,
            date=date_str,
            symbol=symbol,
            action="INCOME",
            shares=0.0,
            price=0.0,
            notional=amount,
            fee=0.0,
            scenario=scenario,
            reason=reason,
            realized_pnl=amount,
        )
        self.trades.append(record)
        return record


class RolloverBacktestEngine2025:
    """2025 全年度動態轉倉引擎回測核心。"""

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        initial_capital: float = 100_000.0,
        start_date: str = "2025-01-02",
        end_date: str = "2025-12-30",
        mode: str = "aggressive",
        enable_trend_continuation: bool = False,
    ) -> None:
        self.mode: str = mode.lower()
        # Regime III-B 趨勢延續進場路徑 (handoff.md §4)。預設關閉＝基準線，
        # 開啟後才加入第二條多頭進場路徑，供 §4.4 強制要求的 A/B 對比使用。
        self.enable_trend_continuation: bool = enable_trend_continuation
        self.cache_dir: Path = (
            cache_dir
            if cache_dir is not None
            else (
                Path("/app/.calibration_cache")
                if Path("/app").is_dir()
                else Path(".calibration_cache")
            )
        )
        self.store: DataStore = DataStore(self.cache_dir)
        self.initial_capital: float = initial_capital
        self.start_date: str = start_date
        self.end_date: str = end_date
        self.portfolio: Portfolio = Portfolio(initial_cash=initial_capital)

        # 標的配置定義
        self.core_symbol: str = "SPY"
        self.alpha_symbol: str = "NVDA"
        self.other_symbol: str = "GLD"
        self.vix_symbol: str = "^VIX"

        # 模式參數調配 (進攻型 vs 防禦型)
        if self.mode == "aggressive":
            # 動能進攻型：5% 現金儲備，95% 主動配置，更高的單筆動能部署與輪動敏捷度
            self.core_target_weight: float = 0.50
            self.alpha_target_weight: float = 0.25
            self.other_target_weight: float = 0.20
            self.cash_target_weight: float = 0.05
            self.tp1_ratio: float = (
                0.30  # 阻力初探平倉 30%（保留 70% 衝刺破牆 TP2/TP3）
            )
            self.core_deploy_ratio: float = 0.80  # 核心超額 80% 投入突破標的
            self.opp_cost_cd_days: int = 3  # 跨資產動能輪動冷卻縮短至 3 天
            self.opp_cost_hurdle: float = 0.02  # EV spread 門檻降至 2.0%
            self.max_satellite_budget_pct: float = (
                0.25  # 單筆突破進場最高可用 NAV 之 25%
            )
        else:
            # 穩健防禦型：10% 現金儲備，標準 TP1 50% 鎖利與 5 天輪動冷卻
            self.core_target_weight = 0.50
            self.alpha_target_weight = 0.25
            self.other_target_weight = 0.15
            self.cash_target_weight = 0.10
            self.tp1_ratio = _MICROSTRUCTURE_TP1_RATIO  # 0.50
            self.core_deploy_ratio = _CORE_DEPLOYMENT_OPPORTUNITY_DEPLOY_RATIO  # 0.50
            self.opp_cost_cd_days = 5
            self.opp_cost_hurdle = (
                _EV_SPREAD_MIN_THRESHOLD + _ESTIMATED_ROUND_TRIP_COST_PCT
            )  # 0.033
            self.max_satellite_budget_pct = 0.15

        # 資料容器
        self.daily_data: dict[str, pd.DataFrame] = dict()
        self.hourly_data: dict[str, pd.DataFrame] = dict()
        self.daily_feat: dict[str, pd.DataFrame] = dict()
        self.hourly_feat: dict[str, pd.DataFrame] = dict()
        self.trading_dates: list[date] = list()

        # 基準投組追蹤 (50% SPY, 25% NVDA, 15% GLD, 10% Cash 靜態持有)
        self.benchmark_shares: dict[str, float] = dict()
        self.benchmark_cash: float = initial_capital * 0.10
        self.benchmark_history: list[float] = list()

        # 轉倉與停損冷卻追蹤
        self.last_exit_date: dict[str, date] = dict()
        self.last_macro_escape_date: Optional[date] = None
        self.last_opp_cost_date: dict[str, date] = dict()
        self.fundamental_broken_events: dict[str, set[str]] = dict()

    def load_and_prepare_data(self) -> None:
        """載入 1d 與 1h 資料並計算無前視時序特徵。"""
        symbols = [
            self.core_symbol,
            self.alpha_symbol,
            self.other_symbol,
            self.vix_symbol,
        ]
        for sym in symbols:
            d = self.store.load("1d", sym)
            if d is None or d.empty:
                raise RuntimeError(f"缺失 {sym} 1d 歷史資料")
            self.daily_data[sym] = d
            self.daily_feat[sym] = daily_features(d)

            if sym != self.vix_symbol:
                h = self.store.load("1h", sym)
                if h is None or h.empty:
                    raise RuntimeError(f"缺失 {sym} 1h 歷史資料")
                self.hourly_data[sym] = h
                self.hourly_feat[sym] = hourly_features(h, self.daily_feat[sym])

        # 篩選出 2025 年交易日
        spy_dfeat = self.daily_feat[self.core_symbol]
        dt_start = datetime.strptime(self.start_date, "%Y-%m-%d").date()
        dt_end = datetime.strptime(self.end_date, "%Y-%m-%d").date()

        dates_in_range: set[date] = set()
        for d_val in spy_dfeat["date"]:
            if isinstance(d_val, date) and dt_start <= d_val <= dt_end:
                dates_in_range.add(d_val)
        self.trading_dates = sorted(list(dates_in_range))

    @staticmethod
    def _compute_psq_from_series(
        close_series: pd.Series, high_series: pd.Series, low_series: pd.Series
    ) -> float:
        """依收盤價歷史計算 PowerSqueeze 分數 (0-100)，完全無前視。"""
        if len(close_series) < 20:
            return 50.0
        c_series = close_series.astype(float)
        h_series = high_series.astype(float)
        l_series = low_series.astype(float)

        sma20 = c_series.rolling(20).mean().iloc[-1]
        std20 = c_series.rolling(20).std().iloc[-1]
        bb_upper = sma20 + 2.0 * std20
        bb_lower = sma20 - 2.0 * std20

        tr0 = (h_series - l_series).abs()
        tr1 = (h_series - c_series.shift(1)).abs()
        tr2 = (l_series - c_series.shift(1)).abs()
        tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)
        atr20 = tr.rolling(20).mean().iloc[-1]

        kc_upper = sma20 + 1.5 * atr20
        kc_lower = sma20 - 1.5 * atr20

        is_squeezing = bool(bb_upper < kc_upper and bb_lower > kc_lower)

        # 動態: 最近 4 根 (c - sma20) 的線性迴歸斜率
        diff = c_series - c_series.rolling(20).mean()
        recent_diff = diff.iloc[-4:].to_numpy()
        if len(recent_diff) >= 4 and not np.any(np.isnan(recent_diff)):
            x = np.array([0, 1, 2, 3], dtype=float)
            slope, _ = np.polyfit(x, recent_diff, 1)
        else:
            slope = 0.0

        is_bullish = slope > 0
        is_bearish = slope < 0

        # 分級對照 (對應 opportunity_cost.py::_normalize_power_squeeze)
        level = "High" if is_squeezing else "Normal"
        table: dict[str, dict[str, float]] = {
            "Release": {"neutral": 10.0, "bull": 75.0, "bear": 5.0},
            "Normal": {"neutral": 30.0, "bull": 40.0, "bear": 20.0},
            "Mid": {"neutral": 60.0, "bull": 70.0, "bear": 45.0},
            "High": {"neutral": 50.0, "bull": 90.0, "bear": 10.0},
        }
        bucket = table.get(level, table["Normal"])
        base_score = (
            bucket["bull"]
            if is_bullish
            else (bucket["bear"] if is_bearish else bucket["neutral"])
        )

        # 突破加速態判定
        if not is_squeezing and is_bullish and slope > std20 * 0.15:
            return 95.0
        if not is_squeezing and is_bearish and slope < -std20 * 0.15:
            return 5.0

        return float(max(0.0, min(100.0, base_score)))

    def _get_daily_proxy_row(
        self, symbol: str, current_date: date
    ) -> Optional[pd.Series]:
        f = self.daily_feat[symbol]
        rows = f[f["date"] == current_date]
        if rows.empty:
            return None
        idx = rows.index[0]
        pos = f.index.get_loc(idx)
        if isinstance(pos, int) and pos > 0:
            # 取 shift(1) 前一日收盤已知資訊
            return f.iloc[pos - 1]
        return None

    def _get_vix_prev(self, current_date: date) -> float:
        vf = self.daily_feat[self.vix_symbol]
        prior = vf[vf["date"] < current_date]
        if prior.empty:
            return 18.0
        return float(prior["close"].iloc[-1])

    def setup_initial_portfolio(self, first_date: date) -> None:
        """建立 2025 第一個交易日的初始建倉與基準投組。"""
        first_dt_str = str(first_date)

        # 取得當日開盤價
        spy_open = float(
            self.daily_feat[self.core_symbol][
                self.daily_feat[self.core_symbol]["date"] == first_date
            ]["open"].iloc[0]
        )
        nvda_open = float(
            self.daily_feat[self.alpha_symbol][
                self.daily_feat[self.alpha_symbol]["date"] == first_date
            ]["open"].iloc[0]
        )
        gld_open = float(
            self.daily_feat[self.other_symbol][
                self.daily_feat[self.other_symbol]["date"] == first_date
            ]["open"].iloc[0]
        )

        # 動態投組建倉 (SPY core_target_weight, NVDA alpha_target_weight, GLD other_target_weight)
        spy_notional = self.initial_capital * self.core_target_weight
        nvda_notional = self.initial_capital * self.alpha_target_weight
        gld_notional = self.initial_capital * self.other_target_weight

        # SPY 核心部位
        self.portfolio.buy(
            symbol=self.core_symbol,
            asset_class="CORE",
            price=spy_open,
            notional=spy_notional,
            scenario="INIT",
            reason=f"2025 初始核心配置 ({self.core_target_weight:.0%})",
            timestamp=f"{first_dt_str} 09:30:00",
            date_str=first_dt_str,
            target_allocation_pct=self.core_target_weight,
            anchor_base=spy_open,
            target_wall=spy_open * 1.05,
            stop_loss=spy_open * 0.90,
        )

        # NVDA 衛星 Alpha 部位
        self.portfolio.buy(
            symbol=self.alpha_symbol,
            asset_class="SATELLITE",
            price=nvda_open,
            notional=nvda_notional,
            scenario="INIT",
            reason=f"2025 初始衛星 Alpha 配置 ({self.alpha_target_weight:.0%})",
            timestamp=f"{first_dt_str} 09:30:00",
            date_str=first_dt_str,
            anchor_base=nvda_open,
            target_wall=nvda_open * 1.08,
            stop_loss=nvda_open * 0.92,
        )

        # GLD 衛星 Other 部位
        self.portfolio.buy(
            symbol=self.other_symbol,
            asset_class="SATELLITE",
            price=gld_open,
            notional=gld_notional,
            scenario="INIT",
            reason=f"2025 初始衛星 Other 避險配置 ({self.other_target_weight:.0%})",
            timestamp=f"{first_dt_str} 09:30:00",
            date_str=first_dt_str,
            anchor_base=gld_open,
            target_wall=gld_open * 1.06,
            stop_loss=gld_open * 0.95,
        )

        # 靜態基準投組股份設定 (扣除手續費) - 基準始終維持標準 50/25/15/10 靜態對照
        fee_rate = self.portfolio.fee_rate
        bench_core_usd = self.initial_capital * 0.50
        bench_alpha_usd = self.initial_capital * 0.25
        bench_other_usd = self.initial_capital * 0.15
        self.benchmark_shares[self.core_symbol] = (
            bench_core_usd * (1.0 - fee_rate)
        ) / spy_open
        self.benchmark_shares[self.alpha_symbol] = (
            bench_alpha_usd * (1.0 - fee_rate)
        ) / nvda_open
        self.benchmark_shares[self.other_symbol] = (
            bench_other_usd * (1.0 - fee_rate)
        ) / gld_open

    def run_simulation(self) -> None:
        """執行 2025 年回測主迴圈。"""
        self.load_and_prepare_data()
        if not self.trading_dates:
            raise RuntimeError("2025 年無有效交易日")

        first_date = self.trading_dates[0]
        self.setup_initial_portfolio(first_date)

        prev_nav = self.initial_capital
        prev_bench_nav = self.initial_capital

        for d_idx, current_date in enumerate(self.trading_dates):
            date_str = str(current_date)
            vix_prev = self._get_vix_prev(current_date)

            # -----------------------------------------------------------------
            # 1. 開盤微觀特徵與行情更新
            # -----------------------------------------------------------------
            spy_prev_row = self._get_daily_proxy_row(self.core_symbol, current_date)
            nvda_prev_row = self._get_daily_proxy_row(self.alpha_symbol, current_date)
            gld_prev_row = self._get_daily_proxy_row(self.other_symbol, current_date)

            # 開盤報價
            spy_open = float(
                self.daily_feat[self.core_symbol][
                    self.daily_feat[self.core_symbol]["date"] == current_date
                ]["open"].iloc[0]
            )
            nvda_open = float(
                self.daily_feat[self.alpha_symbol][
                    self.daily_feat[self.alpha_symbol]["date"] == current_date
                ]["open"].iloc[0]
            )
            gld_open = float(
                self.daily_feat[self.other_symbol][
                    self.daily_feat[self.other_symbol]["date"] == current_date
                ]["open"].iloc[0]
            )

            current_prices = {
                self.core_symbol: spy_open,
                self.alpha_symbol: nvda_open,
                self.other_symbol: gld_open,
            }
            morning_nav = self.portfolio.get_total_nav(current_prices)

            # 計算各標的日線 PSQ 與 EV Proxy
            # NVDA & GLD 動能與 EV
            nvda_close_hist = self.daily_data[self.alpha_symbol][
                self.daily_feat[self.alpha_symbol]["date"] < current_date
            ]["Close"]
            nvda_high_hist = self.daily_data[self.alpha_symbol][
                self.daily_feat[self.alpha_symbol]["date"] < current_date
            ]["High"]
            nvda_low_hist = self.daily_data[self.alpha_symbol][
                self.daily_feat[self.alpha_symbol]["date"] < current_date
            ]["Low"]
            nvda_psq = self._compute_psq_from_series(
                nvda_close_hist, nvda_high_hist, nvda_low_hist
            )

            gld_close_hist = self.daily_data[self.other_symbol][
                self.daily_feat[self.other_symbol]["date"] < current_date
            ]["Close"]
            gld_high_hist = self.daily_data[self.other_symbol][
                self.daily_feat[self.other_symbol]["date"] < current_date
            ]["High"]
            gld_low_hist = self.daily_data[self.other_symbol][
                self.daily_feat[self.other_symbol]["date"] < current_date
            ]["Low"]
            gld_psq = self._compute_psq_from_series(
                gld_close_hist, gld_high_hist, gld_low_hist
            )

            # EV proxy based on Expected Move (02_expected_move_and_max_pain.md):
            # EM_weekly = Spot * max(HV20, 0.15) * sqrt(7 / 365)
            # PSQ momentum weighting adjusts forward expected drift:
            # EV = (EM_weekly / Spot) * (PSQ / 50.0)
            nvda_hv = (
                float(nvda_prev_row["hv_rank"]) / 100.0
                if nvda_prev_row is not None
                else 0.35
            )
            nvda_hv_val = max(0.15, nvda_hv)
            nvda_em_pct = nvda_hv_val * math.sqrt(7.0 / 365.0)
            # 下行風險懲罰 (若跌破 Gamma Flip，施加 30% 懲罰)
            nvda_penalty = (
                0.30
                if (
                    nvda_prev_row is not None
                    and nvda_open < float(nvda_prev_row["sma20"])
                )
                else 0.0
            )
            nvda_ev = nvda_em_pct * (nvda_psq / 50.0) * (1.0 - nvda_penalty)

            gld_hv = (
                float(gld_prev_row["hv_rank"]) / 100.0
                if gld_prev_row is not None
                else 0.20
            )
            gld_hv_val = max(0.15, gld_hv)
            gld_em_pct = gld_hv_val * math.sqrt(7.0 / 365.0)
            gld_penalty = (
                0.30
                if (
                    gld_prev_row is not None and gld_open < float(gld_prev_row["sma20"])
                )
                else 0.0
            )
            gld_ev = gld_em_pct * (gld_psq / 50.0) * (1.0 - gld_penalty)

            # -----------------------------------------------------------------
            # 0. 情境五: FUNDAMENTAL_BROKEN (基本面護城河破滅清倉保護)
            # -----------------------------------------------------------------
            if date_str in self.fundamental_broken_events:
                broken_syms = self.fundamental_broken_events[date_str]
                for b_sym in broken_syms:
                    if self.portfolio.has_long(b_sym):
                        spot_now = current_prices.get(b_sym, 0.0)
                        self.portfolio.sell(
                            symbol=b_sym,
                            price=spot_now,
                            ratio=1.0,
                            scenario=RolloverScenario.FUNDAMENTAL_BROKEN.value,
                            reason="🚨 財報申報偵測到護城河破滅，基本面原型假設失效，強制 100% 清倉撤離",
                            timestamp=f"{date_str} 09:30:00",
                            date_str=date_str,
                        )
                        self.last_exit_date[b_sym] = current_date

            # -----------------------------------------------------------------
            # 2. 情境四: MARGIN_DEFENSE (大盤系統性危機防禦)
            # -----------------------------------------------------------------
            spy_gamma_flip = (
                float(spy_prev_row["sma20"]) if spy_prev_row is not None else spy_open
            )
            is_market_critical = vix_prev >= 25.0 or (
                vix_prev >= 20.0 and spy_open < spy_gamma_flip
            )

            if is_market_critical:
                sat_value = sum(
                    pos.current_value
                    for pos in self.portfolio.positions.values()
                    if pos.asset_class == "SATELLITE" and pos.side == "LONG"
                )
                cash_reserve = max(0.0, self.portfolio.cash)
                is_margin_stressed = sat_value > cash_reserve

                if is_margin_stressed:
                    # 檢查各衛星持倉是否「結構性無勝率」
                    for sat_sym in [self.alpha_symbol, self.other_symbol]:
                        sat_pos = self.portfolio.positions.get(sat_sym)
                        if sat_pos is not None and sat_pos.shares > 0:
                            row = (
                                nvda_prev_row
                                if sat_sym == self.alpha_symbol
                                else gld_prev_row
                            )
                            pw = float(row["low10"]) if row is not None else 0.0
                            gf = float(row["sma20"]) if row is not None else 0.0
                            spot_now = current_prices[sat_sym]
                            is_broken = (spot_now < pw) or (spot_now < gf)
                            if is_broken:
                                self.portfolio.sell(
                                    symbol=sat_sym,
                                    price=spot_now,
                                    ratio=1.0,
                                    scenario=RolloverScenario.MARGIN_DEFENSE.value,
                                    reason=f"🚨 大盤危機 (VIX={vix_prev:.1f}) + 個股破位 (${spot_now:.2f} < PW ${pw:.2f})，保證金防禦全額清倉",
                                    timestamp=f"{date_str} 09:30:00",
                                    date_str=date_str,
                                )

            # -------------------------------------------------------------
            # 3. 情境六: MACRO_TOP_ESCAPE_DEFENSE (宏觀逃頂防禦減碼 25%)
            # -------------------------------------------------------------
            # 當 VIX 嚴重倒掛或恐慌/亢奮合流，進行 25% 防禦性減碼至現金 (單一危機事件窗口僅觸發一次，防範連環削皮)
            if vix_prev >= 28.0:
                is_escape_cd = (
                    self.last_macro_escape_date is not None
                    and (current_date - self.last_macro_escape_date).days < 10
                )
                if not is_escape_cd:
                    triggered_escape = False
                    for sat_sym in [self.alpha_symbol, self.other_symbol]:
                        sat_pos = self.portfolio.positions.get(sat_sym)
                        if sat_pos is not None and sat_pos.shares > 0:
                            spot_now = current_prices[sat_sym]
                            self.portfolio.sell(
                                symbol=sat_sym,
                                price=spot_now,
                                ratio=_MACRO_TOP_ESCAPE_TRIM_RATIO,
                                scenario=RolloverScenario.MACRO_TOP_ESCAPE_DEFENSE.value,
                                reason=f"🛡️ 總經逃頂風控達 CRITICAL (VIX={vix_prev:.1f})，衛星減碼 {_MACRO_TOP_ESCAPE_TRIM_RATIO:.0%} 增持防禦現金",
                                timestamp=f"{date_str} 09:30:00",
                                date_str=date_str,
                            )
                            triggered_escape = True
                    if triggered_escape:
                        self.last_macro_escape_date = current_date

            # -----------------------------------------------------------------
            # 4. 情境一: CORE_DEPLOYMENT (核心超額再平衡與 Covered Call)
            # -----------------------------------------------------------------
            spy_pos = self.portfolio.positions.get(self.core_symbol)
            if spy_pos is not None and spy_pos.shares > 0 and morning_nav > 0:
                spy_alloc = spy_pos.current_value / morning_nav
                # 超額超過 50.5% (閾值 0.5%) 且超額金額達標 ($1,000 以上避免 dust trade)
                if (
                    spy_alloc > (self.core_target_weight + _CORE_EXCESS_MIN_TRADE_PCT)
                    and (
                        spy_pos.current_value - (morning_nav * self.core_target_weight)
                    )
                    >= 1000.0
                ):
                    excess_usd = spy_pos.current_value - (
                        morning_nav * self.core_target_weight
                    )
                    shares_to_trim = excess_usd / spy_open
                    trim_ratio = min(1.0, shares_to_trim / spy_pos.shares)

                    # 賣出 SPY 超額
                    self.portfolio.sell(
                        symbol=self.core_symbol,
                        price=spy_open,
                        ratio=trim_ratio,
                        scenario=RolloverScenario.CORE_DEPLOYMENT.value,
                        reason=f"SPY 核心配置升值至 {spy_alloc:.1%} (超額 ${excess_usd:,.0f})，執行超額再平衡",
                        timestamp=f"{date_str} 09:30:00",
                        date_str=date_str,
                    )

                    # 決定去向: 若 GLD 或 NVDA 處於突破動能 (>80)，且非做空中，部署 core_deploy_ratio 超額至候選，否則留存 CASH
                    deploy_usd = excess_usd * self.core_deploy_ratio
                    if (
                        gld_psq > _BREAKOUT_READY_THRESHOLD
                        and not is_market_critical
                        and not self.portfolio.has_short(self.other_symbol)
                    ):
                        gld_pw = (
                            float(gld_prev_row["low10"])
                            if gld_prev_row is not None
                            else gld_open * 0.95
                        )
                        gld_h60 = (
                            float(gld_prev_row["high60"])
                            if gld_prev_row is not None
                            else gld_open * 1.05
                        )
                        gld_atr1d = (
                            float(gld_prev_row["atr14"])
                            if gld_prev_row is not None
                            else gld_open * 0.015
                        )
                        gld_atr15m = gld_atr1d / math.sqrt(26.0)
                        gld_sl = compute_reference_stop(
                            gld_open, gld_pw, gld_atr15m, "LONG"
                        )
                        gld_target = max(gld_h60, gld_open + 3.0 * gld_atr1d)
                        self.portfolio.buy(
                            symbol=self.other_symbol,
                            asset_class="SATELLITE",
                            price=gld_open,
                            notional=deploy_usd,
                            scenario=RolloverScenario.CORE_DEPLOYMENT.value,
                            reason=f"SPY 超額資金分流至突破候選 GLD (PSQ={gld_psq:.0f})",
                            timestamp=f"{date_str} 09:30:00",
                            date_str=date_str,
                            anchor_base=gld_pw,
                            target_wall=gld_target,
                            stop_loss=gld_sl,
                            entry_regime="REGIME_III_RIGHT_MOMENTUM",
                        )
                    elif (
                        nvda_psq > _BREAKOUT_READY_THRESHOLD
                        and not is_market_critical
                        and not self.portfolio.has_short(self.alpha_symbol)
                    ):
                        nvda_pw = (
                            float(nvda_prev_row["low10"])
                            if nvda_prev_row is not None
                            else nvda_open * 0.95
                        )
                        nvda_h60 = (
                            float(nvda_prev_row["high60"])
                            if nvda_prev_row is not None
                            else nvda_open * 1.08
                        )
                        nvda_atr1d = (
                            float(nvda_prev_row["atr14"])
                            if nvda_prev_row is not None
                            else nvda_open * 0.02
                        )
                        nvda_atr15m = nvda_atr1d / math.sqrt(26.0)
                        nvda_sl = compute_reference_stop(
                            nvda_open, nvda_pw, nvda_atr15m, "LONG"
                        )
                        nvda_target = max(nvda_h60, nvda_open + 3.0 * nvda_atr1d)
                        self.portfolio.buy(
                            symbol=self.alpha_symbol,
                            asset_class="SATELLITE",
                            price=nvda_open,
                            notional=deploy_usd,
                            scenario=RolloverScenario.CORE_DEPLOYMENT.value,
                            reason=f"SPY 超額資金分流至突破候選 NVDA (PSQ={nvda_psq:.0f})",
                            timestamp=f"{date_str} 09:30:00",
                            date_str=date_str,
                            anchor_base=nvda_pw,
                            target_wall=nvda_target,
                            stop_loss=nvda_sl,
                            entry_regime="REGIME_III_RIGHT_MOMENTUM",
                        )

                # Covered Call Overlay 收益增強 (情境七)
                # 當 SPY 貼近 Call Wall 阻力 (spot >= call_wall * 0.98) 且非暴跌日，每週計入 0.08% 權利金增強
                spy_cw = (
                    float(spy_prev_row["high10"])
                    if spy_prev_row is not None
                    else spy_open * 1.05
                )
                if (
                    spy_open >= spy_cw * 0.98
                    and spy_pos.shares >= 50.0
                    and (d_idx % 5 == 0)
                ):
                    premium_yield = (
                        spy_pos.current_value * 0.0008
                    )  # ~0.4% 月化期權權利金收益
                    self.portfolio.add_income(
                        symbol=self.core_symbol,
                        amount=premium_yield,
                        scenario=RolloverScenario.COVERED_CALL_PROFIT_LOCK.value,
                        reason=f"SPY 觸及頂部做市商阻力牆 (${spy_cw:.2f})，覆蓋賣出 OTM Covered Call 收益",
                        timestamp=f"{date_str} 09:30:00",
                        date_str=date_str,
                    )

            # -------------------------------------------------------------
            # 5. 情境二: OPPORTUNITY_COST (NVDA ↔ GLD 機會成本動能輪動)
            # -------------------------------------------------------------
            # 檢驗 NVDA (衰退) -> GLD (突破)
            nvda_pos = self.portfolio.positions.get(self.alpha_symbol)
            nvda_rot_cd = self.last_opp_cost_date.get(self.alpha_symbol)
            can_rot_nvda = (
                nvda_rot_cd is None
                or (current_date - nvda_rot_cd).days >= self.opp_cost_cd_days
            )

            if (
                nvda_pos is not None
                and nvda_pos.shares > 0
                and can_rot_nvda
                and not self.portfolio.has_short(self.other_symbol)
            ):
                ev_spread_gld = gld_ev - nvda_ev
                if (
                    nvda_psq < _MOMENTUM_DECAY_THRESHOLD
                    and gld_psq > _BREAKOUT_READY_THRESHOLD
                    and ev_spread_gld > self.opp_cost_hurdle
                ):
                    rot_ratio = (
                        _ROLLOVER_RATIO_HIGH_PROFIT
                        if nvda_pos.return_pct > _PROFIT_LOCK_PROFIT_PCT_THRESHOLD
                        else _ROLLOVER_RATIO_STANDARD
                    )
                    if (
                        nvda_pos.current_value < 1000.0
                        or (nvda_pos.current_value * (1.0 - rot_ratio)) < 500.0
                    ):
                        rot_ratio = 1.0
                    trade = self.portfolio.sell(
                        symbol=self.alpha_symbol,
                        price=nvda_open,
                        ratio=rot_ratio,
                        scenario=RolloverScenario.OPPORTUNITY_COST.value,
                        reason=f"NVDA 動能衰竭 (PSQ={nvda_psq:.0f}) 轉倉至突破標的 GLD (PSQ={gld_psq:.0f}, ΔEV=+{ev_spread_gld*100:.1f}%)",
                        timestamp=f"{date_str} 09:30:00",
                        date_str=date_str,
                    )
                    if trade is not None:
                        self.last_opp_cost_date[self.alpha_symbol] = current_date
                        proceeds = trade.notional - trade.fee
                        gld_pw = (
                            float(gld_prev_row["low10"])
                            if gld_prev_row is not None
                            else gld_open * 0.95
                        )
                        gld_h60 = (
                            float(gld_prev_row["high60"])
                            if gld_prev_row is not None
                            else gld_open * 1.05
                        )
                        gld_atr1d = (
                            float(gld_prev_row["atr14"])
                            if gld_prev_row is not None
                            else gld_open * 0.015
                        )
                        gld_atr15m = gld_atr1d / math.sqrt(26.0)
                        gld_sl = compute_reference_stop(
                            gld_open, gld_pw, gld_atr15m, "LONG"
                        )
                        gld_target = max(gld_h60, gld_open + 3.0 * gld_atr1d)
                        self.portfolio.buy(
                            symbol=self.other_symbol,
                            asset_class="SATELLITE",
                            price=gld_open,
                            notional=proceeds,
                            scenario=RolloverScenario.OPPORTUNITY_COST.value,
                            reason="機會成本轉倉買入 GLD (接收 NVDA 輪動資金)",
                            timestamp=f"{date_str} 09:30:00",
                            date_str=date_str,
                            anchor_base=gld_pw,
                            target_wall=gld_target,
                            stop_loss=gld_sl,
                            entry_regime="REGIME_III_RIGHT_MOMENTUM",
                        )

            # 檢驗 GLD (衰退) -> NVDA (突破)
            gld_pos = self.portfolio.positions.get(self.other_symbol)
            gld_rot_cd = self.last_opp_cost_date.get(self.other_symbol)
            can_rot_gld = (
                gld_rot_cd is None
                or (current_date - gld_rot_cd).days >= self.opp_cost_cd_days
            )

            if (
                gld_pos is not None
                and gld_pos.shares > 0
                and can_rot_gld
                and not self.portfolio.has_short(self.alpha_symbol)
            ):
                ev_spread_nvda = nvda_ev - gld_ev
                if (
                    gld_psq < _MOMENTUM_DECAY_THRESHOLD
                    and nvda_psq > _BREAKOUT_READY_THRESHOLD
                    and ev_spread_nvda > self.opp_cost_hurdle
                ):
                    rot_ratio = (
                        _ROLLOVER_RATIO_HIGH_PROFIT
                        if gld_pos.return_pct > _PROFIT_LOCK_PROFIT_PCT_THRESHOLD
                        else _ROLLOVER_RATIO_STANDARD
                    )
                    if (
                        gld_pos.current_value < 1000.0
                        or (gld_pos.current_value * (1.0 - rot_ratio)) < 500.0
                    ):
                        rot_ratio = 1.0
                    trade = self.portfolio.sell(
                        symbol=self.other_symbol,
                        price=gld_open,
                        ratio=rot_ratio,
                        scenario=RolloverScenario.OPPORTUNITY_COST.value,
                        reason=f"GLD 動能衰竭 (PSQ={gld_psq:.0f}) 轉倉至突破標的 NVDA (PSQ={nvda_psq:.0f}, ΔEV=+{ev_spread_nvda*100:.1f}%)",
                        timestamp=f"{date_str} 09:30:00",
                        date_str=date_str,
                    )
                    if trade is not None:
                        self.last_opp_cost_date[self.other_symbol] = current_date
                        proceeds = trade.notional - trade.fee
                        nvda_pw = (
                            float(nvda_prev_row["low10"])
                            if nvda_prev_row is not None
                            else nvda_open * 0.95
                        )
                        nvda_h60 = (
                            float(nvda_prev_row["high60"])
                            if nvda_prev_row is not None
                            else nvda_open * 1.08
                        )
                        nvda_atr1d = (
                            float(nvda_prev_row["atr14"])
                            if nvda_prev_row is not None
                            else nvda_open * 0.02
                        )
                        nvda_atr15m = nvda_atr1d / math.sqrt(26.0)
                        nvda_sl = compute_reference_stop(
                            nvda_open, nvda_pw, nvda_atr15m, "LONG"
                        )
                        nvda_target = max(nvda_h60, nvda_open + 3.0 * nvda_atr1d)
                        self.portfolio.buy(
                            symbol=self.alpha_symbol,
                            asset_class="SATELLITE",
                            price=nvda_open,
                            notional=proceeds,
                            scenario=RolloverScenario.OPPORTUNITY_COST.value,
                            reason="機會成本轉倉買入 NVDA (接收 GLD 輪動資金)",
                            timestamp=f"{date_str} 09:30:00",
                            date_str=date_str,
                            anchor_base=nvda_pw,
                            target_wall=nvda_target,
                            stop_loss=nvda_sl,
                            entry_regime="REGIME_III_RIGHT_MOMENTUM",
                        )

            # -----------------------------------------------------------------
            # 6. 盤中小時線走訪迴圈: SL1-4 / TP1-3 / TRANSITION / SHORT_ENTRY
            # -----------------------------------------------------------------
            # 取得當日所有小時 K 線
            spy_hours = self.hourly_feat[self.core_symbol][
                self.hourly_feat[self.core_symbol]["date"] == current_date
            ]
            nvda_hours = self.hourly_feat[self.alpha_symbol][
                self.hourly_feat[self.alpha_symbol]["date"] == current_date
            ]
            gld_hours = self.hourly_feat[self.other_symbol][
                self.hourly_feat[self.other_symbol]["date"] == current_date
            ]

            num_bars = min(len(spy_hours), len(nvda_hours), len(gld_hours))

            for b_idx in range(num_bars):
                nvda_bar = nvda_hours.iloc[b_idx]
                gld_bar = gld_hours.iloc[b_idx]

                ts_str = str(spy_hours.index[b_idx])

                # -------------------------------------------------------------
                # 6.1 情境八: TRANSITION_ENGINE (左側接刀演化為右側動能)
                # -------------------------------------------------------------
                for sym, bar in [
                    (self.alpha_symbol, nvda_bar),
                    (self.other_symbol, gld_bar),
                ]:
                    pos = self.portfolio.positions.get(sym)
                    if (
                        pos is not None
                        and pos.side == "LONG"
                        and pos.entry_regime == "REGIME_I_LEFT_CATCH"
                    ):
                        c_val = float(bar["close"])
                        vwap = float(bar["session_vwap"])
                        gf = float(bar["sma20_prev"])
                        vol_r = float(bar["vol_ratio"])
                        # 帶量站上 VWAP 與 Gamma Flip
                        if (
                            c_val > vwap
                            and c_val > gf
                            and vol_r >= 1.2
                            and not pos.is_pyramided
                        ):
                            # 上移停損至保本
                            pos.stop_loss = max(pos.avg_cost, pos.anchor_base)
                            pos.is_pyramided = True
                            pos.entry_regime = "REGIME_III_RIGHT_MOMENTUM"
                            # 授權 Pyramiding 加碼 20%
                            add_notional = pos.current_value * 0.20
                            if self.portfolio.cash >= add_notional:
                                self.portfolio.buy(
                                    symbol=sym,
                                    asset_class="SATELLITE",
                                    price=c_val,
                                    notional=add_notional,
                                    scenario=RolloverScenario.TRANSITION_ENGINE.value,
                                    reason=f"⚡ 左側接刀部位演化站穩 VWAP (${vwap:.2f}) 與 Gamma Flip (${gf:.2f})，啟動保本停損與動能加碼",
                                    timestamp=ts_str,
                                    date_str=date_str,
                                )

                # -------------------------------------------------------------
                # 6.1.1 自選股分析中心/心跳 Regime 路由進場 (閒置衛星資金部署)
                # -------------------------------------------------------------
                if not is_market_critical:
                    cash_above_reserve = max(
                        0.0,
                        self.portfolio.cash - (morning_nav * self.cash_target_weight),
                    )
                    for sym, bar, target_wt in [
                        (self.alpha_symbol, nvda_bar, self.alpha_target_weight),
                        (self.other_symbol, gld_bar, self.other_target_weight),
                    ]:
                        pos = self.portfolio.positions.get(sym)
                        # 檢查轉倉/停損冷卻期 (Cooldown): 停損出場後 3 個自然日內不重複躁進；做空中嚴禁買多
                        last_exit = self.last_exit_date.get(sym)
                        in_cooldown = (
                            last_exit is not None
                            and (current_date - last_exit).days < 3
                        )
                        is_shorted = self.portfolio.has_short(sym)

                        if (
                            (pos is None or pos.shares <= 0)
                            and not in_cooldown
                            and not is_shorted
                            and cash_above_reserve > 1000.0
                        ):
                            effective_wt = (
                                self.max_satellite_budget_pct
                                if self.mode == "aggressive"
                                else target_wt
                            )
                            budget = min(cash_above_reserve, morning_nav * effective_wt)
                            c_val = float(bar["close"])
                            vwap = float(bar["session_vwap"])
                            gf = float(bar["sma20_prev"])
                            h10 = float(bar["high10_prev"])
                            h60 = float(bar["high60_prev"])
                            pw = float(bar["low10_prev"])
                            atr1d = float(bar["atr14_prev"])
                            atr1h = float(bar["atr_1h"])
                            atr_15m_eq = atr1h / 2.0
                            vol_r = float(bar["vol_ratio"])
                            rsi_val = float(bar["rsi"])
                            is_bull = c_val > float(bar["open"])

                            # 鋼鐵底牆與自適應空間門檻計算
                            # 右側動能突破：突破 high10_prev，以 high10_prev 為支撐錨點，以 high60_prev 或晴空萬里擴展為目標天花板
                            eff_target = max(h60, c_val + 3.0 * atr1d)
                            target_room = (eff_target - c_val) / c_val
                            buffer_eval = evaluate_wall_buffer(
                                c_val, h10, atr_15m_eq, atr1d, profile="RIGHT"
                            )
                            room_eval = compute_dynamic_room_threshold(
                                c_val, h10, atr_15m_eq, atr1d, direction="LONG"
                            )
                            sl_candidate = compute_reference_stop(
                                c_val, h10, atr_15m_eq, "LONG"
                            )

                            min_vol_r = 1.15 if self.mode == "aggressive" else 1.25
                            min_rsi = 50.0 if self.mode == "aggressive" else 52.0

                            # Regime III: 右側動能突破六重鐵律確認 (必須實質站穩 high10_prev 與 Flip 之上，且距目標阻力具備充足非對稱空間)
                            regime_iii_ok = (
                                c_val > vwap
                                and is_bull
                                and vol_r >= min_vol_r
                                and rsi_val > min_rsi
                                and c_val > h10
                                and c_val > gf
                                and target_room >= room_eval.threshold_pct
                                and buffer_eval.state != "TOO_TIGHT"
                            )
                            # Regime III-B: 右側趨勢延續 (非突破瞬間)。判定優先序
                            # 必須在 Regime III 之後——突破當下兩者都會成立，
                            # 歸類為 III 才不會把更強的進場證據降級。
                            regime_iii_b_ok = False
                            held_bars_1h = 0
                            if self.enable_trend_continuation and not regime_iii_ok:
                                window = (
                                    nvda_hours
                                    if sym == self.alpha_symbol
                                    else gld_hours
                                )
                                lo = max(0, b_idx + 1 - _BT_TREND_CONT_LOOKBACK_BARS_1H)
                                win = window.iloc[lo : b_idx + 1]
                                if len(win) >= _BT_TREND_CONT_LOOKBACK_BARS_1H:
                                    level = max(gf, vwap)
                                    held_bars_1h = int((win["close"] > level).sum())
                                    regime_iii_b_ok = (
                                        held_bars_1h >= _BT_TREND_CONT_MIN_HELD_BARS_1H
                                        and c_val > vwap
                                        and c_val > gf
                                        and c_val > h10
                                        and 50.0 < rsi_val < 78.0
                                        and target_room >= room_eval.threshold_pct
                                        and buffer_eval.state != "TOO_TIGHT"
                                    )

                            # Regime I: 左側超跌均值回歸接刀確認
                            dist_pw = (c_val - pw) / c_val if c_val > 0 else 0.0
                            regime_i_ok = (
                                rsi_val <= 32.0
                                and c_val <= vwap - 1.2 * atr_15m_eq
                                and -0.015 <= dist_pw <= 0.02
                                and c_val > (pw * 0.985)
                            )

                            if regime_iii_ok:
                                self.portfolio.buy(
                                    symbol=sym,
                                    asset_class="SATELLITE",
                                    price=c_val,
                                    notional=budget,
                                    scenario="REGIME_III_MOMENTUM",
                                    reason=f"🚀 標的分析中心確認右側動能突破 (${c_val:.2f} 突破 H10 ${h10:.2f}, RSI={rsi_val:.1f}, 空間={target_room:.1%})",
                                    timestamp=ts_str,
                                    date_str=date_str,
                                    anchor_base=h10,
                                    target_wall=eff_target,
                                    stop_loss=sl_candidate,
                                    entry_regime="REGIME_III_RIGHT_MOMENTUM",
                                )
                                cash_above_reserve -= budget
                            elif regime_iii_b_ok:
                                self.portfolio.buy(
                                    symbol=sym,
                                    asset_class="SATELLITE",
                                    price=c_val,
                                    notional=budget,
                                    scenario="REGIME_III_B_TREND_CONT",
                                    reason=(
                                        f"🚀 趨勢延續進場 (${c_val:.2f} 持續站穩結構 "
                                        f"{held_bars_1h}/{_BT_TREND_CONT_LOOKBACK_BARS_1H} 根, "
                                        f"RSI={rsi_val:.1f}, 空間={target_room:.1%})"
                                    ),
                                    timestamp=ts_str,
                                    date_str=date_str,
                                    anchor_base=h10,
                                    target_wall=eff_target,
                                    stop_loss=sl_candidate,
                                    entry_regime="REGIME_III_B_TREND_CONTINUATION",
                                )
                                cash_above_reserve -= budget
                            elif regime_i_ok:
                                self.portfolio.buy(
                                    symbol=sym,
                                    asset_class="SATELLITE",
                                    price=c_val,
                                    notional=budget * 0.6,
                                    scenario="REGIME_I_CATCH",
                                    reason=f"🎣 標的分析中心確認左側接刀超跌 (${c_val:.2f} 貼近 Put Wall ${pw:.2f}, RSI={rsi_val:.1f})",
                                    timestamp=ts_str,
                                    date_str=date_str,
                                    anchor_base=pw,
                                    target_wall=h10,
                                    stop_loss=pw * 0.985,
                                    entry_regime="REGIME_I_LEFT_CATCH",
                                )
                                cash_above_reserve -= budget * 0.6

                # -------------------------------------------------------------
                # 6.2 情境九: SHORT_ENTRY (Regime V 破位追空獨立做空)
                # -------------------------------------------------------------
                # 檢驗 NVDA 是否出現 Regime V 追空訊號 (若已有空頭或多頭部位則跳過)
                short_pos_key = f"{self.alpha_symbol}_SHORT"
                if not self.portfolio.has_short(self.alpha_symbol):
                    nvda_c = float(nvda_bar["close"])
                    nvda_pw = float(nvda_bar["low10_prev"])
                    nvda_gf = float(nvda_bar["sma20_prev"])
                    nvda_l60 = float(nvda_bar["low60_prev"])
                    nvda_atr1d = float(nvda_bar["atr14_prev"])
                    nvda_atr1h = float(nvda_bar["atr_1h"])
                    nvda_rsi = float(nvda_bar["rsi"])
                    nvda_vwap = float(nvda_bar["session_vwap"])
                    nvda_vol_r = float(nvda_bar["vol_ratio"])

                    # 破位追空六重鐵律條件驗證
                    is_breakdown_regime_v = (
                        nvda_c < nvda_vwap
                        and nvda_c < nvda_pw
                        and nvda_c < nvda_gf
                        and nvda_rsi < 45.0
                        and nvda_vol_r >= 1.3
                        and (nvda_c - nvda_l60) >= (1.8 * nvda_atr1d)
                    )
                    if is_breakdown_regime_v:
                        # 若尚持有微量殘存多頭 (< $500)，先清空碎片以放行做空
                        if self.portfolio.has_long(self.alpha_symbol):
                            long_pos = self.portfolio.positions.get(self.alpha_symbol)
                            if long_pos is not None and long_pos.current_value < 500.0:
                                self.portfolio.sell(
                                    symbol=self.alpha_symbol,
                                    price=nvda_c,
                                    ratio=1.0,
                                    scenario=RolloverScenario.SATELLITE_REBALANCE.value,
                                    reason=f"🚨 偵測到破位追空訊號，清理微量殘存多頭 (${long_pos.current_value:.2f}) 以放行做空",
                                    timestamp=ts_str,
                                    date_str=date_str,
                                )

                        if not self.portfolio.has_long(self.alpha_symbol):
                            # 構建 ShortEntryEvaluation
                            ev = ShortEntryEvaluation(
                                all_passed=True,
                                reason="2025 破位追空微觀結構確認",
                                structure_directive="SHORT_EQUITY",
                                sub_mode="破位追空",
                                conditions=(True, True, True, True, True, True),
                                spot=nvda_c,
                                resistance_wall=nvda_gf,
                                call_wall=float(nvda_bar["high10_prev"]),
                                put_wall=nvda_pw,
                                gamma_flip=nvda_gf,
                                next_negative_node=nvda_l60,
                                net_gex=-1.5,
                                session_vwap=nvda_vwap,
                                atr_15m=nvda_atr1h / 2.0,
                                atr_1d=nvda_atr1d,
                                ivr=35.0,
                            )
                            levels = build_short_entry_levels(ev)
                            if levels is not None and levels.reward_risk_ratio >= 1.8:
                                sizing = compute_short_entry_sizing(
                                    levels=levels,
                                    capital=morning_nav,
                                    risk_limit_pct=_SHORT_ENTRY_ACCOUNT_RISK_PCT,
                                    vix_spot=vix_prev,
                                    rsi_15m=nvda_rsi,
                                )
                                if sizing.share_qty > 0:
                                    self.portfolio.short(
                                        symbol=self.alpha_symbol,
                                        price=nvda_c,
                                        shares=float(sizing.share_qty),
                                        stop_price=levels.stop_price,
                                        target_price=levels.target_price,
                                        scenario=RolloverScenario.SHORT_ENTRY.value,
                                        reason=f"Regime V 破位追空 (跌破底牆 ${nvda_pw:.2f} 與 Flip ${nvda_gf:.2f}，盈虧比 {levels.reward_risk_ratio:.2f}:1)",
                                        timestamp=ts_str,
                                        date_str=date_str,
                                    )

                # -------------------------------------------------------------
                # 6.3 空頭部位鏡像微觀結構出場 (COVER / SL / TP)
                # -------------------------------------------------------------
                active_short = self.portfolio.positions.get(short_pos_key)
                if active_short is not None:
                    nvda_h = float(nvda_bar["close"])
                    # 停損觸發: 價格反彈突破阻力防守線
                    if nvda_h >= active_short.stop_loss and active_short.stop_loss > 0:
                        self.portfolio.cover(
                            symbol=self.alpha_symbol,
                            price=nvda_h,
                            ratio=1.0,
                            scenario=RolloverScenario.SATELLITE_REBALANCE.value,
                            reason=f"🚨 做空部位觸發結構停損 (${nvda_h:.2f} >= ${active_short.stop_loss:.2f})",
                            timestamp=ts_str,
                            date_str=date_str,
                        )
                    # 獲利了結觸發: 價格到達次級負 GEX 節點
                    elif (
                        nvda_h <= active_short.anchor_base
                        and active_short.anchor_base > 0
                    ):
                        self.portfolio.cover(
                            symbol=self.alpha_symbol,
                            price=nvda_h,
                            ratio=1.0,
                            scenario=RolloverScenario.SATELLITE_REBALANCE.value,
                            reason=f"🎯 做空部位到達目標價 (${nvda_h:.2f} <= ${active_short.anchor_base:.2f}) 全額獲利了結",
                            timestamp=ts_str,
                            date_str=date_str,
                        )

                # -------------------------------------------------------------
                # 6.4 情境三: SATELLITE_REBALANCE (微觀結構雙軌防洗盤 SL1-4 / TP1-3)
                # -------------------------------------------------------------
                for sat_sym, bar in [
                    (self.alpha_symbol, nvda_bar),
                    (self.other_symbol, gld_bar),
                ]:
                    pos = self.portfolio.positions.get(sat_sym)
                    if pos is None or pos.side != "LONG" or pos.shares <= 0:
                        continue

                    spot_val = float(bar["close"])
                    cw_val = (
                        pos.target_wall
                        if pos.target_wall > 0
                        else float(bar["high10_prev"])
                    )
                    if cw_val <= pos.anchor_base * 1.035:
                        cw_val = max(float(bar["high10_prev"]), pos.anchor_base * 1.05)
                    pw_val = float(bar["low10_prev"])
                    gf_val = float(bar["sma20_prev"])
                    atr_15m_val = float(bar["atr_1h"]) / 2.0
                    net_gex_val = (spot_val - gf_val) / float(bar["atr14_prev"])

                    # 更新錨點與停損
                    if pos.anchor_base <= 0:
                        pos.anchor_base = pw_val
                    if pos.stop_loss <= 0:
                        pos.stop_loss = compute_reference_stop(
                            spot_val, pos.anchor_base, atr_15m_val, "LONG"
                        )

                    # SL1: 結構失效 (跌破 stop_loss)
                    if spot_val < pos.stop_loss and pos.stop_loss > 0:
                        self.portfolio.sell(
                            symbol=sat_sym,
                            price=spot_val,
                            ratio=1.0,
                            scenario=RolloverScenario.SATELLITE_REBALANCE.value,
                            reason=f"🚨 SL1 結構失效：現價 ${spot_val:.2f} 跌破防守線 ${pos.stop_loss:.2f}，強制 100% 平倉",
                            timestamp=ts_str,
                            date_str=date_str,
                        )
                        self.last_exit_date[sat_sym] = current_date
                        continue

                    # SL2: 狀態翻轉 (Net GEX 翻負且持倉處於虧損中)
                    if (
                        net_gex_val <= _MICROSTRUCTURE_SL_NET_GEX_THRESHOLD
                        and pos.return_pct < -0.02
                    ):
                        self.portfolio.sell(
                            symbol=sat_sym,
                            price=spot_val,
                            ratio=1.0,
                            scenario=RolloverScenario.SATELLITE_REBALANCE.value,
                            reason=f"🚨 SL2 狀態翻轉：Net GEX ({net_gex_val:.2f}) <= {_MICROSTRUCTURE_SL_NET_GEX_THRESHOLD:.1f} 進入負 Gamma 區且持倉虧損，強制平倉",
                            timestamp=ts_str,
                            date_str=date_str,
                        )
                        self.last_exit_date[sat_sym] = current_date
                        continue

                    # SL4: 動態保本 (進展 >= 50% 空間)
                    if cw_val > pos.anchor_base > 0:
                        progress = (spot_val - pos.anchor_base) / (
                            cw_val - pos.anchor_base
                        )
                        if (
                            progress
                            >= _MICROSTRUCTURE_SL_TRAILING_CALLWALL_PROGRESS_PCT
                            and pos.stop_loss < pos.avg_cost
                        ):
                            pos.stop_loss = max(pos.avg_cost, pos.anchor_base)

                    # TP 分層檢查 (優先序: TP3 -> TP2 -> TP1，單輪只觸發單一最高階層)
                    is_tp3 = (not pos.tp3_triggered) and (
                        float(bar["rsi"]) >= 75.0
                        and spot_val < float(bar["session_vwap"])
                    )
                    is_tp2 = (not pos.tp2_triggered) and (
                        spot_val >= cw_val * (1.0 + _MICROSTRUCTURE_TP2_WALL_BREAK_PCT)
                    )
                    is_tp1 = (not pos.tp1_triggered) and (
                        spot_val >= cw_val * _MICROSTRUCTURE_TP1_CALLWALL_PCT
                    )

                    if is_tp3:
                        pos.tp3_triggered = True
                        self.portfolio.sell(
                            symbol=sat_sym,
                            price=spot_val,
                            ratio=_MICROSTRUCTURE_TP3_RATIO,
                            scenario=RolloverScenario.SATELLITE_REBALANCE.value,
                            reason=f"🎯 TP3 終局平倉：超買極值 (RSI={bar['rsi']:.1f}) 且失守 Session VWAP，平倉 {_MICROSTRUCTURE_TP3_RATIO:.0%}",
                            timestamp=ts_str,
                            date_str=date_str,
                        )
                    elif is_tp2:
                        pos.tp2_triggered = True
                        self.portfolio.sell(
                            symbol=sat_sym,
                            price=spot_val,
                            ratio=_MICROSTRUCTURE_TP2_RATIO,
                            scenario=RolloverScenario.SATELLITE_REBALANCE.value,
                            reason=f"🎯 TP2 空間擴展：現價 ${spot_val:.2f} 實質穿越 Call Wall ${cw_val:.2f} (+{_MICROSTRUCTURE_TP2_WALL_BREAK_PCT:.1%})，平倉 {_MICROSTRUCTURE_TP2_RATIO:.0%}",
                            timestamp=ts_str,
                            date_str=date_str,
                        )
                    elif is_tp1:
                        pos.tp1_triggered = True
                        self.portfolio.sell(
                            symbol=sat_sym,
                            price=spot_val,
                            ratio=self.tp1_ratio,
                            scenario=RolloverScenario.SATELLITE_REBALANCE.value,
                            reason=f"🎯 TP1 阻力初探：現價 ${spot_val:.2f} 達阻力牆 ${cw_val:.2f} 之 {_MICROSTRUCTURE_TP1_CALLWALL_PCT:.1%}，平倉 {self.tp1_ratio:.0%}",
                            timestamp=ts_str,
                            date_str=date_str,
                        )

            # -----------------------------------------------------------------
            # 7. 收盤計算 Daily NAV 與 Benchmark NAV
            # -----------------------------------------------------------------
            spy_close = float(
                self.daily_feat[self.core_symbol][
                    self.daily_feat[self.core_symbol]["date"] == current_date
                ]["close"].iloc[0]
            )
            nvda_close = float(
                self.daily_feat[self.alpha_symbol][
                    self.daily_feat[self.alpha_symbol]["date"] == current_date
                ]["close"].iloc[0]
            )
            gld_close = float(
                self.daily_feat[self.other_symbol][
                    self.daily_feat[self.other_symbol]["date"] == current_date
                ]["close"].iloc[0]
            )

            close_prices = {
                self.core_symbol: spy_close,
                self.alpha_symbol: nvda_close,
                self.other_symbol: gld_close,
            }
            daily_nav = self.portfolio.get_total_nav(close_prices)

            # 基準投組合約價值
            bench_spy_val = self.benchmark_shares[self.core_symbol] * spy_close
            bench_nvda_val = self.benchmark_shares[self.alpha_symbol] * nvda_close
            bench_gld_val = self.benchmark_shares[self.other_symbol] * gld_close
            bench_nav = (
                self.benchmark_cash + bench_spy_val + bench_nvda_val + bench_gld_val
            )
            self.benchmark_history.append(bench_nav)

            # 計算當日報酬率
            daily_ret = (daily_nav - prev_nav) / prev_nav if prev_nav > 0 else 0.0
            bench_ret = (
                (bench_nav - prev_bench_nav) / prev_bench_nav
                if prev_bench_nav > 0
                else 0.0
            )
            prev_nav = daily_nav
            prev_bench_nav = bench_nav

            # 持倉權重計算
            spy_val = (
                self.portfolio.positions[self.core_symbol].current_value
                if self.core_symbol in self.portfolio.positions
                else 0.0
            )
            if self.portfolio.has_long(self.alpha_symbol):
                nvda_val = self.portfolio.positions[self.alpha_symbol].current_value
            elif self.portfolio.has_short(self.alpha_symbol):
                short_key = f"{self.alpha_symbol}_SHORT"
                nvda_val = (
                    -abs(self.portfolio.positions[short_key].shares)
                    * close_prices[self.alpha_symbol]
                )
            else:
                nvda_val = 0.0
            gld_val = (
                self.portfolio.positions[self.other_symbol].current_value
                if self.other_symbol in self.portfolio.positions
                else 0.0
            )

            mkt_regime = "CRISIS" if is_market_critical else "NORMAL"

            self.portfolio.daily_history.append(
                DailyNavRecord(
                    date=date_str,
                    nav=daily_nav,
                    cash=self.portfolio.cash,
                    positions_value=daily_nav - self.portfolio.cash,
                    benchmark_nav=bench_nav,
                    spy_weight=spy_val / daily_nav if daily_nav > 0 else 0.0,
                    nvda_weight=nvda_val / daily_nav if daily_nav > 0 else 0.0,
                    gld_weight=gld_val / daily_nav if daily_nav > 0 else 0.0,
                    cash_weight=self.portfolio.cash / daily_nav
                    if daily_nav > 0
                    else 0.0,
                    vix=vix_prev,
                    market_regime=mkt_regime,
                    daily_return=daily_ret,
                    benchmark_daily_return=bench_ret,
                )
            )

    def calculate_metrics(self) -> BacktestMetrics:
        """計算完備的量化指標與基準對比。"""
        if not self.portfolio.daily_history:
            raise RuntimeError("尚未執行回測模擬")

        navs = [rec.nav for rec in self.portfolio.daily_history]
        bench_navs = [rec.benchmark_nav for rec in self.portfolio.daily_history]
        daily_returns = [rec.daily_return for rec in self.portfolio.daily_history[1:]]
        bench_returns = [
            rec.benchmark_daily_return for rec in self.portfolio.daily_history[1:]
        ]

        days = len(self.portfolio.daily_history)
        years = max(1e-4, days / 252.0)

        # 報酬指標 (以初始本金為精算基準)
        total_return = (navs[-1] - self.initial_capital) / self.initial_capital
        cagr = (navs[-1] / self.initial_capital) ** (1.0 / years) - 1.0

        bench_total_return = (
            bench_navs[-1] - self.initial_capital
        ) / self.initial_capital
        bench_cagr = (bench_navs[-1] / self.initial_capital) ** (1.0 / years) - 1.0

        # 波動率 (年化)
        ret_arr = np.array(daily_returns)
        bench_ret_arr = np.array(bench_returns)

        vol = float(np.std(ret_arr) * np.sqrt(252)) if len(ret_arr) > 1 else 0.0
        bench_vol = (
            float(np.std(bench_ret_arr) * np.sqrt(252))
            if len(bench_ret_arr) > 1
            else 0.0
        )

        # 無風險利率 (2025 年以 4.5% 為基準)
        rf = 0.045
        sharpe = (cagr - rf) / vol if vol > 0 else 0.0
        bench_sharpe = (bench_cagr - rf) / bench_vol if bench_vol > 0 else 0.0

        # 下行波動率與 Sortino
        downside_rets = ret_arr[ret_arr < 0]
        downside_vol = (
            float(np.sqrt(np.mean(downside_rets**2)) * np.sqrt(252))
            if len(downside_rets) > 0
            else 1e-4
        )
        sortino = (cagr - rf) / downside_vol if downside_vol > 0 else 0.0

        bench_downside = bench_ret_arr[bench_ret_arr < 0]
        bench_downside_vol = (
            float(np.sqrt(np.mean(bench_downside**2)) * np.sqrt(252))
            if len(bench_downside) > 0
            else 1e-4
        )
        bench_sortino = (
            (bench_cagr - rf) / bench_downside_vol if bench_downside_vol > 0 else 0.0
        )

        # 最大回撤 (MDD)
        cummax_nav = np.maximum.accumulate(np.array(navs))
        drawdowns = (cummax_nav - np.array(navs)) / cummax_nav
        max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

        cummax_bench = np.maximum.accumulate(np.array(bench_navs))
        bench_drawdowns = (cummax_bench - np.array(bench_navs)) / cummax_bench
        bench_max_dd = (
            float(np.max(bench_drawdowns)) if len(bench_drawdowns) > 0 else 0.0
        )

        calmar = cagr / max_dd if max_dd > 0 else 0.0
        bench_calmar = bench_cagr / bench_max_dd if bench_max_dd > 0 else 0.0

        # 交易統計
        realized_trades = [
            t
            for t in self.portfolio.trades
            if t.action in ("SELL", "COVER", "INCOME") and t.scenario != "INIT"
        ]
        win_trades = [t for t in realized_trades if t.realized_pnl > 0]
        loss_trades = [t for t in realized_trades if t.realized_pnl < 0]

        win_rate = len(win_trades) / len(realized_trades) if realized_trades else 0.0
        gross_profit = sum(t.realized_pnl for t in win_trades)
        gross_loss = abs(sum(t.realized_pnl for t in loss_trades))
        profit_factor = (
            gross_profit / gross_loss
            if gross_loss > 0
            else (gross_profit if gross_profit > 0 else 0.0)
        )

        # 9 大情境觸發統計
        scenario_stats: dict[str, dict[str, Any]] = dict()
        for t in self.portfolio.trades:
            if t.scenario == "INIT":
                continue
            sc = t.scenario
            if sc not in scenario_stats:
                scenario_stats[sc] = {
                    "count": 0,
                    "total_pnl": 0.0,
                    "win_count": 0,
                    "loss_count": 0,
                    "total_notional": 0.0,
                }
            scenario_stats[sc]["count"] += 1
            scenario_stats[sc]["total_pnl"] += t.realized_pnl
            scenario_stats[sc]["total_notional"] += t.notional
            if t.realized_pnl > 0:
                scenario_stats[sc]["win_count"] += 1
            elif t.realized_pnl < 0:
                scenario_stats[sc]["loss_count"] += 1

        # 月度報酬率分佈
        monthly_returns: dict[str, float] = dict()
        benchmark_monthly_returns: dict[str, float] = dict()

        df_daily = pd.DataFrame(
            [
                {
                    "date": pd.to_datetime(r.date),
                    "nav": r.nav,
                    "bench_nav": r.benchmark_nav,
                }
                for r in self.portfolio.daily_history
            ]
        ).set_index("date")

        for month, grp in df_daily.groupby(pd.Grouper(freq="ME")):
            m_str = month.strftime("%Y-%m")
            if not grp.empty:
                m_ret = (grp["nav"].iloc[-1] - grp["nav"].iloc[0]) / grp["nav"].iloc[0]
                b_ret = (grp["bench_nav"].iloc[-1] - grp["bench_nav"].iloc[0]) / grp[
                    "bench_nav"
                ].iloc[0]
                monthly_returns[m_str] = float(m_ret)
                benchmark_monthly_returns[m_str] = float(b_ret)

        # --- 減碼 B&H 對照組 (docs/architecture/05 §6.1 的首要 KPI) ---
        # 以年化波動比推回「有效曝險」w，對照組 = w × B&H + (1−w) × 無風險利率。
        # 為什麼需要它：策略年化波動若只有 B&H 的一半，報酬低於 B&H 是必然的，
        # 拿裸 B&H 比較會把「單純減碼」誤讀成「策略變差」，也會把「單純加槓桿」
        # 誤讀成「策略變好」。w 夾在 [0, 1]：本引擎不使用槓桿，w > 1 只會是
        # 波動估計的雜訊，放行會讓對照組憑空虛增。
        scaled_w = min(1.0, max(0.0, vol / bench_vol)) if bench_vol > 0 else 0.0
        scaled_total_return = (
            scaled_w * bench_total_return + (1.0 - scaled_w) * rf * years
        )
        scaled_max_dd = scaled_w * bench_max_dd

        return BacktestMetrics(
            total_return=total_return,
            cagr=cagr,
            benchmark_total_return=bench_total_return,
            benchmark_cagr=bench_cagr,
            annualized_volatility=vol,
            benchmark_volatility=bench_vol,
            sharpe_ratio=sharpe,
            benchmark_sharpe=bench_sharpe,
            sortino_ratio=sortino,
            benchmark_sortino=bench_sortino,
            max_drawdown=max_dd,
            benchmark_max_drawdown=bench_max_dd,
            calmar_ratio=calmar,
            benchmark_calmar=bench_calmar,
            scaled_benchmark_weight=scaled_w,
            scaled_benchmark_total_return=scaled_total_return,
            scaled_benchmark_max_drawdown=scaled_max_dd,
            excess_return_vs_scaled=total_return - scaled_total_return,
            win_rate=win_rate,
            profit_factor=profit_factor,
            total_trades=len(self.portfolio.trades),
            scenario_stats=scenario_stats,
            monthly_returns=monthly_returns,
            benchmark_monthly_returns=benchmark_monthly_returns,
        )
