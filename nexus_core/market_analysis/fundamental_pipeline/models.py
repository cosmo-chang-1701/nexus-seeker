"""基本面分析管線 (Fundamental Event Pipeline) 核心資料模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

LiquidityRegime = Literal["EASY", "NEUTRAL", "TIGHT", "UNKNOWN"]


@dataclass(frozen=True)
class LiquidityReading:
    """央行淨流動性與體制狀態讀數。"""

    trading_date: date
    nfci: float | None
    anfci: float | None
    net_liquidity_bn: float | None
    net_liquidity_chg_13w_pct: float | None
    reserves_chg_13w_pct: float | None
    us10y: float | None
    regime: LiquidityRegime
    equity_risk_premium: float | None


@dataclass(frozen=True)
class MacroSurpriseReading:
    """宏觀數據發布預期差標準化讀數。"""

    event_key: str
    release_time_utc: str
    actual: float
    forecast: float
    raw_diff: float
    z_score: float | None
    growth_sign: int  # 1: 正向經濟增長指標, -1: 反向/通膨/緊縮指標
