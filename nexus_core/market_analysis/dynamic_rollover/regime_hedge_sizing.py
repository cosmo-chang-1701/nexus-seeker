"""兩級體制避險部位求解（純計算葉模組，無 I/O、無 async）。

取代 margin_defense.py 的反向 ETF 路由：
- 第一級（大盤 NORMAL、個股跌破 PutWall）只允許 PSQ（-1x），佔 NAV 10–30%。
- 第二級（大盤 SHORT_GAMMA_CRITICAL／SYSTEMIC_LIQUIDITY_CRISIS）才允許 SQQQ／SOXS（-3x），
  且另須通過「−3x 啟動紅線」（VIX／VTS／SPY 低於 Flip），否則凍結轉 BOXX。

帳戶為現金帳戶、零融資：多頭現貨市值 + 反向 ETF 市值 + 現金等價物 <= NAV。
買反向 ETF 先用現金等價物，不足才賣多頭現貨。本模組只負責「算」，由引擎負責下單與推播。
"""

import math
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping, Optional, Sequence

# 現金等價物（淨 Delta 乘數 0，可動用資金）
CASH_EQUIVALENT_SYMBOLS: frozenset[str] = frozenset({"BOXX", "BIL", "SHV", "SGOV"})
# 反向 ETF 槓桿乘數
INVERSE_ETF_LEVERAGE: dict[str, float] = {"PSQ": -1.0, "SQQQ": -3.0, "SOXS": -3.0}
# 半導體標的（對沖改用 SOXS；其餘以 SQQQ）
SEMICONDUCTOR_SYMBOLS: frozenset[str] = frozenset(
    {
        "NVDA",
        "TSM",
        "AMD",
        "AVGO",
        "MU",
        "INTC",
        "ASML",
        "LRCX",
        "AMAT",
        "TXN",
        "QCOM",
        "ADI",
        "MRVL",
        "ARM",
        "SMH",
        "SOXX",
    }
)

# 等效淨 Delta 守門員（佔 NAV %）
NET_DELTA_FLOOR_PCT: float = -120.0
NET_DELTA_CEILING_PCT: float = 145.0
# 對沖模式目標淨 Delta（0%～-20% 區間中點）
HEDGE_MODE_TARGET_NET_DELTA_PCT: float = -10.0
HEDGE_MODE_NET_DELTA_RANGE_PCT: tuple[float, float] = (-20.0, 0.0)
# 止損轉空：SQQQ+SOXS 合計上限（= -120% 淨 Delta）
STOP_SHORT_MAX_INVERSE_PCT: float = 40.0
# 剩餘多頭低於此 NAV % 視為已清倉 → 止損轉空模式
STOP_SHORT_RESIDUAL_LONG_PCT: float = 1.0
# 第一級 PSQ 佔 NAV 區間
TIER1_PSQ_MIN_PCT: float = 10.0
TIER1_PSQ_MAX_PCT: float = 30.0
# 財報日（美東日期）距今 0–2 天 ≈ 48 小時內視為財報隔離
EARNINGS_ISOLATION_DAYS: int = 2
# 受影響板塊 -3x 腿乘上此係數，削減部分轉 BOXX
EARNINGS_ISOLATION_WEIGHT_FACTOR: float = 0.5
# 各反向 ETF 的 STOP MARKET 距離（%）
STOP_MARKET_PCT: dict[str, float] = {"PSQ": 3.5, "SQQQ": 7.0, "SOXS": 7.0}

# 守門員比較的浮點容差
_GUARD_EPS: float = 1e-9

# −3x 啟動紅線（PRE_CALIBRATION：尚未回測，數值為保守初值）
# VIX 瞬時值下限（含）
MINUS3X_ACTIVATION_MIN_VIX: float = 22.0
# VTS 比率下限（嚴格大於）
MINUS3X_ACTIVATION_MIN_VTS: float = 1.0
# SPY 低於 Gamma Flip 的最小幅度 %（含）
MINUS3X_ACTIVATION_MIN_FLIP_BREACH_PCT: float = 1.0
_REDLINE_PREFIX: str = "−3x 啟動紅線未達："


def minus3x_activation_block_reason(
    vix: Optional[float],
    vts_ratio: Optional[float],
    spy_spot: Optional[float],
    gamma_flip: Optional[float],
) -> Optional[str]:
    """−3x 啟動紅線：三條件全過回傳 None，否則回傳列出所有未達／未知條件的原因。"""
    failed: list[str] = []
    if vix is None:
        failed.append("VIX 未知")
    elif vix < MINUS3X_ACTIVATION_MIN_VIX - _GUARD_EPS:
        failed.append(f"VIX {vix:.2f} < {MINUS3X_ACTIVATION_MIN_VIX:.2f}")
    if vts_ratio is None:
        failed.append("VTS 未知")
    elif vts_ratio <= MINUS3X_ACTIVATION_MIN_VTS + _GUARD_EPS:
        failed.append(f"VTS {vts_ratio:.2f} ≤ {MINUS3X_ACTIVATION_MIN_VTS:.2f}")
    if spy_spot is None or spy_spot <= 0 or gamma_flip is None or gamma_flip <= 0:
        failed.append("SPY 低於 Flip 幅度未知")
    else:
        breach = (gamma_flip - spy_spot) / gamma_flip * 100.0
        if breach < MINUS3X_ACTIVATION_MIN_FLIP_BREACH_PCT - _GUARD_EPS:
            failed.append(
                f"SPY 低於 Flip {breach:.2f}%"
                f"（需 ≥ {MINUS3X_ACTIVATION_MIN_FLIP_BREACH_PCT:.2f}%）"
            )
    if not failed:
        return None
    return _REDLINE_PREFIX + "；".join(failed)


@dataclass(frozen=True)
class HedgePosition:
    symbol: str  # 大寫
    market_value: float  # 帶號市值（現貨；空頭為負）


@dataclass(frozen=True)
class InverseLeg:
    symbol: str  # "PSQ" | "SQQQ" | "SOXS"
    target_value: float  # 本次「新增買進」金額（>=0）
    reference_price: float
    shares: int
    stop_pct: float
    stop_trigger_price: float
    earnings_reduced: bool
    earnings_symbols: tuple[str, ...]


@dataclass(frozen=True)
class HedgePlan:
    tier: int  # 1 | 2
    mode: str  # "TIER1_PSQ" | "HEDGE" | "STOP_SHORT"
    nav: float
    net_delta_before_pct: float
    net_delta_after_pct: float
    legs: tuple[InverseLeg, ...]
    liquidate_symbols: tuple[str, ...]
    liquidate_value: float
    sell_for_funding_value: float
    boxx_target_value: float
    within_guard: bool
    minus3x_frozen_reason: Optional[str]
    notes: tuple[str, ...]


def leverage_multiplier(symbol: str) -> float:
    """現金等價物 0.0；反向 ETF 取其槓桿；其餘 +1.0。"""
    sym = symbol.upper()
    if sym in CASH_EQUIVALENT_SYMBOLS:
        return 0.0
    if sym in INVERSE_ETF_LEVERAGE:
        return INVERSE_ETF_LEVERAGE[sym]
    return 1.0


def equivalent_net_delta_pct(positions: Sequence[HedgePosition], nav: float) -> float:
    """等效淨 Delta（佔 NAV %）。nav <= 0 → 0.0。"""
    if nav <= 0:
        return 0.0
    total = sum(leverage_multiplier(p.symbol) * p.market_value for p in positions)
    return total / nav * 100.0


def is_within_net_delta_guard(net_delta_pct: float) -> bool:
    return (
        NET_DELTA_FLOOR_PCT - _GUARD_EPS
        <= net_delta_pct
        <= NET_DELTA_CEILING_PCT + _GUARD_EPS
    )


def classify_hedge_etf(symbol: str) -> str:
    """半導體 → SOXS；大型科技、寬基 ETF 與未分類標的一律 SQQQ。"""
    sym = symbol.upper()
    if sym in SEMICONDUCTOR_SYMBOLS:
        return "SOXS"
    # lazy import：避免模組載入時拉進 pandas 等重依賴
    from market_analysis.risk_engine import SECTOR_BENCHMARK_MAP

    if SECTOR_BENCHMARK_MAP.get(sym) == "SMH":
        return "SOXS"
    return "SQQQ"


def _is_long_stock(p: HedgePosition) -> bool:
    return leverage_multiplier(p.symbol) == 1.0 and p.market_value > 0


def split_long_exposure(positions: Sequence[HedgePosition]) -> dict[str, float]:
    """多頭現貨市值依對沖 ETF 板塊拆分；兩鍵恆在。"""
    out: dict[str, float] = {"SOXS": 0.0, "SQQQ": 0.0}
    for p in positions:
        if _is_long_stock(p):
            out[classify_hedge_etf(p.symbol)] += p.market_value
    return out


def earnings_isolated_symbols(
    symbols: Iterable[str],
    earnings_dates: Mapping[str, Optional[date]],
    today_et: date,
) -> tuple[str, ...]:
    """財報日距今 0..EARNINGS_ISOLATION_DAYS 天者（排序）。"""
    hit: set[str] = set()
    for s in symbols:
        d = earnings_dates.get(s)
        if d is None:
            continue
        if 0 <= (d - today_et).days <= EARNINGS_ISOLATION_DAYS:
            hit.add(s)
    return tuple(sorted(hit))


def _make_leg(
    symbol: str,
    target_value: float,
    prices: Mapping[str, float],
    notes: list[str],
    earnings_symbols: tuple[str, ...] = (),
) -> InverseLeg:
    price = float(prices.get(symbol, 0.0) or 0.0)
    if not math.isfinite(price) or price <= 0:
        price = 0.0
        shares = 0
        stop_trigger = 0.0
        notes.append(f"{symbol} 參考價缺失，股數待確認")
    else:
        shares = int(target_value // price)
        stop_trigger = round(price * (1 - STOP_MARKET_PCT[symbol] / 100.0), 2)
    return InverseLeg(
        symbol=symbol,
        target_value=target_value,
        reference_price=price,
        shares=shares,
        stop_pct=STOP_MARKET_PCT[symbol],
        stop_trigger_price=stop_trigger,
        earnings_reduced=bool(earnings_symbols),
        earnings_symbols=earnings_symbols,
    )


def build_tier1_plan(
    positions: Sequence[HedgePosition],
    nav: float,
    broken_symbols: Sequence[str],
    cash_equivalent_value: float,
    prices: Mapping[str, float],
) -> HedgePlan:
    """第一級：只買 PSQ，佔 NAV 10–30%（依跌破 PutWall 持股佔比）。"""
    before = equivalent_net_delta_pct(positions, nav)
    notes: list[str] = []
    cash = max(0.0, cash_equivalent_value)

    def _empty() -> HedgePlan:
        return HedgePlan(
            tier=1,
            mode="TIER1_PSQ",
            nav=nav,
            net_delta_before_pct=before,
            net_delta_after_pct=before,
            legs=(),
            liquidate_symbols=(),
            liquidate_value=0.0,
            sell_for_funding_value=0.0,
            boxx_target_value=cash,
            within_guard=is_within_net_delta_guard(before),
            minus3x_frozen_reason=None,
            notes=tuple(notes),
        )

    if nav <= 0:
        return _empty()

    broken = {s.upper() for s in broken_symbols}
    broken_value = sum(
        p.market_value
        for p in positions
        if _is_long_stock(p) and p.symbol.upper() in broken
    )
    pct = min(max(broken_value / nav * 100.0, TIER1_PSQ_MIN_PCT), TIER1_PSQ_MAX_PCT)
    existing_psq = sum(p.market_value for p in positions if p.symbol.upper() == "PSQ")
    add = max(0.0, pct / 100.0 * nav - existing_psq)
    if add <= 0:
        return _empty()

    sell = 0.0
    if add > cash:
        need = add - cash
        sell = min(need, broken_value)
        if sell > 0:
            notes.append("不足部分由跌破 PutWall 的持股先行減碼支應")
        if need > broken_value:
            add = cash + broken_value
            notes.append(
                "現金等價物與跌破 PutWall 持股合計不足，PSQ 新增金額縮小至 "
                f"{add:,.0f}"
            )
    if add <= 0:
        return _empty()

    leg = _make_leg("PSQ", add, prices, notes)
    boxx = max(0.0, cash + sell - add)
    after = before - (sell + add) / nav * 100.0
    return HedgePlan(
        tier=1,
        mode="TIER1_PSQ",
        nav=nav,
        net_delta_before_pct=before,
        net_delta_after_pct=after,
        legs=(leg,),
        liquidate_symbols=(),
        liquidate_value=0.0,
        sell_for_funding_value=sell,
        boxx_target_value=boxx,
        within_guard=is_within_net_delta_guard(after),
        minus3x_frozen_reason=None,
        notes=tuple(notes),
    )


def build_tier2_plan(
    positions: Sequence[HedgePosition],
    nav: float,
    liquidate_symbols: Sequence[str],
    cash_equivalent_value: float,
    prices: Mapping[str, float],
    earnings_dates: Mapping[str, Optional[date]],
    today_et: date,
    minus3x_frozen_reason: Optional[str] = None,
) -> HedgePlan:
    """第二級：對沖（淨 Delta 目標 -10%）或止損轉空（SQQQ+SOXS <= 40% NAV）。"""
    before = equivalent_net_delta_pct(positions, nav)
    notes: list[str] = []
    cash = max(0.0, cash_equivalent_value)
    liq_set = {s.upper() for s in liquidate_symbols}

    liq_positions = [
        p for p in positions if _is_long_stock(p) and p.symbol.upper() in liq_set
    ]
    kept_positions = [
        p for p in positions if _is_long_stock(p) and p.symbol.upper() not in liq_set
    ]
    liquidate_value = sum(p.market_value for p in liq_positions)
    kept_long = sum(p.market_value for p in kept_positions)
    existing_inverse_delta = sum(
        leverage_multiplier(p.symbol) * p.market_value
        for p in positions
        if p.symbol.upper() in INVERSE_ETF_LEVERAGE
    )
    existing_minus3x_value = sum(
        p.market_value for p in positions if p.symbol.upper() in ("SQQQ", "SOXS")
    )
    funds = cash + liquidate_value

    def _empty() -> HedgePlan:
        return HedgePlan(
            tier=2,
            mode="HEDGE",
            nav=nav,
            net_delta_before_pct=before,
            net_delta_after_pct=before,
            legs=(),
            liquidate_symbols=(),
            liquidate_value=0.0,
            sell_for_funding_value=0.0,
            boxx_target_value=cash,
            within_guard=is_within_net_delta_guard(before),
            minus3x_frozen_reason=minus3x_frozen_reason,
            notes=tuple(notes),
        )

    if nav <= 0:
        return _empty()

    residual_low = kept_long < STOP_SHORT_RESIDUAL_LONG_PCT / 100.0 * nav
    if residual_low and liquidate_value <= 0:
        return _empty()
    mode = "STOP_SHORT" if residual_low else "HEDGE"

    # 板塊權重
    weight_src = liq_positions if mode == "STOP_SHORT" else kept_positions
    split = split_long_exposure(weight_src)
    total_split = split["SOXS"] + split["SQQQ"]
    if total_split > 0:
        weights = {k: v / total_split for k, v in split.items()}
    else:
        weights = {"SOXS": 0.0, "SQQQ": 1.0}

    # 求解新增反向名目 S 與減碼 x
    x_cap = 0.0
    if mode == "HEDGE":
        t_nav = HEDGE_MODE_TARGET_NET_DELTA_PCT / 100.0 * nav
        base = kept_long + existing_inverse_delta
        s_amt = max(0.0, (base - t_nav) / 3.0)
        if s_amt > funds:
            x = (base - 3.0 * funds - t_nav) / 4.0
            x_cap = min(max(x, 0.0), kept_long)
            s_amt = funds + x_cap
    else:
        cap = STOP_SHORT_MAX_INVERSE_PCT / 100.0 * nav - existing_minus3x_value
        s_amt = max(0.0, min(cap, funds))

    amounts: dict[str, float] = {k: s_amt * w for k, w in weights.items()}
    earn_syms: dict[str, tuple[str, ...]] = {"SOXS": (), "SQQQ": ()}

    # 財報隔離降權
    today_syms = [p.symbol.upper() for p in weight_src]
    for etf in ("SOXS", "SQQQ"):
        sector = [s for s in today_syms if classify_hedge_etf(s) == etf]
        hit = earnings_isolated_symbols(sector, earnings_dates, today_et)
        if hit and amounts[etf] > 0:
            amounts[etf] *= EARNINGS_ISOLATION_WEIGHT_FACTOR
            earn_syms[etf] = hit
            notes.append(
                f"{etf} 腿因 {'、'.join(hit)} 財報隔離（48 小時內），"
                f"部位降為 {EARNINGS_ISOLATION_WEIGHT_FACTOR:.0%}，削減部分轉入 BOXX"
            )

    # 總經事件凍結
    if minus3x_frozen_reason is not None:
        amounts = {"SOXS": 0.0, "SQQQ": 0.0}
        notes.append(f"-3x 反向 ETF 已凍結：{minus3x_frozen_reason}")

    def _sell_for(s_final: float) -> float:
        return min(x_cap, max(0.0, s_final - funds))

    def _after_pct(s_final: float) -> float:
        sell = _sell_for(s_final)
        return (kept_long - sell + existing_inverse_delta - 3.0 * s_final) / nav * 100.0

    # 守門員
    s_final = amounts["SOXS"] + amounts["SQQQ"]
    if s_final > 0 and _after_pct(s_final) < NET_DELTA_FLOOR_PCT - _GUARD_EPS:
        lo, hi = 0.0, 1.0
        for _ in range(80):
            mid = (lo + hi) / 2.0
            if _after_pct(s_final * mid) >= NET_DELTA_FLOOR_PCT:
                lo = mid
            else:
                hi = mid
        amounts = {k: v * lo for k, v in amounts.items()}
        notes.append(
            f"淨 Delta 守門員：反向部位縮減至等效淨 Delta 不低於 {NET_DELTA_FLOOR_PCT:.0f}%"
        )
        s_final = amounts["SOXS"] + amounts["SQQQ"]

    sell = _sell_for(s_final)
    if sell > 0:
        notes.append("現金等價物不足，需賣出部分多頭現貨支應（現金帳戶零融資）")
    boxx = max(0.0, funds + sell - s_final)
    after = _after_pct(s_final)

    legs = tuple(
        _make_leg(etf, amounts[etf], prices, notes, earn_syms[etf])
        for etf in ("SOXS", "SQQQ")
        if amounts[etf] > 0
    )
    return HedgePlan(
        tier=2,
        mode=mode,
        nav=nav,
        net_delta_before_pct=before,
        net_delta_after_pct=after,
        legs=legs,
        liquidate_symbols=tuple(sorted({p.symbol.upper() for p in liq_positions})),
        liquidate_value=liquidate_value,
        sell_for_funding_value=sell,
        boxx_target_value=boxx,
        within_guard=is_within_net_delta_guard(after),
        minus3x_frozen_reason=minus3x_frozen_reason,
        notes=tuple(notes),
    )
