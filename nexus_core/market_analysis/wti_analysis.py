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
from datetime import datetime
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
    daily_change_pct: Optional[float] = None  # None = 無可靠基準 (顯示 --)
    weekly_change_pct: Optional[float] = None  # None = 無基準或換月 (顯示 --)
    is_contract_roll: bool = False  # 日線與盤中前收差異過大，判定為換月
    trend: OilTrend = OilTrend.NEUTRAL


@dataclass
class CorrelatedStockImpact:
    """關聯股衝擊評估。"""

    symbol: str
    price: float
    daily_change_pct: Optional[float]  # None = 前收盤未知 (漲跌幅無法計算)
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
    pct_change_30min: Optional[float] = None  # None = 無新鮮 30 分鐘基準
    breach_is_continuing: bool = False  # 上一輪價格已越過閾值 (非新突破)
    equity_session_open: bool = True  # 美股是否盤中 (影響能源標的漲跌欄位語意)


# 換月偵測：日線前收與盤中前一交易日收盤的相對差異門檻
_ROLL_DETECT_REL_THRESHOLD: float = 0.01
# 盤中序列中判定「交易日分界」的最小時間缺口
_SESSION_GAP_MINUTES: float = 60.0


def previous_session_close(df: Any) -> Optional[float]:
    """自盤中序列取出前一交易日收盤價。

    定義：最後一個「其後與下一根 K 棒相隔 >= 60 分鐘，且下一根於 18:00 ET 開盤
    (CME Globex 維護或週末後重開)」的 K 棒之 Close；盤中缺漏單根 K 棒不算分界。
    索引為 tz-naive ET。找不到分界則回傳 None。
    """
    try:
        if df is None or df.empty or "Close" not in df.columns or len(df) < 2:
            return None
        idx = df.index
        for i in range(len(idx) - 2, -1, -1):
            gap_min = (idx[i + 1] - idx[i]).total_seconds() / 60.0
            nxt = idx[i + 1]
            # 交易日分界：缺口 >= 60 分鐘，且下一根於 18:00 ET 開盤 (Globex 重開)
            if gap_min >= _SESSION_GAP_MINUTES and nxt.hour == 18 and nxt.minute == 0:
                val = float(df["Close"].iloc[i])
                if val != val or val <= 0.0:
                    return None
                return val
        return None
    except Exception as e:
        logger.debug(f"WTI 前一交易日收盤計算失敗: {e}")
        return None


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

        # 日線 (指標) 與盤中 (前一交易日收盤，已是當前合約) 並行抓取
        daily_res: Any
        intra_res: Any
        daily_res, intra_res = await asyncio.gather(
            get_history_df("CL=F", period="1y", interval="1d"),
            get_history_df("CL=F", period="5d", interval="30m"),
            return_exceptions=True,
        )
        df = None if isinstance(daily_res, BaseException) else daily_res
        intra_df = None if isinstance(intra_res, BaseException) else intra_res
        if isinstance(intra_res, BaseException):
            logger.debug(f"WTI 盤中序列抓取失敗: {intra_res}")
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
        # 日線序列最後一根為當前交易日；前收優先採盤中序列 (已為當前合約)，避免換月混用。
        intra_prev = previous_session_close(intra_df)
        daily_ref: Optional[float] = None
        if intra_prev is not None:
            daily_ref = intra_prev
        elif len(close) >= 2:
            daily_ref = float(close.iloc[-2])

        daily_pct: Optional[float] = None
        if daily_ref is not None and daily_ref > 0.0:
            daily_pct = (price - daily_ref) / daily_ref * 100.0

        is_roll = False
        if intra_prev is not None and len(close) >= 2:
            daily_prev = float(close.iloc[-2])
            if (
                daily_prev > 0.0
                and abs(intra_prev - daily_prev) / daily_prev
                > _ROLL_DETECT_REL_THRESHOLD
            ):
                is_roll = True

        weekly_pct: Optional[float] = None
        if not is_roll and len(close) >= 6:
            week_ago_close = float(close.iloc[-6])
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
            daily_change_pct=round(daily_pct, 2) if daily_pct is not None else None,
            weekly_change_pct=(
                round(weekly_pct, 2) if weekly_pct is not None else None
            ),
            is_contract_roll=is_roll,
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
                daily_change_pct=float(dp) if (dp := q.get("dp")) is not None else None,
                is_in_watchlist=sym in watchlist,
                is_in_holdings=sym in holdings,
            )
        except Exception as e:
            logger.debug(f"關聯股報價抓取失敗 ({sym}): {e}")
            return None

    tasks = [_fetch_one(s) for s in ENERGY_CORRELATED_SYMBOLS]
    results: list[Any] = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, CorrelatedStockImpact)]


def _format_event_time_et(raw: str) -> str:
    """將事件時間 (ISO 字串，可能以 Z 結尾) 轉為 ET `MM/DD HH:MM ET`；失敗則保留原字串。"""
    try:
        import market_time

        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return raw
        return dt.astimezone(market_time.ny_tz).strftime("%m/%d %H:%M ET")
    except Exception:
        return raw


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
                    matched_events.append(
                        f"📅 {_format_event_time_et(ev_time)}: {ev_title}"
                    )
                    break
        return matched_events[:5]
    except Exception as e:
        logger.debug(f"地緣政治事件掃描失敗: {e}")
        return []


async def analyze_wti(
    price: float,
    alert_type: WtiAlertType,
    threshold_value: float,
    pct_change_30min: Optional[float] = None,
    user_watchlist: Optional[list[str]] = None,
    user_holdings: Optional[list[str]] = None,
    prev_price_30m: Optional[float] = None,
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

    # 5. 持續性判定 (上一輪價格已越過閾值 = 非新突破)
    continuing = False
    if prev_price_30m is not None:
        if alert_type == WtiAlertType.UPPER_BREACH:
            continuing = prev_price_30m >= threshold_value
        elif alert_type == WtiAlertType.LOWER_BREACH:
            continuing = prev_price_30m <= threshold_value

    # 6. 美股是否盤中 (Finnhub c/dp 於盤外為最近收盤與該日漲跌)
    try:
        import market_time

        session_open = bool(market_time.is_market_open())
    except Exception:
        session_open = False

    return WtiAnalysisResult(
        alert_type=alert_type,
        technicals=technicals,
        correlated_impacts=correlated,
        geopolitical_events=events,
        oil_risk_weight=oil_weight,
        trigger_price=price,
        threshold_value=threshold_value,
        pct_change_30min=pct_change_30min,
        breach_is_continuing=continuing,
        equity_session_open=session_open,
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
    "previous_session_close",
    "analyze_wti",
]
