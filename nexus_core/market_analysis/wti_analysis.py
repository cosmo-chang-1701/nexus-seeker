"""WTI 原油價格分析引擎。

提供：
- 技術指標計算 (RSI, MA20/50/200, ATR14, 趨勢判定)
- 能源關聯股即時衝擊評估 (XLE, XOM, CVX, OXY, SLB, USO)
- 地緣政治/OPEC 事件風險掃描
- 投資組合油價風險權重影響
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 能源板塊關聯標的
ENERGY_CORRELATED_SYMBOLS: list[str] = [
    "XLE",  # Energy Select Sector SPDR ETF
    "XOM",  # ExxonMobil
    "CVX",  # Chevron
    "OXY",  # Occidental Petroleum
    "SLB",  # Schlumberger
    "USO",  # United States Oil Fund
]

# 油價相關地緣政治/宏觀關鍵字 (用於 calendar event matching)
OIL_GEOPOLITICAL_KEYWORDS: list[str] = [
    "OPEC",
    "crude",
    "oil",
    "petroleum",
    "EIA",
    "inventory",
    "drilling",
    "refinery",
    "pipeline",
    "sanctions",
    "Iran",
    "Russia",
    "Saudi",
    "OPEC+",
    "Middle East",
    "energy",
    "SPR",
]


class WtiAlertType(Enum):
    """WTI 警報觸發類型。"""

    UPPER_BREACH = "upper_breach"  # 突破上限
    LOWER_BREACH = "lower_breach"  # 跌破下限
    PCT_SURGE = "pct_surge"  # 百分比暴漲
    PCT_PLUNGE = "pct_plunge"  # 百分比暴跌


class OilTrend(Enum):
    """油價技術趨勢判定。"""

    STRONG_BULLISH = "strong_bullish"
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"
    STRONG_BEARISH = "strong_bearish"


@dataclass
class WtiTechnicals:
    """WTI 技術指標快照。"""

    price: float
    rsi_14: float = 50.0
    ma_20: float = 0.0
    ma_50: float = 0.0
    ma_200: float = 0.0
    atr_14: float = 0.0
    daily_change_pct: float = 0.0
    weekly_change_pct: float = 0.0
    trend: OilTrend = OilTrend.NEUTRAL


@dataclass
class CorrelatedStockImpact:
    """關聯股衝擊評估。"""

    symbol: str
    price: float
    daily_change_pct: float
    is_in_watchlist: bool = False
    is_in_holdings: bool = False


@dataclass
class WtiAnalysisResult:
    """WTI 完整分析結果。"""

    alert_type: WtiAlertType
    technicals: WtiTechnicals
    correlated_impacts: list[CorrelatedStockImpact] = field(
        default_factory=lambda: list[CorrelatedStockImpact]()
    )
    geopolitical_events: list[str] = field(default_factory=lambda: list[str]())
    oil_risk_weight: float = 1.0  # 當前油價對投資組合的風險權重
    trigger_price: float = 0.0
    threshold_value: float = 0.0
    pct_change_30min: float = 0.0


def compute_oil_risk_weight(price: float) -> float:
    """計算油價風險權重 (與 risk_engine.py 保持一致)。"""
    if price < 75.0:
        return 1.0
    elif price < 85.0:
        return 0.9
    elif price < 95.0:
        return 0.7
    return 0.5


def determine_oil_trend(
    price: float, rsi: float, ma20: float, ma50: float, ma200: float
) -> OilTrend:
    """基於均線排列與 RSI 判定技術趨勢。"""
    bullish_signals = 0
    if ma20 > 0.0 and price > ma20:
        bullish_signals += 1
    if ma50 > 0.0 and price > ma50:
        bullish_signals += 1
    if ma200 > 0.0 and price > ma200:
        bullish_signals += 1
    if ma20 > 0.0 and ma50 > 0.0 and ma20 > ma50:
        bullish_signals += 1

    if bullish_signals >= 4 and rsi > 60.0:
        return OilTrend.STRONG_BULLISH
    elif bullish_signals >= 3:
        return OilTrend.BULLISH
    elif bullish_signals <= 0 and rsi < 40.0:
        return OilTrend.STRONG_BEARISH
    elif bullish_signals <= 1:
        return OilTrend.BEARISH
    return OilTrend.NEUTRAL


async def _compute_wti_technicals(price: float) -> WtiTechnicals:
    """計算 WTI CL=F 技術指標。"""
    try:
        from services.market_data_service import get_history_df
        import pandas_ta as ta

        df = await get_history_df("CL=F", period="1y", interval="1d")
        if df is None or df.empty:
            return WtiTechnicals(price=price)

        close = df["Close"]

        # RSI(14)
        rsi_val = 50.0
        try:
            rsi_series = ta.rsi(close, length=14)
            if rsi_series is not None and not rsi_series.empty:
                last_rsi = float(rsi_series.dropna().iloc[-1])
                if not (last_rsi != last_rsi):  # not NaN
                    rsi_val = last_rsi
        except Exception as e:
            logger.debug(f"WTI RSI 計算異常: {e}")

        # Moving Averages
        ma20 = (
            float(close.rolling(20).mean().dropna().iloc[-1])
            if len(close) >= 20
            else 0.0
        )
        ma50 = (
            float(close.rolling(50).mean().dropna().iloc[-1])
            if len(close) >= 50
            else 0.0
        )
        ma200 = (
            float(close.rolling(200).mean().dropna().iloc[-1])
            if len(close) >= 200
            else 0.0
        )

        # ATR(14)
        atr_val = 0.0
        try:
            if "High" in df.columns and "Low" in df.columns:
                atr_series = ta.atr(df["High"], df["Low"], close, length=14)
                if atr_series is not None and not atr_series.empty:
                    last_atr = float(atr_series.dropna().iloc[-1])
                    if not (last_atr != last_atr):
                        atr_val = last_atr
        except Exception as e:
            logger.debug(f"WTI ATR 計算異常: {e}")

        # Daily & Weekly change
        daily_pct = 0.0
        if len(close) >= 2:
            prev_close = float(close.iloc[-2])
            if prev_close > 0.0:
                daily_pct = (price - prev_close) / prev_close * 100.0

        weekly_pct = 0.0
        if len(close) >= 5:
            week_ago_close = float(close.iloc[-5])
            if week_ago_close > 0.0:
                weekly_pct = (price - week_ago_close) / week_ago_close * 100.0

        trend = determine_oil_trend(price, rsi_val, ma20, ma50, ma200)

        return WtiTechnicals(
            price=price,
            rsi_14=round(rsi_val, 1),
            ma_20=round(ma20, 2),
            ma_50=round(ma50, 2),
            ma_200=round(ma200, 2),
            atr_14=round(atr_val, 2),
            daily_change_pct=round(daily_pct, 2),
            weekly_change_pct=round(weekly_pct, 2),
            trend=trend,
        )
    except Exception as e:
        logger.error(f"WTI 技術指標計算失敗: {e}")
        return WtiTechnicals(price=price)


async def _fetch_correlated_impacts(
    watchlist: list[str], holdings: list[str]
) -> list[CorrelatedStockImpact]:
    """並行抓取能源關聯股報價與漲跌幅。"""
    from services.market_data_service import get_quote

    async def _fetch_one(sym: str) -> Optional[CorrelatedStockImpact]:
        try:
            q = await get_quote(sym)
            if not isinstance(q, dict):
                return None
            return CorrelatedStockImpact(
                symbol=sym,
                price=float(q.get("c", 0.0)),
                daily_change_pct=float(q.get("dp", 0.0)),
                is_in_watchlist=sym in watchlist,
                is_in_holdings=sym in holdings,
            )
        except Exception as e:
            logger.debug(f"關聯股報價抓取失敗 ({sym}): {e}")
            return None

    tasks = [_fetch_one(s) for s in ENERGY_CORRELATED_SYMBOLS]
    results: list[Any] = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, CorrelatedStockImpact)]


async def _scan_geopolitical_events() -> list[str]:
    """掃描近期與油價相關的地緣政治 / 宏觀事件。"""
    try:
        from services.calendar_service import calendar_service

        events = await calendar_service.get_high_impact_events()
        if not events:
            return []

        matched_events: list[str] = []
        for ev in events:
            if isinstance(ev, dict):
                ev_text = str(ev.get("event", "")).lower()
                ev_time = str(ev.get("date") or ev.get("time", "TBD"))
                ev_title = str(ev.get("event", "Unknown"))
            else:
                ev_text = str(getattr(ev, "event", "")).lower()
                ev_time = str(getattr(ev, "time", "TBD"))
                ev_title = str(getattr(ev, "event", "Unknown"))

            for kw in OIL_GEOPOLITICAL_KEYWORDS:
                if kw.lower() in ev_text:
                    matched_events.append(f"📅 {ev_time}: {ev_title}")
                    break
        return matched_events[:5]
    except Exception as e:
        logger.debug(f"地緣政治事件掃描失敗: {e}")
        return []


async def analyze_wti(
    price: float,
    alert_type: WtiAlertType,
    threshold_value: float,
    pct_change_30min: float = 0.0,
    user_watchlist: Optional[list[str]] = None,
    user_holdings: Optional[list[str]] = None,
) -> WtiAnalysisResult:
    """執行完整 WTI 分析 pipeline。"""
    watchlist: list[str] = user_watchlist or []
    holdings: list[str] = user_holdings or []

    # 1. 技術指標
    technicals = await _compute_wti_technicals(price)

    # 2. 關聯股衝擊
    correlated = await _fetch_correlated_impacts(watchlist, holdings)

    # 3. 地緣政治事件掃描
    events = await _scan_geopolitical_events()

    # 4. 油價風險權重
    oil_weight = compute_oil_risk_weight(price)

    return WtiAnalysisResult(
        alert_type=alert_type,
        technicals=technicals,
        correlated_impacts=correlated,
        geopolitical_events=events,
        oil_risk_weight=oil_weight,
        trigger_price=price,
        threshold_value=threshold_value,
        pct_change_30min=pct_change_30min,
    )


__all__ = [
    "ENERGY_CORRELATED_SYMBOLS",
    "OIL_GEOPOLITICAL_KEYWORDS",
    "WtiAlertType",
    "OilTrend",
    "WtiTechnicals",
    "CorrelatedStockImpact",
    "WtiAnalysisResult",
    "compute_oil_risk_weight",
    "determine_oil_trend",
    "analyze_wti",
]
