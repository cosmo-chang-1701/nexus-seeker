import logging
import asyncio
from datetime import date, datetime
from typing import List, Optional, Dict, Any, Set
from zoneinfo import ZoneInfo

import pandas as pd

from market_time import ny_tz, is_market_open
from models.schemas import (
    EnhancedWatchlistMetrics,
    WatchlistEvaluation,
    WatchlistEventContext,
    WatchlistRiskMode,
    WatchlistTacticalPlan,
)
from risk_engine.nro import WatchlistRiskController
from services.market_data_service import BoundedCache

from market_analysis.models.trader_models import (
    TraderAccountState,
    OptionHolding,
    TickerMarketData,
    AdvancedTraderOutput,
)
from market_analysis.gamma_squeeze_engine import NexusGammaSqueezeEngine
from market_analysis.signal_calculator import (
    _derive_buy_levels,
    _derive_sell_levels,
    _buy_zone_status,
    _sell_zone_status,
    _extract_pe_ratio,
    calculate_dynamic_trading_signals,
)
from market_analysis.option_guidance import (
    derive_watchlist_option_guidance,
    build_watchlist_option_plan,
    _pick_watchlist_cover_leg,
    _estimate_watchlist_contract_count,
    _mid_price_from_row,
    _watchlist_event_risk_multiplier,
)


logger = logging.getLogger(__name__)

_WATCHLIST_METRICS_CACHE = BoundedCache(max_size=128)
_WATCHLIST_METRICS_TTL = 20 * 60


def _quote_price(quote: Dict[str, Any], fallback: float = 0.0) -> float:
    for key in ("c", "current_price", "price"):
        value = quote.get(key)
        if value:
            return float(value)
    return fallback


def get_cached_volume_poc(symbol: str) -> float | None:
    from database.cache import get_kv_cache

    val = get_kv_cache(f"volume_poc_{symbol.upper()}")
    return float(val) if val is not None else None


def save_cached_volume_poc(symbol: str, poc: float) -> None:
    from database.cache import save_kv_cache

    save_kv_cache(f"volume_poc_{symbol.upper()}", poc)


def get_cached_gex_putwall(symbol: str) -> float | None:
    from database.cache import get_kv_cache

    val = get_kv_cache(f"gex_putwall_{symbol.upper()}")
    return float(val) if val is not None else None


def save_cached_gex_putwall(symbol: str, wall: float) -> None:
    from database.cache import save_kv_cache

    save_kv_cache(f"gex_putwall_{symbol.upper()}", wall)


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

    last_close = 0.0
    if quote:
        last_close = float(quote.get("pc", 0.0) or quote.get("c", 0.0) or 0.0)
    if last_close <= 0.0 and not df_stock.empty:
        last_close = float(df_stock["Close"].iloc[-1])
    current_price = _quote_price(quote, fallback=last_close)

    # 1. Vol POC (Volume Point of Control) via SQLite cache fallback
    volume_poc = 0.0
    if not df_stock.empty and len(df_stock) >= 60:
        try:
            volume_poc = max(_estimate_volume_poc(df_stock), 0.01)
            save_cached_volume_poc(symbol, volume_poc)
        except Exception as e:
            logger.warning(f"Error calculating Vol POC for {symbol}: {e}")
    if volume_poc <= 0.0:
        cached_poc = get_cached_volume_poc(symbol)
        volume_poc = cached_poc if cached_poc else current_price

    # 2. GEX PutWall via SQLite cache fallback
    gex_max_put_wall = 0.0
    vanna_sensitivity = 0.0
    try:
        gex_max_put_wall, vanna_sensitivity = await _estimate_options_wall_metrics(
            symbol,
            current_price,
            dividend_yield,
        )
        save_cached_gex_putwall(symbol, gex_max_put_wall)
    except Exception as e:
        logger.warning(f"Error calculating GEX PutWall for {symbol}: {e}")
    if gex_max_put_wall <= 0.0:
        cached_wall = get_cached_gex_putwall(symbol)
        gex_max_put_wall = cached_wall if cached_wall else current_price

    # Legacy retail indicators are completely removed from pipeline
    rsi_14 = 50.0
    atr_14 = 0.01
    ma20 = current_price
    ma50 = current_price
    ma200 = current_price
    beta = (
        0.0
        if symbol.upper() == "BOXX"
        else calculate_beta(df_stock, df_spy)
        if not df_stock.empty and not df_spy.empty
        else 1.0
    )
    relative_strength_spy = (
        _relative_strength_vs_spy(df_stock, df_spy)
        if not df_stock.empty and not df_spy.empty
        else 0.0
    )

    buy_phase1, buy_phase2, buy_phase3 = _derive_buy_levels(
        current_price,
        0.0,
        0.0,
        0.0,
        volume_poc,
        max(gex_max_put_wall, 0.01),
        0.0,
    )
    sell_phase1, sell_phase2, sell_phase3 = _derive_sell_levels(
        current_price,
        0.0,
        0.0,
        0.0,
        0.0,
        volume_poc=volume_poc,
        gex_max_put_wall=max(gex_max_put_wall, 0.01),
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
        iv_rank=iv_metrics.iv_rank if iv_metrics else 50.0,
        iv_percentile=iv_metrics.iv_percentile if iv_metrics else 50.0,
        option_skew=float(skew_metrics.get("skew", 0.0)) if skew_metrics else 0.0,
        skew_percentile=float(skew_metrics.get("skew_percentile", 50.0))
        if skew_metrics
        else 50.0,
        option_skew_state=str(skew_metrics.get("state") or "N/A")
        if skew_metrics
        else "N/A",
        pcr=float(pcr_metrics.get("pcr", 0.0)) if pcr_metrics else 0.0,
        volume_poc=volume_poc,
        gex_max_put_wall=max(gex_max_put_wall, 0.01),
        vanna_sensitivity=vanna_sensitivity,
        relative_strength_spy=relative_strength_spy,
        iv_source=iv_metrics.iv_source if iv_metrics else "UNAVAILABLE",
        is_premarket=iv_metrics.is_premarket if iv_metrics else False,
        volume_pcr=float(pcr_metrics.get("volume_pcr", pcr_metrics.get("pcr", 0.0)))
        if pcr_metrics
        else 0.0,
        oi_pcr=float(pcr_metrics.get("oi_pcr", 0.0)) if pcr_metrics else 0.0,
        has_event_loading_applied=iv_metrics.has_event_loading_applied
        if iv_metrics
        else False,
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

    # 零 Gamma 踩踏 Regime 檢查並自動調整網格間距
    try:
        from market_analysis.index_microstructure import get_market_regime

        regime = await get_market_regime()
        if regime == "SHORT_GAMMA_CRITICAL":
            from database.cache import get_kv_cache

            gex_fb = get_kv_cache("macro_gex_is_fallback")
            is_fb = gex_fb is None or int(gex_fb) == 1
            fb_tag = " [備援估算]" if is_fb else ""

            tactical.dynamic_grid_step = round(tactical.dynamic_grid_step * 1.5, 2)
            tactical.action_guideline += f" (⚠️ 偵測到大盤進入 SHORT_GAMMA_CRITICAL 極端踩踏恐慌軌道{fb_tag}，個股網格單觸發間距已自動放大 1.5 倍以防禦資金被過早抽乾。)"
    except Exception as e:
        logger.warning(f"評估市場 Regime 時發生錯誤: {e}")

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

    # 價格暴跌但波動率低壓背離偵測
    try:
        from services import market_data_service

        quote = await market_data_service.get_quote(symbol)
        dp_raw = quote.get("dp") if quote else None
        dp_val = float(dp_raw) if dp_raw is not None else 0.0
        if dp_val < -3.0 and metrics.iv_rank < 15.0:
            tactical = WatchlistTacticalPlan(
                scenario="wait",
                sddm_route="WAIT (IV 壓抑背離)",
                action_guideline=(
                    "⚠️ WARNING: IV Suppression Divergence｜現價暴跌但波動率低壓，"
                    f"IV Rank 處於極低位階 ({metrics.iv_rank:.1f}%)，與現貨大跌 ({dp_val:+.2f}%) 矛盾。"
                    "可能存在系統快取延遲或異常，建議暫緩單腿長權利金操作，"
                    "僅允許小倉位收租並搭配保護性結構。"
                ),
                dynamic_grid_step=tactical.dynamic_grid_step,
                hidden_delta_risk=0.0,
                hedge_instruction=None,
                hedge_allocation_shares=0,
                alert_level="red",
            )
    except Exception as e:
        logger.warning(
            f"[{symbol}] evaluate_watchlist_symbol 背離比對獲取現價失敗: {e}"
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
            metrics=evaluation.metrics,
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
                        try:
                            watchlist_eval = await self.evaluate_watchlist_symbol(
                                ticker
                            )
                            if (
                                watchlist_eval is not None
                                and watchlist_eval.tactical.alert_level != "green"
                            ):
                                if database.is_notification_enabled(
                                    uid, "watchlist_heartbeat_alignment"
                                ):
                                    embed = await self._build_watchlist_heartbeat_embed(
                                        watchlist_eval, ctx
                                    )
                                    await self.bot.queue_dm(
                                        uid,
                                        embed=embed,
                                    )
                                else:
                                    logger.info(
                                        f"使用者 {uid} 已關閉 watchlist_heartbeat_alignment 訂閱，略過心跳推送。"
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
                        except Exception as ticker_err:
                            logger.error(
                                f"❌ IntradayScanPipeline 處理標的 {ticker} 時發生錯誤: {ticker_err}",
                                exc_info=True,
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
