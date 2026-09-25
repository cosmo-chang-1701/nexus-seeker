"""動態轉倉回測模擬引擎 (RolloverBacktestEngine2025).

預設配置為 2025 年 SPY／NVDA／GLD 三標的（回歸不變式的基準，見
tests/unit/test_backtest_regression_invariant.py）；可透過 `universe` 參數換成多資產
配置（`multi_asset_universe()`：VOO 核心 + 8 檔個股衛星 + GLD），並可開啟 BOXX 大盤
退場（`enable_boxx_retreat`）。標的一般化的方式見 `SatelliteSpec` 上方的說明。

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
from calibration.features import daily_features, et_dates, hourly_features
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
    # 各標的收盤市值（多資產報告的逐檔貢獻用；空頭為負值）
    symbol_values: dict[str, float] = field(default_factory=lambda: dict())


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


# ---------------------------------------------------------------------------
# 標的配置（多資產一般化）
# ---------------------------------------------------------------------------
# 原引擎把 SPY（核心）／NVDA（Alpha 衛星）／GLD（Other 避險衛星）寫死在每個情境裡。
# 一般化方式：
# - 「核心持倉」（core_symbol）與「大盤訊號代理」（market_symbol）分開。大盤負 Gamma
#   代理（開盤 < SMA20）、MARGIN_DEFENSE 的危機判定、逃頂分級、保護性 Put 的定價與
#   Beta 計算一律用 market_symbol（SPY：代表指數本身，且期權流動性遠優於 VOO）；
#   核心持倉的建倉、超額再平衡、Covered Call 收益與 BOXX 退場訊號用 core_symbol。
#   預設配置兩者都是 SPY。
# - NVDA／GLD 的專屬常數（EV 代理的 HV 預設、缺值時的 60 日高點／ATR 替代倍數、初始
#   建倉的目標牆與停損倍數）改由每檔衛星的 SatelliteSpec 自帶，預設配置取值與原常數
#   相同。多資產配置中 8 檔個股沿用 NVDA 的值、GLD 沿用 GLD 的值。
# - 機會成本輪動：原本是 NVDA ↔ GLD 兩個方向各檢查一次；一般化為「依設定順序逐檔檢查
#   衰退中的衛星，轉入其餘衛星中 EV 價差最大的突破候選」。只有兩檔時候選唯一，與原
#   邏輯逐位元相同。
# - CORE_DEPLOYMENT 的去向：預設配置沿用原優先序（先 GLD 後 NVDA）；多資產配置改為
#   PSQ 最高的突破候選。
# - Regime V 破位追空：原本只評估 NVDA，一般化為 allow_short 的衛星（多資產配置中為
#   8 檔個股；GLD 不做空，與原引擎一致）。
# - 1h K 棒：原引擎依序號對齊三檔（取最少根數）；多資產配置改以時間戳交集對齊，避免
#   任一檔缺 K 棒時錯位。


@dataclass(frozen=True)
class SatelliteSpec:
    symbol: str
    weight: float
    init_label: str  # INIT 交易理由中的角色字樣（預設配置沿用原字串）
    hv_default: float  # EV 代理：前一日特徵缺值時的 HV 預設
    h60_fallback_mult: float  # 前一日 60 日高點缺值時以開盤價 × 此倍數代替
    atr_fallback_pct: float  # 前一日 ATR14 缺值時以開盤價 × 此比例代替
    init_target_mult: float  # 初始建倉目標牆 = 開盤 × 此倍數
    init_stop_mult: float  # 初始建倉停損 = 開盤 × 此倍數
    allow_short: bool = False  # 是否評估 Regime V 破位追空
    retreat_exempt: bool = False  # BOXX 大盤退場時不動（GLD）


@dataclass(frozen=True)
class BacktestUniverse:
    name: str
    core_symbol: str
    core_weight: float
    satellites: tuple[SatelliteSpec, ...]
    cash_weight: float
    # B&H 對照組（單純持有、不退場）的權重，依序加總
    benchmark_weights: tuple[tuple[str, float], ...]
    benchmark_cash_weight: float
    market_symbol: str = "SPY"
    # CORE_DEPLOYMENT 的候選優先序；None = 依 PSQ 由高到低
    core_deploy_priority: Optional[tuple[str, ...]] = None
    # 1h K 棒對齊：position = 依序號（原引擎行為）；timestamp = 依時間戳交集
    hourly_alignment: str = "position"
    # INIT 交易理由的年份字樣；None = 回測起始年份
    init_reason_year: Optional[str] = None
    # 機會成本輪動的頻率控制：
    # per_source = 原引擎行為，冷卻以「每檔來源衛星」各自計算（兩檔衛星時只有一對、
    #   兩個方向同日互斥，等同「一次一筆」）；
    # portfolio  = 投組層級：每天最多一筆（全部「衰退 → 突破」組合中 EV 價差最大者），
    #   冷卻以投組計算。N 檔衛星若沿用 per_source，會出現同日多檔同時轉入同一標的、
    #   剛轉入的標的數日內又被轉出，偏離原設計「單向、低頻輪動」的語意。
    rotation_policy: str = "per_source"


def equity_satellite(
    symbol: str,
    weight: float,
    init_label: Optional[str] = None,
    allow_short: bool = True,
) -> SatelliteSpec:
    """個股型衛星：沿用原引擎 NVDA 的替代常數。"""
    return SatelliteSpec(
        symbol=symbol,
        weight=weight,
        init_label=init_label if init_label is not None else f"衛星 {symbol} ",
        hv_default=0.35,
        h60_fallback_mult=1.08,
        atr_fallback_pct=0.02,
        init_target_mult=1.08,
        init_stop_mult=0.92,
        allow_short=allow_short,
    )


def gold_satellite(
    symbol: str, weight: float, init_label: str = "衛星 Other 避險"
) -> SatelliteSpec:
    """避險型衛星：沿用原引擎 GLD 的替代常數；不做空、BOXX 退場時不動。"""
    return SatelliteSpec(
        symbol=symbol,
        weight=weight,
        init_label=init_label,
        hv_default=0.20,
        h60_fallback_mult=1.05,
        atr_fallback_pct=0.015,
        init_target_mult=1.06,
        init_stop_mult=0.95,
        allow_short=False,
        retreat_exempt=True,
    )


MULTI_ASSET_EQUITIES: tuple[str, ...] = (
    "NVDA",
    "META",
    "GOOGL",
    "TSLA",
    "MU",
    "PLTR",
    "FCX",
    "MRNA",
)


def legacy_universe(
    core_weight: float, alpha_weight: float, other_weight: float, cash_weight: float
) -> BacktestUniverse:
    """原引擎的 SPY／NVDA／GLD 三標的配置（回歸不變式的基準）。"""
    return BacktestUniverse(
        name="SPY_NVDA_GLD",
        core_symbol="SPY",
        core_weight=core_weight,
        satellites=(
            equity_satellite("NVDA", alpha_weight, "衛星 Alpha "),
            gold_satellite("GLD", other_weight),
        ),
        cash_weight=cash_weight,
        benchmark_weights=(("SPY", 0.50), ("NVDA", 0.25), ("GLD", 0.15)),
        benchmark_cash_weight=0.10,
        market_symbol="SPY",
        core_deploy_priority=("GLD", "NVDA"),
        hourly_alignment="position",
        init_reason_year="2025",
    )


def multi_asset_universe() -> BacktestUniverse:
    """VOO 核心 40%、8 檔個股衛星各 6%、GLD 7%、現金 5%（策略與 B&H 共用）。

    大盤訊號代理維持 SPY（見上方說明），VOO 只是核心持倉。
    """
    sats = tuple(equity_satellite(sym, 0.06) for sym in MULTI_ASSET_EQUITIES) + (
        gold_satellite("GLD", 0.07),
    )
    return BacktestUniverse(
        name="VOO_8SAT_GLD",
        core_symbol="VOO",
        core_weight=0.40,
        satellites=sats,
        cash_weight=0.05,
        benchmark_weights=(("VOO", 0.40),) + tuple((s.symbol, s.weight) for s in sats),
        benchmark_cash_weight=0.05,
        market_symbol="SPY",
        core_deploy_priority=None,
        hourly_alignment="timestamp",
        init_reason_year=None,
        rotation_policy="portfolio",
    )


# --- BOXX 大盤退場（enable_boxx_retreat，預設關閉）---------------------------
# 觸發：核心標的日收盤連續 3 個交易日低於其 200 日均線 → 退場；連續 3 個交易日站回
# 之上 → 回場。以前一交易日收盤判定（無前視），在下一個交易日開盤執行。
# 退場：非豁免衛星全部轉入 BOXX、核心賣出一半轉入 BOXX；GLD（retreat_exempt）不動。
# 回場：賣出 BOXX，核心與非豁免衛星依原目標權重重新建倉。
# 退場期間：暫停衛星的新進場（右側／趨勢延續／左側接刀）、破位追空、左側演化加碼、
# 順勢加碼、機會成本換股，以及核心超額資金部署到衛星；停損／TP、MARGIN_DEFENSE、
# 逃頂防禦與 Covered Call 收益照常運作。
_RETREAT_SMA_WINDOW: int = 200
_RETREAT_CONFIRM_DAYS: int = 3
_RETREAT_CORE_SELL_RATIO: float = 0.50
_RETREAT_REBUILD_MIN_USD: float = 1000.0
BOXX_SYMBOL: str = "BOXX"
BOXX_PROXY_SYMBOL: str = "BIL"  # BOXX 2022-12-28 上市前的日報酬代理
RETREAT_SCENARIO: str = "BOXX_RETREAT"


class RolloverBacktestEngine2025:
    """動態轉倉引擎回測核心（預設 2025 年 SPY／NVDA／GLD；可設定標的配置與期間）。"""

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
        universe: Optional[BacktestUniverse] = None,
        enable_boxx_retreat: bool = False,
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
        # BOXX 大盤退場（預設關閉＝原引擎行為，規則見模組層級說明）
        self.enable_boxx_retreat: bool = enable_boxx_retreat
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
            )
            self.max_satellite_budget_pct = 0.15

        # 標的配置：未指定時沿用原三標的配置（權重依模式而定）。指定時權重以配置為準，
        # 其餘模式參數（TP1 比例、換股門檻／冷卻、部署比例、單筆衛星上限）不變。
        if universe is None:
            universe = legacy_universe(
                self.core_target_weight,
                self.alpha_target_weight,
                self.other_target_weight,
                self.cash_target_weight,
            )
        else:
            self.core_target_weight = universe.core_weight
            self.alpha_target_weight = universe.satellites[0].weight
            self.other_target_weight = universe.satellites[-1].weight
            self.cash_target_weight = universe.cash_weight
        self.universe: BacktestUniverse = universe
        self.core_symbol: str = universe.core_symbol
        self.market_symbol: str = universe.market_symbol
        self.sat_specs: tuple[SatelliteSpec, ...] = universe.satellites
        self.sat_symbols: list[str] = [s.symbol for s in universe.satellites]
        self.sat_spec: dict[str, SatelliteSpec] = {
            s.symbol: s for s in universe.satellites
        }
        # 相容舊屬性：第一檔衛星沿用 alpha、最後一檔沿用 other 的稱呼
        self.alpha_symbol: str = self.sat_symbols[0]
        self.other_symbol: str = self.sat_symbols[-1]

        # 資料容器
        self.daily_data: dict[str, pd.DataFrame] = dict()
        self.hourly_data: dict[str, pd.DataFrame] = dict()
        self.daily_feat: dict[str, pd.DataFrame] = dict()
        self.hourly_feat: dict[str, pd.DataFrame] = dict()
        self._hours_by_date: dict[str, dict[date, pd.DataFrame]] = dict()
        self.trading_dates: list[date] = list()

        # 基準投組追蹤（單純持有、不退場；預設 50% SPY, 25% NVDA, 15% GLD, 10% Cash）
        self.benchmark_shares: dict[str, float] = dict()
        self.benchmark_cash: float = initial_capital * universe.benchmark_cash_weight
        self.benchmark_history: list[float] = list()

        # 轉倉與停損冷卻追蹤
        self.last_exit_date: dict[str, date] = dict()
        self.last_macro_escape_date: Optional[date] = None
        self.last_opp_cost_date: dict[str, date] = dict()
        self.last_portfolio_rotation_date: Optional[date] = None
        self.fundamental_broken_events: dict[str, set[str]] = dict()

        # BOXX 大盤退場狀態
        self.retreat_active: bool = False
        self.retreat_signal: dict[date, str] = dict()
        self.retreat_episodes: list[dict[str, Any]] = list()
        self.boxx_listing_date: Optional[date] = None

    @property
    def traded_symbols(self) -> list[str]:
        """核心 + 衛星（依設定順序）。"""
        return [self.core_symbol] + self.sat_symbols

    def load_and_prepare_data(self) -> None:
        """載入 1d 與 1h 資料並計算無前視時序特徵。"""
        symbols = self.traded_symbols + [self.vix_symbol]
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
                self._hours_by_date[sym] = {
                    k: grp for k, grp in self.hourly_feat[sym].groupby("date")
                }

        # 大盤訊號代理：核心持倉不是 SPY 時另外載入（只需日線）
        if self.market_symbol not in self.daily_feat:
            m = self.store.load("1d", self.market_symbol)
            if m is None or m.empty:
                raise RuntimeError(f"缺失 {self.market_symbol} 1d 歷史資料")
            self.daily_data[self.market_symbol] = m
            self.daily_feat[self.market_symbol] = daily_features(m)

        # VTS (VIX / VIX3M) 只供逃頂分級代理使用；缺資料時該因子不計分。
        if self.enable_escape_tiers or self.enable_pyramid_add:
            v3 = self.store.load("1d", self.vix3m_symbol)
            if v3 is not None and not v3.empty:
                self.daily_data[self.vix3m_symbol] = v3
                self.daily_feat[self.vix3m_symbol] = daily_features(v3)

        if self.enable_boxx_retreat:
            self._prepare_boxx_series()
            self._prepare_retreat_signal()

        # 篩選出回測期間的交易日
        spy_dfeat = self.daily_feat[self.core_symbol]
        dt_start = datetime.strptime(self.start_date, "%Y-%m-%d").date()
        dt_end = datetime.strptime(self.end_date, "%Y-%m-%d").date()

        dates_in_range: set[date] = set()
        for d_val in spy_dfeat["date"]:
            if isinstance(d_val, date) and dt_start <= d_val <= dt_end:
                dates_in_range.add(d_val)
        self.trading_dates = sorted(list(dates_in_range))

        # 多標的配置：每個交易日每檔都必須有日線，否則開盤／收盤取值會失敗
        if self.universe.hourly_alignment == "timestamp":
            required = self.traded_symbols + [self.market_symbol]
            if self.enable_boxx_retreat:
                required.append(BOXX_SYMBOL)
            for sym in required:
                have = set(self.daily_feat[sym]["date"])
                missing = [d for d in self.trading_dates if d not in have]
                if missing:
                    raise RuntimeError(
                        f"{sym} 缺少 {len(missing)} 個交易日的日線（首個 {missing[0]}）"
                    )

    def _prepare_boxx_series(self) -> None:
        """BOXX 日線；上市（2022-12-28）前以 BIL 日報酬代理。

        銜接：把 BOXX 上市日之前的 BIL 價格整段乘上 k = BOXX 上市日收盤 / BIL 同日收盤，
        使代理段在上市日與 BOXX 同價。上市日之前每天的報酬 = BIL 當日報酬（Yahoo 調整後
        價格，含配息，即總報酬），上市日之後 = BOXX 實際價格。兩者皆為短天期國庫券收益，
        銜接點不產生價格跳空。
        """
        boxx = self.store.load("1d", BOXX_SYMBOL)
        bil = self.store.load("1d", BOXX_PROXY_SYMBOL)
        if boxx is None or boxx.empty or bil is None or bil.empty:
            raise RuntimeError("BOXX 退場需要 BOXX 與 BIL 日線")
        first_ts = boxx.index[0]
        if first_ts not in bil.index:
            raise RuntimeError("BIL 缺少 BOXX 上市日的日線，無法銜接")
        k = float(boxx.loc[first_ts, "Close"]) / float(bil.loc[first_ts, "Close"])
        pre = bil[bil.index < first_ts].astype("float64")
        for col in ("Open", "High", "Low", "Close"):
            pre[col] = pre[col] * k
        spliced = pd.concat([pre, boxx.astype("float64")]).sort_index()
        self.daily_data[BOXX_SYMBOL] = spliced
        self.daily_feat[BOXX_SYMBOL] = daily_features(spliced)
        self.boxx_listing_date = et_dates(pd.DatetimeIndex([first_ts]))[0]

    def _prepare_retreat_signal(self) -> None:
        """預先算出每個交易日開盤時的退場／回場訊號（核心標的收盤 vs 200 日均線）。

        交易日 D 的訊號只看 D 之前已收盤的 3 個交易日：全部低於均線 → EXIT；全部高於
        均線 → ENTER。均線以 min_periods=200 計算，暖機期不足時兩者皆不成立。
        """
        f = self.daily_feat[self.core_symbol]
        close = f["close"].astype("float64")
        sma = close.rolling(_RETREAT_SMA_WINDOW, min_periods=_RETREAT_SMA_WINDOW).mean()
        below = (close < sma).to_numpy()
        above = (close > sma).to_numpy()
        dates = list(f["date"])
        n = _RETREAT_CONFIRM_DAYS
        for i in range(n, len(dates)):
            if bool(below[i - n : i].all()):
                self.retreat_signal[dates[i]] = "EXIT"
            elif bool(above[i - n : i].all()):
                self.retreat_signal[dates[i]] = "ENTER"

    def _day_hours(self, symbol: str, current_date: date) -> pd.DataFrame:
        grp = self._hours_by_date.get(symbol, {}).get(current_date)
        if grp is not None:
            return grp
        f = self.hourly_feat[symbol]
        return f[f["date"] == current_date]

    def _open_on(self, symbol: str, current_date: date) -> float:
        f = self.daily_feat[symbol]
        return float(f[f["date"] == current_date]["open"].iloc[0])

    def _close_price_on(self, symbol: str, current_date: date) -> float:
        f = self.daily_feat[symbol]
        return float(f[f["date"] == current_date]["close"].iloc[0])

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
        """建立回測第一個交易日的初始建倉與基準投組。"""
        first_dt_str = str(first_date)
        year = self.universe.init_reason_year or str(first_date.year)

        # 取得當日開盤價
        opens = {sym: self._open_on(sym, first_date) for sym in self.traded_symbols}

        # 核心部位
        core_open = opens[self.core_symbol]
        self.portfolio.buy(
            symbol=self.core_symbol,
            asset_class="CORE",
            price=core_open,
            notional=self.initial_capital * self.core_target_weight,
            scenario="INIT",
            reason=f"{year} 初始核心配置 ({self.core_target_weight:.0%})",
            timestamp=f"{first_dt_str} 09:30:00",
            date_str=first_dt_str,
            target_allocation_pct=self.core_target_weight,
            anchor_base=core_open,
            target_wall=core_open * 1.05,
            stop_loss=core_open * 0.90,
        )

        # 衛星部位（依設定順序）
        for spec in self.sat_specs:
            sat_open = opens[spec.symbol]
            weight = self._sat_target_weight(spec)
            self.portfolio.buy(
                symbol=spec.symbol,
                asset_class="SATELLITE",
                price=sat_open,
                notional=self.initial_capital * weight,
                scenario="INIT",
                reason=f"{year} 初始{spec.init_label}配置 ({weight:.0%})",
                timestamp=f"{first_dt_str} 09:30:00",
                date_str=first_dt_str,
                anchor_base=sat_open,
                target_wall=sat_open * spec.init_target_mult,
                stop_loss=sat_open * spec.init_stop_mult,
            )

        # 靜態基準投組股份設定 (扣除手續費)；基準始終維持單純持有、不退場
        fee_rate = self.portfolio.fee_rate
        for sym, weight in self.universe.benchmark_weights:
            bench_usd = self.initial_capital * weight
            self.benchmark_shares[sym] = (bench_usd * (1.0 - fee_rate)) / opens[sym]

    def _sat_target_weight(self, spec: SatelliteSpec) -> float:
        """衛星目標權重（預設配置的兩檔權重已由模式決定並寫入 SatelliteSpec）。"""
        return spec.weight

    def _prev_rows(self, current_date: date) -> dict[str, Optional[pd.Series]]:
        syms = list(dict.fromkeys(self.traded_symbols + [self.market_symbol]))
        return {sym: self._get_daily_proxy_row(sym, current_date) for sym in syms}

    def _sat_psq(self, symbol: str, current_date: date) -> float:
        mask = self.daily_feat[symbol]["date"] < current_date
        data = self.daily_data[symbol]
        return self._compute_psq_from_series(
            data[mask]["Close"], data[mask]["High"], data[mask]["Low"]
        )

    def _sat_ev(
        self,
        spec: SatelliteSpec,
        prev_row: Optional[pd.Series],
        open_px: float,
        psq: float,
    ) -> float:
        # EV proxy based on Expected Move (02_expected_move_and_max_pain.md):
        # EM_weekly = Spot * max(HV20, 0.15) * sqrt(7 / 365)
        # PSQ momentum weighting adjusts forward expected drift:
        # EV = (EM_weekly / Spot) * (PSQ / 50.0)
        hv = (
            float(prev_row["hv_rank"]) / 100.0
            if prev_row is not None
            else spec.hv_default
        )
        hv_val = max(0.15, hv)
        em_pct = hv_val * math.sqrt(7.0 / 365.0)
        # 下行風險懲罰 (若跌破 Gamma Flip，施加 30% 懲罰)
        penalty = (
            0.30
            if (prev_row is not None and open_px < float(prev_row["sma20"]))
            else 0.0
        )
        return em_pct * (psq / 50.0) * (1.0 - penalty)

    def _momentum_entry_levels(
        self, spec: SatelliteSpec, prev_row: Optional[pd.Series], open_px: float
    ) -> tuple[float, float, float]:
        """(put wall, 目標天花板, 參考停損)：CORE_DEPLOYMENT 與機會成本轉入時共用。"""
        pw = float(prev_row["low10"]) if prev_row is not None else open_px * 0.95
        h60 = (
            float(prev_row["high60"])
            if prev_row is not None
            else open_px * spec.h60_fallback_mult
        )
        atr1d = (
            float(prev_row["atr14"])
            if prev_row is not None
            else open_px * spec.atr_fallback_pct
        )
        atr15m = atr1d / math.sqrt(26.0)
        sl = compute_reference_stop(open_px, pw, atr15m, "LONG")
        target = max(h60, open_px + 3.0 * atr1d)
        return pw, target, sl

    def _core_deploy_candidates(self, psq: dict[str, float]) -> list[str]:
        order = self.universe.core_deploy_priority
        if order is not None:
            return [s for s in order if s in self.sat_spec]
        return sorted(self.sat_symbols, key=lambda s: -psq[s])

    # ------------------------------------------------------------------
    # BOXX 大盤退場
    # ------------------------------------------------------------------
    def _apply_retreat_signal(
        self,
        current_date: date,
        current_prices: dict[str, float],
        prev_rows: dict[str, Optional[pd.Series]],
        date_str: str,
    ) -> None:
        signal = self.retreat_signal.get(current_date)
        ts = f"{date_str} 09:30:00"
        boxx_px = current_prices[BOXX_SYMBOL]
        if signal == "EXIT" and not self.retreat_active:
            proceeds = 0.0
            for spec in self.sat_specs:
                if spec.retreat_exempt or not self.portfolio.has_long(spec.symbol):
                    continue
                tr = self.portfolio.sell(
                    symbol=spec.symbol,
                    price=current_prices[spec.symbol],
                    ratio=1.0,
                    scenario=RETREAT_SCENARIO,
                    reason=f"🏦 大盤退場：{self.core_symbol} 連續 3 日收盤低於 200 日均線，衛星全數轉入 BOXX",
                    timestamp=ts,
                    date_str=date_str,
                )
                if tr is not None:
                    proceeds += tr.notional - tr.fee
            tr = self.portfolio.sell(
                symbol=self.core_symbol,
                price=current_prices[self.core_symbol],
                ratio=_RETREAT_CORE_SELL_RATIO,
                scenario=RETREAT_SCENARIO,
                reason=f"🏦 大盤退場：核心 {self.core_symbol} 賣出 {_RETREAT_CORE_SELL_RATIO:.0%} 轉入 BOXX",
                timestamp=ts,
                date_str=date_str,
            )
            if tr is not None:
                proceeds += tr.notional - tr.fee
            if proceeds > 0:
                self.portfolio.buy(
                    symbol=BOXX_SYMBOL,
                    asset_class="DEFENSE",
                    price=boxx_px,
                    notional=proceeds,
                    scenario=RETREAT_SCENARIO,
                    reason="🏦 大盤退場：資金停泊於 BOXX（短天期國庫券）",
                    timestamp=ts,
                    date_str=date_str,
                )
            self.retreat_active = True
            self.retreat_episodes.append(
                {
                    "exit_date": date_str,
                    "exit_idx": self.trading_dates.index(current_date),
                    "exit_core_px": current_prices[self.core_symbol],
                    "exit_boxx_px": boxx_px,
                    "proceeds": proceeds,
                    "enter_date": None,
                    "enter_core_px": None,
                    "enter_boxx_px": None,
                }
            )
        elif signal == "ENTER" and self.retreat_active:
            self.portfolio.sell(
                symbol=BOXX_SYMBOL,
                price=boxx_px,
                ratio=1.0,
                scenario=RETREAT_SCENARIO,
                reason="🏦 大盤回場：賣出 BOXX",
                timestamp=ts,
                date_str=date_str,
            )
            nav = self.portfolio.get_total_nav(current_prices)
            targets: list[tuple[str, str, float]] = [
                (self.core_symbol, "CORE", self.core_target_weight)
            ] + [
                (s.symbol, "SATELLITE", self._sat_target_weight(s))
                for s in self.sat_specs
                if not s.retreat_exempt
            ]
            for sym, asset_class, weight in targets:
                pos = self.portfolio.positions.get(sym)
                held = (
                    pos.current_value if pos is not None and pos.side == "LONG" else 0.0
                )
                need = nav * weight - held
                if need < _RETREAT_REBUILD_MIN_USD or self.portfolio.has_short(sym):
                    continue
                px = current_prices[sym]
                kwargs: dict[str, Any] = {}
                if asset_class == "SATELLITE":
                    spec = self.sat_spec[sym]
                    pw, target, sl = self._momentum_entry_levels(
                        spec, prev_rows.get(sym), px
                    )
                    kwargs = {"anchor_base": pw, "target_wall": target, "stop_loss": sl}
                self.portfolio.buy(
                    symbol=sym,
                    asset_class=asset_class,
                    price=px,
                    notional=need,
                    scenario=RETREAT_SCENARIO,
                    reason=f"🏦 大盤回場：{self.core_symbol} 連續 3 日站回 200 日均線，{sym} 依目標權重 {weight:.0%} 重新建倉",
                    timestamp=ts,
                    date_str=date_str,
                    **kwargs,
                )
            self.retreat_active = False
            ep = self.retreat_episodes[-1]
            ep["enter_date"] = date_str
            ep["days"] = self.trading_dates.index(current_date) - ep["exit_idx"]
            ep["enter_core_px"] = current_prices[self.core_symbol]
            ep["enter_boxx_px"] = boxx_px

    def run_simulation(self) -> None:
        """執行回測主迴圈。"""
        self.load_and_prepare_data()
        if not self.trading_dates:
            raise RuntimeError("回測期間無有效交易日")

        first_date = self.trading_dates[0]
        self.setup_initial_portfolio(first_date)

        prev_nav = self.initial_capital
        prev_bench_nav = self.initial_capital
        core = self.core_symbol
        market = self.market_symbol

        for d_idx, current_date in enumerate(self.trading_dates):
            date_str = str(current_date)
            vix_prev = self._get_vix_prev(current_date)

            # -----------------------------------------------------------------
            # 1. 開盤微觀特徵與行情更新
            # -----------------------------------------------------------------
            prev_rows = self._prev_rows(current_date)
            market_prev_row = prev_rows[market]
            core_prev_row = prev_rows[core]

            # 開盤報價
            current_prices: dict[str, float] = {
                sym: self._open_on(sym, current_date) for sym in self.traded_symbols
            }
            market_open = (
                current_prices[market]
                if market in current_prices
                else self._open_on(market, current_date)
            )
            if market not in current_prices:
                current_prices[market] = market_open
            if self.enable_boxx_retreat:
                current_prices[BOXX_SYMBOL] = self._open_on(BOXX_SYMBOL, current_date)
            core_open = current_prices[core]

            morning_nav = self.portfolio.get_total_nav(current_prices)
            if self.active_hedge is not None:
                morning_nav += self.active_hedge.value

            # 各衛星日線 PSQ 與 EV Proxy
            psq: dict[str, float] = {
                sym: self._sat_psq(sym, current_date) for sym in self.sat_symbols
            }
            ev: dict[str, float] = {
                spec.symbol: self._sat_ev(
                    spec,
                    prev_rows[spec.symbol],
                    current_prices[spec.symbol],
                    psq[spec.symbol],
                )
                for spec in self.sat_specs
            }

            # -----------------------------------------------------------------
            # 0.5 BOXX 大盤退場 / 回場（以前一交易日收盤判定，今日開盤執行）
            # -----------------------------------------------------------------
            if self.enable_boxx_retreat:
                self._apply_retreat_signal(
                    current_date, current_prices, prev_rows, date_str
                )
                morning_nav = self.portfolio.get_total_nav(current_prices)
                if self.active_hedge is not None:
                    morning_nav += self.active_hedge.value
            retreating = self.retreat_active

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
            # 危機判定以大盤訊號代理 (SPY) 為準
            spy_gamma_flip = (
                float(market_prev_row["sma20"])
                if market_prev_row is not None
                else market_open
            )
            is_market_critical = vix_prev >= 25.0 or (
                vix_prev >= 20.0 and market_open < spy_gamma_flip
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
                    for sat_sym in self.sat_symbols:
                        sat_pos = self.portfolio.positions.get(sat_sym)
                        if sat_pos is not None and sat_pos.shares > 0:
                            row = prev_rows[sat_sym]
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
                    for sat_sym in self.sat_symbols:
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
            spy_pos = self.portfolio.positions.get(core)
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
                    shares_to_trim = excess_usd / core_open
                    trim_ratio = min(1.0, shares_to_trim / spy_pos.shares)

                    # 賣出核心超額
                    self.portfolio.sell(
                        symbol=core,
                        price=core_open,
                        ratio=trim_ratio,
                        scenario=RolloverScenario.CORE_DEPLOYMENT.value,
                        reason=f"{core} 核心配置升值至 {spy_alloc:.1%} (超額 ${excess_usd:,.0f})，執行超額再平衡",
                        timestamp=f"{date_str} 09:30:00",
                        date_str=date_str,
                    )

                    # 決定去向: 突破動能 (>80) 且非做空中的衛星，部署 core_deploy_ratio 超額，否則留存 CASH。
                    # 大盤退場期間不部署到衛星（留在現金）。
                    deploy_usd = excess_usd * self.core_deploy_ratio
                    if not retreating:
                        for cand in self._core_deploy_candidates(psq):
                            if not (
                                psq[cand] > _BREAKOUT_READY_THRESHOLD
                                and not is_market_critical
                                and not self.portfolio.has_short(cand)
                            ):
                                continue
                            cand_open = current_prices[cand]
                            pw, target, sl = self._momentum_entry_levels(
                                self.sat_spec[cand], prev_rows[cand], cand_open
                            )
                            self.portfolio.buy(
                                symbol=cand,
                                asset_class="SATELLITE",
                                price=cand_open,
                                notional=deploy_usd,
                                scenario=RolloverScenario.CORE_DEPLOYMENT.value,
                                reason=f"{core} 超額資金分流至突破候選 {cand} (PSQ={psq[cand]:.0f})",
                                timestamp=f"{date_str} 09:30:00",
                                date_str=date_str,
                                anchor_base=pw,
                                target_wall=target,
                                stop_loss=sl,
                                entry_regime="REGIME_III_RIGHT_MOMENTUM",
                            )
                            break

                # Covered Call Overlay 收益增強 (情境七)
                # 當核心貼近 Call Wall 阻力 (spot >= call_wall * 0.98) 且非暴跌日，每週計入 0.08% 權利金增強
                spy_cw = (
                    float(core_prev_row["high10"])
                    if core_prev_row is not None
                    else core_open * 1.05
                )
                if (
                    core_open >= spy_cw * 0.98
                    and spy_pos.shares >= 50.0
                    and (d_idx % 5 == 0)
                ):
                    premium_yield = (
                        spy_pos.current_value * 0.0008
                    )  # ~0.4% 月化期權權利金收益
                    self.portfolio.add_income(
                        symbol=core,
                        amount=premium_yield,
                        scenario=RolloverScenario.COVERED_CALL_PROFIT_LOCK.value,
                        reason=f"{core} 觸及頂部做市商阻力牆 (${spy_cw:.2f})，覆蓋賣出 OTM Covered Call 收益",
                        timestamp=f"{date_str} 09:30:00",
                        date_str=date_str,
                    )

            # -------------------------------------------------------------
            # 5. 情境二: OPPORTUNITY_COST (衛星間機會成本動能輪動)
            # -------------------------------------------------------------
            # 「衰退中的衛星 → 突破中的衛星」單向輪動。頻率控制見
            # BacktestUniverse.rotation_policy。大盤退場期間暫停。
            if not retreating:
                if self.universe.rotation_policy == "portfolio":
                    self._rotate_portfolio_level(
                        current_date, current_prices, prev_rows, psq, ev, date_str
                    )
                else:
                    for src in self.sat_symbols:
                        src_pos = self.portfolio.positions.get(src)
                        src_rot_cd = self.last_opp_cost_date.get(src)
                        can_rot = (
                            src_rot_cd is None
                            or (current_date - src_rot_cd).days >= self.opp_cost_cd_days
                        )
                        if not (src_pos is not None and src_pos.shares > 0 and can_rot):
                            continue
                        best = self._best_rotation_target(src, psq, ev)
                        if best is None:
                            continue
                        self._execute_rotation(
                            src,
                            best[0],
                            best[1],
                            current_date,
                            current_prices,
                            prev_rows,
                            psq,
                            date_str,
                        )

            # -----------------------------------------------------------------
            # 6. 盤中小時線走訪迴圈: SL1-4 / TP1-3 / TRANSITION / SHORT_ENTRY
            # -----------------------------------------------------------------
            day_hours: dict[str, pd.DataFrame] = {
                sym: self._day_hours(sym, current_date) for sym in self.traded_symbols
            }
            if self.universe.hourly_alignment == "timestamp":
                common = day_hours[core].index
                for sym in self.sat_symbols:
                    common = common.intersection(day_hours[sym].index)
                common = common.sort_values()
                day_hours = {sym: df.loc[common] for sym, df in day_hours.items()}
            num_bars = min(len(df) for df in day_hours.values())

            for b_idx in range(num_bars):
                self._bar_counter += 1
                bars = {sym: day_hours[sym].iloc[b_idx] for sym in self.sat_symbols}

                ts_str = str(day_hours[core].index[b_idx])

                # -------------------------------------------------------------
                # 6.1 情境八: TRANSITION_ENGINE (左側接刀演化為右側動能)
                # -------------------------------------------------------------
                for sym in self.sat_symbols:
                    bar = bars[sym]
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
                            # 上移停損至保本（防護，退場期間照常）
                            pos.stop_loss = max(pos.avg_cost, pos.anchor_base)
                            pos.is_pyramided = True
                            pos.entry_regime = "REGIME_III_RIGHT_MOMENTUM"
                            # 授權 Pyramiding 加碼 20%（加碼，大盤退場期間暫停）
                            add_notional = pos.current_value * 0.20
                            if self.portfolio.cash >= add_notional and not retreating:
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
                if not is_market_critical and not retreating:
                    cash_above_reserve = max(
                        0.0,
                        self.portfolio.cash - (morning_nav * self.cash_target_weight),
                    )
                    for spec in self.sat_specs:
                        sym = spec.symbol
                        bar = bars[sym]
                        target_wt = self._sat_target_weight(spec)
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
                                window = day_hours[sym]
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
                # 6.2 / 6.3 情境九: SHORT_ENTRY (Regime V 破位追空) 與空頭鏡像出場
                # -------------------------------------------------------------
                for spec in self.sat_specs:
                    if not spec.allow_short:
                        continue
                    self._short_entry_and_exit(
                        spec.symbol,
                        bars[spec.symbol],
                        morning_nav,
                        vix_prev,
                        ts_str,
                        date_str,
                        allow_new=not retreating,
                    )

                # -------------------------------------------------------------
                # 6.4 情境三: SATELLITE_REBALANCE (微觀結構雙軌防洗盤 SL1-4 / TP1-3)
                # -------------------------------------------------------------
                for sat_sym in self.sat_symbols:
                    bar = bars[sat_sym]
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
                    elif self.enable_pyramid_add and not retreating:
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
            close_prices: dict[str, float] = {
                sym: self._close_price_on(sym, current_date)
                for sym in self.traded_symbols
            }
            market_close = (
                close_prices[market]
                if market in close_prices
                else self._close_price_on(market, current_date)
            )
            if self.enable_boxx_retreat:
                close_prices[BOXX_SYMBOL] = self._close_price_on(
                    BOXX_SYMBOL, current_date
                )
            daily_nav = self.portfolio.get_total_nav(close_prices)
            if self.active_hedge is not None:
                daily_nav += self._mark_hedge(current_date, market_close, date_str)

            # 基準投組合約價值（單純持有、不退場）
            bench_nav = self.benchmark_cash
            for sym, _w in self.universe.benchmark_weights:
                bench_nav = bench_nav + self.benchmark_shares[sym] * close_prices[sym]
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
            symbol_values: dict[str, float] = dict()
            for sym in self.traded_symbols + (
                [BOXX_SYMBOL] if self.enable_boxx_retreat else []
            ):
                if self.portfolio.has_long(sym):
                    symbol_values[sym] = self.portfolio.positions[sym].current_value
                elif self.portfolio.has_short(sym):
                    symbol_values[sym] = (
                        -abs(self.portfolio.positions[f"{sym}_SHORT"].shares)
                        * close_prices[sym]
                    )
                else:
                    symbol_values[sym] = 0.0
            spy_val = (
                self.portfolio.positions[core].current_value
                if core in self.portfolio.positions
                else 0.0
            )
            nvda_val = symbol_values[self.alpha_symbol]
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
                    symbol_values=symbol_values,
                )
            )

    def _best_rotation_target(
        self, src: str, psq: dict[str, float], ev: dict[str, float]
    ) -> Optional[tuple[str, float]]:
        """src 動能衰竭時，其餘衛星中 EV 價差最大的突破候選 (標的, 價差)。"""
        if psq[src] >= _MOMENTUM_DECAY_THRESHOLD:
            return None
        best: Optional[tuple[str, float]] = None
        for dst in self.sat_symbols:
            if dst == src or self.portfolio.has_short(dst):
                continue
            spread = ev[dst] - ev[src]
            if (
                psq[dst] > _BREAKOUT_READY_THRESHOLD
                and spread > self.opp_cost_hurdle
                and (best is None or spread > best[1])
            ):
                best = (dst, spread)
        return best

    def _rotate_portfolio_level(
        self,
        current_date: date,
        current_prices: dict[str, float],
        prev_rows: dict[str, Optional[pd.Series]],
        psq: dict[str, float],
        ev: dict[str, float],
        date_str: str,
    ) -> None:
        """投組層級輪動：冷卻期內不換股；否則在所有「衰退 → 突破」組合中只執行
        EV 價差最大的一筆。"""
        last = self.last_portfolio_rotation_date
        if last is not None and (current_date - last).days < self.opp_cost_cd_days:
            return
        pick: Optional[tuple[str, str, float]] = None
        for src in self.sat_symbols:
            pos = self.portfolio.positions.get(src)
            if pos is None or pos.shares <= 0:
                continue
            best = self._best_rotation_target(src, psq, ev)
            if best is not None and (pick is None or best[1] > pick[2]):
                pick = (src, best[0], best[1])
        if pick is None:
            return
        if self._execute_rotation(
            pick[0],
            pick[1],
            pick[2],
            current_date,
            current_prices,
            prev_rows,
            psq,
            date_str,
        ):
            self.last_portfolio_rotation_date = current_date

    def _execute_rotation(
        self,
        src: str,
        dst: str,
        ev_spread: float,
        current_date: date,
        current_prices: dict[str, float],
        prev_rows: dict[str, Optional[pd.Series]],
        psq: dict[str, float],
        date_str: str,
    ) -> bool:
        src_pos = self.portfolio.positions[src]
        rot_ratio = (
            _ROLLOVER_RATIO_HIGH_PROFIT
            if src_pos.return_pct > _PROFIT_LOCK_PROFIT_PCT_THRESHOLD
            else _ROLLOVER_RATIO_STANDARD
        )
        if (
            src_pos.current_value < 1000.0
            or (src_pos.current_value * (1.0 - rot_ratio)) < 500.0
        ):
            rot_ratio = 1.0
        trade = self.portfolio.sell(
            symbol=src,
            price=current_prices[src],
            ratio=rot_ratio,
            scenario=RolloverScenario.OPPORTUNITY_COST.value,
            reason=f"{src} 動能衰竭 (PSQ={psq[src]:.0f}) 轉倉至突破標的 {dst} (PSQ={psq[dst]:.0f}, ΔEV=+{ev_spread * 100:.1f}%)",
            timestamp=f"{date_str} 09:30:00",
            date_str=date_str,
        )
        if trade is None:
            return False
        self.last_opp_cost_date[src] = current_date
        proceeds = trade.notional - trade.fee
        dst_open = current_prices[dst]
        pw, target, sl = self._momentum_entry_levels(
            self.sat_spec[dst], prev_rows[dst], dst_open
        )
        self.portfolio.buy(
            symbol=dst,
            asset_class="SATELLITE",
            price=dst_open,
            notional=proceeds,
            scenario=RolloverScenario.OPPORTUNITY_COST.value,
            reason=f"機會成本轉倉買入 {dst} (接收 {src} 輪動資金)",
            timestamp=f"{date_str} 09:30:00",
            date_str=date_str,
            anchor_base=pw,
            target_wall=target,
            stop_loss=sl,
            entry_regime="REGIME_III_RIGHT_MOMENTUM",
        )
        return True

    def _short_entry_and_exit(
        self,
        sym: str,
        bar: pd.Series,
        morning_nav: float,
        vix_prev: float,
        ts_str: str,
        date_str: str,
        allow_new: bool = True,
    ) -> None:
        """情境九 Regime V 破位追空（6.2）與空頭部位鏡像出場（6.3）。

        原引擎只對 NVDA 評估；一般化為每檔 allow_short 的衛星依序評估。
        大盤退場期間不開新空單（allow_new=False），既有空單的停損／停利照常。
        """
        short_pos_key = f"{sym}_SHORT"
        if allow_new and not self.portfolio.has_short(sym):
            nvda_c = float(bar["close"])
            nvda_pw = float(bar["low10_prev"])
            nvda_gf = float(bar["sma20_prev"])
            nvda_l60 = float(bar["low60_prev"])
            nvda_atr1d = float(bar["atr14_prev"])
            nvda_atr1h = float(bar["atr_1h"])
            nvda_rsi = float(bar["rsi"])
            nvda_vwap = float(bar["session_vwap"])
            nvda_vol_r = float(bar["vol_ratio"])

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
                if self.portfolio.has_long(sym):
                    long_pos = self.portfolio.positions.get(sym)
                    if long_pos is not None and long_pos.current_value < 500.0:
                        self.portfolio.sell(
                            symbol=sym,
                            price=nvda_c,
                            ratio=1.0,
                            scenario=RolloverScenario.SATELLITE_REBALANCE.value,
                            reason=f"🚨 偵測到破位追空訊號，清理微量殘存多頭 (${long_pos.current_value:.2f}) 以放行做空",
                            timestamp=ts_str,
                            date_str=date_str,
                        )

                if not self.portfolio.has_long(sym):
                    # 構建 ShortEntryEvaluation
                    ev = ShortEntryEvaluation(
                        all_passed=True,
                        reason="2025 破位追空微觀結構確認",
                        structure_directive="SHORT_EQUITY",
                        sub_mode="破位追空",
                        conditions=(True, True, True, True, True, True),
                        spot=nvda_c,
                        resistance_wall=nvda_gf,
                        call_wall=float(bar["high10_prev"]),
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
                                symbol=sym,
                                price=nvda_c,
                                shares=float(sizing.share_qty),
                                stop_price=levels.stop_price,
                                target_price=levels.target_price,
                                scenario=RolloverScenario.SHORT_ENTRY.value,
                                reason=f"Regime V 破位追空 (跌破底牆 ${nvda_pw:.2f} 與 Flip ${nvda_gf:.2f}，盈虧比 {levels.reward_risk_ratio:.2f}:1)",
                                timestamp=ts_str,
                                date_str=date_str,
                            )

        # 空頭部位鏡像微觀結構出場 (COVER / SL / TP)
        active_short = self.portfolio.positions.get(short_pos_key)
        if active_short is not None:
            nvda_h = float(bar["close"])
            # 停損觸發: 價格反彈突破阻力防守線
            if nvda_h >= active_short.stop_loss and active_short.stop_loss > 0:
                self.portfolio.cover(
                    symbol=sym,
                    price=nvda_h,
                    ratio=1.0,
                    scenario=RolloverScenario.SATELLITE_REBALANCE.value,
                    reason=f"🚨 做空部位觸發結構停損 (${nvda_h:.2f} >= ${active_short.stop_loss:.2f})",
                    timestamp=ts_str,
                    date_str=date_str,
                )
            # 獲利了結觸發: 價格到達次級負 GEX 節點
            elif nvda_h <= active_short.anchor_base and active_short.anchor_base > 0:
                self.portfolio.cover(
                    symbol=sym,
                    price=nvda_h,
                    ratio=1.0,
                    scenario=RolloverScenario.SATELLITE_REBALANCE.value,
                    reason=f"🎯 做空部位到達目標價 (${nvda_h:.2f} <= ${active_short.anchor_base:.2f}) 全額獲利了結",
                    timestamp=ts_str,
                    date_str=date_str,
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
        - 大盤負 Gamma = 大盤訊號代理 (SPY，非核心持倉) 開盤 < SMA20 (與本引擎
          MARGIN_DEFENSE 同一代理)
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

        spy_open = current_prices.get(self.market_symbol, 0.0)
        is_negative_gamma = spy_open > 0 and spy_open < spy_gamma_flip

        longs = [sym for sym in self.sat_symbols if self.portfolio.has_long(sym)]
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
        for sat_sym in self.sat_symbols:
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
        """相對大盤訊號代理 (SPY) 的 Beta；保護性 Put 以 SPY 定價，故 Delta 以 SPY 股數等值計。"""
        if symbol == self.market_symbol:
            return 1.0
        a = self.daily_feat.get(symbol)
        if a is None:
            return 0.0
        b = self.daily_feat[self.market_symbol]
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
        # 保護性 Put 買的是 SPY（大盤訊號代理；VOO 期權流動性遠不及 SPY）
        spy_price = current_prices.get(self.market_symbol, 0.0)
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
