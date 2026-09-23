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
反轉：停損距離為 `Spot − RatchetStop`。條件二已保證 `ratchet_stop >= avg_cost`，
因此加碼只動用「已實現的帳面利潤」承險，不增加原始部位的本金曝險——這是金字塔
加碼與盲目攤平的唯一分界，任何修改都不得放寬條件二。
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, NamedTuple, Optional

from config import get_vix_tier, get_vix_sizing_multiplier
from market_analysis.kelly_priors import KELLY_PRIOR_ODDS, get_win_rate_prior
from market_analysis.risk_engine import kelly_position_fraction
from market_analysis.room_threshold import (
    compute_dynamic_room_threshold,
    resolve_effective_target,
)

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
    2. 停損已在成本之上：dynamic_strategy_state["ratchet_stop"] >= avg_cost
       （不變式，任何修改都不得放寬——這是加碼只動用帳面利潤承險的唯一保證）
    3. 趨勢結構完好：Spot > SessionVWAP、Spot > GammaFlip、NetGEX > 0
       （NetGEX 缺失 (None) 視為未知，fail-closed 不通過——這是承擔新曝險的
       閘門，與「已有部位是否出場」的 fail-open 哲學不同）
    4. 上方仍有空間：晴空萬里有效目標天花板 (room_threshold.py 公式 D) 距現價
       空間 >= 動態自適應波動率門檻 (公式 A)
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

    # 條件二：停損已在成本之上（不變式，任何修改都不得放寬）
    ratchet_stop = float(state.get("ratchet_stop", 0.0) or 0.0)
    if ratchet_stop < avg_cost:
        return []

    # 條件三：趨勢結構完好
    session_vwap = float(metrics.get("session_vwap", 0.0))
    gamma_flip = float(metrics.get("gamma_flip", 0.0))
    net_gex_raw = metrics.get("net_gex")
    net_gex: Optional[float] = float(net_gex_raw) if net_gex_raw is not None else None
    if not (
        session_vwap > 0
        and spot > session_vwap
        and gamma_flip > 0
        and spot > gamma_flip
        and net_gex is not None
        and net_gex > 0.0
    ):
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

    # 條件四：晴空萬里有效目標天花板空間（需要 60 日高點，延遲至此才發動網路
    # 抓取——前面較便宜的條件已先行過濾，避免對明顯不合格的部位浪費請求）
    symbol = str(asset.get("symbol", ""))
    call_wall = float(metrics.get("call_wall", 0.0))
    put_wall = float(metrics.get("put_wall", 0.0))
    atr_15m = float(metrics.get("atr_15m", 0.0))
    atr_1d = float(metrics.get("atr_14", 0.0))

    from market_analysis.atr_utils import fetch_high_60d

    high_60d = await fetch_high_60d(symbol)
    eff_target = resolve_effective_target(spot, call_wall, high_60d, atr_1d)
    room = compute_dynamic_room_threshold(
        spot, put_wall, atr_15m, atr_1d, direction="LONG"
    )
    room_pct = (eff_target.target - spot) / spot if eff_target.target > 0 else 0.0
    if room_pct < room.threshold_pct:
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
        spot, ratchet_stop, capital, risk_limit_pct, vix_spot, rsi_val
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
    plan = build_pyramid_add_plan(sizing, spot, ratchet_stop, pyramid_count + 1)

    reason = (
        "📈 **順勢金字塔加碼 (PYRAMID_ADD)**\n"
        f"{symbol} 部位獲利 {profit_pct:+.1%}（棘輪停損 ${ratchet_stop:.2f} 已鎖定於成本"
        f"${avg_cost:.2f} 之上），現價 ${spot:.2f} 站穩 Session VWAP ${session_vwap:.2f} 與 "
        f"Gamma Flip ${gamma_flip:.2f}、NetGEX {net_gex:+,.0f} 仍為正，上方空間 "
        f"{room_pct:+.2%} 達動態門檻 {room.threshold_pct:.2%}，判定趨勢延伸中。"
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
