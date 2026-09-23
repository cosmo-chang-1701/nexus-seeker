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
from datetime import date, datetime, timedelta
import math
from statistics import NormalDist
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
    _MACRO_TOP_ESCAPE_ELEVATED_TRIM_RATIO,
    _MACRO_TOP_ESCAPE_PUT_DTE_MAX,
    _MACRO_TOP_ESCAPE_PUT_DTE_MIN,
    _MACRO_TOP_ESCAPE_PUT_TARGET_DELTA,
    _PYRAMID_COOLDOWN_BARS,
    _PYRAMID_MAX_ADDS,
    _PYRAMID_PROFIT_THRESHOLD_PCT,
    _TP1_TREND_EXEMPT_MIGRATION_PCT,
    _WATCH_TIER_HEDGE_RATIO,
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
    resolve_effective_target,
)
from market_analysis.outcome_labeling import directional_touch, label_forward_path
from market_analysis.downside_risk import (
    annualized_downside_deviation,
    historical_var_cvar,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
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
    # --- 判讀指標（依重要性）：Sortino 為主，MDD 與 VaR / CVaR 為輔 ---
    # 定義一律來自 market_analysis/downside_risk.py（單一權威）。
    sortino_ratio: float
    benchmark_sortino: float
    annualized_downside_deviation: float
    benchmark_downside_deviation: float
    max_drawdown: float
    benchmark_max_drawdown: float
    # 1 日歷史模擬 VaR95 / CVaR95（正值損失比例）；樣本不足時為 0.0
    var_95: float
    cvar_95: float
    benchmark_var_95: float
    benchmark_cvar_95: float
    # 減碼 B&H 對照組 (docs/architecture/05 §6.1 指定的最重要 KPI)：以「與本策略
    # 同等**下行差**的 B&H + 現金」為基準。回答的是「這套引擎創造 alpha，還是只是在
    # 降低曝險」——下行風險只有 B&H 一半的策略，報酬本來就該低於 B&H，拿裸 B&H
    # 比較會同時誤判它變好或變壞。
    scaled_benchmark_weight: float
    scaled_benchmark_total_return: float
    scaled_benchmark_max_drawdown: float
    excess_return_vs_scaled: float
    # --- 描述性指標：只出現在回測報告，不作任何判讀或優化目標 ---
    # Sharpe 對上下行波動一視同仁，會把「砍獲利部位」誤判為風險改善；以總波動
    # 對齊的減碼對照組是它的同類建構，一併降為描述欄位。
    sharpe_ratio: float
    benchmark_sharpe: float
    calmar_ratio: float
    benchmark_calmar: float
    vol_scaled_benchmark_weight: float
    excess_return_vs_vol_scaled: float
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


# --- 階段 3 WATCH 級 Protective Put 複刻 (enable_escape_tiers) ---------------
# 回測沒有歷史期權鏈，以 BSM 定價、VIX 當 SPY 隱含波動率代理。合約參數沿用
# production 常數 (Delta / DTE 區間)，其餘為回測專屬的具名假設。
_HEDGE_PUT_DTE_DAYS: int = (
    _MACRO_TOP_ESCAPE_PUT_DTE_MIN + _MACRO_TOP_ESCAPE_PUT_DTE_MAX
) // 2
_HEDGE_PUT_ROLL_OUT_DTE_DAYS: int = 21  # 剩餘天數低於此值即平倉 (避開 Theta 加速區)
_HEDGE_PUT_SLIPPAGE_PCT: float = 0.02  # 單邊權利金滑價 (指數期權價差代理)
_HEDGE_RISK_FREE_RATE: float = 0.045
_BETA_LOOKBACK_DAYS: int = 60
_ESCAPE_TIER_COOLDOWN_DAYS: int = 10  # 沿用既有單一事件窗口 10 日去重
_NORMAL_DIST = NormalDist()


def _bsm_put_price(spot: float, strike: float, t_years: float, sigma: float) -> float:
    if spot <= 0 or strike <= 0:
        return 0.0
    if t_years <= 0 or sigma <= 0:
        return max(strike - spot, 0.0)
    r = _HEDGE_RISK_FREE_RATE
    vol_t = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / vol_t
    d2 = d1 - vol_t
    return float(
        strike * math.exp(-r * t_years) * _NORMAL_DIST.cdf(-d2)
        - spot * _NORMAL_DIST.cdf(-d1)
    )


def _strike_for_put_delta(
    spot: float, target_delta: float, t_years: float, sigma: float
) -> float:
    """解出 BSM Put Delta = target_delta 的履約價 (Put Delta = N(d1) − 1)。"""
    d1 = _NORMAL_DIST.inv_cdf(1.0 + target_delta)
    r = _HEDGE_RISK_FREE_RATE
    vol_t = sigma * math.sqrt(t_years)
    return float(spot * math.exp(-(d1 * vol_t - (r + 0.5 * sigma * sigma) * t_years)))


@dataclass
class HedgePut:
    """回測中唯一的一筆 SPY 保護性 Put (同時最多持有一筆)。"""

    strike: float
    expiry: date
    contracts: int
    entry_premium: float  # 每股權利金 (含滑價)
    entry_date: str
    mark: float = 0.0  # 每股最新估值

    @property
    def value(self) -> float:
        return self.mark * 100.0 * self.contracts


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
        enable_tp1_trend_exempt: bool = False,
        enable_pyramid_add: bool = False,
        enable_escape_tiers: bool = False,
    ) -> None:
        self.mode: str = mode.lower()
        # Regime III-B 趨勢延續進場路徑 (handoff.md §4)。預設關閉＝基準線，
        # 開啟後才加入第二條多頭進場路徑，供 §4.4 強制要求的 A/B 對比使用。
        self.enable_trend_continuation: bool = enable_trend_continuation
        # 階段 1A／1B／3 的複刻開關 (handoff.md §9 待辦 4)。本引擎是 production
        # 的獨立複刻，這三項功能上線時並未同步進來；跨 commit 對照因此量不到它們，
        # 必須在同一版程式碼上以開關做 A/B。三者預設關閉＝上線前的回測行為。
        self.enable_tp1_trend_exempt: bool = enable_tp1_trend_exempt
        self.enable_pyramid_add: bool = enable_pyramid_add
        self.enable_escape_tiers: bool = enable_escape_tiers
        # 出場分層事件 (handoff.md §5.4 SL 分層檢討)。純觀測，不影響任何決策；
        # 回測結束後由 label_exit_events() 以 production 共用的前向路徑定義標註。
        self.exit_events: list[dict[str, Any]] = list()
        self._bar_counter: int = 0
        self.active_hedge: Optional[HedgePut] = None
        self.escape_tier_history: dict[str, int] = dict()
        self.last_escape_tier_date: dict[str, date] = dict()
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
        self.vix3m_symbol: str = "^VIX3M"

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

        # VTS (VIX / VIX3M) 只供逃頂分級代理使用；缺資料時該因子不計分。
        if self.enable_escape_tiers or self.enable_pyramid_add:
            v3 = self.store.load("1d", self.vix3m_symbol)
            if v3 is not None and not v3.empty:
                self.daily_data[self.vix3m_symbol] = v3
                self.daily_feat[self.vix3m_symbol] = daily_features(v3)

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
            if self.active_hedge is not None:
                morning_nav += self.active_hedge.value

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
            # 逃頂分級 (production evaluate_macro_top_escape_score 的代理輸入)。
            # 只有開啟分級或 PYRAMID_ADD (條件八) 時才計算，基準線零額外成本。
            macro_tier = "NORMAL"
            if self.enable_escape_tiers or self.enable_pyramid_add:
                macro_tier = self._resolve_escape_tier(
                    current_date, current_prices, spy_gamma_flip
                )
            if macro_tier != "NORMAL":
                self.escape_tier_history[macro_tier] = (
                    self.escape_tier_history.get(macro_tier, 0) + 1
                )
            if self.enable_escape_tiers:
                self._apply_escape_tier(
                    macro_tier, current_date, current_prices, vix_prev, date_str
                )
            elif vix_prev >= 28.0:
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
                        reason=f"NVDA 動能衰竭 (PSQ={nvda_psq:.0f}) 轉倉至突破標的 GLD (PSQ={gld_psq:.0f}, ΔEV=+{ev_spread_gld * 100:.1f}%)",
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
                        reason=f"GLD 動能衰竭 (PSQ={gld_psq:.0f}) 轉倉至突破標的 NVDA (PSQ={nvda_psq:.0f}, ΔEV=+{ev_spread_nvda * 100:.1f}%)",
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
                self._bar_counter += 1
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
                        # 停損已被 SL4／Transition 上推至成本之上時，觸發的其實是
                        # 保本停損而非原始結構停損——兩者的洗盤率要分開看。
                        self._log_exit_event(
                            sat_sym,
                            "SL_BREAKEVEN_STOP"
                            if pos.stop_loss >= pos.avg_cost
                            else "SL_STRUCTURAL",
                            bar,
                            spot_val,
                        )
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
                        self._log_exit_event(sat_sym, "SL_REGIME_FLIP", bar, spot_val)
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
                    # 1A TP1 趨勢豁免 (production anti_washout.py)：牆仍在上移 +
                    # 正 Gamma + 站上 VWAP 時不減碼，改抬停損至 anchor_base。
                    # TP2/TP3 優先序不受影響 (只在單純 TP1 時才評估豁免)。
                    if (
                        self.enable_tp1_trend_exempt
                        and is_tp1
                        and not is_tp2
                        and not is_tp3
                        and self._is_tp1_trend_exempt(
                            sat_sym, current_date, bar, spot_val, net_gex_val
                        )
                    ):
                        is_tp1 = False
                        pos.stop_loss = max(pos.stop_loss, pos.anchor_base)
                        self._log_exit_event(sat_sym, "TP1_TREND_EXEMPT", bar, spot_val)

                    if is_tp3:
                        self._log_exit_event(sat_sym, "TP3", bar, spot_val)
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
                        self._log_exit_event(sat_sym, "TP2", bar, spot_val)
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
                        self._log_exit_event(sat_sym, "TP1", bar, spot_val)
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
                    elif self.enable_pyramid_add:
                        self._try_pyramid_add(
                            sat_sym,
                            current_date,
                            pos,
                            bar,
                            spot_val,
                            cw_val,
                            pw_val,
                            gf_val,
                            atr_15m_val,
                            net_gex_val,
                            morning_nav,
                            vix_prev,
                            macro_tier,
                            ts_str,
                            date_str,
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
            if self.active_hedge is not None:
                daily_nav += self._mark_hedge(current_date, spy_close, date_str)

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

    # ------------------------------------------------------------------
    # 階段 1A／1B／3 複刻與出場分層事件 (handoff.md §9 待辦 3、4)
    # ------------------------------------------------------------------
    def _prior_row(
        self, symbol: str, current_date: date, lag: int = 1
    ) -> Optional[pd.Series]:
        """第 lag 個前一交易日的日線列 (lag=1 等同 _get_daily_proxy_row)。"""
        f = self.daily_feat.get(symbol)
        if f is None:
            return None
        prior = f[f["date"] < current_date]
        if len(prior) < lag:
            return None
        return prior.iloc[-lag]

    def _close_on(self, symbol: str, current_date: date) -> Optional[float]:
        f = self.daily_feat.get(symbol)
        if f is None:
            return None
        rows = f[f["date"] == current_date]
        return float(rows["close"].iloc[0]) if not rows.empty else None

    def _resolve_escape_tier(
        self,
        current_date: date,
        current_prices: dict[str, float],
        spy_gamma_flip: float,
    ) -> str:
        """以 production 評分函式計算逃頂分級，輸入全為前一日已知資訊的代理：

        - VTS = VIX / VIX3M (前一日收盤)；缺 VIX3M 時以 0.88 (正價差) 代入＝不計分
        - 大盤負 Gamma = SPY 開盤 < SMA20 (與本引擎 MARGIN_DEFENSE 同一代理)
        - 衛星亢奮廣度 = 多頭衛星中開盤已達 10 日高點 × TP1 比例的比例
        - Fear & Greed、FedWatch 無歷史資料，以中性值代入＝這兩個因子恆不計分

        因此代理分數上限為 3 (CRITICAL 需要三個可觀測因子同時成立)，觸發頻率
        必然低估 production——報告中必須揭露。
        """
        from market_analysis.index_microstructure import (
            evaluate_macro_top_escape_score,
        )

        vix_prev = self._get_vix_prev(current_date)
        v3_row = self._prior_row(self.vix3m_symbol, current_date)
        vix3m_prev = float(v3_row["close"]) if v3_row is not None else 0.0
        vts_ratio = vix_prev / vix3m_prev if vix3m_prev > 0 else 0.88

        spy_open = current_prices.get(self.core_symbol, 0.0)
        is_negative_gamma = spy_open > 0 and spy_open < spy_gamma_flip

        longs = [
            sym
            for sym in (self.alpha_symbol, self.other_symbol)
            if self.portfolio.has_long(sym)
        ]
        euphoria_ratio: Optional[float] = None
        if longs:
            hits = 0
            for sym in longs:
                row = self._prior_row(sym, current_date)
                if (
                    row is not None
                    and current_prices.get(sym, 0.0)
                    >= float(row["high10"]) * _MICROSTRUCTURE_TP1_CALLWALL_PCT
                ):
                    hits += 1
            euphoria_ratio = hits / len(longs)

        _score, tier, _title, _factors = evaluate_macro_top_escape_score(
            vts_ratio=vts_ratio,
            fear_greed=48.0,
            prob=None,
            is_negative_gamma=is_negative_gamma,
            satellite_euphoria_ratio=euphoria_ratio,
        )
        return str(tier)

    def _apply_escape_tier(
        self,
        tier: str,
        current_date: date,
        current_prices: dict[str, float],
        vix_prev: float,
        date_str: str,
    ) -> None:
        """階段 3 三級階梯：WATCH 買 SPY 保護性 Put、ELEVATED 減碼 25%、CRITICAL
        減碼 50%。每一級各自沿用 10 日單一事件窗口去重 (升級不被較低級的冷卻擋住)。"""
        if tier == "NORMAL":
            return
        last = self.last_escape_tier_date.get(tier)
        if last is not None and (current_date - last).days < _ESCAPE_TIER_COOLDOWN_DAYS:
            return
        if tier == "WATCH":
            if self._open_watch_hedge(current_date, current_prices, vix_prev, date_str):
                self.last_escape_tier_date[tier] = current_date
            return
        ratio = (
            _MACRO_TOP_ESCAPE_TRIM_RATIO
            if tier == "CRITICAL"
            else _MACRO_TOP_ESCAPE_ELEVATED_TRIM_RATIO
        )
        trimmed = False
        for sat_sym in (self.alpha_symbol, self.other_symbol):
            if self.portfolio.has_long(sat_sym):
                self.portfolio.sell(
                    symbol=sat_sym,
                    price=current_prices[sat_sym],
                    ratio=ratio,
                    scenario=RolloverScenario.MACRO_TOP_ESCAPE_DEFENSE.value,
                    reason=f"🛡️ 逃頂分級 {tier}：衛星減碼 {ratio:.0%} 增持防禦現金",
                    timestamp=f"{date_str} 09:30:00",
                    date_str=date_str,
                )
                trimmed = True
        if trimmed:
            self.last_escape_tier_date[tier] = current_date

    def _beta_vs_spy(self, symbol: str, current_date: date) -> float:
        if symbol == self.core_symbol:
            return 1.0
        a = self.daily_feat[symbol]
        b = self.daily_feat[self.core_symbol]
        ra = a[a["date"] < current_date].set_index("date")["close"].pct_change()
        rb = b[b["date"] < current_date].set_index("date")["close"].pct_change()
        joined = (
            pd.concat([ra, rb], axis=1, join="inner").dropna().tail(_BETA_LOOKBACK_DAYS)
        )
        if len(joined) < 20:
            return 1.0
        var_b = float(joined.iloc[:, 1].var())
        if var_b <= 0:
            return 1.0
        return float(joined.iloc[:, 0].cov(joined.iloc[:, 1]) / var_b)

    def _open_watch_hedge(
        self,
        current_date: date,
        current_prices: dict[str, float],
        vix_prev: float,
        date_str: str,
    ) -> bool:
        """WATCH 級：Q_put = ceil(Δβ × ρ / (|Δput| × 100))，Δβ 以 SPY 股數等值計。"""
        if self.active_hedge is not None:
            return False
        spy_price = current_prices.get(self.core_symbol, 0.0)
        if spy_price <= 0:
            return False
        weighted_delta = 0.0
        for sym, pos in self.portfolio.positions.items():
            if pos.side != "LONG" or pos.shares <= 0:
                continue
            price = current_prices.get(sym, pos.current_price)
            weighted_delta += (
                pos.shares * price * self._beta_vs_spy(sym, current_date) / spy_price
            )
        if weighted_delta <= 0:
            return False
        contracts = math.ceil(
            weighted_delta
            * _WATCH_TIER_HEDGE_RATIO
            / (abs(_MACRO_TOP_ESCAPE_PUT_TARGET_DELTA) * 100.0)
        )
        sigma = max(vix_prev, 1.0) / 100.0
        t_years = _HEDGE_PUT_DTE_DAYS / 365.0
        strike = _strike_for_put_delta(
            spy_price, _MACRO_TOP_ESCAPE_PUT_TARGET_DELTA, t_years, sigma
        )
        premium = _bsm_put_price(spy_price, strike, t_years, sigma) * (
            1.0 + _HEDGE_PUT_SLIPPAGE_PCT
        )
        cost = premium * 100.0 * contracts
        if contracts < 1 or premium <= 0 or cost > max(0.0, self.portfolio.cash):
            return False
        self.portfolio.cash -= cost
        self.active_hedge = HedgePut(
            strike=strike,
            expiry=current_date + timedelta(days=_HEDGE_PUT_DTE_DAYS),
            contracts=contracts,
            entry_premium=premium,
            entry_date=date_str,
            mark=premium,
        )
        self.portfolio.trades.append(
            TradeRecord(
                timestamp=f"{date_str} 09:30:00",
                date=date_str,
                symbol="SPY_PUT",
                action="BUY",
                shares=float(contracts * 100),
                price=premium,
                notional=cost,
                fee=0.0,
                scenario=RolloverScenario.MACRO_TOP_ESCAPE_DEFENSE.value,
                reason=(
                    f"🛡️ 逃頂分級 WATCH：買入 {contracts} 口 SPY "
                    f"${strike:.0f} Put ({_HEDGE_PUT_DTE_DAYS}DTE, "
                    f"Δ≈{_MACRO_TOP_ESCAPE_PUT_TARGET_DELTA:.3f})，保留 100% 上檔曝險"
                ),
            )
        )
        return True

    def _mark_hedge(self, current_date: date, spy_close: float, date_str: str) -> float:
        """收盤估值；剩餘天數低於 _HEDGE_PUT_ROLL_OUT_DTE_DAYS 即平倉。回傳估值
        (已平倉則為 0.0，權利金回收已入現金)。"""
        hedge = self.active_hedge
        if hedge is None:
            return 0.0
        vix_close = self._close_on(self.vix_symbol, current_date) or self._get_vix_prev(
            current_date
        )
        days_left = (hedge.expiry - current_date).days
        hedge.mark = _bsm_put_price(
            spy_close, hedge.strike, max(days_left, 0) / 365.0, vix_close / 100.0
        )
        if days_left > _HEDGE_PUT_ROLL_OUT_DTE_DAYS:
            return hedge.value
        exit_price = hedge.mark * (1.0 - _HEDGE_PUT_SLIPPAGE_PCT)
        proceeds = exit_price * 100.0 * hedge.contracts
        self.portfolio.cash += proceeds
        self.portfolio.trades.append(
            TradeRecord(
                timestamp=f"{date_str} 16:00:00",
                date=date_str,
                symbol="SPY_PUT",
                action="SELL",
                shares=float(hedge.contracts * 100),
                price=exit_price,
                notional=proceeds,
                fee=0.0,
                scenario=RolloverScenario.MACRO_TOP_ESCAPE_DEFENSE.value,
                reason=f"🛡️ 保護性 Put 剩餘 {days_left} 天，平倉避開 Theta 加速區",
                realized_pnl=proceeds - hedge.entry_premium * 100.0 * hedge.contracts,
            )
        )
        self.active_hedge = None
        return 0.0

    def _is_tp1_trend_exempt(
        self,
        symbol: str,
        current_date: date,
        bar: pd.Series,
        spot: float,
        net_gex_proxy: float,
    ) -> bool:
        """1A 豁免條件的代理：牆 = 10 日高點 (本引擎 TP 階梯既有的 Call Wall 代理)，
        遷移以「昨日 vs 前日」的 10 日高點比較 (皆為已收盤資訊，無前視)。"""
        cur = self._prior_row(symbol, current_date, lag=1)
        prev = self._prior_row(symbol, current_date, lag=2)
        if cur is None or prev is None:
            return False
        wall_now = float(cur["high10"])
        wall_prev = float(prev["high10"])
        if wall_prev <= 0:
            return False
        migration = (wall_now - wall_prev) / wall_prev
        session_vwap = float(bar["session_vwap"])
        return (
            migration >= _TP1_TREND_EXEMPT_MIGRATION_PCT
            and net_gex_proxy > 0.0
            and session_vwap > 0.0
            and spot > session_vwap
        )

    def _try_pyramid_add(
        self,
        symbol: str,
        current_date: date,
        pos: Position,
        bar: pd.Series,
        spot: float,
        call_wall: float,
        put_wall: float,
        gamma_flip: float,
        atr_15m: float,
        net_gex_proxy: float,
        nav: float,
        vix_prev: float,
        macro_tier: str,
        ts_str: str,
        date_str: str,
    ) -> None:
        """1B PYRAMID_ADD 八項條件 (production pyramid_add.py) 的複刻。倉位直接呼叫
        production 的 compute_pyramid_add_sizing，冷卻以 1h K 棒換算 (8 根 15m =
        2 根 1h)。"""
        if pos.avg_cost <= 0 or spot <= 0:
            return
        # 條件一：獲利門檻
        if (spot - pos.avg_cost) / pos.avg_cost < _PYRAMID_PROFIT_THRESHOLD_PCT:
            return
        # 條件二：停損已在成本之上 (不變式，不得放寬)
        if pos.stop_loss < pos.avg_cost:
            return
        # 條件三：趨勢結構完好
        session_vwap = float(bar["session_vwap"])
        if not (
            session_vwap > 0
            and spot > session_vwap
            and gamma_flip > 0
            and spot > gamma_flip
            and net_gex_proxy > 0.0
        ):
            return
        # 條件五：次數上限
        count = int(pos.dynamic_state.get("pyramid_count", 0))
        if count >= _PYRAMID_MAX_ADDS:
            return
        # 條件六：冷卻 (15m bar 數換算為 1h bar)
        last_bar = pos.dynamic_state.get("last_pyramid_bar")
        cooldown_1h = max(1, math.ceil(_PYRAMID_COOLDOWN_BARS / 4))
        if last_bar is not None and self._bar_counter - int(last_bar) < cooldown_1h:
            return
        # 條件四：晴空萬里有效目標天花板空間
        row = self._prior_row(symbol, current_date)
        high_60d = float(row["high60"]) if row is not None else 0.0
        atr_1d = float(bar["atr14_prev"])
        eff = resolve_effective_target(spot, call_wall, high_60d, atr_1d)
        room = compute_dynamic_room_threshold(
            spot, put_wall, atr_15m, atr_1d, direction="LONG"
        )
        room_pct = (eff.target - spot) / spot if eff.target > 0 else 0.0
        if room_pct < room.threshold_pct:
            return
        # 條件八：非逃頂警戒
        if macro_tier != "NORMAL":
            return

        from market_analysis.dynamic_rollover.pyramid_add import (
            compute_pyramid_add_sizing,
        )

        rsi = float(bar["rsi"]) if pd.notna(bar["rsi"]) else None
        sizing = compute_pyramid_add_sizing(
            spot, pos.stop_loss, nav, None, vix_prev, rsi
        )
        qty = sizing.share_qty
        # 條件七：加碼後曝險不得超過單筆衛星預算上限，超過則降量
        remaining = nav * self.max_satellite_budget_pct - abs(pos.current_value)
        if remaining <= 0:
            return
        qty = min(qty, int(math.floor(remaining / spot)))
        if qty < 1:
            return
        record = self.portfolio.buy(
            symbol=symbol,
            asset_class="SATELLITE",
            price=spot,
            notional=qty * spot,
            scenario="PYRAMID_ADD",
            reason=(
                f"📈 順勢金字塔加碼第 {count + 1}/{_PYRAMID_MAX_ADDS} 次：{qty} 股 "
                f"(風險預算 ${sizing.risk_budget_usd:,.0f} ÷ 停損距離 "
                f"${spot - pos.stop_loss:.2f})"
            ),
            timestamp=ts_str,
            date_str=date_str,
        )
        if record is not None:
            pos.dynamic_state["pyramid_count"] = count + 1
            pos.dynamic_state["last_pyramid_bar"] = self._bar_counter

    def _log_exit_event(
        self, symbol: str, tier: str, bar: pd.Series, spot: float
    ) -> None:
        self.exit_events.append(
            {
                "symbol": symbol,
                "tier": tier,
                "ts": bar.name,
                "spot": float(spot),
                "atr_1d": float(bar["atr14_prev"]),
            }
        )

    def label_exit_events(self) -> list[dict[str, Any]]:
        """以 production 的前向路徑定義 (outcome_labeling.py，±1.5×ATR₁D 先觸及)
        標註每筆出場分層事件。方向語意與 evaluation_recorder.record_exit_signal
        一致：平倉類押注 SHORT (outcome=+1 出場正確、−1 被洗盤)，抬停損類押注 LONG。"""
        from market_analysis.evaluation_recorder import HOLD_EXIT_TIERS

        labeled: list[dict[str, Any]] = list()
        for ev in self.exit_events:
            bars = self.hourly_data.get(ev["symbol"])
            if bars is None:
                continue
            label = label_forward_path(
                bars, pd.Timestamp(ev["ts"]).to_pydatetime(), ev["spot"], ev["atr_1d"]
            )
            if label is None:
                continue
            touch = next(t.touch for t in label.touches if abs(t.k - 1.5) < 1e-9)
            direction = "LONG" if ev["tier"] in HOLD_EXIT_TIERS else "SHORT"
            labeled.append(
                {
                    **ev,
                    "direction": direction,
                    "outcome": directional_touch(touch, direction),
                    "fwd_ret_5d": label.fwd_ret_5d,
                }
            )
        return labeled

    def summarize_exit_events(self) -> list[dict[str, Any]]:
        """依分層彙總：n、訊號正確率、洗盤率、逾時率、5 日報酬中位數。"""
        rows = self.label_exit_events()
        tiers = sorted({r["tier"] for r in rows})
        out: list[dict[str, Any]] = list()
        for tier in tiers:
            grp = [r for r in rows if r["tier"] == tier]
            n = len(grp)
            fwd = [r["fwd_ret_5d"] for r in grp if r["fwd_ret_5d"] is not None]
            out.append(
                {
                    "tier": tier,
                    "n": n,
                    "correct_rate": sum(r["outcome"] == 1 for r in grp) / n,
                    "washout_rate": sum(r["outcome"] == -1 for r in grp) / n,
                    "timeout_rate": sum(r["outcome"] == 0 for r in grp) / n,
                    "median_fwd_ret_5d": float(np.median(fwd)) if fwd else None,
                }
            )
        return out

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

        # 無風險利率 (2025 年以 4.5% 為基準)；同時作為 Sortino 的 MAR，使分子
        # (CAGR − rf) 與分母 (低於 rf 的下行差) 以同一條基準線衡量。
        rf = 0.045

        # 判讀主指標：Sortino（下行差以 MAR 為界、分母為全樣本數）
        downside_dev = annualized_downside_deviation(ret_arr, rf)
        bench_downside_dev = annualized_downside_deviation(bench_ret_arr, rf)
        sortino = sortino_ratio(ret_arr, rf, annual_return=cagr)
        bench_sortino = sortino_ratio(bench_ret_arr, rf, annual_return=bench_cagr)

        # 最大回撤 (MDD)
        max_dd = max_drawdown(navs).max_drawdown
        bench_max_dd = max_drawdown(bench_navs).max_drawdown

        # 1 日歷史模擬 VaR95 / CVaR95
        tail = historical_var_cvar(ret_arr)
        bench_tail = historical_var_cvar(bench_ret_arr)

        # 描述性指標（不作判讀）
        sharpe = sharpe_ratio(ret_arr, rf, annual_return=cagr)
        bench_sharpe = sharpe_ratio(bench_ret_arr, rf, annual_return=bench_cagr)
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
        # 以下行差比推回「有效曝險」w，對照組 = w × B&H + (1−w) × 無風險利率。
        # 為什麼需要它：策略下行風險若只有 B&H 的一半，報酬低於 B&H 是必然的，
        # 拿裸 B&H 比較會把「單純減碼」誤讀成「策略變差」，也會把「單純加槓桿」
        # 誤讀成「策略變好」。
        # 為什麼以下行差（MAR = rf）而非總波動對齊：混合組合的超額報酬恰為
        # w × (B&H − rf)，其下行差精確等於 w × B&H 下行差，因此對照組與策略承擔
        # 完全相同的 Sortino 分母；以總波動對齊則會把策略「砍掉的上行波動」也算成
        # 降低的風險，替對照組多扣曝險。w 夾在 [0, 1]：本引擎不使用槓桿，w > 1 只會是
        # 估計雜訊，放行會讓對照組憑空虛增。
        def _scaled_return(w: float) -> float:
            return w * bench_total_return + (1.0 - w) * rf * years

        scaled_w = (
            min(1.0, max(0.0, downside_dev / bench_downside_dev))
            if bench_downside_dev > 0
            else 0.0
        )
        scaled_total_return = _scaled_return(scaled_w)
        scaled_max_dd = scaled_w * bench_max_dd
        vol_scaled_w = min(1.0, max(0.0, vol / bench_vol)) if bench_vol > 0 else 0.0

        return BacktestMetrics(
            total_return=total_return,
            cagr=cagr,
            benchmark_total_return=bench_total_return,
            benchmark_cagr=bench_cagr,
            annualized_volatility=vol,
            benchmark_volatility=bench_vol,
            sortino_ratio=sortino,
            benchmark_sortino=bench_sortino,
            annualized_downside_deviation=downside_dev,
            benchmark_downside_deviation=bench_downside_dev,
            max_drawdown=max_dd,
            benchmark_max_drawdown=bench_max_dd,
            var_95=tail.var if tail else 0.0,
            cvar_95=tail.cvar if tail else 0.0,
            benchmark_var_95=bench_tail.var if bench_tail else 0.0,
            benchmark_cvar_95=bench_tail.cvar if bench_tail else 0.0,
            scaled_benchmark_weight=scaled_w,
            scaled_benchmark_total_return=scaled_total_return,
            scaled_benchmark_max_drawdown=scaled_max_dd,
            excess_return_vs_scaled=total_return - scaled_total_return,
            sharpe_ratio=sharpe,
            benchmark_sharpe=bench_sharpe,
            calmar_ratio=calmar,
            benchmark_calmar=bench_calmar,
            vol_scaled_benchmark_weight=vol_scaled_w,
            excess_return_vs_vol_scaled=total_return - _scaled_return(vol_scaled_w),
            win_rate=win_rate,
            profit_factor=profit_factor,
            total_trades=len(self.portfolio.trades),
            scenario_stats=scenario_stats,
            monthly_returns=monthly_returns,
            benchmark_monthly_returns=benchmark_monthly_returns,
        )
