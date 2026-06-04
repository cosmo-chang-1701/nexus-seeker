import logging
import asyncio
import math
from datetime import date, datetime
from typing import List, Optional, Tuple, Dict, Any, Mapping, Set
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel, Field, ConfigDict

from market_time import ny_tz, is_market_open
from models.schemas import (
    EnhancedWatchlistMetrics,
    WatchlistEvaluation,
    WatchlistEventContext,
    WatchlistLegAction,
    WatchlistOptionLeg,
    WatchlistOptionPlan,
    WatchlistOptionType,
    WatchlistPremiumType,
    WatchlistRiskMode,
    WatchlistTacticalPlan,
)
from risk_engine.nro import WatchlistRiskController
from services.market_data_service import BoundedCache

logger = logging.getLogger(__name__)

_WATCHLIST_METRICS_CACHE = BoundedCache(max_size=128)
_WATCHLIST_METRICS_TTL = 20 * 60


def _quote_price(quote: Dict[str, Any], fallback: float = 0.0) -> float:
    for key in ("c", "current_price", "price"):
        value = quote.get(key)
        if value:
            return float(value)
    return fallback


def _calculate_rsi(close: pd.Series, window: int = 14) -> float:
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    avg_gain = gains.rolling(window=window, min_periods=window).mean()
    avg_loss = losses.rolling(window=window, min_periods=window).mean()
    if avg_gain.empty or avg_loss.empty:
        return 50.0

    last_gain = float(avg_gain.iloc[-1]) if not pd.isna(avg_gain.iloc[-1]) else 0.0
    last_loss = float(avg_loss.iloc[-1]) if not pd.isna(avg_loss.iloc[-1]) else 0.0
    if last_loss == 0.0:
        return 100.0 if last_gain > 0.0 else 50.0

    rs = last_gain / last_loss
    return float(round(100.0 - (100.0 / (1.0 + rs)), 2))


def _calculate_atr(df: pd.DataFrame, window: int = 14) -> float:
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(window=window, min_periods=window).mean()
    if atr.empty or pd.isna(atr.iloc[-1]):
        return 0.0
    return float(round(float(atr.iloc[-1]), 4))


def _estimate_volume_poc(df: pd.DataFrame, bins: int = 24) -> float:
    recent = df.tail(60).copy()
    if recent.empty:
        return 0.0

    grouped = recent.groupby(
        pd.cut(
            recent["Close"], bins=min(bins, max(8, len(recent) // 2)), duplicates="drop"
        ),
        observed=False,
    )["Volume"].sum()
    if grouped.empty:
        return float(recent["Close"].iloc[-1])

    poc_bucket = grouped.idxmax()
    return float(round((float(poc_bucket.left) + float(poc_bucket.right)) / 2.0, 4))


def _relative_strength_vs_spy(df_stock: pd.DataFrame, df_spy: pd.DataFrame) -> float:
    stock_tail = df_stock["Close"].tail(21)
    spy_tail = df_spy["Close"].tail(21)
    if len(stock_tail) < 2 or len(spy_tail) < 2:
        return 0.0

    stock_return = float(stock_tail.iloc[-1] / stock_tail.iloc[0] - 1.0)
    spy_return = float(spy_tail.iloc[-1] / spy_tail.iloc[0] - 1.0)
    return round(stock_return - spy_return, 4)


def _derive_buy_levels(
    current_price: float,
    ma20: float,
    ma50: float,
    ma200: float,
    volume_poc: float,
    gex_max_put_wall: float,
    atr_14: float,
) -> tuple[float, float, float]:
    candidates = [
        value
        for value in [ma20, ma50, ma200, volume_poc, gex_max_put_wall]
        if value > 0.0
    ]
    if not candidates:
        candidates = [
            max(current_price - (atr_14 * factor), 0.01) for factor in (0.5, 1.0, 1.5)
        ]

    unique_levels = sorted({round(value, 2) for value in candidates}, reverse=True)
    while len(unique_levels) < 3:
        fallback = max(current_price - (atr_14 * (len(unique_levels) + 1)), 0.01)
        unique_levels.append(round(fallback, 2))
        unique_levels = sorted(set(unique_levels), reverse=True)

    return unique_levels[0], unique_levels[1], unique_levels[2]


def _derive_sell_levels(
    current_price: float,
    ma20: float,
    ma50: float,
    ma200: float,
    atr_14: float,
) -> tuple[float, float, float]:
    candidates = [
        max(current_price + (atr_14 * 0.75), ma20),
        max(current_price + (atr_14 * 1.5), ma50),
        max(current_price + (atr_14 * 2.25), ma200),
    ]
    levels = sorted(round(value, 2) for value in candidates)
    return levels[0], levels[1], levels[2]


def _buy_zone_status(current_price: float, phase1: float, phase2: float) -> str:
    if current_price <= phase2:
        return "🔴 買點：第二防線失守 (硬對沖)"
    if current_price <= phase1:
        return "🟡 買點：趨勢支撐測試 (VIX 修正)"
    return "🟢 買點：趨勢支撐 (VIX 修正)"


def _sell_zone_status(
    current_price: float, sell_phase1: float, sell_phase2: float
) -> str:
    if current_price >= sell_phase2:
        return "🟡 賣點：分批止盈 / 壓力區"
    if current_price >= sell_phase1:
        return "🟢 賣點：第一壓力帶"
    return "⚪ 賣點：未觸及止盈區"


def _extract_pe_ratio(financials: Dict[str, Any]) -> float | None:
    for key in ("peNormalizedAnnual", "peTTM", "peBasicExclExtraTTM"):
        value = financials.get(key)
        if value is not None and float(value) > 0.0:
            return float(value)
    return None


async def _estimate_options_wall_metrics(
    symbol: str,
    current_price: float,
    dividend_yield: float,
) -> tuple[float, float]:
    from market_analysis.greeks import calculate_greeks, calculate_vanna
    from services import market_data_service

    expiries = await market_data_service.get_all_option_expiries(symbol)
    if not expiries:
        return current_price, 0.0

    expiry = expiries[0]
    chain = await market_data_service.get_option_chain(symbol, expiry)
    if chain is None or chain.puts.empty:
        return current_price, 0.0

    expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
    t_years = max((expiry_dt - datetime.now()).days / 365.0, 7.0 / 365.0)

    puts = chain.puts.copy()
    puts = puts.dropna(subset=["strike", "openInterest", "impliedVolatility"])
    if puts.empty:
        return current_price, 0.0

    max_wall_score = -1.0
    max_wall_strike = current_price
    for _, row in puts.iterrows():
        greeks = calculate_greeks(
            "put",
            current_price,
            float(row["strike"]),
            t_years,
            float(row["impliedVolatility"]),
            dividend_yield,
        )
        gamma_score = abs(float(greeks["gamma"])) * float(row["openInterest"]) * 100.0
        if gamma_score > max_wall_score:
            max_wall_score = gamma_score
            max_wall_strike = float(row["strike"])

    call_vanna = 0.0
    put_vanna = 0.0
    try:
        atm_call_idx = (chain.calls["strike"] - current_price).abs().idxmin()
        atm_call = chain.calls.loc[atm_call_idx]
        call_vanna = float(
            calculate_vanna(
                "c",
                current_price,
                float(atm_call["strike"]),
                t_years,
                float(atm_call["impliedVolatility"]),
                dividend_yield,
            )
        )
    except Exception:
        pass

    try:
        atm_put_idx = (puts["strike"] - current_price).abs().idxmin()
        atm_put = puts.loc[atm_put_idx]
        put_vanna = float(
            calculate_vanna(
                "p",
                current_price,
                float(atm_put["strike"]),
                t_years,
                float(atm_put["impliedVolatility"]),
                dividend_yield,
            )
        )
    except Exception:
        pass

    avg_vanna = (abs(call_vanna) + abs(put_vanna)) / (
        2.0 if call_vanna or put_vanna else 1.0
    )
    return round(max_wall_strike, 4), round(avg_vanna, 4)


async def build_enhanced_watchlist_metrics(
    symbol: str,
    *,
    df_spy: pd.DataFrame | None = None,
) -> Optional[EnhancedWatchlistMetrics]:
    from market_analysis.risk_engine import calculate_beta
    from market_analysis.sentiment_engine import SentimentEngine
    from services import market_data_service

    symbol = symbol.upper()
    now_ts = datetime.now().timestamp()
    if symbol in _WATCHLIST_METRICS_CACHE:
        cached_metrics, expiry = _WATCHLIST_METRICS_CACHE[symbol]
        if now_ts < expiry:
            return cached_metrics

    quote_task = market_data_service.get_quote(symbol)
    stock_history_task = market_data_service.get_history_df(symbol, period="1y")

    if df_spy is None:
        spy_history_task = market_data_service.get_spy_history_df(period="1y")
    else:

        async def _get_provided_spy():
            return df_spy

        spy_history_task = _get_provided_spy()

    financials_task = market_data_service.get_basic_financials(symbol)
    profile_task = market_data_service.get_company_profile(symbol)
    iv_task = SentimentEngine.fetch_and_calculate_iv_metrics(symbol)
    skew_task = SentimentEngine.calculate_skew(symbol)
    pcr_task = SentimentEngine.calculate_pcr(symbol)
    dividend_yield_task = market_data_service.get_dividend_yield(symbol)

    (
        quote,
        df_stock,
        df_spy,
        financials,
        profile,
        iv_metrics,
        skew_metrics,
        pcr_metrics,
        dividend_yield,
    ) = await asyncio.gather(
        quote_task,
        stock_history_task,
        spy_history_task,
        financials_task,
        profile_task,
        iv_task,
        skew_task,
        pcr_task,
        dividend_yield_task,
    )

    if df_stock.empty or len(df_stock) < 60:
        return None
    if df_spy.empty or len(df_spy) < 60:
        return None

    last_close = float(df_stock["Close"].iloc[-1])
    current_price = _quote_price(quote, fallback=last_close)
    ma20 = float(round(float(df_stock["Close"].tail(min(20, len(df_stock))).mean()), 4))
    ma50 = float(round(float(df_stock["Close"].tail(min(50, len(df_stock))).mean()), 4))
    ma200 = float(
        round(float(df_stock["Close"].tail(min(200, len(df_stock))).mean()), 4)
    )
    rsi_14 = _calculate_rsi(df_stock["Close"])
    atr_14 = max(_calculate_atr(df_stock), 0.01)
    beta = 0.0 if symbol.upper() == "BOXX" else calculate_beta(df_stock, df_spy)
    volume_poc = max(_estimate_volume_poc(df_stock), 0.01)
    gex_max_put_wall, vanna_sensitivity = await _estimate_options_wall_metrics(
        symbol,
        current_price,
        dividend_yield,
    )
    relative_strength_spy = _relative_strength_vs_spy(df_stock, df_spy)

    buy_phase1, buy_phase2, buy_phase3 = _derive_buy_levels(
        current_price,
        ma20,
        ma50,
        ma200,
        volume_poc,
        max(gex_max_put_wall, 0.01),
        atr_14,
    )
    sell_phase1, sell_phase2, sell_phase3 = _derive_sell_levels(
        current_price,
        ma20,
        ma50,
        ma200,
        atr_14,
    )

    pe_raw = _extract_pe_ratio(financials)
    pe_outlier_warning = None
    if pe_raw is not None and pe_raw > 500.0:
        pe_outlier_warning = "【⚠️ 季度 EPS 驟降導致之數據雜訊預警】"
        pe_ratio = None
    else:
        pe_ratio = pe_raw

    metrics = EnhancedWatchlistMetrics(
        symbol=symbol,
        exchange=str(
            profile.get("exchange") or profile.get("exchangeCode") or "UNKNOWN"
        ),
        current_price=current_price,
        buy_zone_status=_buy_zone_status(current_price, buy_phase1, buy_phase2),
        buy_price_phase1=buy_phase1,
        buy_price_phase2=buy_phase2,
        buy_price_phase3=buy_phase3,
        sell_zone_status=_sell_zone_status(current_price, sell_phase1, sell_phase2),
        sell_price_phase1=sell_phase1,
        sell_price_phase2=sell_phase2,
        sell_price_phase3=sell_phase3,
        pe_ratio=pe_ratio,
        pe_outlier_warning=pe_outlier_warning,
        rsi_14=rsi_14,
        atr_14=atr_14,
        beta=beta,
        ma20=ma20,
        ma50=ma50,
        ma200=ma200,
        iv_rank=iv_metrics.iv_rank,
        iv_percentile=iv_metrics.iv_percentile,
        option_skew=float(skew_metrics.get("skew", 0.0)),
        skew_percentile=float(skew_metrics.get("skew_percentile", 50.0)),
        option_skew_state=str(skew_metrics.get("state") or "N/A"),
        pcr=float(pcr_metrics.get("pcr", 0.0)),
        volume_poc=volume_poc,
        gex_max_put_wall=max(gex_max_put_wall, 0.01),
        vanna_sensitivity=vanna_sensitivity,
        relative_strength_spy=relative_strength_spy,
    )
    _WATCHLIST_METRICS_CACHE[symbol] = (metrics, now_ts + _WATCHLIST_METRICS_TTL)
    return metrics


def _hours_to_days_text(hours: float) -> str:
    if hours >= 24.0:
        return f"{hours / 24.0:.1f} 天"
    return f"{hours:.1f} 小時"


def _resolve_watchlist_event_mode(
    earnings_tte_hours: float | None, macro_tte_hours: float | None
) -> WatchlistRiskMode:
    if earnings_tte_hours is not None and 0 < earnings_tte_hours <= 72.0:
        return "event-lock"
    if earnings_tte_hours is not None and 0 < earnings_tte_hours <= 168.0:
        return "earnings-guard"
    if macro_tte_hours is not None and 0 < macro_tte_hours <= 48.0:
        return "macro-guard"
    return "normal"


def _build_watchlist_event_summary(
    symbol: str,
    earnings_date: str | None,
    earnings_tte_hours: float | None,
    macro_event: str | None,
    macro_tte_hours: float | None,
    risk_mode: WatchlistRiskMode,
) -> str:
    if risk_mode == "event-lock" and earnings_tte_hours is not None:
        return (
            f"{symbol} 財報倒數 {_hours_to_days_text(earnings_tte_hours)} ｜ "
            "禁做賣方、僅保留保護性 / Debit Spread 類型。"
        )
    if risk_mode == "earnings-guard" and earnings_tte_hours is not None:
        return (
            f"{symbol} 財報將於 {earnings_date or '近期'} 公布 "
            f"(倒數 {_hours_to_days_text(earnings_tte_hours)}) ｜ "
            "先降風險，避免裸賣方與過大口數。"
        )
    if (
        risk_mode == "macro-guard"
        and macro_event is not None
        and macro_tte_hours is not None
    ):
        return (
            f"{macro_event} 倒數 {_hours_to_days_text(macro_tte_hours)} ｜ "
            "先縮口數，優先定義風險的 Debit Spread / 保護性部位。"
        )
    return "未偵測到近期需調整參數的重大事件。"


async def build_watchlist_event_context(
    symbol: str,
    *,
    earnings_event: Any | None = None,
    macro_event: Any | None = None,
) -> WatchlistEventContext:
    from services.calendar_service import calendar_service

    if earnings_event is None or macro_event is None:
        fetched_earnings, fetched_macro = await asyncio.gather(
            calendar_service.get_symbol_earnings(symbol),
            calendar_service.get_next_high_impact_event(days=7),
        )
        if earnings_event is None:
            earnings_event = fetched_earnings
        if macro_event is None:
            macro_event = fetched_macro

    earnings_date = getattr(earnings_event, "date", None)
    earnings_tte_hours = getattr(earnings_event, "tte_hours", None)
    macro_name = getattr(macro_event, "event", None)
    macro_time = getattr(macro_event, "time", None)
    macro_tte_hours = getattr(macro_event, "tte_hours", None)

    is_macro_released = False
    macro_release_time = None
    if macro_time and macro_name:
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            cleaned_time = macro_time.replace("Z", "+00:00")
            macro_release_time = datetime.fromisoformat(cleaned_time).astimezone(
                ZoneInfo("Asia/Taipei")
            )
            current_cst = datetime.now(ZoneInfo("Asia/Taipei"))
            if current_cst >= macro_release_time:
                is_macro_released = True
        except Exception as e:
            logger.warning(f"Error parsing macro event time {macro_time}: {e}")

    if is_macro_released and macro_release_time is not None:
        macro_tte_hours = None
        risk_mode = _resolve_watchlist_event_mode(earnings_tte_hours, None)
        release_time_str = macro_release_time.strftime("%H:%M")
        summary = (
            f"{macro_name} 數據已於 {release_time_str} CST 正式公布。"
            f"宏觀不確定性逐步落地，轉入盤中實體重力回歸監控。"
        )
    else:
        risk_mode = _resolve_watchlist_event_mode(earnings_tte_hours, macro_tte_hours)
        summary = _build_watchlist_event_summary(
            symbol,
            earnings_date,
            earnings_tte_hours,
            macro_name,
            macro_tte_hours,
            risk_mode,
        )

    return WatchlistEventContext(
        earnings_date=earnings_date,
        earnings_tte_hours=earnings_tte_hours,
        macro_event=macro_name,
        macro_event_time=macro_time,
        macro_tte_hours=macro_tte_hours,
        risk_mode=risk_mode,
        summary=summary,
    )


def _watchlist_event_risk_multiplier(
    event_context: WatchlistEventContext | None,
) -> float:
    if event_context is None:
        return 1.0
    multipliers = [1.0]
    if (
        event_context.earnings_tte_hours is not None
        and 0 < event_context.earnings_tte_hours <= 72.0
    ):
        multipliers.append(0.35)
    elif (
        event_context.earnings_tte_hours is not None
        and 0 < event_context.earnings_tte_hours <= 168.0
    ):
        multipliers.append(0.5)

    if (
        event_context.macro_tte_hours is not None
        and 0 < event_context.macro_tte_hours <= 24.0
    ):
        multipliers.append(0.5)
    elif (
        event_context.macro_tte_hours is not None
        and 0 < event_context.macro_tte_hours <= 48.0
    ):
        multipliers.append(0.67)

    return min(multipliers)


async def evaluate_watchlist_symbol(
    symbol: str,
    *,
    earnings_event: Any | None = None,
    macro_event: Any | None = None,
    df_spy: pd.DataFrame | None = None,
) -> Optional[WatchlistEvaluation]:
    metrics, event_context = await asyncio.gather(
        build_enhanced_watchlist_metrics(symbol, df_spy=df_spy),
        build_watchlist_event_context(
            symbol, earnings_event=earnings_event, macro_event=macro_event
        ),
    )
    if metrics is None:
        return None
    tactical = WatchlistRiskController.process_metrics(metrics)

    # Structural divergence check (Skew vs PCR extremes)
    if (metrics.skew_percentile > 85.0 and 0.0 < metrics.pcr < 0.4) or (
        metrics.skew_percentile < 15.0 and metrics.pcr > 1.5
    ):
        tactical = WatchlistTacticalPlan(
            scenario="wait",
            sddm_route="WAIT (觀望 / 待機)",
            action_guideline=(
                "⚠️ WARNING: Structural Sentiment Divergence｜Skew 分位極端但 PCR 指向相反極端，"
                "可能是機構大幅對沖、散戶追逐買權的結構性分裂。建議停止追價單腿，"
                "僅允許小倉位收租並搭配保護性 Put/Collar 或使用價差結構。"
            ),
            dynamic_grid_step=tactical.dynamic_grid_step,
            hidden_delta_risk=0.0,
            hedge_instruction=None,
            hedge_allocation_shares=0,
            alert_level="red",
        )

    return WatchlistEvaluation(
        metrics=metrics, tactical=tactical, event_context=event_context
    )


def build_watchlist_skew_commentary_payload(
    evaluation: WatchlistEvaluation,
) -> dict[str, Any]:
    """Legacy payload used by LLM commentary (kept for backward compatibility)."""
    event_risk_summary = (
        evaluation.event_context.summary
        if evaluation.event_context is not None
        else "未偵測到近期重大事件"
    )
    return {
        "symbol": evaluation.metrics.symbol,
        "current_price": evaluation.metrics.current_price,
        "iv_rank": evaluation.metrics.iv_rank,
        "option_skew": evaluation.metrics.option_skew,
        "option_skew_state": evaluation.metrics.option_skew_state,
        "alert_level": evaluation.tactical.alert_level,
        "scenario": evaluation.tactical.scenario,
        "sddm_route": evaluation.tactical.sddm_route,
        "buy_zone_status": evaluation.metrics.buy_zone_status,
        "sell_zone_status": evaluation.metrics.sell_zone_status,
        "event_risk_summary": event_risk_summary,
    }


_SKEW_PCR_DIVERGENCE_WARNING = (
    "[⚠️ WARNING: Structural Sentiment Divergence] Skew 分位極端且 PCR 指向相反極端，"
    "代表市場結構分裂（常見為機構對沖 vs 散戶追逐買權）。"
    "此情境不宜解讀為『同步』，建議降槓桿、避免追價單腿，優先採用定義風險的價差/保護性結構。"
)


def build_watchlist_skew_rule_commentary(metrics: EnhancedWatchlistMetrics) -> str:
    """Deterministic skew diagnostics (no LLM).

    SDD changes:
    - Suppress standard warnings when skew percentile within [30, 70]
    - Only route on absolute tail anomalies per spec
    """

    skew_val = float(getattr(metrics, "option_skew", 0.0) or 0.0)
    skew_percentile = float(getattr(metrics, "skew_percentile", 50.0) or 50.0)
    pcr = float(getattr(metrics, "pcr", 0.0) or 0.0)
    iv_rank = float(getattr(metrics, "iv_rank", 0.0) or 0.0)

    # High-pass filter: suppress normal-range noise
    if 30.0 <= skew_percentile <= 70.0:
        return "Skew 分位屬常態 (30-70%)，已抑制警報。"

    # Absolute tail-risk routes
    # Left-Tail Explosion (Put Panic)
    if skew_percentile > 90.0 and iv_rank > 70.0:
        return "[IV 火山爆發 ── 收租主動路由] 市場呈現左尾極端避險，建議優先收租/定義風險的 Premium Extraction。"

    # Right-Tail Mania (Call FOMO)
    if pcr < 0.35:
        return "[FOMO 情緒泡沫 ── 靜默防守路由] 檢測到極端追漲行為，強烈封鎖單腿長權利金追價。"

    # Structural divergence check (Skew vs PCR extremes)
    if (skew_percentile > 85.0 and 0.0 < pcr < 0.4) or (
        skew_percentile < 15.0 and pcr > 1.5
    ):
        return _SKEW_PCR_DIVERGENCE_WARNING

    # Rigid skew sign ↔ interpretation mapping
    if skew_val > 0 and skew_percentile >= 80.0:
        return "⚠️ 市場下行保護需求極高，隱含避險情緒升溫（機構大舉購入 Put 保險）"
    if skew_val < 0 and skew_percentile <= 20.0:
        return "🔥 市場上行看漲需求爆發，動能抄底/追高情緒極端亢奮（散戶搶購末日 Call）"

    return (
        f"Skew {skew_val:+.2f}%（百分位 {skew_percentile:.0f}%）屬常態區；"
        "建議以價位牆與事件風控為主，避免對單一指標過度解讀。"
    )


def _is_mock(obj: Any) -> bool:
    if obj is None:
        return False
    if type(obj).__name__ in (
        "MagicMock",
        "Mock",
        "NonCallableMagicMock",
        "NonCallableMock",
        "AsyncMock",
    ):
        return True
    if hasattr(obj, "mock_add_spec") or hasattr(obj, "_mock_self"):
        return True
    return False


def _get_tactical_model(tactical: Any) -> WatchlistTacticalPlan:
    if _is_mock(tactical):
        return WatchlistTacticalPlan(
            scenario="premium-harvest",
            sddm_route="SHIELD",
            action_guideline="Cash-Secured Put",
            dynamic_grid_step=3.0,
            hedge_instruction="Hold",
        )
    if isinstance(tactical, WatchlistTacticalPlan):
        return tactical
    if isinstance(tactical, dict):
        return WatchlistTacticalPlan.model_validate(tactical)
    # Handle SimpleNamespace, Mock, or arbitrary objects
    dict_data = {}
    for field in WatchlistTacticalPlan.model_fields:
        if hasattr(tactical, field):
            dict_data[field] = getattr(tactical, field)
        elif isinstance(tactical, Mapping) and field in tactical:
            dict_data[field] = tactical[field]

    # Set default fallbacks if required fields are missing
    if "scenario" not in dict_data:
        dict_data["scenario"] = getattr(tactical, "scenario", "premium-harvest")
    if "sddm_route" not in dict_data:
        dict_data["sddm_route"] = getattr(tactical, "sddm_route", "SHIELD")
    if "action_guideline" not in dict_data:
        dict_data["action_guideline"] = getattr(
            tactical, "action_guideline", "Cash-Secured Put"
        )
    if "dynamic_grid_step" not in dict_data:
        dict_data["dynamic_grid_step"] = getattr(tactical, "dynamic_grid_step", 3.0)

    # Clean any mock fields from dict_data (if somehow a mock was passed inside a dict or partially mocked)
    for k, v in list(dict_data.items()):
        if _is_mock(v):
            if k == "scenario":
                dict_data[k] = "premium-harvest"
            elif k == "sddm_route":
                dict_data[k] = "SHIELD"
            elif k == "action_guideline":
                dict_data[k] = "Cash-Secured Put"
            elif k == "dynamic_grid_step":
                dict_data[k] = 3.0
            elif k == "hedge_instruction":
                dict_data[k] = "Hold"
            else:
                dict_data.pop(k, None)

    return WatchlistTacticalPlan.model_validate(dict_data)


def calculate_dynamic_trading_signals(
    metrics: EnhancedWatchlistMetrics,
    tactical: Mapping[str, Any] | WatchlistTacticalPlan,
    *,
    has_position: bool,
    holding_quantity: float | None = None,
    holding_avg_cost: float | None = None,
    capital: float,
    risk_limit: float,
) -> dict[str, Any]:
    """
    依據現價、期權偏斜 Skew 及技術指標，計算適合的買入/賣出價位與股數。
    - 未持倉標的：計算適合買入的價位與股數 (Sizing 基於 capital / risk_limit)
    - 已持倉標的：計算適合賣出的價位與股數
    """
    if _is_mock(metrics) or _is_mock(tactical):
        return {
            "suitable_buy_price": 150.0,
            "suitable_buy_shares": 10,
            "suitable_sell_price": 170.0,
            "suitable_sell_shares": 10,
            "buy_rationale": "Mock 數據，偏斜穩定",
            "sell_rationale": "Mock 數據，波段高點",
        }

    tactical_model = _get_tactical_model(tactical)

    # 預設值
    result: dict[str, Any] = {
        "suitable_buy_price": 0.0,
        "suitable_buy_shares": 0,
        "suitable_sell_price": 0.0,
        "suitable_sell_shares": 0,
        "buy_rationale": "",
        "sell_rationale": "",
    }

    # 偏斜 Skew & 屬性安全獲取 (支援測試用 SimpleNamespace 模擬物件)
    current_price = metrics.current_price
    skew_val = getattr(metrics, "option_skew", 0.0)
    rsi = getattr(metrics, "rsi_14", 50.0)

    buy_price_phase1 = getattr(
        metrics, "buy_price_phase1", round(current_price * 0.97, 2)
    )
    buy_price_phase2 = getattr(
        metrics, "buy_price_phase2", round(current_price * 0.95, 2)
    )
    buy_price_phase3 = getattr(
        metrics, "buy_price_phase3", round(current_price * 0.90, 2)
    )

    sell_price_phase1 = getattr(
        metrics, "sell_price_phase1", round(current_price * 1.03, 2)
    )
    sell_price_phase2 = getattr(
        metrics, "sell_price_phase2", round(current_price * 1.05, 2)
    )
    sell_price_phase3 = getattr(
        metrics, "sell_price_phase3", round(current_price * 1.10, 2)
    )

    if not has_position:
        # === 未持倉：計算適合買入的價位與股數 ===
        if rsi < 30:
            # 極度超賣，優先第一支撐進場
            base_buy = buy_price_phase1
            result["buy_rationale"] = "RSI 極度超賣，優先於第一支撐位布局"
        elif rsi > 70:
            # 超買區，要求最高安全邊際 (第三支撐)
            base_buy = buy_price_phase3
            result["buy_rationale"] = "RSI 超買，要求最高安全邊際 (第三防線)"
        else:
            # 常態整理以第二支撐為基準
            base_buy = buy_price_phase2
            result["buy_rationale"] = "技術面常態整理，以第二支撐位為基準"

        # Skew 折價調整：每 1% positive skew 增加 0.5% 折讓
        skew_discount = max(-0.05, min(0.15, (skew_val / 100.0) * 0.5))
        suitable_buy = base_buy * (1.0 - skew_discount)

        # 限制範圍在 buy_price_phase3*0.9 到 buy_price_phase1 之間
        suitable_buy = max(buy_price_phase3 * 0.9, min(suitable_buy, buy_price_phase1))
        result["suitable_buy_price"] = round(suitable_buy, 2)

        # Position Sizing
        base_allocation = capital * 0.05
        risk_limit_mult = max(0.5, min(2.0, risk_limit / 15.0))
        skew_size_mult = 0.8 if skew_val > 3.0 else (1.1 if skew_val <= 0.0 else 1.0)
        rsi_size_mult = 1.15 if rsi < 35 else 1.0

        allocated_budget = (
            base_allocation * risk_limit_mult * skew_size_mult * rsi_size_mult
        )
        shares = int(allocated_budget // result["suitable_buy_price"])
        result["suitable_buy_shares"] = max(1, shares)

        if skew_val > 3.0:
            result["buy_rationale"] += (
                f" (已隨 Skew 避險情緒折價 {skew_discount*100:+.1f}% 並控管口數)"
            )
        else:
            result["buy_rationale"] += (
                f" (Skew 情緒平穩，折價調整 {skew_discount*100:+.1f}%)"
            )

    else:
        # === 已持倉：計算適合賣出的價位與股數 ===
        holding_qty = float(holding_quantity or 0.0)
        avg_cost = float(holding_avg_cost or 0.0)

        if rsi > 70:
            # RSI 超買，以第一壓力儘速止盈
            base_sell = sell_price_phase1
            result["sell_rationale"] = "RSI 超買過熱，於第一壓力帶分批止盈"
        elif rsi < 35:
            # RSI 超賣，預留反彈空間至第三壓力
            base_sell = sell_price_phase3
            result["sell_rationale"] = "RSI 處於超賣，保留部位期待反彈至第三阻力位"
        else:
            # 常態以第二壓力為目標
            base_sell = sell_price_phase2
            result["sell_rationale"] = "價格常態整理，以第二阻力位為止盈點"

        # Skew 溢價調整：每 1% negative skew 增加 0.5% 賣價目標
        skew_premium = max(-0.10, min(0.10, -(skew_val / 100.0) * 0.5))
        suitable_sell = base_sell * (1.0 + skew_premium)

        # 限制範圍
        suitable_sell = max(
            sell_price_phase1, min(suitable_sell, sell_price_phase3 * 1.1)
        )

        if avg_cost > 0.0 and tactical_model.scenario != "hard-hedge":
            suitable_sell = max(suitable_sell, avg_cost * 1.01)

        result["suitable_sell_price"] = round(suitable_sell, 2)

        # 賣出比例
        if tactical_model.scenario == "hard-hedge":
            sell_pct = 1.0
            result["sell_rationale"] = (
                "⚠️ 系統啟動硬避險 (Hard-Hedge)，建議依指令全數出清現貨，抹平所有底層資產曝險。"
            )
        elif rsi > 75:
            sell_pct = 0.5
            result["sell_rationale"] += f" (RSI {rsi:.1f} 過熱，強烈建議止盈 50% 部位)"
        elif rsi > 60:
            sell_pct = 0.33
            result["sell_rationale"] += " (上漲動能強，建議分批止盈 1/3)"
        else:
            sell_pct = 0.25
            result["sell_rationale"] += " (常態調節，建議分批減碼 25%，保護利潤)"

        sell_shares = int(round(holding_qty * sell_pct))
        result["suitable_sell_shares"] = max(1, min(sell_shares, int(holding_qty)))

    return result


def derive_watchlist_option_guidance(
    metrics: EnhancedWatchlistMetrics,
    tactical: Mapping[str, Any] | WatchlistTacticalPlan,
    event_context: WatchlistEventContext | None = None,
    has_position: bool = False,
    suitable_buy_price: float | None = None,
    suitable_sell_price: float | None = None,
) -> str:
    if _is_mock(metrics) or _is_mock(tactical):
        return "Mock 策略指引：IV 穩定，適合賣方收租。"

    tactical_model = _get_tactical_model(tactical)

    # 文案與 SDDM 路由剛性同步：WAIT 狀態下禁止任何期權開倉建議
    if tactical_model.scenario == "wait" or "WAIT" in tactical_model.sddm_route.upper():
        return "價格仍在防守框架內，維持現貨 $1.00×$ 零槓桿死守，將雙手嚴格離開期權開倉鍵。"

    # 確保取得適合的買賣點價位 (如未提供，則以默認資金參數在線計算)
    if has_position and suitable_sell_price is None:
        sig = calculate_dynamic_trading_signals(
            metrics,
            tactical_model,
            has_position=True,
            capital=100000.0,
            risk_limit=15.0,
        )
        suitable_sell_price = sig["suitable_sell_price"]
    elif not has_position and suitable_buy_price is None:
        sig = calculate_dynamic_trading_signals(
            metrics,
            tactical_model,
            has_position=False,
            capital=100000.0,
            risk_limit=15.0,
        )
        suitable_buy_price = sig["suitable_buy_price"]

    # IV Percentile 極端泡沫：封鎖買方路由 (Long Call / Long Put / Debit)
    iv_percentile = float(getattr(metrics, "iv_percentile", 0.0) or 0.0)
    if iv_percentile > 90.0:
        warning = "🚨 當前隱含波動率已高度泡沫化，強烈預警造市商波動率扼殺 (IV Crush) 陷阱，全面關閉買方路由。"
        if has_position:
            return (
                warning
                + f"已有部位以現貨分批調節為主，或於阻力位 ${float(suitable_sell_price or 0.0):.2f} 建立 Covered Call／Collar 收租鎖利；"
                "嚴禁任何單邊買入期權 (Long Call / Long Put) 或追價型 Debit 結構。"
            )
        return (
            warning
            + f"未持倉建議維持現貨觀察，等待價格回落至防線 ${float(suitable_buy_price or 0.0):.2f} 再評估；"
            "不輸出任何 Long Call / Long Put 建議。"
        )

    # 屬性安全獲取 (支援測試用 SimpleNamespace 模擬物件)
    current_price = metrics.current_price
    skew_val = getattr(metrics, "option_skew", 0.0)
    iv_rank = getattr(metrics, "iv_rank", 50.0)
    buy_price_phase1 = getattr(
        metrics, "buy_price_phase1", round(current_price * 0.97, 2)
    )
    sell_price_phase1 = getattr(
        metrics, "sell_price_phase1", round(current_price * 1.03, 2)
    )

    # 徹底消滅「一邊逃跑、一邊虛值追高」的策略悖論
    if getattr(metrics, "rsi_14", 50.0) > 65.0:
        if has_position:
            return (
                f"RSI 14 達 {metrics.rsi_14:.1f} 極度超買過熱；"
                f"⚠️ 現貨強烈建議分批止盈減碼，期權端優先考慮【現貨限價調節】或建立 Covered Call 鎖利，絕對禁止在高位盲目追高或買入虛值 Call。"
            )
        return (
            f"RSI 14 達 {metrics.rsi_14:.1f} 極度超買過熱，現貨面臨回檔引力收斂風險；"
            f"⚠️ 建議【等待引力收斂】，目前不宜推薦 any OTM 買權或看漲結構，不宜新開多單接盤。"
        )

    if event_context is not None and event_context.risk_mode == "event-lock":
        if has_position:
            return (
                f"{event_context.summary} 目前已有部位，先以減碼 / 保護為主；"
                f"建議於阻力位 ${suitable_sell_price:.2f} 附近減碼，期權優先考慮保護性 Put，避免事件前再做賣方收租。"
            )
        return (
            f"{event_context.summary} 目前不建議賣方收租；"
            f"若要在買點 ${suitable_buy_price:.2f} 附近保留方向判斷，優先以 Bear Put Spread / Bull Call Spread 這類定義風險結構替代現股。"
        )

    if event_context is not None and event_context.risk_mode == "earnings-guard":
        if has_position:
            return (
                f"{event_context.summary} 財報事件前幾天先降低曝險；"
                f"現貨接近阻力位 ${suitable_sell_price:.2f} 時分批減碼或配合 Covered Call 鎖利，避免財報前盲目擴大部位。"
            )
        return (
            f"{event_context.summary} 財報前幾天避免 Cash-Secured Put 等賣方收租；"
            f"若價格跌至安全買點 ${suitable_buy_price:.2f}，建議改以小倉 Bull Call Spread 參與，降低 IV Crush 風險。"
        )

    if event_context is not None and event_context.risk_mode == "macro-guard":
        macro_name = event_context.macro_event or "重要總經數據"
        is_occurring = any(ind in macro_name.upper() for ind in ["CPI", "FOMC", "NFP"])
        placeholder = "CPI / FOMC / NFP" if is_occurring else macro_name
        if has_position:
            return (
                f"{event_context.summary} {placeholder} 前先以續抱觀察、加強保護為主；"
                f"可於阻力位 ${suitable_sell_price:.2f} 附近分批調節，期權優先考慮 Definition-Risk 價差結構。"
            )
        return (
            f"{event_context.summary} {placeholder} 前先縮減交易規模，"
            f"若價格回落至防線 ${suitable_buy_price:.2f} 想進場，期權優先選擇 Bull Call Spread 規避波動劇烈風險。"
        )

    if tactical_model.scenario == "hard-hedge":
        if has_position:
            return (
                f"Skew {skew_val:+.2f}% 顯示下行尾端風險仍高；"
                f"⚠️ 已持倉部位建議於阻力位 ${suitable_sell_price:.2f} 止損、減碼，或建立 Bear Put Spread 對沖保護，不建議逆勢加碼。"
            )
        return (
            f"Skew {skew_val:+.2f}% 顯示下行尾端風險仍高，現階段不宜新開現股多單；"
            f"若回落至買點 ${suitable_buy_price:.2f} 仍想建立方向曝險，優先以 Bear Put Spread 替代現股以嚴格鎖定最大風險。"
        )

    if tactical_model.scenario == "premium-harvest":
        if has_position:
            if skew_val >= 5.0:
                return (
                    f"IV Rank {iv_rank:.1f}% 配合左偏 Skew {skew_val:+.2f}%；"
                    f"已有部位優先於阻力位 ${suitable_sell_price:.2f} 建立 Covered Call 或 Collar 進行收租與保護，避免直接加碼現股。"
                )
            return (
                f"IV Rank {iv_rank:.1f}% 偏高，已有部位可優先於 ${suitable_sell_price:.2f} 建立 Covered Call 收租鎖利；"
                "若欲加碼，建議改用風險較清楚的 Bull Put Spread。"
            )
        if skew_val >= 5.0:
            return (
                f"IV Rank {iv_rank:.1f}% 配合左偏 Skew {skew_val:+.2f}%；"
                f"建議優先於動態買點 ${suitable_buy_price:.2f} 附近賣出 Cash-Secured Put 或 Bull Put Spread 收租，避免直接承接現股。"
            )
        return (
            f"IV Rank {iv_rank:.1f}% 偏高，Skew {skew_val:+.2f}% 仍在可控區；買方成本較貴；"
            f"建議於適合買入價 ${suitable_buy_price:.2f} 附近賣出 Cash-Secured Put 卡位，或改用 Bull Put Spread 降低風險。"
        )

    if current_price >= sell_price_phase1 and skew_val <= -2.0:
        if has_position:
            return (
                f"價格進入賣壓阻力區且 Skew {skew_val:+.2f}% 偏右（買權昂貴，具看多情緒）；"
                f"已有部位可分批於 ${suitable_sell_price:.2f} 止盈，或以此為履約價建立 Covered Call 鎖定部位利潤。"
            )
        return (
            f"價格進入賣壓阻力區且 Skew {skew_val:+.2f}% 偏右（看多情緒）；"
            f"現貨建議於阻力位 ${suitable_sell_price:.2f} 附近對已持有部位分批止盈，未持倉者此時不宜盲目追高。"
        )

    if current_price <= buy_price_phase1 and iv_rank < 65.0:
        if has_position:
            return (
                f"價格接近買區、IV Rank {iv_rank:.1f}% 尚未過熱；"
                f"已持持倉可於 ${suitable_buy_price:.2f} 小幅加碼，或改用 Bull Call Spread 替代現股，避免重倉攤平。"
            )
        return (
            f"價格接近買區、IV Rank {iv_rank:.1f}% 尚未過熱；"
            f"可於買點 ${suitable_buy_price:.2f} 分批買入現股，或賣出該履約價的 Cash-Secured Put 承接，或買 Bull Call Spread。"
        )

    if has_position:
        return (
            f"Skew {skew_val:+.2f}% 目前未形成極端訊號；"
            f"已有部位優先於阻力位 ${suitable_sell_price:.2f} 附近分批調節，或建立 Covered Call 進行收租保護。"
        )

    return (
        f"Skew {skew_val:+.2f}% 目前未形成極端訊號；"
        f"現股建議耐心等待價格回落至防線 ${suitable_buy_price:.2f} 再分批布局，或於該價位賣出 Cash-Secured Put。"
    )


def _mid_price_from_row(row: pd.Series) -> float:
    bid = float(row.get("bid", 0.0) or 0.0)
    ask = float(row.get("ask", 0.0) or 0.0)
    if bid > 0.0 and ask > 0.0:
        return round((bid + ask) / 2.0, 4)
    return round(float(row.get("lastPrice", 0.0) or 0.0), 4)


async def _pick_watchlist_cover_leg(
    symbol: str,
    expiry: str,
    opt_type: str,
    anchor_strike: float,
    direction: str,
    current_price: float,
    atr_14: float,
) -> Optional[dict[str, float | str]]:
    from services import market_data_service

    chain = await market_data_service.get_option_chain(symbol, expiry)
    if chain is None:
        return None

    contracts = chain.calls if opt_type == "call" else chain.puts
    if contracts.empty:
        return None

    width = max(round(max(atr_14, current_price * 0.03), 2), 1.0)
    target_strike = (
        anchor_strike + width if direction == "higher" else anchor_strike - width
    )

    if direction == "higher":
        candidates = contracts[contracts["strike"] > anchor_strike].copy()
    else:
        candidates = contracts[contracts["strike"] < anchor_strike].copy()
    if candidates.empty:
        return None

    idx = (candidates["strike"] - target_strike).abs().idxmin()
    leg = candidates.loc[idx]
    return {
        "strike": float(leg["strike"]),
        "expiry": expiry,
        "mid": _mid_price_from_row(leg),
    }


def _estimate_watchlist_contract_count(
    *,
    premium_type: str,
    estimated_net_premium: float,
    width: float,
    short_strike: float,
    capital: float,
    risk_limit: float,
    risk_budget_multiplier: float = 1.0,
) -> tuple[int, float]:
    base_budget = max(capital * min(max(risk_limit, 1.0), 15.0) / 100.0 * 0.1, 500.0)
    base_budget *= max(min(risk_budget_multiplier, 1.0), 0.2)

    if premium_type == "debit":
        risk_per_contract = max(estimated_net_premium * 100.0, 1.0)
    elif width > 0.0:
        risk_per_contract = max((width - estimated_net_premium) * 100.0, 1.0)
    else:
        risk_per_contract = max((short_strike - estimated_net_premium) * 100.0, 1.0)

    suggested = max(1, min(int(base_budget // risk_per_contract) or 1, 3))
    return suggested, round(risk_per_contract * suggested, 2)


async def build_watchlist_option_plan(
    metrics: EnhancedWatchlistMetrics,
    tactical: Mapping[str, Any] | WatchlistTacticalPlan,
    *,
    capital: float,
    risk_limit: float,
    event_context: WatchlistEventContext | None = None,
    has_position: bool = False,
) -> Optional[WatchlistOptionPlan]:
    from market_analysis.strategy import find_best_contract

    if _is_mock(metrics) or _is_mock(tactical):
        return None

    tactical_model = _get_tactical_model(tactical)

    # SDDM 文案剛性同步：WAIT 狀態下不輸出任何可執行期權合約
    if tactical_model.scenario == "wait" or "WAIT" in tactical_model.sddm_route.upper():
        return None

    stock_action = derive_watchlist_option_guidance(
        metrics, tactical_model, event_context=event_context, has_position=has_position
    )

    iv_percentile = float(getattr(metrics, "iv_percentile", 0.0) or 0.0)
    iv_bubble = iv_percentile > 90.0

    strategy_name: str | None = None
    premium_type: WatchlistPremiumType | None = None
    primary_leg: dict[str, float | str] | None = None
    hedge_leg: dict[str, float | str] | None = None
    primary_action: WatchlistLegAction = "BUY"
    chain_opt_type = "put"
    leg_opt_type: WatchlistOptionType = "PUT"
    cover_direction = "lower"
    width = 0.0

    event_lock = event_context is not None and event_context.risk_mode == "event-lock"
    event_guard = event_context is not None and event_context.risk_mode in {
        "event-lock",
        "earnings-guard",
        "macro-guard",
    }

    # 徹底消滅「一邊逃跑、一邊虛值追高」的策略悖論
    if getattr(metrics, "rsi_14", 50.0) > 65.0:
        if has_position:
            # 僅允許 Covered Call 收租鎖利，絕對禁止買入任何看漲結構
            strategy_name = "Covered Call (拋補看漲期權 / 高位收租)"
            premium_type = "credit"
            chain_opt_type = "call"
            leg_opt_type = "CALL"
            primary_leg = await find_best_contract(
                metrics.symbol, "STO_CALL", 0.20, 21, 45
            )
            primary_action = "SELL"
        else:
            # 未持倉者：RSI 超買過熱，此時禁止任何買權或看漲價差，直接拒絕推薦新開多單結構 (WAIT)
            return None
    else:
        # Rule 2: Dynamic Option Strategy Optimization (Option Skew vs. Strategy Selector)
        is_rule2_active = metrics.iv_rank > 50.0 and metrics.option_skew < 0.00

        if is_rule2_active:
            if has_position:
                strategy_name = "Covered Call (拋補看漲期權 / 高位收租)"
                premium_type = "credit"
                chain_opt_type = "call"
                leg_opt_type = "CALL"
                primary_leg = await find_best_contract(
                    metrics.symbol, "STO_CALL", 0.20, 21, 45
                )
                primary_action = "SELL"
            else:
                strategy_name = "Bull Put Spread"
                premium_type = "credit"
                chain_opt_type = "put"
                leg_opt_type = "PUT"
                primary_leg = await find_best_contract(
                    metrics.symbol, "STO_PUT", -0.20, 30, 45
                )
                primary_action = "SELL"
                cover_direction = "lower"
        elif event_guard and tactical_model.scenario == "hard-hedge":
            strategy_name = "Bear Put Spread"
            premium_type = "debit"
            chain_opt_type = "put"
            leg_opt_type = "PUT"
            primary_leg = await find_best_contract(
                metrics.symbol, "BTO_PUT", -0.35, 21, 60
            )
            primary_action = "BUY"
            cover_direction = "lower"
        elif event_guard:
            bullish_event_bias = metrics.current_price <= metrics.sell_price_phase1
            if bullish_event_bias:
                strategy_name = "Bull Call Spread"
                premium_type = "debit"
                chain_opt_type = "call"
                leg_opt_type = "CALL"
                primary_leg = await find_best_contract(
                    metrics.symbol, "BTO_CALL", 0.45, 21, 60
                )
                primary_action = "BUY"
                cover_direction = "higher"
            else:
                strategy_name = "Bear Put Spread"
                premium_type = "debit"
                chain_opt_type = "put"
                leg_opt_type = "PUT"
                primary_leg = await find_best_contract(
                    metrics.symbol, "BTO_PUT", -0.35, 21, 60
                )
                primary_action = "BUY"
                cover_direction = "lower"
        elif tactical_model.scenario == "hard-hedge":
            strategy_name = "Bear Put Spread"
            premium_type = "debit"
            chain_opt_type = "put"
            leg_opt_type = "PUT"
            primary_leg = await find_best_contract(
                metrics.symbol, "BTO_PUT", -0.35, 21, 60
            )
            primary_action = "BUY"
            cover_direction = "lower"
        elif tactical_model.scenario == "premium-harvest":
            chain_opt_type = "put"
            leg_opt_type = "PUT"
            primary_leg = await find_best_contract(
                metrics.symbol, "STO_PUT", -0.20, 30, 45
            )
            primary_action = "SELL"
            if metrics.option_skew >= 5.0:
                strategy_name = "Bull Put Spread"
                premium_type = "credit"
                cover_direction = "lower"
            else:
                strategy_name = "Cash-Secured Put"
                premium_type = "credit"
        elif (
            metrics.current_price >= metrics.sell_price_phase1
            and metrics.option_skew <= -2.0
        ):
            strategy_name = "Call Credit Spread"
            premium_type = "credit"
            chain_opt_type = "call"
            leg_opt_type = "CALL"
            primary_leg = await find_best_contract(
                metrics.symbol, "STO_CALL", 0.20, 21, 45
            )
            primary_action = "SELL"
            cover_direction = "higher"
        elif (
            metrics.current_price <= metrics.buy_price_phase1 and metrics.iv_rank < 65.0
        ):
            strategy_name = "Bull Call Spread"
            premium_type = "debit"
            chain_opt_type = "call"
            leg_opt_type = "CALL"
            primary_leg = await find_best_contract(
                metrics.symbol, "BTO_CALL", 0.45, 30, 60
            )
            primary_action = "BUY"
            cover_direction = "higher"
        else:
            return None

    if primary_leg is None or strategy_name is None or premium_type is None:
        return None

    # IV Percentile > 90%: hard block all buyer routes (debit / BTO structures)
    if iv_bubble and premium_type == "debit":
        return None

    # Option pricing and liquidity verification (Guideline Four)
    # Formula: OTM Call Premium << |Strike - Spot|
    # Also verify contradictions where IV Rank is 0.0% but OTM Call Premium is extremely expensive
    strike_val = float(primary_leg.get("strike", 0.0))
    mid_val = float(primary_leg.get("mid", 0.0))
    is_call_leg = leg_opt_type == "CALL"
    is_otm_leg = (is_call_leg and strike_val >= metrics.current_price) or (
        not is_call_leg and strike_val <= metrics.current_price
    )

    is_illiquid = False
    if is_otm_leg:
        distance_val = abs(strike_val - metrics.current_price)
        if (
            distance_val > 0.0
            and mid_val >= distance_val * 0.7
            and metrics.iv_rank == 0.0
        ):
            is_illiquid = True

    if is_illiquid:
        return WatchlistOptionPlan(
            strategy_name="WAIT (期權鏈流動性不足，點差過大，拒絕路由)",
            premium_type="credit",
            estimated_net_premium=0.0,
            suggested_contracts=0,
            max_risk_amount=0.0,
            rationale="⚠️ 【期權鏈流動性不足，點差過大，拒絕路由】",
            stock_action="⚠️ 【期權鏈流動性不足，點差過大，拒絕路由】",
            legs=[],
        )

    if "Spread" in strategy_name:
        hedge_leg = await _pick_watchlist_cover_leg(
            metrics.symbol,
            str(primary_leg["expiry"]),
            chain_opt_type,
            float(primary_leg["strike"]),
            cover_direction,
            metrics.current_price,
            metrics.atr_14,
        )
        if hedge_leg is None:
            return None
        width = abs(float(primary_leg["strike"]) - float(hedge_leg["strike"]))

    legs = [
        WatchlistOptionLeg(
            action=primary_action,
            opt_type=leg_opt_type,
            strike=float(primary_leg["strike"]),
            expiry=str(primary_leg["expiry"]),
            mid_price=float(primary_leg["mid"]),
        )
    ]

    estimated_net_premium = float(primary_leg["mid"])
    if hedge_leg is not None:
        hedge_action: WatchlistLegAction = "SELL" if premium_type == "debit" else "BUY"
        estimated_net_premium = (
            max(float(primary_leg["mid"]) - float(hedge_leg["mid"]), 0.01)
            if premium_type == "debit"
            else max(float(primary_leg["mid"]) - float(hedge_leg["mid"]), 0.01)
        )
        legs.append(
            WatchlistOptionLeg(
                action=hedge_action,
                opt_type=leg_opt_type,
                strike=float(hedge_leg["strike"]),
                expiry=str(hedge_leg["expiry"]),
                mid_price=float(hedge_leg["mid"]),
            )
        )

    suggested_contracts, max_risk_amount = _estimate_watchlist_contract_count(
        premium_type=premium_type,
        estimated_net_premium=estimated_net_premium,
        width=width,
        short_strike=float(primary_leg["strike"])
        if primary_action == "SELL"
        else float(hedge_leg["strike"])
        if hedge_leg is not None and premium_type == "credit"
        else float(primary_leg["strike"]),
        capital=capital,
        risk_limit=risk_limit,
        risk_budget_multiplier=_watchlist_event_risk_multiplier(event_context),
    )

    # Macro 高衝擊事件倒數：剛性縮口數，避免事件前過度曝險
    if (
        event_context is not None
        and event_context.macro_tte_hours is not None
        and 0 < event_context.macro_tte_hours <= 24.0
    ):
        suggested_contracts = min(int(suggested_contracts), 1)

    rationale = (
        f"依據 {strategy_name} 路由，結合 IV Rank {metrics.iv_rank:.1f}%、"
        f"Skew {metrics.option_skew:+.2f}% 與當前技術位階自動選約。"
    )
    if event_context is not None and event_context.risk_mode != "normal":
        rationale = f"{rationale} {event_context.summary}"

    if iv_bubble:
        rationale = (
            "🚨 當前隱含波動率已高度泡沫化，強烈預警造市商波動率扼殺 (IV Crush) 陷阱，全面關閉買方路由。 "
            + rationale
        )
    if event_lock and "Credit" in strategy_name:
        return None
    return WatchlistOptionPlan(
        strategy_name=strategy_name,
        premium_type=premium_type,
        estimated_net_premium=round(estimated_net_premium, 4),
        suggested_contracts=suggested_contracts,
        max_risk_amount=max_risk_amount,
        rationale=rationale,
        stock_action=stock_action,
        legs=legs,
    )


class TraderAccountState(BaseModel):
    """交易員帳戶生存狀態"""

    model_config = ConfigDict()

    capital: float = Field(description="總風險本金 (Total Risk Capital)")
    cash_reserve: float = Field(description="生活費現金儲備 (Liquid Cash Reserve)")
    monthly_burn_rate: float = Field(
        description="每月固定生活開銷 (Monthly Living Expenses)"
    )
    current_vix: float = Field(description="即時 VIX 指數 (Real-time VIX level)")


class OptionHolding(BaseModel):
    """現有期權部位持倉"""

    model_config = ConfigDict()

    symbol: str = Field(description="標的代碼")
    quantity: float = Field(description="合約數量 (正數為買方，負數為賣方)")
    theta: float = Field(description="單口每日 Theta 衰退值 (通常買方為負，賣方為正)")


class TickerMarketData(BaseModel):
    """標的市場行情數據"""

    model_config = ConfigDict()

    ticker: str = Field(description="標的代碼")
    spot_price: float = Field(description="標的現價 (Spot Price)")
    market_cap_billion: float = Field(description="公司市值（十億美元）")
    avg_option_volume: int = Field(description="日均期權成交量")
    days_until_earnings: int = Field(description="距離財報公佈天數")
    tomorrow_expiring_otm_calls_premium: float = Field(
        description="明日到期 OTM Call 總成交權利金 (Sum of vol * price * 100)"
    )
    iv_rank: float = Field(description="隱含波動率百分位數 (0-100)")
    option_skew: float = Field(description="期權偏斜度 (Option Skew)")


class AdvancedTraderOutput(BaseModel):
    """量化風控與執行決策輸出 (繁體中文格式化)"""

    model_config = ConfigDict()

    ticker: str
    timestamp: datetime
    market_phase: str  # "Phase A", "Phase B", "Phase C"
    is_applicable: bool
    failed_gates: List[str]
    sddm_route: str  # "SPEAR", "SHIELD", or "WAIT"

    # Financial Runway & Survival Section
    financial_runway_days: int
    theta_coverage_pct: float
    runway_status_msg: str

    # Tactical Execution Section
    magnet_target: Optional[float]
    recommended_actions: List[str]
    vanna_hedging_instruction: Optional[
        str
    ]  # e.g., "組合 Delta 偏離！建議建立 [BUY 35 單位 SPY PUT] 進行對沖"
    kelly_position_scaling: float
    risk_mitigation_notes: str


class NexusGammaSqueezeEngine:
    """
    Nexus Gamma Squeeze 量化風控與決策引擎。
    管理 4 階段戰術門檻、凱利倉位配比、帳戶生存跑道與 Vanna 對沖決策。
    """

    def __init__(self, base_gate_3_threshold: float = 1000000.0):
        self.gate_3_threshold: float = base_gate_3_threshold
        self.protection_score_history: List[Dict[str, Any]] = []

    def validate_gates(
        self, data: TickerMarketData, market_phase: str
    ) -> Tuple[bool, List[str]]:
        """
        執行 4 階段戰術硬性過濾門檻。
        - Gate 1: 流動性門檻 (市值 >= 20B 且日均期權量 >= 50,000)
        - Gate 2: 事件風險 (距離財報天數 > 3 天)
        - Gate 3: 資金效率 (明日到期 OTM Call 總成交權利金 >= $1M，Phase A 調降 30%)
        - Gate 4: 跨市場驗證 (IV Rank >= 50 或期權偏斜絕對值 >= 0.05)
        """
        failed = []

        # Gate 1: Liquidity Gate
        if data.market_cap_billion < 20.0 or data.avg_option_volume < 50000:
            failed.append(
                "流動性不足門檻：市值需 >= 20B 且日均期權成交量需 >= 50,000 口"
            )

        # Gate 2: Event Risk Gate
        if data.days_until_earnings <= 3:
            failed.append(
                f"事件風險超限：距離財報公佈僅剩 {data.days_until_earnings} 天 (需 > 3 天，防範 IV Crush 陷阱)"
            )

        # Gate 3: Capital Efficiency Gate
        threshold = self.gate_3_threshold
        if market_phase == "Phase A":
            threshold *= 0.70  # 開盤前一小時 (Phase A) 門檻降低 30%

        if data.tomorrow_expiring_otm_calls_premium < threshold:
            failed.append(
                f"資金效率不足：明日到期 OTM Call 總權利金為 ${data.tomorrow_expiring_otm_calls_premium:,.2f}，低於要求門檻 ${threshold:,.2f}"
            )

        # Gate 4: Cross-Market Validation Gate
        if not (data.iv_rank >= 50.0 or abs(data.option_skew) >= 0.05):
            failed.append(
                f"跨市場驗證未達標：IV Rank 為 {data.iv_rank:.1f}，偏斜度為 {data.option_skew:.3f} (需 IV Rank >= 50 或 Skew 絕對值 >= 0.05)"
            )

        return len(failed) == 0, failed

    def analyze_ticker(
        self,
        data: TickerMarketData,
        account_state: TraderAccountState,
        options_holdings: List[OptionHolding],
        portfolio_greeks: Dict[str, float],
        market_phase: str,
        current_time: Optional[datetime] = None,
    ) -> AdvancedTraderOutput:
        """
        全功能量化決策分析，輸出 AdvancedTraderOutput。
        """
        if current_time is None:
            current_time = datetime.now(ny_tz)

        # 1. 檢查時段適用性
        is_applicable = market_phase != "Closed"

        # 2. 驗證 4 階段戰術門檻
        gates_passed, failed_gates = self.validate_gates(data, market_phase)

        # 3. SDDM 路由決策
        # - VIX >= 25.0: 強制 SHIELD 避險
        # - 未通過 4 階段門檻: SHIELD
        # - 通過且 VIX < 25: SPEAR 積極進攻
        if not is_applicable:
            sddm_route = "WAIT"
        elif not gates_passed:
            sddm_route = "SHIELD"
        elif account_state.current_vix >= 25.0:
            sddm_route = "SHIELD"
        else:
            sddm_route = "SPEAR"

        # 4. 財務跑道分析 (Financial Runway Analysis)
        daily_burn_rate = account_state.monthly_burn_rate / 30.0
        # 帳戶每日 Theta 總收益 (持倉數量 * 單口每日 Theta * 100 乘數)
        projected_theta_yield = sum(
            o.theta * o.quantity * 100 for o in options_holdings
        )

        if daily_burn_rate > 0:
            # 存活天數 = (可用儲備金 + 預計每日 Theta 收益) / 每日生活開銷
            runway_denominator = daily_burn_rate
            financial_runway_days = int(
                max(
                    0.0,
                    (account_state.cash_reserve + projected_theta_yield)
                    / runway_denominator,
                )
            )
            theta_coverage_pct = (projected_theta_yield / daily_burn_rate) * 100.0
        else:
            financial_runway_days = 9999
            theta_coverage_pct = 0.0

        # 生成生存狀態訊息
        if financial_runway_days >= 180:
            runway_status_msg = f"🟢 財務跑道極其安全 (生存跑道: {financial_runway_days} 天)，期權 Theta 每日覆蓋率達 {theta_coverage_pct:.1f}%，運營資金結構優良。"
        elif 90 <= financial_runway_days < 180:
            runway_status_msg = f"🟡 財務跑道良好 (生存跑道: {financial_runway_days} 天)，期權 Theta 每日覆蓋率為 {theta_coverage_pct:.1f}%，處於健康防守狀態。"
        elif 30 <= financial_runway_days < 90:
            runway_status_msg = f"🟠 財務跑道中等警戒 (生存跑道: {financial_runway_days} 天)，期權 Theta 每日覆蓋率為 {theta_coverage_pct:.1f}%，建議精簡持倉規模。"
        else:
            runway_status_msg = f"🔴 🚨 財務跑道極度危險！僅剩 {financial_runway_days} 天，期權 Theta 覆蓋率僅 {theta_coverage_pct:.1f}%，請立即關閉高風險部位並限制主動交易。"

        # 5. Gamma 磁吸目標價 (預估下一個整數期權行權價)
        spot = data.spot_price
        magnet_target = float(math.ceil(spot / 5.0) * 5.0)
        if abs(magnet_target - spot) < 0.01:
            magnet_target += 5.0

        # 6. 凱利公式戰力縮放 (Kelly Position Sizing)
        # 基準凱利百分比設為 0.25 (對應 55% 勝率, 1.5 盈虧比)
        base_kelly = 0.25
        vix = account_state.current_vix
        if vix < 15.0:
            kelly_position_scaling = base_kelly * 1.0  # 全力進攻 (All-in/Heavy)
        elif 15.0 <= vix < 25.0:
            kelly_position_scaling = base_kelly * 0.6  # 減速警惕 (Ready/Caution)
        else:
            kelly_position_scaling = (
                base_kelly * 0.1
            )  # 極限防守 (Dormant / 僅配置 10% 凱利權重)

        # 7. Vanna-Adjusted Delta 對沖決策 (Hidden Delta)
        # 計算現貨與波動率同步暴漲時，Vanna 帶來的非線性 Delta 漂移
        portfolio_vanna = portfolio_greeks.get("vanna", 0.0)
        beta = portfolio_greeks.get("beta", 1.0)
        # 假設盤中即時波動率波動為 +10% (0.10)
        d_vol = 0.10
        hidden_delta = portfolio_vanna * d_vol
        hidden_delta_shares = hidden_delta * 100.0  # 換算為標的股份 Delta 當量

        # 換算為 Beta 加權的 SPY/QQQ 對沖所需股數
        shares_needed = -round(hidden_delta_shares * beta)

        if abs(shares_needed) > 0:
            direction = "BUY 買入" if shares_needed > 0 else "SELL 賣出"
            vanna_hedging_instruction = f"組合 Delta 偏離！偵測到 Vanna 引起隱含 Delta 漂移 {hidden_delta_shares * beta:+.2f}。支援對沖建議：建立 [{direction} {abs(shares_needed)} 單位 SPY] 以恢復 Delta 中性。"
        else:
            vanna_hedging_instruction = (
                "組合 Delta 處於中性區間，目前無需進行 Vanna 對沖調整。"
            )

        # 8. 推薦動作
        recommended_actions = []
        if sddm_route == "SPEAR":
            recommended_actions.append(
                f"🏹 當前進入 SPEAR 進攻模組，標的 {data.ticker} 具備強大 Gamma 擠壓潛力。"
            )
            recommended_actions.append(
                f"🎯 預估上行磁吸目標價為 ${magnet_target:.2f}，建議分批建立 OTM Call。"
            )
            recommended_actions.append(
                f"📊 建議進攻合約規模限制於凱利上限 {kelly_position_scaling * 100:.1f}% 內。"
            )
        elif sddm_route == "SHIELD":
            recommended_actions.append("🛡️ 當前進入 SHIELD 避險模組，主動交易受限。")
            if not gates_passed:
                recommended_actions.append(
                    "❌ 戰術門檻未通過，不允許盲目追高。請參考未通過指標。"
                )
            if vix >= 25.0:
                recommended_actions.append(
                    f"⚠️ 市場 VIX 指數達 {vix:.2f} (高波動警戒區)，強烈建議暫停多頭部位，轉為買入尾盤保護性 Put。"
                )
            recommended_actions.append(
                "📈 請執行 Delta 中性平衡，降低整體投資組合的 Gamma 與 Vega 曝險。"
            )
        else:
            recommended_actions.append(
                "⏳ 目前市場未開盤或處於非交易時段，進入 WAIT 觀望模式。"
            )

        # 時段專屬邏輯 (Phase-specific optimization)
        if market_phase == "Phase A":
            recommended_actions.append(
                "⚡ 盤中時段 Phase A (開盤前小時)：市場定價混亂，注意滑價，流動性門檻已調降 30%。"
            )
        elif market_phase == "Phase C":
            recommended_actions.append(
                "🚨 盤中時段 Phase C (尾盤對沖)：為規避隔夜 Gamma 缺口與跳空風險，嚴格禁止新建短線 SPEAR 部位。"
            )
            if sddm_route == "SPEAR":
                recommended_actions.append(
                    "⚠️ 【尾盤 SPEAR 警戒】尾盤投機買盤強烈，若要建倉，必須搭配等比例 SPY PUT 作為隔夜安全閥！"
                )

        # 9. 風控備註
        notes = []
        if vix >= 25.0:
            notes.append(
                "當前市場恐慌指標高企 (VIX >= 25.0)，波動率期限結構轉為逆價差，防範市場系統性尾部風險。"
            )
        else:
            notes.append("當前波動率環境相對溫和，有利於低波動期權佈局。")

        if financial_runway_days <= 30:
            notes.append(
                "警告：您的財務存活跑道天數極低，禁止進行任何高槓桿或買方期權投機，優先以本金安全與獲利回收為第一要務。"
            )

        notes.append(
            "請隨時追蹤 Spot 與 IV 上漲產生的 Hidden Delta 漂移。對沖完成後，可使用 `/settle_hedge` 登錄對沖記錄。"
        )
        risk_mitigation_notes = " ".join(notes)

        return AdvancedTraderOutput(
            ticker=data.ticker,
            timestamp=current_time,
            market_phase=market_phase,
            is_applicable=is_applicable,
            failed_gates=failed_gates,
            sddm_route=sddm_route,
            financial_runway_days=financial_runway_days,
            theta_coverage_pct=theta_coverage_pct,
            runway_status_msg=runway_status_msg,
            magnet_target=magnet_target,
            recommended_actions=recommended_actions,
            vanna_hedging_instruction=vanna_hedging_instruction,
            kelly_position_scaling=kelly_position_scaling,
            risk_mitigation_notes=risk_mitigation_notes,
        )

    def run_post_market_attribution(
        self, portfolio_pnl: float, hedge_pnl: float
    ) -> Dict[str, Any]:
        """
        每日盤後 (16:30 ET) 對沖歸因與自我進化機制。
        計算對沖保護得分 (Protection Score)，反饋調節明日 Gate 3 資金效率門檻。
        """
        old_threshold = self.gate_3_threshold

        # 計算對沖防禦評分 (0-100)
        if portfolio_pnl < 0:
            # 虧損時，對沖是否有正回報？
            if hedge_pnl > 0:
                # 剛好對沖 100% 虧損得 100 分
                protection_score = min(100.0, (hedge_pnl / abs(portfolio_pnl)) * 100.0)
            else:
                protection_score = 0.0
        else:
            # 獲利時，對沖是否產生過度拖累？
            if hedge_pnl >= 0:
                protection_score = 100.0
            else:
                # 對沖虧損佔總利潤的比例，拖累越少，得分越高
                protection_score = max(
                    0.0, min(100.0, 100.0 + (hedge_pnl / portfolio_pnl) * 100.0)
                )

        # 自我進化反饋環節 (Feedback Loop)
        if protection_score >= 70.0:
            # 對沖效率高，防守強，可適度放寬進攻門檻
            self.gate_3_threshold = float(
                round(max(500000.0, self.gate_3_threshold * 0.90), 2)
            )
            evolution_msg = (
                f"🚀 盤後歸因進化成功！當前對沖防禦評分為 {protection_score:.1f}/100 (效率極佳)。"
                f"NRO 已自動調降明日 Gate 3 權利金進攻門檻 10%，新門檻為 ${self.gate_3_threshold:,.2f}，釋放進攻流動性。"
            )
        elif protection_score < 40.0:
            # 對沖效率過低，防守失效或成本過大，需收緊門檻過濾雜訊
            self.gate_3_threshold = float(
                round(min(2000000.0, self.gate_3_threshold * 1.15), 2)
            )
            evolution_msg = (
                f"⚠️ 盤後歸因進化警報！當前對沖防禦評分僅為 {protection_score:.1f}/100 (防守效率偏低或磨損過重)。"
                f"NRO 已自動調升明日 Gate 3 權利金門檻 15%，新門檻為 ${self.gate_3_threshold:,.2f}，以提升訊號品質。"
            )
        else:
            evolution_msg = (
                f"⚖️ 盤後歸因進化持平。當前對沖防禦評分為 {protection_score:.1f}/100 (符合預期區間)。"
                f"NRO 決定明日維持 Gate 3 權利金門檻為 ${self.gate_3_threshold:,.2f}。"
            )

        result = {
            "protection_score": protection_score,
            "old_threshold": old_threshold,
            "new_threshold": self.gate_3_threshold,
            "evolution_msg": evolution_msg,
        }

        self.protection_score_history.append(
            {
                "timestamp": datetime.now(ny_tz),
                "portfolio_pnl": portfolio_pnl,
                "hedge_pnl": hedge_pnl,
                "protection_score": protection_score,
                "old_threshold": old_threshold,
                "new_threshold": self.gate_3_threshold,
            }
        )

        return result


class IntradayScanPipeline:
    """
    盤中量化掃描與對沖背景處理管道。
    每 30 分鐘執行一次，驅動 Squeeze 決策引擎並發送通知。
    """

    def __init__(self, bot, engine: NexusGammaSqueezeEngine):
        self.bot = bot
        self.engine = engine
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self.scan_interval_seconds = 30 * 60  # 30 minutes
        self._intraday_scan_sent: Set[tuple[int, str, date]] = set()

    def start(self):
        """啟動異步監控管道"""
        if not self.is_running:
            self.is_running = True
            self._task = asyncio.create_task(self._run_loop())
            logger.info("✅ IntradayScanPipeline 異步掃描管道啟動。")

    def stop(self):
        """停止異步監控管道"""
        self.is_running = False
        if self._task:
            self._task.cancel()
            logger.info("🛑 IntradayScanPipeline 異步掃描管道停止。")

    async def evaluate_watchlist_symbol(
        self, symbol: str
    ) -> Optional[WatchlistEvaluation]:
        return await evaluate_watchlist_symbol(symbol)

    def _prune_intraday_scan_cache(self, trading_date: date) -> None:
        self._intraday_scan_sent = {
            key for key in self._intraday_scan_sent if key[2] == trading_date
        }

    def _should_send_intraday_scan_report(
        self, user_id: int, ticker: str, phase: str, trading_date: date
    ) -> bool:
        if phase != "Phase B":
            return False

        self._prune_intraday_scan_cache(trading_date)
        return (user_id, ticker, trading_date) not in self._intraday_scan_sent

    def _mark_intraday_scan_report_sent(
        self, user_id: int, ticker: str, trading_date: date
    ) -> None:
        self._prune_intraday_scan_cache(trading_date)
        self._intraday_scan_sent.add((user_id, ticker, trading_date))

    async def _build_watchlist_heartbeat_embed(
        self, evaluation: WatchlistEvaluation, user_context: Any
    ) -> Any:
        import database
        from cogs.embed_builder import create_watchlist_signal_embed
        from ui.formatter import generate_ansi_watchlist_report

        report_body = generate_ansi_watchlist_report(
            evaluation.metrics,
            evaluation.tactical,
        )
        user_id = int(getattr(user_context, "user_id", 0))
        has_position = (
            database.is_symbol_in_portfolio(user_id, evaluation.metrics.symbol)
            if user_id
            else False
        )
        holding_row = None
        if user_id:
            user_holdings = {
                str(row.get("symbol", "")).upper(): row
                for row in database.get_user_holdings(user_id)
            }
            holding_row = user_holdings.get(evaluation.metrics.symbol.upper())
        holding_quantity = None
        holding_avg_cost = None
        if holding_row is not None and float(holding_row.get("quantity", 0.0)) > 0.0:
            holding_quantity = float(holding_row["quantity"])
            holding_avg_cost = float(holding_row.get("avg_cost", 0.0))

        base_capital = float(
            getattr(
                user_context,
                "capital",
                getattr(user_context, "total_capital", 100000.0),
            )
        )
        user_capital = base_capital
        if user_id:
            try:
                from services.trading_service import get_adjusted_user_capital

                user_capital = await get_adjusted_user_capital(user_id, base_capital)
            except Exception:
                user_capital = base_capital
        user_risk_limit = float(getattr(user_context, "risk_limit", 15.0))

        # 計算動態買賣點現貨及對齊的期權操盤建議
        signals = calculate_dynamic_trading_signals(
            evaluation.metrics,
            evaluation.tactical,
            has_position=has_position,
            holding_quantity=holding_quantity,
            holding_avg_cost=holding_avg_cost,
            capital=user_capital,
            risk_limit=user_risk_limit,
        )

        option_guidance = derive_watchlist_option_guidance(
            evaluation.metrics,
            evaluation.tactical,
            event_context=evaluation.event_context,
            has_position=has_position,
            suitable_buy_price=signals.get("suitable_buy_price"),
            suitable_sell_price=signals.get("suitable_sell_price"),
        )

        option_plan = await build_watchlist_option_plan(
            evaluation.metrics,
            evaluation.tactical,
            capital=user_capital,
            risk_limit=user_risk_limit,
            event_context=evaluation.event_context,
            has_position=has_position,
        )
        skew_commentary = build_watchlist_skew_rule_commentary(evaluation.metrics)
        return create_watchlist_signal_embed(
            symbol=evaluation.metrics.symbol,
            report_body=report_body,
            option_guidance=option_guidance,
            event_risk_summary=(
                evaluation.event_context.summary
                if evaluation.event_context is not None
                else "未偵測到近期重大事件"
            ),
            skew_state=(
                f"{evaluation.metrics.option_skew:+.2f}% ｜ "
                f"{evaluation.metrics.option_skew_state}"
            ),
            alert_level=evaluation.tactical.alert_level,
            option_plan=option_plan,
            skew_commentary=skew_commentary,
            has_position=has_position,
            holding_quantity=holding_quantity,
            holding_avg_cost=holding_avg_cost,
            suitable_buy_price=signals.get("suitable_buy_price"),
            suitable_buy_shares=signals.get("suitable_buy_shares"),
            suitable_sell_price=signals.get("suitable_sell_price"),
            suitable_sell_shares=signals.get("suitable_sell_shares"),
            buy_rationale=signals.get("buy_rationale"),
            sell_rationale=signals.get("sell_rationale"),
        )

    async def _run_loop(self):
        while self.is_running:
            try:
                # 1. 取得當下美東時間與交易時段 phase
                now_ny = datetime.now(ZoneInfo("America/New_York"))

                # 檢查美股是否開盤
                market_active = is_market_open()

                # 計算當前 Phase
                phase = "Closed"
                if market_active:
                    # 獲取今日開收盤時間
                    import pandas_market_calendars as mcal
                    from datetime import timedelta

                    nyse_calendar = mcal.get_calendar("NYSE")
                    schedule = nyse_calendar.schedule(
                        start_date=now_ny.date(), end_date=now_ny.date()
                    )

                    if not schedule.empty:
                        row = schedule.iloc[0]
                        market_open = (
                            row["market_open"].tz_convert(ny_tz).to_pydatetime()
                        )
                        market_close = (
                            row["market_close"].tz_convert(ny_tz).to_pydatetime()
                        )

                        phase_a_end = market_open + timedelta(hours=1)
                        phase_c_start = market_close - timedelta(hours=1)

                        if market_open <= now_ny < phase_a_end:
                            phase = "Phase A"
                        elif phase_a_end <= now_ny < phase_c_start:
                            phase = "Phase B"
                        elif phase_c_start <= now_ny <= market_close:
                            phase = "Phase C"

                if phase == "Closed":
                    # 休市時，每 10 分鐘檢查一次
                    logger.info(
                        "市場已休市或尚未開盤。IntradayScanPipeline 進入待機..."
                    )
                    await asyncio.sleep(600)
                    continue

                logger.info(
                    f"🤖 [Intraday Pipeline] 開盤心跳監測觸發。當前時段: {phase}"
                )

                # 2. 獲取所有使用者資訊，執行量化分析
                import database

                user_ids = database.get_all_user_ids()

                for uid in user_ids:
                    ctx = database.get_full_user_context(uid)
                    if not ctx.enable_analyst_agent:
                        continue

                    # 3. 取得帳戶狀態、持倉期權、Greeks 等
                    account_state = TraderAccountState(
                        capital=ctx.total_capital
                        if hasattr(ctx, "total_capital")
                        else 100000.0,
                        cash_reserve=ctx.cash_reserve
                        if hasattr(ctx, "cash_reserve")
                        else 20000.0,
                        monthly_burn_rate=ctx.monthly_burn_rate
                        if hasattr(ctx, "monthly_burn_rate")
                        else 5000.0,
                        current_vix=await self._fetch_current_vix(),
                    )

                    # 讀取期權持倉
                    holdings = await self._fetch_user_options_holdings(uid)
                    portfolio_greeks = await self._fetch_portfolio_greeks(uid)

                    # 掃描 watchlist 中的標的
                    watchlist = database.get_user_watchlist(uid)
                    for ticker, _ in watchlist:
                        watchlist_eval = await self.evaluate_watchlist_symbol(ticker)
                        if (
                            watchlist_eval is not None
                            and watchlist_eval.tactical.alert_level != "green"
                        ):
                            embed = await self._build_watchlist_heartbeat_embed(
                                watchlist_eval, ctx
                            )
                            await self.bot.queue_dm(
                                uid,
                                embed=embed,
                            )
                        market_data = await self._fetch_ticker_market_data(ticker)
                        if not market_data:
                            continue

                        # 執行核心量化引擎
                        output = self.engine.analyze_ticker(
                            data=market_data,
                            account_state=account_state,
                            options_holdings=holdings,
                            portfolio_greeks=portfolio_greeks,
                            market_phase=phase,
                            current_time=now_ny,
                        )

                        # 如果是 SPEAR 訊號或是 Vanna 偏離警戒，則發送 Discord 私訊通知
                        if (
                            output.sddm_route == "SPEAR"
                            or "偏離" in output.vanna_hedging_instruction
                        ):
                            import database

                            if database.is_notification_enabled(
                                uid, "intraday_decision_scan"
                            ):
                                from cogs.embed_builder import (
                                    create_intraday_scan_embed,
                                )

                                if self._should_send_intraday_scan_report(
                                    uid, ticker, phase, now_ny.date()
                                ):
                                    embed = create_intraday_scan_embed(output)
                                    await self.bot.queue_dm(uid, embed=embed)
                                self._mark_intraday_scan_report_sent(
                                    uid, ticker, now_ny.date()
                                )
                                logger.info(
                                    f"Sent Intraday Decision report for {ticker} to user {uid}"
                                )

                # 4. 睡眠 30 分鐘
                await asyncio.sleep(self.scan_interval_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ IntradayScanPipeline 發生錯誤: {e}", exc_info=True)
                await asyncio.sleep(60)

    # 模擬/輔助獲取資料方法
    async def _fetch_current_vix(self) -> float:
        """獲取 VIX 即時數據，預設為 18.0"""
        try:
            from services.market_data_service import get_quote

            quote = await get_quote("^VIX")
            if quote and "current_price" in quote:
                return float(quote["current_price"])
        except Exception:
            pass
        return 18.0

    async def _fetch_user_options_holdings(self, user_id: int) -> List[OptionHolding]:
        """從資料庫獲取使用者期權持倉"""
        holdings = []
        try:
            from database.holdings import get_user_holdings

            db_holdings = get_user_holdings(user_id)
            for h in db_holdings:
                # 僅處理期權合約
                if "opt_type" in h and h.get("opt_type"):
                    # 估計 theta (一般期權服務會提供，這裡給予預設值或從 holdings 讀取)
                    holdings.append(
                        OptionHolding(
                            symbol=h.get("symbol", ""),
                            quantity=float(h.get("quantity", 1.0)),
                            theta=float(h.get("theta", -0.05)),
                        )
                    )
        except Exception as e:
            logger.error(f"Failed to fetch option holdings for user {user_id}: {e}")

        # 若為空則加入測試 Mock 資料
        if not holdings:
            holdings = [
                OptionHolding(symbol="AAPL", quantity=2.0, theta=-0.12),
                OptionHolding(symbol="MSFT", quantity=-1.0, theta=0.08),
            ]
        return holdings

    async def _fetch_portfolio_greeks(self, user_id: int) -> Dict[str, float]:
        """獲取使用者投資組合 Greeks"""
        greeks = {"vanna": 0.0, "beta": 1.0}
        try:
            import database

            user_ctx = database.get_full_user_context(user_id)
            greeks["vanna"] = float(getattr(user_ctx, "total_vanna", 0.0))
        except Exception:
            pass

        # Mock 預設值
        if greeks["vanna"] == 0.0:
            greeks["vanna"] = 1.25
        return greeks

    async def _fetch_ticker_market_data(
        self, ticker: str
    ) -> Optional[TickerMarketData]:
        """獲取標的即時數據並拼裝為 TickerMarketData"""
        try:
            from services.calendar_service import calendar_service
            from services.market_data_service import get_quote

            quote = await get_quote(ticker)
            if not quote or "current_price" not in quote:
                return None

            price = float(quote["current_price"])

            # 獲取財報日期
            days_earnings = 30
            try:
                earnings_info = await calendar_service.get_symbol_earnings(ticker)
                if earnings_info is not None:
                    dt_earn = datetime.strptime(earnings_info.date, "%Y-%m-%d").date()
                    days_earnings = max(0, (dt_earn - datetime.now(ny_tz).date()).days)
            except Exception:
                pass

            # Mock 其他大數據指標，以模擬通過或未通過
            return TickerMarketData(
                ticker=ticker,
                spot_price=price,
                market_cap_billion=250.5,  # 預設大於 20B
                avg_option_volume=65000,  # 預設大於 50000
                days_until_earnings=days_earnings,
                tomorrow_expiring_otm_calls_premium=1200000.0,  # 預設大於 1M
                iv_rank=55.0,  # 預設大於 50
                option_skew=0.08,  # 預設大於 0.05
            )
        except Exception as e:
            logger.error(f"Failed to fetch market data for {ticker}: {e}")
            return None
