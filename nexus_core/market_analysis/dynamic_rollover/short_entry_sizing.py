"""short_entry_sizing.py — SHORT_ENTRY 做空進場的價位與倉位計算 (純函式、零 I/O)。

輸入全部是 `short_side_entry.evaluate_short_entry()` 在六重鐵律評估過程中本來
就已算出的中間值，本模組只做算術，確保「進場確認」與「下單價位」建立在同一份
資料快照上。

倉位模型刻意採「風險預算 ÷ 停損距離」，而非配置比例：

    risk_usd  = capital × min(ACCOUNT_RISK_PCT, f_kelly) × m_vix
    qty       = min(⌊risk_usd / (stop − entry)⌋, ⌊capital × risk_limit% / entry⌋)

* `f_kelly`：方向感知凱利先驗 (kelly_priors.py)，賠率取 min(實際 R:R, 先驗賠率)
  ——永遠不信任高於先驗的賠率。R:R 太差時凱利為 0，本身就是一道天然的進場閘門。
* `m_vix`：做空倒 U 形 VIX 乘數 (config.get_short_vix_multiplier)，VIX >= 35 為 0。
* `risk_limit`：使用者的組合曝險上限 (%)，沿用為單筆名目金額上限。

停損取「結構停損」與「出場引擎停損」兩者**較遠**者：倉位必須以部位登錄後
出場矩陣真正會執行的停損為準；較遠的停損給出較小的倉位，是保守的一側。
"""

import math
from typing import NamedTuple, Optional

from config import get_short_vix_multiplier, get_vix_tier
from market_analysis.kelly_priors import KELLY_PRIOR_ODDS, get_win_rate_prior
from market_analysis.risk_engine import kelly_position_fraction
from market_analysis.room_threshold import compute_reference_stop

from .anti_washout import resolve_short_anchor
from .constants import (
    _MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT,
    _SHORT_ENTRY_ACCOUNT_RISK_PCT,
    _SHORT_ENTRY_KELLY_CAP,
    _SHORT_ENTRY_KELLY_SCALE,
)
from .models import ShortEntryEvaluation, ShortEntryPlan


class ShortEntryLevels(NamedTuple):
    sub_mode: str
    entry_price: float
    stop_price: float
    stop_price_structural: float
    stop_price_exit_engine: float
    target_price: float
    reward_risk_ratio: float
    invalidation_note: Optional[str]


class ShortEntrySizing(NamedTuple):
    risk_budget_usd: float
    share_qty: int
    notional_usd: float
    stop_distance_usd: float
    reward_risk_ratio: float
    vix_spot: Optional[float]
    vix_tier_name: str
    short_vix_multiplier: float
    kelly_fraction: float
    # RISK_PCT / KELLY / EXPOSURE_CAP / VIX_ZERO / NO_EDGE / INVALID_INPUT
    binding_constraint: str
    degrade_reason: Optional[str]


def _is_positive(value: Optional[float]) -> bool:
    return value is not None and not math.isnan(value) and value > 0


def build_short_entry_levels(ev: ShortEntryEvaluation) -> Optional[ShortEntryLevels]:
    """由做空評估結果推導進場／停損／目標。任何價位不合法一律回傳 None (fail-closed)。

    * 進場：評估當下現價 (限價放空)。
    * 結構停損：`compute_reference_stop(spot, 頂牆, ATR₁₅ₘ, "SHORT")`，與進場鐵律
      條件二／三的停損定義一致。
    * 出場引擎停損：`resolve_short_anchor()` + 0.5×ATR₁₅ₘ，模擬部位登錄後做空
      鏡像出場矩陣的「SL-結構失效」線。餵入的 metrics 只含 portfolio_monitor
      實際會提供的欄位 (不含 resistance_wall)，才能反映真正會執行的停損。
    * 目標：區間內做空 = Put Wall；破位追空 = 次級負 GEX 節點。
    """
    spot = ev.spot
    atr_15m = ev.atr_15m
    if not _is_positive(spot) or not _is_positive(atr_15m):
        return None

    if ev.sub_mode == "破位追空":
        target = ev.next_negative_node
        invalidation_note: Optional[str] = (
            f"收復 Put Wall ${ev.put_wall:.2f} 即論點失效"
            if _is_positive(ev.put_wall)
            else None
        )
    else:
        target = ev.put_wall
        invalidation_note = None
    if not _is_positive(target) or target >= spot:
        return None

    stop_structural = (
        compute_reference_stop(spot, ev.resistance_wall, atr_15m, "SHORT")
        if _is_positive(ev.resistance_wall)
        else 0.0
    )
    anchor_short, _floor = resolve_short_anchor(
        {
            "spot_price": spot,
            "put_wall": ev.put_wall,
            "call_wall": ev.call_wall,
            "gamma_flip": ev.gamma_flip,
        }
    )
    stop_exit_engine = (
        anchor_short + _MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT * atr_15m
        if _is_positive(anchor_short)
        else 0.0
    )
    # 出場引擎錨點若落在現價以下 (例如 Call Wall 缺失、錨點退回現價)，該停損
    # 登錄後會立即觸發，不具參考意義，不納入較遠者比較。
    candidates = [s for s in (stop_structural, stop_exit_engine) if s > spot]
    if not candidates:
        return None
    stop = max(candidates)

    risk = stop - spot
    reward = spot - target
    reward_risk_ratio = reward / risk if risk > 0 else 0.0

    return ShortEntryLevels(
        sub_mode=ev.sub_mode,
        entry_price=round(spot, 2),
        stop_price=round(stop, 2),
        stop_price_structural=round(stop_structural, 2),
        stop_price_exit_engine=round(stop_exit_engine, 2),
        target_price=round(target, 2),
        reward_risk_ratio=round(reward_risk_ratio, 2),
        invalidation_note=invalidation_note,
    )


def compute_short_entry_sizing(
    levels: ShortEntryLevels,
    capital: float,
    risk_limit_pct: float,
    vix_spot: Optional[float],
    rsi_15m: Optional[float],
) -> ShortEntrySizing:
    """計算做空倉位 (股數)。`share_qty < 1` 代表不應產生指令，原因見 binding_constraint。"""
    m_vix = get_short_vix_multiplier(vix_spot)
    vix_known = vix_spot is not None and not math.isnan(vix_spot)
    vix_tier_name = get_vix_tier(vix_spot)["name"] if vix_known else "未知"
    degrade_reason = (
        None if vix_known else f"VIX 資料缺失，做空乘數保守退回 {m_vix:.2f}x"
    )

    stop_distance = levels.stop_price - levels.entry_price

    def _result(
        risk_usd: float, qty: int, kelly_f: float, binding: str
    ) -> ShortEntrySizing:
        return ShortEntrySizing(
            risk_budget_usd=round(risk_usd, 2),
            share_qty=qty,
            notional_usd=round(qty * levels.entry_price, 2),
            stop_distance_usd=round(stop_distance, 2),
            reward_risk_ratio=levels.reward_risk_ratio,
            vix_spot=vix_spot if vix_known else None,
            vix_tier_name=vix_tier_name,
            short_vix_multiplier=m_vix,
            kelly_fraction=round(kelly_f, 5),
            binding_constraint=binding,
            degrade_reason=degrade_reason,
        )

    if not _is_positive(capital) or stop_distance <= 0 or levels.entry_price <= 0:
        return _result(0.0, 0, 0.0, "INVALID_INPUT")
    if m_vix <= 0:
        return _result(0.0, 0, 0.0, "VIX_ZERO")

    win_prob = get_win_rate_prior("SHORT", rsi_15m)
    odds = min(levels.reward_risk_ratio, KELLY_PRIOR_ODDS["SHORT"])
    kelly_f = kelly_position_fraction(
        win_prob=win_prob,
        odds=odds,
        kelly_scale=_SHORT_ENTRY_KELLY_SCALE,
        cap=_SHORT_ENTRY_KELLY_CAP,
    )
    if kelly_f <= 0:
        return _result(0.0, 0, 0.0, "NO_EDGE")

    if kelly_f < _SHORT_ENTRY_ACCOUNT_RISK_PCT:
        risk_fraction, binding = kelly_f, "KELLY"
    else:
        risk_fraction, binding = _SHORT_ENTRY_ACCOUNT_RISK_PCT, "RISK_PCT"
    risk_usd = capital * risk_fraction * m_vix

    qty_risk = int(math.floor(risk_usd / stop_distance))
    qty_cap = (
        int(math.floor(capital * max(risk_limit_pct, 0.0) / 100.0 / levels.entry_price))
        if risk_limit_pct is not None
        else qty_risk
    )
    if qty_cap < qty_risk:
        return _result(risk_usd, max(qty_cap, 0), kelly_f, "EXPOSURE_CAP")
    return _result(risk_usd, max(qty_risk, 0), kelly_f, binding)


def build_short_entry_plan(
    levels: ShortEntryLevels, sizing: ShortEntrySizing
) -> ShortEntryPlan:
    return {
        "sub_mode": levels.sub_mode,
        "entry_price": levels.entry_price,
        "stop_price": levels.stop_price,
        "stop_price_structural": levels.stop_price_structural,
        "stop_price_exit_engine": levels.stop_price_exit_engine,
        "target_price": levels.target_price,
        "reward_risk_ratio": levels.reward_risk_ratio,
        "risk_budget_usd": sizing.risk_budget_usd,
        "share_qty": sizing.share_qty,
        "notional_usd": sizing.notional_usd,
        "binding_constraint": sizing.binding_constraint,
        "vix_spot": sizing.vix_spot,
        "vix_tier_name": sizing.vix_tier_name,
        "short_vix_multiplier": sizing.short_vix_multiplier,
        "kelly_fraction": sizing.kelly_fraction,
        "invalidation_note": levels.invalidation_note,
    }
