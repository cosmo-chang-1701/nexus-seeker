"""日內資料一致性閘門（`/x` 標的深度分析的呈現層防線）。

`/x` 一次彙整多個資料源：現價與日高低點來自 `get_quote`（Tier 0 為 Alpaca IEX
單一交易所串流，其次 Finnhub），Session VWAP 與 15m K 棒來自 yfinance 全市場
K 線，期權成交價來自期權鏈的 `lastPrice`。各源時間戳與成交涵蓋範圍不同，任何
一邊落後或偏窄，就會出現「VWAP 高於當日最高價」、「15m K 棒低點低於全日低點」、
「期權權利金低於內含價值」這類物理上不可能的組合。

本模組只放不做 I/O 的純函式，把這些不變式集中在一處檢查：
  * 能以較完整資料源修正的（IEX 高低點偏窄）→ 修正並回報已修正；
  * 無法判斷哪邊正確的（K 棒凍結、多根合併）→ 不修改數值，只回報異常，由呈現層
    據實揭露並停用依賴該數值的判定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

# 已收盤 K 棒的收盤時刻落後現在超過此值即視為資料延遲（盤中才判定）。
BAR_STALE_AFTER = timedelta(minutes=30)
# 當日已累積至少這麼多根 15m K 棒時，單根成交量 >= 當日總量此比例即判為多根合併。
_MERGED_BAR_MIN_SESSION_BARS = 8
_MERGED_BAR_VOLUME_SHARE = 0.5
# 價格比較的相對容差（浮點誤差與不同源的小數位差）。
_PRICE_REL_TOL = 1e-4


def _pos(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def reconcile_daily_range(
    quote: dict[str, Any],
    session_high: Optional[float],
    session_low: Optional[float],
    session_date: Optional[date],
    today: date,
) -> tuple[dict[str, Any], bool]:
    """以全市場當日 K 線極值與現價校正報價的日高低點。

    日高低必須涵蓋當日任何一筆成交，因此取聯集：
        H = max(quote.h, session_high, c)
        L = min(quote.l, session_low, c)
    只放寬、不收窄——IEX 單一交易所的極值只可能比全市場窄，反之不成立。
    K 線不屬於今日（盤前時 yfinance period=1d 回傳前一交易日）時不做合併，
    避免把昨天的極值塞進今天的區間。

    回傳 (新報價 dict, 是否有修正)；輸入 dict 不被修改。
    """
    if not quote:
        return quote, False
    c = _pos(quote.get("c"))
    h = _pos(quote.get("h"))
    lo = _pos(quote.get("l"))

    candidates_high: list[float] = [v for v in (h, c) if v is not None]
    candidates_low: list[float] = [v for v in (lo, c) if v is not None]
    if session_date == today:
        sh, sl = _pos(session_high), _pos(session_low)
        if sh is not None:
            candidates_high.append(sh)
        if sl is not None:
            candidates_low.append(sl)

    if not candidates_high or not candidates_low:
        return quote, False

    new_h = max(candidates_high)
    new_l = min(candidates_low)
    changed = (h is None or not math.isclose(new_h, h, rel_tol=_PRICE_REL_TOL)) or (
        lo is None or not math.isclose(new_l, lo, rel_tol=_PRICE_REL_TOL)
    )
    if not changed:
        return quote, False
    fixed = dict(quote)
    fixed["h"] = new_h
    fixed["l"] = new_l
    return fixed, True


def is_vwap_within_range(vwap: Optional[float], high: float, low: float) -> bool:
    """VWAP 是成交價的加權平均，必然落在 [當日最低, 當日最高] 之內。"""
    v = _pos(vwap)
    hi, lo = _pos(high), _pos(low)
    if v is None or hi is None or lo is None:
        return False
    tol = v * _PRICE_REL_TOL
    return lo - tol <= v <= hi + tol


@dataclass
class BarAssessment:
    """15m K 棒的新鮮度與一致性檢查結果。"""

    is_stale: bool = False
    is_anomalous: bool = False
    notes: list[str] = field(default_factory=list)


def assess_15m_bar(
    bar_time: Optional[datetime],
    high: Optional[float],
    low: Optional[float],
    volume: Optional[float],
    *,
    now_ny: datetime,
    market_open: bool,
    day_high: Optional[float],
    day_low: Optional[float],
    session_date: Optional[date],
    session_volume: Optional[float],
    session_bar_count: int,
) -> BarAssessment:
    """檢查最新已收盤 15m K 棒是否凍結、是否超出當日區間、是否為多根合併。

    `bar_time` 為 K 棒**起始**時間（tz-naive 美東，與 `Confirmed15mBar` 一致）。
    K 棒不屬於今日（開盤第一根尚未收盤時，最近已收盤的是昨天最後一根）時只做
    新鮮度檢查，不與今日區間比較。
    """
    result = BarAssessment()
    if bar_time is None:
        return result
    bar_naive = bar_time.replace(tzinfo=None)
    now_naive = now_ny.replace(tzinfo=None)

    if market_open:
        lag = now_naive - (bar_naive + timedelta(minutes=15))
        if lag > BAR_STALE_AFTER:
            result.is_stale = True
            result.notes.append(
                f"K 棒資料延遲 {int(lag.total_seconds() // 60)} 分鐘（資料源未更新）"
            )

    if session_date is None or bar_naive.date() != session_date:
        return result

    hi, lo = _pos(high), _pos(low)
    dh, dl = _pos(day_high), _pos(day_low)
    if (hi is not None and dh is not None and hi > dh * (1 + _PRICE_REL_TOL)) or (
        lo is not None and dl is not None and lo < dl * (1 - _PRICE_REL_TOL)
    ):
        result.is_anomalous = True
        result.notes.append("K 棒極值超出當日區間（資料源時序未對齊）")

    vol, sess_vol = _pos(volume), _pos(session_volume)
    if vol is not None and sess_vol is not None:
        if vol > sess_vol * (1 + _PRICE_REL_TOL):
            result.is_anomalous = True
            result.notes.append("K 棒成交量大於當日累積量（資料源異常）")
        elif (
            session_bar_count >= _MERGED_BAR_MIN_SESSION_BARS
            and vol >= sess_vol * _MERGED_BAR_VOLUME_SHARE
        ):
            result.is_anomalous = True
            result.notes.append(
                f"單根 K 棒佔當日成交量 {vol / sess_vol:.0%}，疑似多根 K 棒合併"
            )
    return result


def option_intrinsic_value(is_call: bool, strike: float, spot: float) -> float:
    return max(0.0, spot - strike) if is_call else max(0.0, strike - spot)


def sanitize_option_trade_price(
    trade_price: float,
    bid: float,
    ask: float,
    strike: float,
    spot: float,
    is_call: bool,
) -> Optional[float]:
    """剔除違反無套利下界（權利金 < 內含價值）的成交價。

    美式期權的權利金不可能明顯低於內含價值，否則立即履約即可套利；出現這種值
    代表 `lastPrice` 是現價大幅移動**之前**的舊成交。處理順序：
      1. 成交價 >= 內含價值 − 容差 → 原值；
      2. 否則改用當下 bid/ask 中價（呼叫端的方向分類會因此歸為 MIDPOINT，
         正確反映「無法判定這筆舊成交是買方還是賣方主動」）；
      3. 中價也不合理 → None，由呼叫端剔除該合約。
    """
    if strike <= 0 or spot <= 0:
        return trade_price
    intrinsic = option_intrinsic_value(is_call, strike, spot)
    tol = max(0.05, intrinsic * 0.005)
    if trade_price >= intrinsic - tol:
        return trade_price
    if bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
        if mid >= intrinsic - tol:
            return mid
    return None
