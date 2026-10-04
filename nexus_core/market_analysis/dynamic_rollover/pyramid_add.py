"""pyramid_add.py — 情境十：PYRAMID_ADD 順勢金字塔加碼。

讓右側進場的獲利部位能在趨勢延續時**加碼**，而非只能減碼——是 handoff.md
§1.2「曝險單調遞減、沒有遞增路徑」問題的核心對症機制。

與 `transition_engine.py` 路徑一的 `OPEN_PYRAMID` 刻意分離：路徑一是「左側倉
進化為右側倉」的一次性狀態切換（由 `entry_regime` 觸發、`pyramided` 旗標燒掉，
只觸發一次），本情境是「任何右側獲利倉在趨勢延續時的例行加碼」（可重複觸發至
`_PYRAMID_MAX_ADDS` 次）。兩者觸發源與次數上限皆不同，合併會讓路徑一的一次性
保證失效。

八項觸發條件全部為 AND（見 `evaluate_pyramid_add_impl` docstring）。倉位模型
直接沿用 `short_entry_sizing.py` 已驗證的「風險預算 ÷ 停損距離」模型，方向
反轉：停損距離為 `Spot − 參考停損`。條件二保證參考停損 >= avg_cost，因此加碼只
動用「已實現的帳面利潤」承險，不增加原始部位的本金曝險——這是金字塔加碼與盲目
攤平的唯一分界，任何修改都不得放寬條件二。

條件二～四自 2026-10 起改由多時間框架擠壓判定（`market_analysis/squeeze_entry/`，
規格 docs/strategies/10）：原本讀取的 `dynamic_strategy_state["ratchet_stop"]` 在
顧問模式下永遠不會寫入（`advisory_mode.py` 丟棄 HOLD 指令的狀態補丁，而所有多頭
現貨皆為顧問模式），導致條件二恆不成立；改以擠壓參考停損即時計算，不變式語意
（停損 >= 成本）不變。
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, NamedTuple, Optional

from config import get_vix_tier, get_vix_sizing_multiplier
from market_analysis.kelly_priors import KELLY_PRIOR_ODDS, get_win_rate_prior
from market_analysis.risk_engine import kelly_position_fraction

from .constants import (
    _PYRAMID_ACCOUNT_RISK_PCT,
    _PYRAMID_COOLDOWN_BARS,
    _PYRAMID_KELLY_CAP,
    _PYRAMID_KELLY_SCALE,
    _PYRAMID_MAX_ADDS,
    _PYRAMID_PROFIT_THRESHOLD_PCT,
    RiskProfile,
)
from .models import PyramidAddPlan, RolloverInstruction, RolloverScenario

_BAR_DURATION = timedelta(minutes=15)


def _is_positive(value: Optional[float]) -> bool:
    return value is not None and not math.isnan(value) and value > 0


class PyramidAddSizing(NamedTuple):
    risk_budget_usd: float
    share_qty: int
    notional_usd: float
    stop_distance_usd: float
    vix_spot: Optional[float]
    vix_tier_name: str
    vix_multiplier: float
    kelly_fraction: float
    binding_constraint: (
        str  # RISK_PCT / KELLY / EXPOSURE_CAP / VIX_ZERO / NO_EDGE / INVALID_INPUT
    )


def compute_pyramid_add_sizing(
    spot: float,
    ratchet_stop: float,
    capital: float,
    risk_limit_pct: Optional[float],
    vix_spot: Optional[float],
    rsi_15m: Optional[float],
) -> PyramidAddSizing:
    """計算加碼股數（風險預算 ÷ 停損距離，方向反轉自 short_entry_sizing.py）。

    risk_usd = capital × min(_PYRAMID_ACCOUNT_RISK_PCT, f_kelly) × m_vix
    qty      = min(⌊risk_usd / (spot − ratchet_stop)⌋, ⌊capital × risk_limit% / spot⌋)

    VIX 乘數刻意採 `config.get_vix_sizing_multiplier(vix_spot, "DIRECTIONAL_LONG")`
    （沿用 `market_analysis/strategy/analyze.py` 已建立的 DIRECTIONAL_LONG 呼叫
    慣例），而非 `short_entry_sizing.py` 的倒 U 形做空乘數——後者是為「VIX 極端
    區軋空風險」設計的方向性做空語意，不適用於多頭順勢加碼。
    """
    m_vix = get_vix_sizing_multiplier(vix_spot, "DIRECTIONAL_LONG")
    vix_known = vix_spot is not None and not math.isnan(vix_spot)
    vix_tier_name = get_vix_tier(vix_spot)["name"] if vix_known else "未知"

    stop_distance = spot - ratchet_stop

    def _result(
        risk_usd: float, qty: int, kelly_f: float, binding: str
    ) -> PyramidAddSizing:
        return PyramidAddSizing(
            risk_budget_usd=round(risk_usd, 2),
            share_qty=qty,
            notional_usd=round(qty * spot, 2),
            stop_distance_usd=round(stop_distance, 2),
            vix_spot=vix_spot if vix_known else None,
            vix_tier_name=vix_tier_name,
            vix_multiplier=m_vix,
            kelly_fraction=round(kelly_f, 5),
            binding_constraint=binding,
        )

    if not _is_positive(capital) or stop_distance <= 0 or spot <= 0:
        return _result(0.0, 0, 0.0, "INVALID_INPUT")
    if m_vix <= 0:
        return _result(0.0, 0, 0.0, "VIX_ZERO")

    win_prob = get_win_rate_prior("LONG", rsi_15m)
    kelly_f = kelly_position_fraction(
        win_prob=win_prob,
        odds=KELLY_PRIOR_ODDS["LONG"],
        kelly_scale=_PYRAMID_KELLY_SCALE,
        cap=_PYRAMID_KELLY_CAP,
    )
    if kelly_f <= 0:
        return _result(0.0, 0, 0.0, "NO_EDGE")

    if kelly_f < _PYRAMID_ACCOUNT_RISK_PCT:
        risk_fraction, binding = kelly_f, "KELLY"
    else:
        risk_fraction, binding = _PYRAMID_ACCOUNT_RISK_PCT, "RISK_PCT"
    risk_usd = capital * risk_fraction * m_vix

    qty_risk = int(math.floor(risk_usd / stop_distance))
    qty_cap = (
        int(math.floor(capital * max(risk_limit_pct, 0.0) / 100.0 / spot))
        if risk_limit_pct is not None
        else qty_risk
    )
    if qty_cap < qty_risk:
        return _result(risk_usd, max(qty_cap, 0), kelly_f, "EXPOSURE_CAP")
    return _result(risk_usd, max(qty_risk, 0), kelly_f, binding)


def build_pyramid_add_plan(
    sizing: PyramidAddSizing, spot: float, ratchet_stop: float, pyramid_count_after: int
) -> PyramidAddPlan:
    return {
        "entry_price": round(spot, 2),
        "stop_price": round(ratchet_stop, 2),
        "stop_distance_usd": sizing.stop_distance_usd,
        "reward_risk_ratio": 0.0,
        "risk_budget_usd": sizing.risk_budget_usd,
        "share_qty": sizing.share_qty,
        "notional_usd": sizing.notional_usd,
        "binding_constraint": sizing.binding_constraint,
        "vix_spot": sizing.vix_spot,
        "vix_tier_name": sizing.vix_tier_name,
        "vix_multiplier": sizing.vix_multiplier,
        "kelly_fraction": sizing.kelly_fraction,
        "pyramid_count_after": pyramid_count_after,
    }


async def evaluate_pyramid_add_impl(
    engine: Any,
    user_id: int,
    asset: Dict[str, Any],
    metrics: Dict[str, Any],
    profile: RiskProfile,
    capital: float,
    risk_limit_pct: Optional[float],
    vix_spot: Optional[float],
    resolve_macro_tier: Callable[[], Awaitable[str]],
) -> List[RolloverInstruction]:
    """情境十：順勢金字塔加碼。八項條件全部為 AND：

    1. 部位已獲利 (Spot-AvgCost)/AvgCost >= _PYRAMID_PROFIT_THRESHOLD_PCT (3%)
    2. 停損已在成本之上：擠壓參考停損（D 擠壓區間低點與 20SMA 較低者 −
       0.5×ATR₁D）>= avg_cost（不變式，任何修改都不得放寬——這是加碼只動用帳面
       利潤承險的唯一保證；參考停損算不出來一律 fail-closed）
    3. 趨勢延續訊號：D 動能 > 0，且 65m／D／3D／W 任一出現 Green Dot（擠壓剛
       解除），或收盤站上壓力區（壓力區突破）
    4. 不在壓力區下緣：現價未進入「尚未突破的壓力區下緣 0.5×ATR₁D 以內」
    5. 加碼次數未達上限：pyramid_count < _PYRAMID_MAX_ADDS (2)
    6. 距上次加碼已冷卻：now - last_pyramid_at >= _PYRAMID_COOLDOWN_BARS (8 根
       15m bar = 2 小時)；從未加碼過視為冷卻已滿足
    7. 加碼後總曝險未超過 profile.max_satellite_budget_pct：超過時降量而非
       直接拒絕（沿用既有股數計算，只是以曝險上限反推可加碼股數上限）
    8. 非逃頂警戒：`await resolve_macro_tier()` == "NORMAL"（呼叫端每位使用者
       提供一個記憶化的 async callable，本函式僅在條件一~四皆通過後才真正
       呼叫，避免對明顯不合格的部位觸發宏觀逃頂評分的多次資料抓取）

    僅適用多頭部位 (quantity > 0)；空頭部位完全不進入本路徑。任一資料缺失
    導致無法判定的條件，一律 fail-closed（不加碼），因為本情境是**承擔新
    曝險**的決策，語意上不同於「既有部位是否該出場」的 fail-open 慣例。
    """
    quantity = float(asset.get("quantity", 0.0))
    if quantity <= 0:
        return []

    state: Dict[str, Any] = asset.get("dynamic_strategy_state") or {}
    avg_cost = float(asset.get("avg_cost", 0.0))
    spot = float(metrics.get("spot_price", 0.0))
    if avg_cost <= 0 or spot <= 0:
        return []

    # 條件一：獲利門檻
    profit_pct = (spot - avg_cost) / avg_cost
    if profit_pct < _PYRAMID_PROFIT_THRESHOLD_PCT:
        return []

    # 條件五：加碼次數上限
    pyramid_count = int(state.get("pyramid_count", 0) or 0)
    if pyramid_count >= _PYRAMID_MAX_ADDS:
        return []

    # 條件六：冷卻期
    last_pyramid_at = state.get("last_pyramid_at")
    if last_pyramid_at:
        try:
            last_dt = datetime.fromisoformat(str(last_pyramid_at))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (
                datetime.now(timezone.utc) - last_dt
                < _PYRAMID_COOLDOWN_BARS * _BAR_DURATION
            ):
                return []
        except ValueError:
            pass  # 無法解析的時間戳視為未曾加碼，不阻擋

    # 條件二～四需要多時間框架 K 線（三次抓取），延遲到便宜的條件一／五／六
    # 都通過後才發動。
    symbol = str(asset.get("symbol", ""))
    from market_analysis import squeeze_entry

    ev = await squeeze_entry.evaluate_symbol(symbol, spot)

    # 條件二：停損已在成本之上（不變式，任何修改都不得放寬）
    ref_stop = ev.result.stop
    if ref_stop is None or ref_stop < avg_cost:
        return []

    # 條件三：趨勢延續訊號
    d_state = ev.matrix.get("D")
    if d_state is None or d_state.momentum_value <= 0:
        return []
    continuation = [
        f"{tf} Green Dot"
        for tf in ("65m", "D", "3D", "W")
        if tf in ev.matrix and ev.matrix[tf].green_dot
    ]
    resistance = ev.resistance
    if resistance is not None and resistance.broken is not None:
        continuation.append(f"站上壓力區 ${resistance.broken.top:.2f}")
    if not continuation:
        return []

    # 條件四：不在尚未突破的壓力區下緣
    if (
        resistance is not None
        and resistance.is_approaching
        and resistance.broken is None
    ):
        return []

    # 條件八：非逃頂警戒。刻意放在條件一~七之後才呼叫（宏觀評分呼叫端有自己的
    # 快取，但仍涉及 VTS/Fear&Greed/FedWatch 三次資料抓取），前面已先行過濾掉
    # 明顯不合格的部位，避免無謂觸發。
    macro_tier = await resolve_macro_tier()
    if macro_tier != "NORMAL":
        return []

    # 倉位計算（風險預算 ÷ 停損距離）
    rsi_15m = metrics.get("rsi_15m")
    rsi_val = float(rsi_15m) if rsi_15m is not None else None
    sizing = compute_pyramid_add_sizing(
        spot, ref_stop, capital, risk_limit_pct, vix_spot, rsi_val
    )
    if sizing.share_qty < 1:
        return []

    # 條件七：加碼後總曝險不得超過預算上限，超過時降量而非直接拒絕
    current_value = abs(float(asset.get("current_value", 0.0)))
    max_budget_usd = capital * profile.max_satellite_budget_pct if capital > 0 else 0.0
    remaining_budget = max_budget_usd - current_value
    if remaining_budget <= 0:
        return []
    if sizing.notional_usd > remaining_budget:
        capped_qty = int(math.floor(remaining_budget / spot)) if spot > 0 else 0
        if capped_qty < 1:
            return []
        sizing = sizing._replace(
            share_qty=capped_qty,
            notional_usd=round(capped_qty * spot, 2),
            binding_constraint="EXPOSURE_CAP",
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    state_patch = {
        "pyramid_count": pyramid_count + 1,
        "last_pyramid_at": now_iso,
    }
    plan = build_pyramid_add_plan(sizing, spot, ref_stop, pyramid_count + 1)

    reason = (
        "📈 **順勢金字塔加碼 (PYRAMID_ADD)**\n"
        f"{symbol} 部位獲利 {profit_pct:+.1%}（擠壓參考停損 ${ref_stop:.2f} 位於成本 "
        f"${avg_cost:.2f} 之上），D 動能仍為正，趨勢延續訊號："
        f"{'、'.join(continuation)}。"
        f"本次為第 {pyramid_count + 1}/{_PYRAMID_MAX_ADDS} 次加碼。"
    )

    instruction: RolloverInstruction = {
        "symbol": symbol,
        "action": "OPEN_PYRAMID",
        "sell_ratio": 0.0,
        "target_core": symbol,
        "reason": reason,
        "suggested_strategy": (
            f"加碼 {sizing.share_qty:,} 股 @ ${spot:.2f}（風險預算 "
            f"${sizing.risk_budget_usd:,.0f}，停損距離 ${sizing.stop_distance_usd:.2f}）"
        ),
        "scenario": RolloverScenario.PYRAMID_ADD.value,
        "instrument_type": "SPOT",
        "asset_id": asset.get("asset_id"),
        "dynamic_state_patch": state_patch,
        "pyramid_add_plan": plan,
    }
    return [instruction]
