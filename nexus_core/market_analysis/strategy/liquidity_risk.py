"""流動性評估、風險驗證、VIX 階梯與倉位大小計算。"""

import logging
import math
from typing import Any, Optional

import pandas as pd

from config import get_vix_tier, VixTier
from market_analysis.risk_engine import kelly_position_fraction

logger = logging.getLogger(__name__)


def _evaluate_option_liquidity(option_data: dict) -> dict:
    """評估期權報價的流動性與買賣價差。"""
    bid = option_data.get("bid", 0.0)
    ask = option_data.get("ask", 0.0)
    oi = option_data.get("oi", 0)
    volume = option_data.get("volume", 0)
    dte = option_data.get("dte", 0)
    delta = abs(option_data.get("delta", 0.5))

    if ask <= 0 or bid < 0 or ask <= bid:
        return {
            "status": "🔴 異常",
            "embed_msg": "報價異常 (Ask 需大於 Bid 且大於 0)",
            "is_pass": False,
        }

    mid_price = (bid + ask) / 2
    abs_spread = ask - bid
    rel_spread = abs_spread / mid_price

    if oi < 100 or volume < 10:
        return {
            "status": "🔴 極差",
            "embed_msg": f"流動性枯竭 (OI: {oi}, Vol: {volume})，滑價風險極高",
            "is_pass": False,
        }

    max_rel_spread = 0.10
    if dte > 90:
        max_rel_spread += 0.05
    if delta > 0.80 or delta < 0.15:
        max_rel_spread += 0.05

    is_spread_valid = True
    if ask < 1.00:
        if abs_spread > 0.10:
            is_spread_valid = False
    else:
        if rel_spread > max_rel_spread:
            is_spread_valid = False

    if not is_spread_valid:
        return {
            "status": "🔴 警示",
            "embed_msg": f"價差過寬 (Spread: {rel_spread:.1%}, 絕對值: ${abs_spread:.2f})",
            "is_pass": False,
        }

    if rel_spread < 0.05:
        return {
            "status": "🟢 優良",
            "embed_msg": f"流動性極佳 (Spread: {rel_spread:.1%}) | 建議：可嘗試掛 Mid-price 成交",
            "is_pass": True,
        }
    elif rel_spread <= 0.10:
        return {
            "status": "🟡 尚可",
            "embed_msg": f"流動性普通 (Spread: {rel_spread:.1%}) | 建議：嚴格掛 Mid-price 等待成交",
            "is_pass": True,
        }
    else:
        return {
            "status": "🔴 警告",
            "embed_msg": f"流動性較差 (Spread: {rel_spread:.1%}) | 滑價風險高，務必堅守限價單",
            "is_pass": True,
        }


def _validate_risk_and_liquidity(  # type: ignore
    strategy: Any,
    best_contract: Any,
    price: Any,
    hv_current: Any,
    days_to_expiry: Any,
    symbol: Any,
):
    """驗證流動性、VRP 與 預期波動。"""
    bid = best_contract.get("bid", 0.0)
    ask = best_contract.get("ask", 0.0)
    strike = best_contract.get("strike", 0.0)
    iv = best_contract.get("impliedVolatility", 0.0)
    delta = best_contract.get("bs_delta", 0.0)

    oi = best_contract.get("openInterest", 0)
    oi = 0 if pd.isna(oi) else int(oi)
    volume = best_contract.get("volume", 0)
    volume = 0 if pd.isna(volume) else int(volume)

    option_data_for_liq = {
        "bid": bid,
        "ask": ask,
        "oi": oi,
        "volume": volume,
        "dte": days_to_expiry,
        "delta": delta,
    }
    liq_eval = _evaluate_option_liquidity(option_data_for_liq)

    if not liq_eval["is_pass"]:
        logger.info(f"[{symbol}] 剔除: {liq_eval['status']} - {liq_eval['embed_msg']}")
        return None

    vrp = iv - hv_current
    if strategy in ["STO_PUT", "STO_CALL"]:
        if vrp < 0:
            logger.info(f"[{symbol}] 剔除: 賣方策略但 VRP {vrp * 100:.2f}% < 0")
            return None
    elif strategy in ["BTO_PUT", "BTO_CALL"]:
        if vrp > 0.03:
            logger.info(f"[{symbol}] 剔除: 買方策略但 VRP 高達 {vrp * 100:.2f}%")
            return None

    # Zero-IV 防禦：若 IV 為零，回退至 HV 代理
    effective_iv = iv if iv > 0.001 else hv_current
    if effective_iv <= 0.001:
        effective_iv = 0.15  # 極端降級：使用 15% 年化波動率底線
        logger.warning(
            f"[{symbol}] Zero-IV AND Zero-HV in risk validation, using 15% floor for EM"
        )
    expected_move = price * effective_iv * math.sqrt(max(days_to_expiry, 1) / 365.0)
    em_lower = price - expected_move
    em_upper = price + expected_move

    if strategy == "STO_PUT":
        breakeven = strike - bid
        if breakeven > em_lower:
            logger.info(
                f"[{symbol}] 剔除: 損益兩平點 ${breakeven:.2f} 落入 1σ 預期跌幅內"
            )
            return None
    elif strategy == "STO_CALL":
        breakeven = strike + bid
        if breakeven < em_upper:
            logger.info(
                f"[{symbol}] 剔除: 損益兩平點 ${breakeven:.2f} 落入 1σ 預期漲幅內"
            )
            return None

    mid_price = (ask + bid) / 2.0
    spread = ask - bid
    spread_ratio = (spread / mid_price) * 100 if mid_price > 0 else 999.0

    suggested_hedge_strike = (
        em_upper
        if strategy == "BTO_CALL"
        else (em_lower if strategy == "BTO_PUT" else None)
    )

    return {
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "spread_ratio": spread_ratio,
        "vrp": vrp,
        "expected_move": expected_move,
        "em_lower": em_lower,
        "em_upper": em_upper,
        "mid_price": mid_price,
        "suggested_hedge_strike": suggested_hedge_strike,
        "liq_status": liq_eval["status"],
        "liq_msg": liq_eval["embed_msg"],
    }


def apply_vix_ladder(vix_spot: Optional[float]) -> VixTier:
    """根據 VIX 即時水位回傳對應的戰情階梯配置。

    回傳值為 tier dict，包含：
    - allow_signal: 是否允許 STO 訊號
    - sto_delta_cap: STO Delta 上限 (負數)
    - sizing_multiplier: 倉位大小乘數
    - kelly_fraction_override: 可選的 Kelly 分數覆寫
    - vtr_entry_allowed: 是否允許 VTR 自動建倉
    """
    return get_vix_tier(vix_spot)


def _calculate_sizing(
    strategy: Any,
    best_contract: Any,
    days_to_expiry: Any,
    expected_move: Any = 0.0,
    price: Any = 0.0,
    stock_cost: Any = 0.0,
    kelly_fraction: Any = 0.5,
    kelly_fraction_override: Optional[float] = None,
) -> Any:
    """計算資金效率與倉位大小

    Args:
        kelly_fraction_override: 若非 None，則覆寫 kelly_fraction（用於 VIX All-in 階梯）。
    """
    effective_kelly = (
        kelly_fraction_override
        if kelly_fraction_override is not None
        else kelly_fraction
    )
    aroc, alloc_pct, margin_per_contract = 0.0, 0.0, 0.0

    bid = best_contract.get("bid", 0.0)
    ask = best_contract.get("ask", 0.0)
    strike = best_contract.get("strike", 0.0)
    delta = best_contract.get("bs_delta", 0.0)

    if strategy in ["STO_PUT", "STO_CALL"]:
        margin_required = (
            (strike - bid)
            if strategy == "STO_PUT"
            else (
                stock_cost
                if stock_cost > 0.0
                else max(
                    (0.20 * price) - max(0, strike - price) + bid, 0.10 * price + bid
                )
                if price > 0
                else strike
            )
        )
        if margin_required > 0:
            aroc = (bid / margin_required) * (365.0 / max(days_to_expiry, 1)) * 100
            if aroc >= 15.0:
                p = 1.0 - abs(delta)
                b = bid / margin_required
                if b > 0:
                    alloc_pct = kelly_position_fraction(
                        win_prob=p, odds=b, kelly_scale=effective_kelly, cap=0.05
                    )
                    margin_per_contract = margin_required * 100
    elif strategy in ["BTO_CALL", "BTO_PUT"]:
        premium = ask
        if premium > 0 and expected_move > 0:
            potential_profit = expected_move - premium
            aroc = (
                (potential_profit / premium) * (365.0 / max(days_to_expiry, 1)) * 100
                if potential_profit > 0
                else 0.0
            )
            if aroc >= 30.0:
                p = abs(delta)
                b = potential_profit / premium
                if b > 0:
                    alloc_pct = kelly_position_fraction(
                        win_prob=p, odds=b, kelly_scale=effective_kelly, cap=0.03
                    )
            margin_per_contract = premium * 100

    return aroc, alloc_pct, margin_per_contract
